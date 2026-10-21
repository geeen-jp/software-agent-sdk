import asyncio
import json
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from unittest.mock import patch

import pytest

from openhands.sdk.agent.acp_claude_auth import CLAUDE_CREDENTIALS_SECRET_NAME
from openhands.sdk.agent.acp_file_credentials import (
    CODEX_AUTH_SECRET_NAME,
    create_file_credential_lifecycle,
    resolve_codex_auth_credentials,
)
from openhands.sdk.conversation.secret_registry import SecretRegistry
from openhands.sdk.credential import (
    CredentialAuthorizationRejected,
    CredentialConflict,
    CredentialNeedsReauthentication,
    CredentialSyncError,
    ResolvedCredential,
)


def _auth(
    refresh_token: str,
    access_token: str = "access",
    *,
    last_refresh: str | None = None,
) -> str:
    value: dict[str, Any] = {
        "auth_mode": "chatgpt",
        "tokens": {
            "refresh_token": refresh_token,
            "access_token": access_token,
        },
    }
    if last_refresh is not None:
        value["last_refresh"] = last_refresh
    return json.dumps(value)


def _claude_auth(
    refresh_token: str,
    access_token: str = "access",
    expires_at: int = 9_999_999_999_000,
) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "refreshToken": refresh_token,
                "accessToken": access_token,
                "expiresAt": expires_at,
            }
        }
    )


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


class MemoryBinding:
    def __init__(self, value: str) -> None:
        self.value = value
        self.generation = 0
        self.replace_calls = 0
        self._lock = threading.Lock()

    async def load(self) -> ResolvedCredential:
        with self._lock:
            return ResolvedCredential(self.value, str(self.generation))

    async def replace(self, expected_version: str, value: str) -> str:
        with self._lock:
            if expected_version != str(self.generation):
                raise CredentialConflict("conflict")
            self.replace_calls += 1
            self.generation += 1
            self.value = value
            return str(self.generation)


class ConcurrentBinding(MemoryBinding):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.loads = threading.Barrier(2)
        self.load_calls = 0

    async def load(self) -> ResolvedCredential:
        with self._lock:
            self.load_calls += 1
            wait_for_concurrent_loads = self.load_calls <= 2
        if wait_for_concurrent_loads:
            await asyncio.to_thread(self.loads.wait, 2)
        return await super().load()


class ConflictReloadBinding(MemoryBinding):
    def __init__(self, value: str, competing_value: str) -> None:
        super().__init__(value)
        self.competing_value = competing_value
        self.replace_attempts = 0

    async def replace(self, expected_version: str, value: str) -> str:
        self.replace_attempts += 1
        if self.replace_attempts == 1:
            with self._lock:
                self.value = self.competing_value
                self.generation += 1
            raise CredentialConflict("conflict")
        return await super().replace(expected_version, value)


class AmbiguousBinding(MemoryBinding):
    async def replace(self, expected_version: str, value: str) -> str:
        await super().replace(expected_version, value)
        raise CredentialSyncError("lost response")


class BlockingBinding(MemoryBinding):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.replace_started = threading.Event()
        self.replace_allowed = threading.Event()

    async def replace(self, expected_version: str, value: str) -> str:
        self.replace_started.set()
        allowed = await asyncio.to_thread(self.replace_allowed.wait, 2)
        assert allowed
        return await super().replace(expected_version, value)


class MissingBinding(MemoryBinding):
    async def replace(self, expected_version: str, value: str) -> str:
        raise CredentialNeedsReauthentication("missing")


class RevokedBinding(MemoryBinding):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.authorization_revision = 0
        self.rejected = True
        self.rejection_observed = threading.Event()

    async def replace(self, expected_version: str, value: str) -> str:
        if self.rejected:
            self.rejection_observed.set()
            raise CredentialAuthorizationRejected("rejected")
        return await super().replace(expected_version, value)

    def reauthorize(self) -> None:
        self.rejected = False
        self.authorization_revision += 1


class FailingBinding(MemoryBinding):
    async def replace(self, expected_version: str, value: str) -> str:
        self.replace_calls += 1
        raise CredentialSyncError("unavailable")


class FlakyBinding(MemoryBinding):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.first_replace_failed = threading.Event()

    async def replace(self, expected_version: str, value: str) -> str:
        if not self.first_replace_failed.is_set():
            self.first_replace_failed.set()
            raise CredentialSyncError("temporarily unavailable")
        return await super().replace(expected_version, value)


class DisappearingBinding(MemoryBinding):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.failed_replace = False
        self.failed_loads = 0

    async def load(self) -> ResolvedCredential:
        if not self.failed_replace:
            return await super().load()
        self.failed_loads += 1
        if self.failed_loads == 1:
            raise CredentialSyncError("unavailable")
        raise CredentialNeedsReauthentication("missing")

    async def replace(self, expected_version: str, value: str) -> str:
        self.failed_replace = True
        raise CredentialSyncError("unavailable")


def _lifecycle(binding: MemoryBinding, registry: SecretRegistry):
    lifecycle = create_file_credential_lifecycle(
        CODEX_AUTH_SECRET_NAME,
        binding,
        _run,
    )
    assert lifecycle is not None
    env: dict[str, str] = {}
    lifecycle.materialize(registry, env)
    return lifecycle, env


def _claude_lifecycle(binding: MemoryBinding, registry: SecretRegistry):
    lifecycle = create_file_credential_lifecycle(
        CLAUDE_CREDENTIALS_SECRET_NAME,
        binding,
        _run,
    )
    assert lifecycle is not None
    env: dict[str, str] = {}
    lifecycle.materialize(registry, env)
    return lifecycle, env


def _wait_for_value(binding: MemoryBinding, value: str) -> None:
    deadline = time.monotonic() + 2
    while binding.value != value and time.monotonic() < deadline:
        time.sleep(0.02)
    assert binding.value == value


def _stop_monitor(lifecycle: Any) -> None:
    runtime = cast(Any, lifecycle)
    runtime._stop.set()
    assert runtime._monitor is not None
    runtime._monitor.join(timeout=1)


def test_resolve_codex_auth_credentials_reads_runtime_host_source(tmp_path) -> None:
    source_root = tmp_path / "home" / ".codex"
    source_root.mkdir(parents=True)
    credentials = _auth("host-refresh", "host-access")
    auth_path = source_root / "auth.json"
    auth_path.write_text(credentials, encoding="utf-8")

    with patch(
        "openhands.sdk.agent.acp_file_credentials.Path.home",
        return_value=tmp_path / "home",
    ):
        assert resolve_codex_auth_credentials() == credentials
    assert auth_path.read_text(encoding="utf-8") == credentials


def test_resolve_codex_auth_credentials_uses_explicit_source_environment(
    tmp_path, monkeypatch
) -> None:
    source_root = tmp_path / "managed-codex-source"
    source_root.mkdir()
    credentials = _auth("managed-refresh", "managed-access")
    (source_root / "auth.json").write_text(credentials, encoding="utf-8")
    monkeypatch.setenv("OPENHANDS_CODEX_AUTH_SOURCE", str(source_root))

    assert resolve_codex_auth_credentials() == credentials


def test_materialize_adopts_newer_live_auth_with_cas_and_redacted_logging(
    tmp_path, monkeypatch, caplog
) -> None:
    source_root = tmp_path / "live-codex"
    source_root.mkdir()
    live = _auth(
        "refresh-live-secret",
        "access-live-secret",
        last_refresh="2026-05-02T12:00:00Z",
    )
    live_path = source_root / "auth.json"
    live_path.write_text(live, encoding="utf-8")
    monkeypatch.setenv("OPENHANDS_CODEX_AUTH_SOURCE", str(source_root))
    binding = MemoryBinding(
        _auth(
            "refresh-store-secret",
            "access-store-secret",
            last_refresh="2026-05-01T12:00:00Z",
        )
    )

    with caplog.at_level("INFO", logger="openhands.sdk.agent.acp_file_credentials"):
        lifecycle, _ = _lifecycle(binding, SecretRegistry())
    path = lifecycle.path
    assert path is not None
    try:
        assert path.read_text(encoding="utf-8") == live
        assert binding.value == live
        assert binding.replace_calls == 1
        assert live_path.read_text(encoding="utf-8") == live
        materialized = next(
            record
            for record in caplog.records
            if record.msg == "credential_binding_materialized"
        )
        assert materialized.outcome == "adopted_live"
        assert "refresh-live-secret" not in caplog.text
        assert "access-live-secret" not in caplog.text
        assert "refresh-store-secret" not in caplog.text
        assert "access-store-secret" not in caplog.text
    finally:
        lifecycle.close()


@pytest.mark.parametrize(
    ("store_refresh", "live_refresh", "live_valid", "live_exists"),
    [
        ("2026-05-02T00:00:00Z", "2026-05-01T00:00:00Z", True, True),
        ("2026-05-01T00:00:00Z", "2026-05-01T00:00:00Z", True, True),
        (None, "2026-05-02T00:00:00Z", True, True),
        ("2026-05-01T00:00:00Z", "invalid", True, True),
        ("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", False, True),
        ("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", True, False),
    ],
)
def test_materialize_keeps_store_unless_live_is_valid_and_strictly_newer(
    tmp_path,
    monkeypatch,
    store_refresh: str | None,
    live_refresh: str,
    live_valid: bool,
    live_exists: bool,
) -> None:
    source_root = tmp_path / "live-codex"
    source_root.mkdir()
    store = _auth("refresh-store", last_refresh=store_refresh)
    live = (
        _auth("refresh-live", last_refresh=live_refresh)
        if live_valid
        else json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {},
                "last_refresh": live_refresh,
            }
        )
    )
    live_path = source_root / "auth.json"
    if live_exists:
        live_path.write_text(live, encoding="utf-8")
    monkeypatch.setenv("OPENHANDS_CODEX_AUTH_SOURCE", str(source_root))
    binding = MemoryBinding(store)

    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    path = lifecycle.path
    assert path is not None
    try:
        assert path.read_text(encoding="utf-8") == store
        assert binding.value == store
        assert binding.replace_calls == 0
        if live_exists:
            assert live_path.read_text(encoding="utf-8") == live
    finally:
        lifecycle.close()


def test_materialize_conflict_reloads_store_once_and_keeps_newer_value(
    tmp_path, monkeypatch, caplog
) -> None:
    source_root = tmp_path / "live-codex"
    source_root.mkdir()
    live = _auth("refresh-live", last_refresh="2026-05-02T00:00:00Z")
    (source_root / "auth.json").write_text(live, encoding="utf-8")
    monkeypatch.setenv("OPENHANDS_CODEX_AUTH_SOURCE", str(source_root))
    competing = _auth("refresh-competing", last_refresh="2026-05-03T00:00:00Z")
    binding = ConflictReloadBinding(
        _auth("refresh-store", last_refresh="2026-05-01T00:00:00Z"), competing
    )

    with caplog.at_level("INFO", logger="openhands.sdk.agent.acp_file_credentials"):
        lifecycle, _ = _lifecycle(binding, SecretRegistry())
    path = lifecycle.path
    assert path is not None
    try:
        assert path.read_text(encoding="utf-8") == competing
        assert binding.value == competing
        assert binding.replace_attempts == 1
        materialized = next(
            record
            for record in caplog.records
            if record.msg == "credential_binding_materialized"
        )
        assert materialized.outcome == "conflict_reloaded"
    finally:
        lifecycle.close()


def test_concurrent_materialize_cas_converges_on_live_auth(
    tmp_path, monkeypatch
) -> None:
    source_root = tmp_path / "live-codex"
    source_root.mkdir()
    live = _auth("refresh-live", last_refresh="2026-05-02T00:00:00Z")
    live_path = source_root / "auth.json"
    live_path.write_text(live, encoding="utf-8")
    monkeypatch.setenv("OPENHANDS_CODEX_AUTH_SOURCE", str(source_root))
    binding = ConcurrentBinding(
        _auth("refresh-store", last_refresh="2026-05-01T00:00:00Z")
    )

    def materialize():
        lifecycle = create_file_credential_lifecycle(
            CODEX_AUTH_SECRET_NAME, binding, _run
        )
        assert lifecycle is not None
        lifecycle.materialize(SecretRegistry(), {})
        return lifecycle

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(materialize)
        second_future = executor.submit(materialize)
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)
    try:
        assert first.path is not None
        assert second.path is not None
        assert first.path.read_text(encoding="utf-8") == live
        assert second.path.read_text(encoding="utf-8") == live
        assert binding.value == live
        assert binding.replace_calls == 1
        assert live_path.read_text(encoding="utf-8") == live
    finally:
        first.close()
        second.close()


def test_resync_from_live_rewrites_runtime_auth_once(tmp_path, monkeypatch) -> None:
    source_root = tmp_path / "live-codex"
    source_root.mkdir()
    monkeypatch.setenv("OPENHANDS_CODEX_AUTH_SOURCE", str(source_root))
    store = _auth("refresh-store", last_refresh="2026-05-01T00:00:00Z")
    live = _auth("refresh-live", last_refresh="2026-05-02T00:00:00Z")
    binding = MemoryBinding(store)
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    path = lifecycle.path
    assert path is not None
    live_path = source_root / "auth.json"
    live_path.write_text(live, encoding="utf-8")

    try:
        assert lifecycle.resync_from_live()
        assert path.read_text(encoding="utf-8") == live
        assert binding.value == live
        assert not lifecycle.resync_from_live()
        assert binding.replace_calls == 1
    finally:
        lifecycle.close()


@pytest.mark.parametrize(
    "contents",
    [None, "not-json", json.dumps({"auth_mode": "chatgpt", "tokens": {}})],
)
def test_resolve_codex_auth_credentials_fails_closed(tmp_path, contents) -> None:
    source_root = tmp_path / ".codex"
    source_root.mkdir()
    if contents is not None:
        (source_root / "auth.json").write_text(contents, encoding="utf-8")

    with pytest.raises(CredentialNeedsReauthentication, match="Codex"):
        resolve_codex_auth_credentials(source_root=source_root)


def test_background_rotation_writes_through_and_masks() -> None:
    initial = _auth("refresh-r0", "access-r0")
    rotated = _auth("refresh-r1", "access-r1")
    binding = MemoryBinding(initial)
    registry = SecretRegistry()
    lifecycle, env = _lifecycle(binding, registry)
    path = lifecycle.path
    assert path is not None
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    try:
        path.write_text(rotated, encoding="utf-8")
        _wait_for_value(binding, rotated)
        assert registry.mask_secrets_in_output(initial) == "<secret-hidden>"
        assert registry.mask_secrets_in_output(rotated) == "<secret-hidden>"
        assert registry.mask_secrets_in_output("access-r1") == "<secret-hidden>"
        assert env["CODEX_HOME"] == str(path.parent)
    finally:
        lifecycle.close()
    assert not path.parent.exists()


def test_parallel_lifecycles_use_distinct_private_runtime_files() -> None:
    first, second = MemoryBinding(_auth("refresh-a")), MemoryBinding(_auth("refresh-b"))
    first_lifecycle, first_env = _lifecycle(first, SecretRegistry())
    second_lifecycle, second_env = _lifecycle(second, SecretRegistry())
    first_path = first_lifecycle.path
    second_path = second_lifecycle.path
    assert first_path is not None
    assert second_path is not None
    try:
        assert first_path.parent != second_path.parent
        assert first_env["CODEX_HOME"] == str(first_path.parent)
        assert second_env["CODEX_HOME"] == str(second_path.parent)
        assert first_path.read_text(encoding="utf-8") == first.value
        assert second_path.read_text(encoding="utf-8") == second.value
    finally:
        first_lifecycle.close()
        second_lifecycle.close()
    assert not first_path.parent.exists()
    assert not second_path.parent.exists()


def test_claude_oauth_rotation_writes_through_and_masks() -> None:
    initial = _claude_auth("refresh-r0", "access-r0", expires_at=0)
    rotated = _claude_auth("refresh-r1", "access-r1")
    binding = MemoryBinding(initial)
    registry = SecretRegistry()
    lifecycle, env = _claude_lifecycle(binding, registry)
    path = lifecycle.path
    assert path is not None
    try:
        path.write_text(rotated, encoding="utf-8")
        _wait_for_value(binding, rotated)
        assert env["CLAUDE_CONFIG_DIR"] == str(path.parent)
        assert path.name == ".credentials.json"
        assert registry.mask_secrets_in_output("access-r1") == "<secret-hidden>"
        assert registry.mask_secrets_in_output("refresh-r1") == "<secret-hidden>"
    finally:
        lifecycle.close()
    assert not path.parent.exists()


def test_claude_oauth_invalid_canonical_requires_reauthentication() -> None:
    binding = MemoryBinding("malformed")
    lifecycle = create_file_credential_lifecycle(
        CLAUDE_CREDENTIALS_SECRET_NAME,
        binding,
        _run,
    )
    assert lifecycle is not None
    with pytest.raises(CredentialNeedsReauthentication, match="Claude"):
        lifecycle.materialize(SecretRegistry(), {})


def test_claude_oauth_unreadable_runtime_reports_claude() -> None:
    lifecycle, _ = _claude_lifecycle(
        MemoryBinding(_claude_auth("refresh-r0")), SecretRegistry()
    )
    assert lifecycle.path is not None
    runtime = cast(Any, lifecycle)
    try:
        with patch.object(runtime, "_read_stable", return_value=None):
            with pytest.raises(CredentialSyncError, match="Claude OAuth"):
                lifecycle.flush()
    finally:
        lifecycle.discard()


def test_claude_oauth_close_retains_runtime_when_final_flush_fails() -> None:
    lifecycle, _ = _claude_lifecycle(
        MemoryBinding(_claude_auth("refresh-r0")), SecretRegistry()
    )
    assert lifecycle.path is not None
    runtime = cast(Any, lifecycle)
    runtime_dir = lifecycle.path.parent
    try:
        with patch.object(
            runtime,
            "_flush",
            side_effect=CredentialSyncError("final flush failed"),
        ):
            with pytest.raises(CredentialSyncError, match="final flush failed"):
                lifecycle.close()
        assert runtime_dir.exists()
        assert lifecycle.path is not None
        assert lifecycle.path.parent == runtime_dir
        lifecycle.close()
        assert not runtime_dir.exists()
        assert lifecycle.path is None
    finally:
        lifecycle.discard()


def test_mask_tracking_does_not_sleep_or_write() -> None:
    initial = _auth("refresh-r0", "access-r0")
    rotated = _auth("refresh-r1", "access-r1")
    binding = MemoryBinding(initial)
    registry = SecretRegistry()
    lifecycle, _ = _lifecycle(binding, registry)
    assert lifecycle.path is not None
    _stop_monitor(lifecycle)
    try:
        lifecycle.path.write_text(rotated, encoding="utf-8")
        with patch(
            "openhands.sdk.agent.acp_file_credentials.time.sleep",
            side_effect=AssertionError("mask tracking must not sleep"),
        ):
            lifecycle.track_current()
        assert binding.value == initial
        assert binding.replace_calls == 0
        assert registry.mask_secrets_in_output("access-r1") == "<secret-hidden>"
    finally:
        lifecycle.close()


def test_mask_tracking_does_not_wait_for_monitor_write() -> None:
    rotated = _auth("refresh-r1", "access-r1")
    binding = BlockingBinding(_auth("refresh-r0", "access-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    try:
        lifecycle.path.write_text(rotated, encoding="utf-8")
        assert binding.replace_started.wait(2)

        tracked = threading.Event()
        errors: list[BaseException] = []

        def track() -> None:
            try:
                lifecycle.track_current()
            except BaseException as exc:
                errors.append(exc)
            finally:
                tracked.set()

        thread = threading.Thread(target=track)
        thread.start()
        completed = tracked.wait(0.5)
        binding.replace_allowed.set()
        thread.join(timeout=2)
        assert completed
        assert not errors
        _wait_for_value(binding, rotated)
    finally:
        binding.replace_allowed.set()
        lifecycle.close()


def test_partial_file_is_not_published() -> None:
    initial = _auth("refresh-r0")
    rotated = _auth("refresh-r1")
    binding = MemoryBinding(initial)
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    try:
        lifecycle.path.write_text('{"tokens":', encoding="utf-8")
        time.sleep(0.25)
        assert binding.value == initial
        assert binding.replace_calls == 0
        lifecycle.path.write_text(rotated, encoding="utf-8")
        _wait_for_value(binding, rotated)
    finally:
        lifecycle.close()


def test_unstable_read_does_not_poison_lifecycle() -> None:
    rotated = _auth("refresh-r1")
    binding = MemoryBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    _stop_monitor(lifecycle)
    runtime = cast(Any, lifecycle)
    lifecycle.path.write_text(rotated, encoding="utf-8")

    with (
        patch.object(runtime, "_read_stable", return_value=None),
        pytest.raises(CredentialSyncError, match="read safely"),
    ):
        lifecycle.flush()

    lifecycle.flush()
    assert binding.value == rotated
    lifecycle.close()


def test_monitor_recovers_after_transient_writeback_failure() -> None:
    rotated = _auth("refresh-r1")
    binding = FlakyBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    runtime = cast(Any, lifecycle)
    try:
        lifecycle.path.write_text(rotated, encoding="utf-8")
        assert binding.first_replace_failed.wait(2)
        assert runtime._monitor.is_alive()
        _wait_for_value(binding, rotated)
        lifecycle.flush()
    finally:
        lifecycle.close()


def test_monitor_logs_persistent_writeback_failure_once() -> None:
    binding = FailingBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    runtime = cast(Any, lifecycle)
    try:
        with patch(
            "openhands.sdk.agent.acp_file_credentials.logger.warning"
        ) as warning:
            lifecycle.path.write_text(_auth("refresh-r1"), encoding="utf-8")
            deadline = time.monotonic() + 2
            while binding.replace_calls < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert binding.replace_calls >= 2
            assert runtime._monitor.is_alive()
            assert warning.call_count == 1
    finally:
        lifecycle.discard()


def test_monitor_stops_when_recovery_probe_requires_reauthentication() -> None:
    binding = DisappearingBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    runtime = cast(Any, lifecycle)
    lifecycle.path.write_text(_auth("refresh-r1"), encoding="utf-8")
    assert runtime._monitor is not None
    runtime._monitor.join(timeout=2)

    assert not runtime._monitor.is_alive()
    with pytest.raises(CredentialNeedsReauthentication, match="missing"):
        lifecycle.flush()
    lifecycle.discard()


def test_unchanged_file_does_not_write() -> None:
    binding = MemoryBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    lifecycle.flush()
    lifecycle.close()
    assert binding.replace_calls == 0


def test_ambiguous_committed_write_converges() -> None:
    rotated = _auth("refresh-r1")
    binding = AmbiguousBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    lifecycle.path.write_text(rotated, encoding="utf-8")
    lifecycle.flush()
    assert binding.value == rotated
    assert binding.replace_calls == 1
    lifecycle.close()


def test_writeback_failure_is_retried_after_successful_load() -> None:
    binding = FailingBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    runtime_dir = lifecycle.path.parent
    _stop_monitor(lifecycle)
    lifecycle.path.write_text(_auth("refresh-r1"), encoding="utf-8")

    with pytest.raises(CredentialSyncError, match="unavailable"):
        lifecycle.flush()
    with pytest.raises(CredentialSyncError, match="unavailable"):
        lifecycle.flush()
    with pytest.raises(CredentialSyncError, match="unavailable"):
        lifecycle.close()

    assert binding.replace_calls == 3
    assert runtime_dir.exists()
    lifecycle.discard()
    assert not runtime_dir.exists()


def test_conflict_is_sticky() -> None:
    binding = MemoryBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    runtime_dir = lifecycle.path.parent
    binding.generation += 1
    lifecycle.path.write_text(_auth("refresh-r1"), encoding="utf-8")

    with pytest.raises(CredentialConflict):
        lifecycle.flush()
    with pytest.raises(CredentialConflict):
        lifecycle.track_current()
    with pytest.raises(CredentialConflict):
        lifecycle.close()

    assert runtime_dir.exists()
    lifecycle.discard()


def test_deleted_credential_error_is_sticky() -> None:
    binding = MissingBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    _stop_monitor(lifecycle)
    lifecycle.path.write_text(_auth("refresh-r1"), encoding="utf-8")

    with pytest.raises(CredentialNeedsReauthentication):
        lifecycle.flush()
    with pytest.raises(CredentialNeedsReauthentication):
        lifecycle.track_current()
    with pytest.raises(CredentialNeedsReauthentication):
        lifecycle.close()

    lifecycle.discard()


def test_reauthorization_clears_authorization_rejection() -> None:
    binding = RevokedBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    _stop_monitor(lifecycle)
    lifecycle.path.write_text(_auth("refresh-r1"), encoding="utf-8")

    with pytest.raises(CredentialAuthorizationRejected):
        lifecycle.flush()
    binding.reauthorize()
    lifecycle.flush()

    assert binding.value == _auth("refresh-r1")
    lifecycle.close()


def test_monitor_recovers_after_reauthorization() -> None:
    rotated = _auth("refresh-r1")
    binding = RevokedBinding(_auth("refresh-r0"))
    lifecycle, _ = _lifecycle(binding, SecretRegistry())
    assert lifecycle.path is not None
    runtime = cast(Any, lifecycle)
    try:
        lifecycle.path.write_text(rotated, encoding="utf-8")
        assert binding.rejection_observed.wait(2)
        assert runtime._monitor.is_alive()
        binding.reauthorize()
        _wait_for_value(binding, rotated)
    finally:
        lifecycle.close()


def test_runtime_state_does_not_serialize_binding_values() -> None:
    secret = _auth("never-serialize")
    binding = MemoryBinding(secret)
    lifecycle, env = _lifecycle(binding, SecretRegistry())
    try:
        assert secret not in json.dumps(env)
        assert secret not in json.dumps(lifecycle.__dict__, default=str)
    finally:
        lifecycle.close()
