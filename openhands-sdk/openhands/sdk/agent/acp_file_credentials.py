from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from openhands.sdk.conversation.secret_registry import SecretRegistry
from openhands.sdk.credential import (
    CredentialAuthorizationRejected,
    CredentialBindingError,
    CredentialConflict,
    CredentialInvalidResponse,
    CredentialNeedsReauthentication,
    CredentialSyncError,
    ResolvedCredential,
    VersionedCredentialBinding,
)
from openhands.sdk.logger import get_logger
from openhands.sdk.utils.files import atomic_write_text


logger = get_logger(__name__)

CODEX_AUTH_SECRET_NAME = "CODEX_AUTH_JSON"
CODEX_AUTH_SOURCE_ROOT_ENV = "OPENHANDS_CODEX_AUTH_SOURCE"

_CHATGPT_AUTH_PATH = Path(".codex") / "auth.json"
_MONITOR_INTERVAL_SECONDS = 0.1
_MONITOR_MAX_RETRY_INTERVAL_SECONDS = 5.0
_MONITOR_JOIN_TIMEOUT_SECONDS = 2.0
_STABLE_READ_DELAY_SECONDS = 0.01
_SYNC_RETRY_DELAYS: tuple[float, ...] = (0.1, 0.5)

ACPFileCredentialNeedsReauthError = CredentialNeedsReauthentication
ACPFileCredentialSyncError = CredentialSyncError

AsyncRunner = Callable[[Coroutine[Any, Any, Any]], Any]


class _TransientCredentialReadError(CredentialSyncError):
    pass


class ACPFileCredentialLifecycle(Protocol):
    secret_name: str
    path: Path | None

    def materialize(self, registry: SecretRegistry, env: dict[str, str]) -> None: ...

    def resync_from_live(self) -> bool: ...

    def diagnostics(self) -> str: ...

    def track_current(self) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def discard(self) -> None: ...


def codex_auth_file(env: Mapping[str, str]) -> Path:
    codex_home = env.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "auth.json"
    return Path.home() / _CHATGPT_AUTH_PATH


def codex_auth_source_root(
    *,
    source_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the host Codex config directory at credential lookup time.

    ``CODEX_HOME`` belongs to the child ACP process and may already point to an
    isolated conversation directory.  The runtime bootstrap therefore uses a
    separate, explicit source-root variable for the host ChatGPT credential.
    """
    if source_root is not None:
        return source_root
    env = os.environ if environ is None else environ
    configured = env.get(CODEX_AUTH_SOURCE_ROOT_ENV)
    if configured:
        return Path(configured)
    return Path.home() / _CHATGPT_AUTH_PATH.parent


def codex_auth_file_is_chatgpt(env: Mapping[str, str]) -> bool:
    path = codex_auth_file(env)
    if not path.is_file():
        return False
    try:
        return is_valid_codex_auth(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return False


def write_secret_file(path: Path, value: str) -> None:
    atomic_write_text(path, value)


def is_valid_codex_auth(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("auth_mode") not in (None, "chatgpt"):
        return False
    tokens = payload.get("tokens")
    return (
        isinstance(tokens, dict)
        and isinstance(tokens.get("refresh_token"), str)
        and bool(tokens["refresh_token"])
    )


def _last_refresh(value: str) -> datetime | None:
    try:
        raw = json.loads(value).get("last_refresh")
        if not isinstance(raw, str):
            return None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _digest_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _read_live_codex_auth() -> str | None:
    try:
        value = (codex_auth_source_root() / "auth.json").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return value if is_valid_codex_auth(value) else None


def _is_newer_live_auth(store_value: str, live_value: str | None) -> bool:
    if live_value is None or store_value == live_value:
        return False
    store_refresh = _last_refresh(store_value)
    live_refresh = _last_refresh(live_value)
    return (
        store_refresh is not None
        and live_refresh is not None
        and live_refresh > store_refresh
    )


def resolve_codex_auth_credentials(
    *,
    source_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Read valid host ChatGPT credentials without modifying the source file."""
    path = (
        codex_auth_source_root(source_root=source_root, environ=environ) / "auth.json"
    )
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        value = None
    if isinstance(value, str) and is_valid_codex_auth(value):
        return value
    raise CredentialNeedsReauthentication(
        "Codex ChatGPT subscription credentials are missing or invalid. "
        "Sign in with Codex before starting an isolated session."
    )


class _CodexAuthLifecycle:
    secret_name = CODEX_AUTH_SECRET_NAME
    credential_label = "Codex"

    def __init__(
        self,
        binding: VersionedCredentialBinding,
        run_async: AsyncRunner,
    ) -> None:
        self.binding = binding
        self.run_async = run_async
        self.path: Path | None = None
        self._runtime_dir: Path | None = None
        self._registry: SecretRegistry | None = None
        self._expected_version: str | None = None
        self._local_digest: str | None = None
        self._error: CredentialBindingError | None = None
        self._binding_authorization_revision = self._authorization_revision()
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._tracked_digests: set[str] = set()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._closed = False

    def materialize(self, registry: SecretRegistry, env: dict[str, str]) -> None:
        resolved = self._load()
        live_value = _read_live_codex_auth()
        resolved, outcome = self._reconcile_with_live(resolved, live_value)
        if not is_valid_codex_auth(resolved.value):
            raise CredentialNeedsReauthentication(
                "ChatGPT authentication is invalid. Please sign in again."
            )
        runtime_dir = Path(tempfile.mkdtemp(prefix="openhands-codex-"))
        runtime_dir.chmod(0o700)
        path = runtime_dir / "auth.json"
        try:
            write_secret_file(path, resolved.value)
        except BaseException:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            raise
        self.path = path
        self._runtime_dir = runtime_dir
        self._registry = registry
        self._expected_version = resolved.version
        self._local_digest = self._digest(resolved.value)
        self._track(resolved.value)
        env["CODEX_HOME"] = str(runtime_dir)
        monitor = threading.Thread(
            target=self._monitor_loop,
            name="codex-credential-monitor",
            daemon=True,
        )
        try:
            monitor.start()
        except BaseException:
            env.pop("CODEX_HOME", None)
            self._cleanup_runtime()
            raise
        self._monitor = monitor
        logger.info(
            "credential_binding_materialized",
            extra=self._diagnostic_extra(resolved.value, live_value, outcome),
        )

    def resync_from_live(self) -> bool:
        """CAS-sync a newer host auth.json and refresh the isolated runtime file."""
        with self._sync_lock:
            resolved = self._load()
            live_value = _read_live_codex_auth()
            resolved, outcome = self._reconcile_with_live(resolved, live_value)
            if not is_valid_codex_auth(resolved.value):
                raise CredentialNeedsReauthentication(
                    "ChatGPT authentication is invalid. Please sign in again."
                )
            digest = self._digest(resolved.value)
            with self._lock:
                path = self.path
                changed = digest != self._local_digest
            if path is None:
                raise CredentialSyncError("Codex runtime credentials are unavailable.")
            if changed:
                self._track(resolved.value)
                write_secret_file(path, resolved.value)
            with self._lock:
                self._expected_version = resolved.version
                self._local_digest = digest
            logger.info(
                "credential_binding_resync",
                extra=self._diagnostic_extra(resolved.value, live_value, outcome),
            )
            return changed

    def diagnostics(self) -> str:
        """Return token-free store/live refresh timestamps and digest prefixes."""
        try:
            store_value = self._load().value
        except Exception:
            store_value = None
        live_value = _read_live_codex_auth()
        return self._diagnostic_summary(store_value, live_value)

    def _reconcile_with_live(
        self,
        resolved: ResolvedCredential,
        live_value: str | None,
    ) -> tuple[ResolvedCredential, str]:
        if not _is_newer_live_auth(resolved.value, live_value):
            return resolved, "kept_store"
        assert live_value is not None
        result, outcome, conflicted = self._replace_with_live(resolved, live_value)
        if conflicted:
            if _is_newer_live_auth(result.value, live_value):
                result, _retry_outcome, _second_conflict = self._replace_with_live(
                    result, live_value
                )
            outcome = "conflict_reloaded"
        logger.info(
            "credential_binding_replace",
            extra=self._diagnostic_extra(result.value, live_value, outcome),
        )
        return result, outcome

    def _replace_with_live(
        self,
        resolved: ResolvedCredential,
        live_value: str,
    ) -> tuple[ResolvedCredential, str, bool]:
        try:
            version = self.run_async(self.binding.replace(resolved.version, live_value))
        except CredentialConflict:
            return self._load_after_conflict(), "conflict_reloaded", True
        except CredentialAuthorizationRejected:
            return resolved, "kept_store", False
        except CredentialSyncError:
            latest = self._load_after_ambiguous_write()
            adopted = latest is not None and latest.value == live_value
            result = latest if latest is not None else resolved
            outcome = "adopted_live" if adopted else "kept_store"
            return result, outcome, False
        if not isinstance(version, str) or not version:
            raise CredentialSyncError("Credential source returned an invalid version.")
        return ResolvedCredential(live_value, version), "adopted_live", False

    def _diagnostic_extra(
        self,
        store_value: str | None,
        live_value: str | None,
        outcome: str,
    ) -> dict[str, Any]:
        return {
            "credential": self.secret_name,
            "outcome": outcome,
            "store_digest": _digest_prefix(store_value),
            "store_last_refresh": self._refresh_text(store_value),
            "live_digest": _digest_prefix(live_value),
            "live_last_refresh": self._refresh_text(live_value),
        }

    @staticmethod
    def _refresh_text(value: str | None) -> str | None:
        parsed = _last_refresh(value) if value is not None else None
        return parsed.isoformat() if parsed is not None else None

    def _diagnostic_summary(
        self,
        store_value: str | None,
        live_value: str | None,
    ) -> str:
        store_refresh = self._refresh_text(store_value) or "unavailable"
        live_refresh = self._refresh_text(live_value) or "unavailable"
        store_digest = _digest_prefix(store_value) or "unavailable"
        live_digest = _digest_prefix(live_value) or "unavailable"
        return (
            f"store(last_refresh={store_refresh}, digest={store_digest}); "
            f"live(last_refresh={live_refresh}, digest={live_digest})"
        )

    def track_current(self) -> None:
        try:
            self._raise_sticky_error()
            value = self._read_current()
            if value is None:
                raise _TransientCredentialReadError(
                    f"{self.credential_label} credentials could not be read safely."
                )
            self._track_if_changed(value)
            self._raise_sticky_error()
        except _TransientCredentialReadError:
            raise
        except (CredentialNeedsReauthentication, CredentialSyncError) as exc:
            self._set_error(exc)
            raise

    def flush(self) -> None:
        self._flush()

    def _flush(self) -> None:
        try:
            with self._sync_lock:
                self._raise_sticky_error()
                value = self._read_stable(attempts=3)
                if value is None:
                    raise _TransientCredentialReadError(
                        f"{self.credential_label} credentials could not be read safely."
                    )
                self._sync_value(value)
                self._raise_sticky_error()
        except _TransientCredentialReadError:
            raise
        except (CredentialNeedsReauthentication, CredentialSyncError) as exc:
            self._set_error(exc)
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        self._stop.set()
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=_MONITOR_JOIN_TIMEOUT_SECONDS)
        try:
            self._flush()
            logger.info(
                "credential_binding_final_flush",
                extra={"credential": self.secret_name, "outcome": "success"},
            )
        except BaseException:
            logger.warning(
                "credential_binding_final_flush",
                extra={"credential": self.secret_name, "outcome": "failure"},
            )
            raise
        self._cleanup_runtime()

    def discard(self) -> None:
        self._stop.set()
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=_MONITOR_JOIN_TIMEOUT_SECONDS)
        self._cleanup_runtime()

    def _cleanup_runtime(self) -> None:
        runtime_dir = self._runtime_dir
        if runtime_dir is not None:
            shutil.rmtree(runtime_dir, ignore_errors=True)
        with self._lock:
            self.path = None
            self._runtime_dir = None
            self._registry = None
            self._monitor = None
            self._closed = True

    def _monitor_loop(self) -> None:
        failure_logged = False
        retry_interval = _MONITOR_INTERVAL_SECONDS
        while not self._stop.wait(retry_interval):
            try:
                with self._sync_lock:
                    self._raise_sticky_error()
                    value = self._read_stable(attempts=1)
                    if value is not None:
                        self._sync_value(value)
                        failure_logged = False
                    retry_interval = _MONITOR_INTERVAL_SECONDS
            except (
                CredentialNeedsReauthentication,
                CredentialConflict,
                CredentialInvalidResponse,
            ) as exc:
                self._set_error(exc)
                return
            except CredentialSyncError as exc:
                self._set_error(exc)
                if not failure_logged:
                    logger.warning("credential_binding_monitor_failed", exc_info=exc)
                    failure_logged = True
                retry_interval = min(
                    retry_interval * 2,
                    _MONITOR_MAX_RETRY_INTERVAL_SECONDS,
                )
            except Exception as exc:
                self._set_error(
                    CredentialSyncError("Codex credential monitoring failed.")
                )
                if not failure_logged:
                    logger.warning("credential_binding_monitor_failed", exc_info=exc)
                    failure_logged = True
                retry_interval = min(
                    retry_interval * 2,
                    _MONITOR_MAX_RETRY_INTERVAL_SECONDS,
                )

    def _read_current(self) -> str | None:
        with self._lock:
            path = self.path
        if path is None:
            return None
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        return value if is_valid_codex_auth(value) else None

    def _read_stable(self, *, attempts: int) -> str | None:
        with self._lock:
            path = self.path
        if path is None:
            return None
        for attempt in range(attempts):
            try:
                first = path.read_bytes()
                time.sleep(_STABLE_READ_DELAY_SECONDS)
                second = path.read_bytes()
                if first == second:
                    value = second.decode("utf-8")
                    if is_valid_codex_auth(value):
                        return value
            except (OSError, UnicodeError):
                pass
            if attempt + 1 < attempts:
                delay_index = min(attempt, len(_SYNC_RETRY_DELAYS) - 1)
                time.sleep(_SYNC_RETRY_DELAYS[delay_index])
        return None

    def _sync_value(self, value: str) -> None:
        digest = self._digest(value)
        with self._lock:
            if digest == self._local_digest:
                return
        self._track(value)
        logger.info(
            "credential_binding_rotation_detected",
            extra={"credential": self.secret_name},
        )
        with self._lock:
            expected_version = self._expected_version
        if expected_version is None:
            raise CredentialSyncError("Credential binding was not initialized.")
        try:
            successor = self.run_async(self.binding.replace(expected_version, value))
        except CredentialConflict:
            resolved = self._load_after_conflict()
            if resolved.value == value:
                with self._lock:
                    self._expected_version = resolved.version
                    self._local_digest = digest
                logger.info(
                    "credential_binding_replace",
                    extra={"credential": self.secret_name, "outcome": "converged"},
                )
                return
            logger.warning(
                "credential_binding_replace",
                extra={"credential": self.secret_name, "outcome": "conflict"},
            )
            raise
        except CredentialAuthorizationRejected:
            logger.warning(
                "credential_binding_replace",
                extra={"credential": self.secret_name, "outcome": "rejected"},
            )
            raise
        except CredentialSyncError:
            resolved = self._load_after_ambiguous_write()
            if resolved is not None and resolved.value == value:
                with self._lock:
                    self._expected_version = resolved.version
                    self._local_digest = digest
                logger.info(
                    "credential_binding_replace",
                    extra={"credential": self.secret_name, "outcome": "converged"},
                )
                return
            raise
        if not isinstance(successor, str) or not successor:
            raise CredentialSyncError("Credential source returned an invalid version.")
        with self._lock:
            self._expected_version = successor
            self._local_digest = digest
        logger.info(
            "credential_binding_replace",
            extra={"credential": self.secret_name, "outcome": "success"},
        )

    def _load(self) -> ResolvedCredential:
        resolved = self.run_async(self.binding.load())
        if not isinstance(resolved, ResolvedCredential):
            raise CredentialSyncError("Credential source returned an invalid response.")
        return resolved

    def _load_after_ambiguous_write(self) -> ResolvedCredential | None:
        try:
            return self._load()
        except CredentialNeedsReauthentication as exc:
            raise CredentialConflict(
                "The canonical credential was deleted during synchronization."
            ) from exc
        except CredentialAuthorizationRejected:
            raise
        except CredentialSyncError:
            return None

    def _load_after_conflict(self) -> ResolvedCredential:
        try:
            return self._load()
        except CredentialNeedsReauthentication as exc:
            raise CredentialConflict(
                "The canonical credential was deleted during synchronization."
            ) from exc
        except CredentialAuthorizationRejected:
            raise
        except CredentialSyncError as exc:
            raise CredentialSyncError(
                "Credential conflict could not be resolved."
            ) from exc

    def _track(self, value: str) -> None:
        digest = self._digest(value)
        with self._lock:
            if digest in self._tracked_digests:
                return
            registry = self._registry
        if registry is None:
            return
        mask_name = f"{self.secret_name}.{digest}"
        exported_values = {mask_name: value}
        try:
            tokens = json.loads(value).get("tokens", {})
        except (AttributeError, TypeError, ValueError):
            tokens = None
        if isinstance(tokens, dict):
            for name, token in tokens.items():
                if isinstance(token, str) and token:
                    exported_values[f"{mask_name}.tokens.{name}"] = token
        try:
            registry.track_exported_values(exported_values)
        except Exception as exc:
            raise CredentialSyncError(
                "Rotated credentials could not be registered for masking."
            ) from exc
        with self._lock:
            self._tracked_digests.add(digest)

    def _track_if_changed(self, value: str) -> None:
        digest = self._digest(value)
        with self._lock:
            if digest == self._local_digest:
                return
        self._track(value)

    def _set_error(self, error: CredentialBindingError) -> None:
        with self._lock:
            if self._error is None:
                self._error = error

    def _raise_sticky_error(self) -> None:
        self._refresh_authorization_state()
        with self._lock:
            if self._error is not None:
                raise self._error

    def _refresh_authorization_state(self) -> None:
        revision = self._authorization_revision()
        with self._lock:
            error = self._error
            if (
                revision is not None
                and revision != self._binding_authorization_revision
            ):
                self._binding_authorization_revision = revision
                if isinstance(error, CredentialAuthorizationRejected):
                    self._error = None
                    return
            if error is None or isinstance(
                error,
                (
                    CredentialAuthorizationRejected,
                    CredentialConflict,
                    CredentialInvalidResponse,
                    CredentialNeedsReauthentication,
                ),
            ):
                return
        try:
            self._load()
        except (
            CredentialAuthorizationRejected,
            CredentialConflict,
            CredentialInvalidResponse,
            CredentialNeedsReauthentication,
        ) as exc:
            with self._lock:
                if self._error is error:
                    self._error = exc
            return
        except CredentialBindingError:
            return
        with self._lock:
            if self._error is error:
                self._error = None

    def _authorization_revision(self) -> int | None:
        revision = getattr(self.binding, "authorization_revision", None)
        return revision if isinstance(revision, int) else None

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _ClaudeOAuthLifecycle(_CodexAuthLifecycle):
    """Versioned lifecycle for Claude Code's isolated OAuth file.

    Claude Code refreshes both OAuth tokens in ``.credentials.json`` under its
    ``CLAUDE_CONFIG_DIR``.  The synchronization machinery is the same as the
    Codex file lifecycle, but the file shape and environment variable differ.
    The local imports avoid a cycle because ``acp_claude_auth`` reuses
    :func:`write_secret_file` from this module.
    """

    secret_name = "CLAUDE_CREDENTIALS_JSON"
    credential_label = "Claude OAuth"

    def materialize(self, registry: SecretRegistry, env: dict[str, str]) -> None:
        from openhands.sdk.agent.acp_claude_auth import (
            is_valid_claude_oauth_credentials,
        )

        resolved = self._load()
        if not is_valid_claude_oauth_credentials(resolved.value):
            raise CredentialNeedsReauthentication(
                "Claude subscription/OAuth credentials are invalid. "
                "Sign in with Claude Code again."
            )
        runtime_dir = Path(tempfile.mkdtemp(prefix="openhands-claude-acp-"))
        runtime_dir.chmod(0o700)
        path = runtime_dir / ".credentials.json"
        try:
            write_secret_file(path, resolved.value)
        except BaseException:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            raise
        self.path = path
        self._runtime_dir = runtime_dir
        self._registry = registry
        self._expected_version = resolved.version
        self._local_digest = self._digest(resolved.value)
        self._track(resolved.value)
        env["CLAUDE_CONFIG_DIR"] = str(runtime_dir)
        monitor = threading.Thread(
            target=self._monitor_loop,
            name="claude-oauth-credential-monitor",
            daemon=True,
        )
        try:
            monitor.start()
        except BaseException:
            env.pop("CLAUDE_CONFIG_DIR", None)
            self._cleanup_runtime()
            raise
        self._monitor = monitor
        logger.info(
            "credential_binding_materialized",
            extra={"credential": self.secret_name},
        )

    def resync_from_live(self) -> bool:
        return False

    def diagnostics(self) -> str:
        return ""

    def _read_current(self) -> str | None:
        from openhands.sdk.agent.acp_claude_auth import (
            is_valid_claude_oauth_credentials,
        )

        with self._lock:
            path = self.path
        if path is None:
            return None
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        return value if is_valid_claude_oauth_credentials(value) else None

    def _read_stable(self, *, attempts: int) -> str | None:
        from openhands.sdk.agent.acp_claude_auth import (
            is_valid_claude_oauth_credentials,
        )

        with self._lock:
            path = self.path
        if path is None:
            return None
        for attempt in range(attempts):
            try:
                first = path.read_bytes()
                time.sleep(_STABLE_READ_DELAY_SECONDS)
                second = path.read_bytes()
                if first == second:
                    value = second.decode("utf-8")
                    if is_valid_claude_oauth_credentials(value):
                        return value
            except (OSError, UnicodeError):
                pass
            if attempt + 1 < attempts:
                delay_index = min(attempt, len(_SYNC_RETRY_DELAYS) - 1)
                time.sleep(_SYNC_RETRY_DELAYS[delay_index])
        return None

    def _track(self, value: str) -> None:
        from openhands.sdk.agent.acp_claude_auth import (
            track_claude_oauth_credentials_for_masking,
        )

        digest = self._digest(value)
        with self._lock:
            if digest in self._tracked_digests:
                return
            registry = self._registry
        if registry is None:
            return
        try:
            track_claude_oauth_credentials_for_masking(registry, value)
        except Exception as exc:
            raise CredentialSyncError(
                "Rotated credentials could not be registered for masking."
            ) from exc
        with self._lock:
            self._tracked_digests.add(digest)


def create_file_credential_lifecycle(
    secret_name: str,
    binding: VersionedCredentialBinding | None,
    run_async: AsyncRunner,
) -> ACPFileCredentialLifecycle | None:
    if binding is None:
        return None
    lifecycle = _FILE_CREDENTIAL_LIFECYCLES.get(secret_name)
    return lifecycle(binding, run_async) if lifecycle is not None else None


_FILE_CREDENTIAL_LIFECYCLES = {
    CODEX_AUTH_SECRET_NAME: _CodexAuthLifecycle,
    "CLAUDE_CREDENTIALS_JSON": _ClaudeOAuthLifecycle,
}


def supports_file_credential_binding(secret_name: str) -> bool:
    return secret_name in _FILE_CREDENTIAL_LIFECYCLES
