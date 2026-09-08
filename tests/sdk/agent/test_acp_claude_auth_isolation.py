from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from openhands.sdk.agent.acp_agent import ACPAgent
from openhands.sdk.agent.acp_claude_auth import (
    CLAUDE_CREDENTIALS_FILENAME,
    CLAUDE_CREDENTIALS_SECRET_NAME,
    CLAUDE_OAUTH_TOKEN_ENV,
    claude_oauth_source_root,
    is_valid_claude_oauth_credentials,
    resolve_claude_oauth_credentials,
    seed_claude_oauth_credentials,
    track_claude_oauth_credentials_from_file,
)
from openhands.sdk.agent.acp_file_credentials import write_secret_file
from openhands.sdk.conversation.secret_registry import SecretRegistry
from openhands.sdk.credential import CredentialNeedsReauthentication


def _oauth_payload(refresh: str = "refresh-token", access: str = "access-token") -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": 9_999_999_999_000,
            }
        }
    )


def test_is_valid_claude_oauth_credentials() -> None:
    assert is_valid_claude_oauth_credentials(_oauth_payload()) is True
    assert is_valid_claude_oauth_credentials("{}") is False
    assert is_valid_claude_oauth_credentials('{"claudeAiOauth": {}}') is False


def test_missing_isolated_oauth_fails_before_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "missing-home"),
    )
    registry = SecretRegistry()
    isolated = tmp_path / "claude-code"
    with pytest.raises(CredentialNeedsReauthentication):
        seed_claude_oauth_credentials(isolated, registry)
    assert not (isolated / CLAUDE_CREDENTIALS_FILENAME).exists()


def test_optional_seed_without_oauth_leaves_isolated_dir_for_env_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "missing-home"),
    )
    registry = SecretRegistry()
    isolated = tmp_path / "claude-code"
    assert not seed_claude_oauth_credentials(isolated, registry, required=False).seeded
    assert isolated.is_dir()
    assert not (isolated / CLAUDE_CREDENTIALS_FILENAME).exists()


def test_write_secret_file_is_owner_only_and_leaves_no_temp_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".credentials.json"
    target.write_text("stale", encoding="utf-8")
    write_secret_file(target, _oauth_payload("atomic"))
    assert target.read_text(encoding="utf-8") == _oauth_payload("atomic")
    assert (target.stat().st_mode & 0o777) == 0o600
    leftovers = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(f".{target.name}.") or path.name.startswith(".")
    ]
    assert leftovers == [target]


def test_seed_does_not_mutate_original_auth_state(tmp_path: Path) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    source_file = source_root / CLAUDE_CREDENTIALS_FILENAME
    payload = _oauth_payload("source-refresh")
    source_file.write_text(payload, encoding="utf-8")
    before = source_file.read_bytes()

    registry = SecretRegistry()
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    seed_claude_oauth_credentials(
        isolated,
        registry,
        source_root=source_root,
    )

    assert source_file.read_bytes() == before
    seeded = json.loads((isolated / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert seeded["claudeAiOauth"]["refreshToken"] == "source-refresh"
    assert ((isolated / CLAUDE_CREDENTIALS_FILENAME).stat().st_mode & 0o777) == 0o600


def test_restart_reuses_conversation_scoped_auth_state(tmp_path: Path) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("initial"),
        encoding="utf-8",
    )
    registry = SecretRegistry()
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"

    seed_claude_oauth_credentials(isolated, registry, source_root=source_root)
    first = (isolated / CLAUDE_CREDENTIALS_FILENAME).read_text(encoding="utf-8")

    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("rotated"),
        encoding="utf-8",
    )
    seed_claude_oauth_credentials(isolated, registry, source_root=source_root)

    assert (isolated / CLAUDE_CREDENTIALS_FILENAME).read_text(encoding="utf-8") == first


def test_concurrent_conversations_have_isolated_auth_state(tmp_path: Path) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("shared-source"),
        encoding="utf-8",
    )
    registry = SecretRegistry()
    errors: list[BaseException] = []

    def seed_one(name: str, refresh_suffix: str) -> None:
        try:
            isolated = tmp_path / name / "acp" / "claude-code"
            seed_claude_oauth_credentials(isolated, registry, source_root=source_root)
            target = isolated / CLAUDE_CREDENTIALS_FILENAME
            data = json.loads(target.read_text(encoding="utf-8"))
            data["claudeAiOauth"]["refreshToken"] = f"local-{refresh_suffix}"
            target.write_text(json.dumps(data), encoding="utf-8")
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=seed_one, args=("conversation-a", "a")),
        threading.Thread(target=seed_one, args=("conversation-b", "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    a = json.loads(
        (
            tmp_path
            / "conversation-a"
            / "acp"
            / "claude-code"
            / CLAUDE_CREDENTIALS_FILENAME
        ).read_text()
    )
    b = json.loads(
        (
            tmp_path
            / "conversation-b"
            / "acp"
            / "claude-code"
            / CLAUDE_CREDENTIALS_FILENAME
        ).read_text()
    )
    assert a["claudeAiOauth"]["refreshToken"] == "local-a"
    assert b["claudeAiOauth"]["refreshToken"] == "local-b"


def test_secret_override_is_used_without_touching_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("from-home"),
        encoding="utf-8",
    )
    registry = SecretRegistry()
    registry.update_secrets({"CLAUDE_CREDENTIALS_JSON": _oauth_payload("from-secret")})
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    seed_claude_oauth_credentials(isolated, registry, source_root=source_root)

    seeded = json.loads((isolated / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert seeded["claudeAiOauth"]["refreshToken"] == "from-secret"
    source = json.loads((source_root / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert source["claudeAiOauth"]["refreshToken"] == "from-home"


def test_seeded_file_credentials_are_masked_in_output(tmp_path: Path) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    payload = _oauth_payload("file-refresh-xyz", "file-access-xyz")
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(payload, encoding="utf-8")
    registry = SecretRegistry()
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    seed_claude_oauth_credentials(isolated, registry, source_root=source_root)

    leaked = (
        "subprocess said accessToken=file-access-xyz "
        f"refreshToken=file-refresh-xyz blob={payload}"
    )
    masked = registry.mask_secrets_in_output(leaked)
    assert "file-access-xyz" not in masked
    assert "file-refresh-xyz" not in masked
    assert payload not in masked
    assert (source_root / CLAUDE_CREDENTIALS_FILENAME).read_text(
        encoding="utf-8"
    ) == payload


def test_reuse_existing_credentials_are_masked_in_output(tmp_path: Path) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    first = _oauth_payload("reuse-refresh-xyz", "reuse-access-xyz")
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(first, encoding="utf-8")
    registry = SecretRegistry()
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    seed_claude_oauth_credentials(isolated, registry, source_root=source_root)

    later_registry = SecretRegistry()
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("rotated-refresh-xyz", "rotated-access-xyz"),
        encoding="utf-8",
    )
    seed_claude_oauth_credentials(isolated, later_registry, source_root=source_root)

    leaked = "reuse-access-xyz reuse-refresh-xyz"
    masked = later_registry.mask_secrets_in_output(leaked)
    assert "reuse-access-xyz" not in masked
    assert "reuse-refresh-xyz" not in masked
    assert later_registry.mask_secrets_in_output(first) == "<secret-hidden>"


def test_secret_origin_tokens_are_masked_in_output(tmp_path: Path) -> None:
    payload = _oauth_payload("secret-refresh-xyz", "secret-access-xyz")
    registry = SecretRegistry()
    registry.update_secrets({"CLAUDE_CREDENTIALS_JSON": payload})
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    seed_claude_oauth_credentials(
        isolated, registry, source_root=tmp_path / "unused-home"
    )
    leaked = "secret-access-xyz and secret-refresh-xyz"
    masked = registry.mask_secrets_in_output(leaked)
    assert "secret-access-xyz" not in masked
    assert "secret-refresh-xyz" not in masked


def test_claude_config_dir_is_preferred_over_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_root = tmp_path / "home" / ".claude"
    home_root.mkdir(parents=True)
    (home_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("from-home"),
        encoding="utf-8",
    )
    config_dir = tmp_path / "config-dir"
    config_dir.mkdir()
    (config_dir / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("from-config-dir"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "home"),
    )
    registry = SecretRegistry()
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    seed_claude_oauth_credentials(
        isolated,
        registry,
        environ={"CLAUDE_CONFIG_DIR": str(config_dir)},
    )
    seeded = json.loads((isolated / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert seeded["claudeAiOauth"]["refreshToken"] == "from-config-dir"
    home = json.loads((home_root / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert home["claudeAiOauth"]["refreshToken"] == "from-home"
    config = json.loads((config_dir / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert config["claudeAiOauth"]["refreshToken"] == "from-config-dir"


def test_home_fallback_is_resolved_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    late_home = tmp_path / "late-home"
    late_claude = late_home / ".claude"
    late_claude.mkdir(parents=True)
    (late_claude / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("late-home"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: late_home),
    )
    assert claude_oauth_source_root() == late_claude
    registry = SecretRegistry()
    value = resolve_claude_oauth_credentials(registry)
    assert json.loads(value)["claudeAiOauth"]["refreshToken"] == "late-home"


def test_resolve_claude_oauth_credentials_rejects_invalid_secret(
    tmp_path: Path,
) -> None:
    registry = SecretRegistry()
    registry.update_secrets({"CLAUDE_CREDENTIALS_JSON": "{}"})
    with pytest.raises(CredentialNeedsReauthentication):
        resolve_claude_oauth_credentials(
            registry,
            source_root=tmp_path / "missing-home",
        )


def test_acp_agent_isolate_data_dir_seeds_claude_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("runtime"),
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "home"),
    )

    import uuid

    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.workspace.local import LocalWorkspace

    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
    )
    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(tmp_path / "persist"),
    )
    env: dict[str, str] = {}
    agent._isolate_acp_data_dir(state, env)

    isolated = Path(env["CLAUDE_CONFIG_DIR"])
    persist = tmp_path / "persist"
    assert persist not in isolated.parents
    assert isolated != persist
    assert list(persist.rglob(CLAUDE_CREDENTIALS_FILENAME)) == []
    assert (isolated / CLAUDE_CREDENTIALS_FILENAME).is_file()
    seeded = json.loads((isolated / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert seeded["claudeAiOauth"]["refreshToken"] == "runtime"
    source = json.loads((source_root / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert source["claudeAiOauth"]["refreshToken"] == "runtime"
    leaked = "runtime access-token"
    masked = state.secret_registry.mask_secrets_in_output(leaked)
    assert "runtime" not in masked
    assert "access-token" not in masked
    agent._cleanup_claude_config_runtime(discard=True)
    assert not isolated.exists()
    assert list(persist.rglob(CLAUDE_CREDENTIALS_FILENAME)) == []


def test_writable_isolate_without_oauth_keeps_isolated_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "missing-home"),
    )

    import uuid

    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.workspace.local import LocalWorkspace

    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
    )
    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(tmp_path / "persist"),
    )
    env: dict[str, str] = {}
    assert agent._isolate_acp_data_dir(state, env) is False
    isolated = Path(env["CLAUDE_CONFIG_DIR"])
    assert isolated.is_dir()
    assert not (isolated / CLAUDE_CREDENTIALS_FILENAME).exists()


def test_isolate_acp_data_dir_fails_closed_without_oauth_when_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "missing-home"),
    )

    import uuid

    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.workspace.local import LocalWorkspace

    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
        acp_permission_policy="read_only",
    )
    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(tmp_path / "persist"),
    )
    env: dict[str, str] = {}
    with pytest.raises(CredentialNeedsReauthentication):
        agent._isolate_acp_data_dir(state, env)
    assert "CLAUDE_CONFIG_DIR" not in env


def test_writable_start_without_oauth_uses_isolated_dir_and_env_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.utils.async_executor import AsyncExecutor
    from openhands.sdk.workspace.local import LocalWorkspace

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "missing-home"),
    )
    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
        acp_env={"ANTHROPIC_API_KEY": "sk-writable"},
    )
    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(tmp_path / "persist"),
    )
    captured: dict[str, str] = {}
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()

    async def _fake_create_subprocess_exec(*_args, env=None, **_kwargs):
        captured.update(env or {})
        return mock_process

    async def _fake_filter(_src, _dst):
        return None

    conn = MagicMock()
    init_response = MagicMock()
    init_response.agent_info = MagicMock()
    init_response.agent_info.name = "claude-agent-acp"
    init_response.agent_info.version = "1.0"
    init_response.auth_methods = []
    conn.initialize = AsyncMock(return_value=init_response)
    new_response = MagicMock()
    new_response.session_id = "sess-writable-auth"
    conn.new_session = AsyncMock(return_value=new_response)
    conn.load_session = AsyncMock(return_value=MagicMock())
    conn.set_session_mode = AsyncMock()
    conn.set_session_model = AsyncMock()
    conn.authenticate = AsyncMock()
    conn.close = AsyncMock()

    agent._executor = AsyncExecutor()
    try:
        with (
            patch(
                "openhands.sdk.agent.acp_agent.asyncio.create_subprocess_exec",
                new=_fake_create_subprocess_exec,
            ),
            patch(
                "openhands.sdk.agent.acp_agent.ClientSideConnection",
                return_value=conn,
            ),
            patch(
                "openhands.sdk.agent.acp_agent._filter_jsonrpc_lines",
                new=_fake_filter,
            ),
            patch(
                "openhands.sdk.agent.acp_agent.asyncio.StreamReader",
                return_value=MagicMock(),
            ),
        ):
            agent._start_acp_server(state)
    finally:
        agent._executor.close(timeout=1.0)

    assert captured["ANTHROPIC_API_KEY"] == "sk-writable"
    isolated = Path(captured["CLAUDE_CONFIG_DIR"])
    assert isolated.is_dir()
    assert not (isolated / CLAUDE_CREDENTIALS_FILENAME).exists()


def test_read_only_start_without_oauth_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.workspace.local import LocalWorkspace

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "missing-home"),
    )
    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
        acp_permission_policy="read_only",
    )
    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(tmp_path / "persist"),
    )
    with pytest.raises(CredentialNeedsReauthentication):
        agent._start_acp_server(state)


def test_updated_registry_credentials_replace_isolated_copy(tmp_path: Path) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    source_payload = _oauth_payload("from-home")
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        source_payload, encoding="utf-8"
    )
    registry = SecretRegistry()
    first_payload = _oauth_payload("first-refresh", "first-access")
    registry.update_secrets({CLAUDE_CREDENTIALS_SECRET_NAME: first_payload})
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    first = seed_claude_oauth_credentials(isolated, registry, source_root=source_root)
    updated = _oauth_payload("updated-refresh", "updated-access")
    registry.update_secrets({CLAUDE_CREDENTIALS_SECRET_NAME: updated})
    seed_claude_oauth_credentials(
        isolated,
        registry,
        source_root=source_root,
        last_source_digest=first.source_digest,
    )
    seeded = json.loads((isolated / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert seeded["claudeAiOauth"]["refreshToken"] == "updated-refresh"
    assert (source_root / CLAUDE_CREDENTIALS_FILENAME).read_text(
        encoding="utf-8"
    ) == source_payload


def test_runtime_rotated_tokens_are_masked_without_source_mutation(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    original = _oauth_payload("orig-refresh", "orig-access")
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(original, encoding="utf-8")
    registry = SecretRegistry()
    isolated = tmp_path / "conversation-a" / "acp" / "claude-code"
    seed_claude_oauth_credentials(isolated, registry, source_root=source_root)
    rotated = _oauth_payload("rotated-refresh", "rotated-access")
    (isolated / CLAUDE_CREDENTIALS_FILENAME).write_text(rotated, encoding="utf-8")
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(original, encoding="utf-8")

    track_claude_oauth_credentials_from_file(
        registry, isolated / CLAUDE_CREDENTIALS_FILENAME
    )
    leaked = "orig-access orig-refresh rotated-access rotated-refresh"
    masked = registry.mask_secrets_in_output(leaked)
    assert "orig-access" not in masked
    assert "orig-refresh" not in masked
    assert "rotated-access" not in masked
    assert "rotated-refresh" not in masked
    assert (source_root / CLAUDE_CREDENTIALS_FILENAME).read_text(
        encoding="utf-8"
    ) == original


def test_agent_resume_replaces_isolated_copy_on_updated_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("home"), encoding="utf-8"
    )
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "home"),
    )

    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.workspace.local import LocalWorkspace

    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
    )
    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(tmp_path / "persist"),
    )
    first_payload = _oauth_payload("resume-first", "resume-first-access")
    state.secret_registry.update_secrets(
        {CLAUDE_CREDENTIALS_SECRET_NAME: first_payload}
    )
    env: dict[str, str] = {}
    agent._isolate_acp_data_dir(state, env)
    isolated = Path(env["CLAUDE_CONFIG_DIR"])
    updated = _oauth_payload("resume-updated", "resume-updated-access")
    state.secret_registry.update_secrets({CLAUDE_CREDENTIALS_SECRET_NAME: updated})
    agent.restart_for_updated_credentials({CLAUDE_CREDENTIALS_SECRET_NAME})
    agent._isolate_acp_data_dir(state, env)
    seeded = json.loads((isolated / CLAUDE_CREDENTIALS_FILENAME).read_text())
    assert seeded["claudeAiOauth"]["refreshToken"] == "resume-updated"
    assert (
        json.loads((source_root / CLAUDE_CREDENTIALS_FILENAME).read_text())[
            "claudeAiOauth"
        ]["refreshToken"]
        == "home"
    )
    durable = list((tmp_path / "persist").rglob(CLAUDE_CREDENTIALS_FILENAME))
    assert durable == []
    agent._cleanup_claude_config_runtime(discard=True)
    assert not (isolated / CLAUDE_CREDENTIALS_FILENAME).exists()


def test_durable_leftover_credentials_are_removed_on_isolate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "home" / ".claude"
    source_root.mkdir(parents=True)
    (source_root / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("ok"), encoding="utf-8"
    )
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "home"),
    )

    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.workspace.local import LocalWorkspace

    persist = tmp_path / "persist"
    leftover = persist / "acp" / "claude-code"
    leftover.mkdir(parents=True)
    (leftover / CLAUDE_CREDENTIALS_FILENAME).write_text(
        _oauth_payload("durable-leak"), encoding="utf-8"
    )
    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
    )
    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(persist),
    )
    env: dict[str, str] = {}
    agent._isolate_acp_data_dir(state, env)
    assert not (leftover / CLAUDE_CREDENTIALS_FILENAME).exists()
    isolated = Path(env["CLAUDE_CONFIG_DIR"])
    assert persist not in isolated.parents
    agent._cleanup_claude_config_runtime(discard=True)


def _capture_start_env(agent: ACPAgent, tmp_path: Path) -> dict[str, str]:
    from unittest.mock import AsyncMock, MagicMock, patch

    from openhands.sdk.conversation.state import ConversationState
    from openhands.sdk.secret import SecretSource
    from openhands.sdk.utils.async_executor import AsyncExecutor
    from openhands.sdk.workspace.local import LocalWorkspace

    state = ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
        persistence_dir=str(tmp_path / "persist"),
    )
    if agent.agent_context and agent.agent_context.secrets:
        exported: dict[str, str] = {}
        for name, secret in agent.agent_context.secrets.items():
            value = (
                secret.get_value() if isinstance(secret, SecretSource) else str(secret)
            )
            if value:
                exported[name] = value
        if exported:
            state.secret_registry.update_secrets(exported)
    captured: dict[str, str] = {}
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()

    async def _fake_create_subprocess_exec(*_args, env=None, **_kwargs):
        captured.update(env or {})
        return mock_process

    async def _fake_filter(_src, _dst):
        return None

    conn = MagicMock()
    init_response = MagicMock()
    init_response.agent_info = MagicMock()
    init_response.agent_info.name = "custom-acp"
    init_response.agent_info.version = "1.0"
    init_response.auth_methods = []
    conn.initialize = AsyncMock(return_value=init_response)
    new_response = MagicMock()
    new_response.session_id = "sess-auth"
    conn.new_session = AsyncMock(return_value=new_response)
    conn.load_session = AsyncMock(return_value=MagicMock())
    conn.set_session_mode = AsyncMock()
    conn.set_session_model = AsyncMock()
    conn.authenticate = AsyncMock()
    conn.close = AsyncMock()

    agent._executor = AsyncExecutor()
    try:
        with (
            patch(
                "openhands.sdk.agent.acp_agent.asyncio.create_subprocess_exec",
                new=_fake_create_subprocess_exec,
            ),
            patch(
                "openhands.sdk.agent.acp_agent.ClientSideConnection",
                return_value=conn,
            ),
            patch(
                "openhands.sdk.agent.acp_agent._filter_jsonrpc_lines",
                new=_fake_filter,
            ),
            patch(
                "openhands.sdk.agent.acp_agent.asyncio.StreamReader",
                return_value=MagicMock(),
            ),
        ):
            agent._start_acp_server(state)
    finally:
        agent._executor.close(timeout=1.0)
        agent._cleanup_claude_config_runtime(discard=True)
    suffix = agent._render_suffix(state)
    captured["_suffix"] = suffix or ""
    return captured


@pytest.mark.parametrize(
    "command",
    [
        ["npx", "-y", "@agentclientprotocol/codex-acp"],
        ["npx", "-y", "@google/gemini-cli", "--acp"],
        ["echo"],
    ],
)
def test_claude_oauth_is_not_exported_to_non_claude_providers(
    tmp_path: Path, command: list[str]
) -> None:
    payload = _oauth_payload("cross-provider-refresh", "cross-provider-access")
    from pydantic import SecretStr

    from openhands.sdk.context import AgentContext
    from openhands.sdk.secret import StaticSecret

    agent = ACPAgent(
        acp_command=command,
        agent_context=AgentContext(
            secrets={
                CLAUDE_CREDENTIALS_SECRET_NAME: StaticSecret(value=SecretStr(payload)),
                CLAUDE_OAUTH_TOKEN_ENV: StaticSecret(
                    value=SecretStr("claude-oauth-token")
                ),
                "UNRELATED_TOKEN": StaticSecret(value=SecretStr("keep-me")),
            }
        ),
    )
    captured = _capture_start_env(agent, tmp_path)
    assert CLAUDE_CREDENTIALS_SECRET_NAME not in captured
    assert CLAUDE_OAUTH_TOKEN_ENV not in captured
    assert captured.get("UNRELATED_TOKEN") == "keep-me"
    assert payload not in captured["_suffix"]
    assert CLAUDE_CREDENTIALS_SECRET_NAME not in captured["_suffix"]
    assert CLAUDE_OAUTH_TOKEN_ENV not in captured["_suffix"]
    assert "cross-provider-refresh" not in captured["_suffix"]


def test_oauth_token_without_file_strips_payg_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "openhands.sdk.agent.acp_claude_auth.Path.home",
        classmethod(lambda cls: tmp_path / "missing-home"),
    )
    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=True,
        acp_env={
            CLAUDE_OAUTH_TOKEN_ENV: "claude-sub-token",
            "ANTHROPIC_API_KEY": "sk-payg",
            "ANTHROPIC_BASE_URL": "https://proxy.example.com",
        },
    )
    captured = _capture_start_env(agent, tmp_path)
    assert captured[CLAUDE_OAUTH_TOKEN_ENV] == "claude-sub-token"
    assert "ANTHROPIC_API_KEY" not in captured
    assert "ANTHROPIC_BASE_URL" not in captured
    isolated = Path(captured["CLAUDE_CONFIG_DIR"])
    assert not (isolated / CLAUDE_CREDENTIALS_FILENAME).exists()


def test_oauth_token_without_isolate_still_strips_payg(tmp_path: Path) -> None:
    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        acp_isolate_data_dir=False,
        acp_env={
            CLAUDE_OAUTH_TOKEN_ENV: "claude-sub-token",
            "ANTHROPIC_API_KEY": "sk-payg",
            "ANTHROPIC_BASE_URL": "https://proxy.example.com",
        },
    )
    captured = _capture_start_env(agent, tmp_path)
    assert captured[CLAUDE_OAUTH_TOKEN_ENV] == "claude-sub-token"
    assert "ANTHROPIC_API_KEY" not in captured
    assert "ANTHROPIC_BASE_URL" not in captured
    assert "CLAUDE_CONFIG_DIR" not in captured
