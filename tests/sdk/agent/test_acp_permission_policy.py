from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp.schema import (
    AllowedOutcome,
    CurrentModeUpdate,
    DeniedOutcome,
    NewSessionResponse,
    PermissionOption,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionMode,
    SessionModeState,
    SetSessionConfigOptionResponse,
)

from openhands.sdk.agent.acp_agent import (
    ACPAgent,
    ACPSessionModeError,
    _apply_acp_session_mode,
    _apply_session_config_options,
    _extract_session_modes,
    _OpenHandsACPBridge,
)
from openhands.sdk.agent.acp_permission_policy import (
    assert_config_options_compatible_with_permission_policy,
    is_permission_mode_config_option,
    normalize_acp_permission_policy,
    resolve_permission_response,
    resolve_session_mode_for_policy,
)
from openhands.sdk.conversation.state import ConversationState
from openhands.sdk.utils.async_executor import AsyncExecutor
from openhands.sdk.workspace.local import LocalWorkspace


_CLAUDE_COMMAND = [
    "npx",
    "-y",
    "@agentclientprotocol/claude-agent-acp",
]


def _claude_session_modes(current: str = "default") -> SessionModeState:
    return SessionModeState(
        available_modes=[
            SessionMode(id="default", name="Default"),
            SessionMode(id="bypassPermissions", name="Bypass permissions"),
        ],
        current_mode_id=current,
    )


def test_default_policy_is_writable() -> None:
    assert normalize_acp_permission_policy(None) == "writable"
    assert ACPAgent(acp_command=["echo"]).acp_permission_policy == "writable"


def test_unknown_policy_fails_closed_at_validation() -> None:
    with pytest.raises(ValueError, match="Unsupported acp_permission_policy"):
        normalize_acp_permission_policy("auto_approve")
    with pytest.raises(ValueError, match="Unsupported acp_permission_policy"):
        normalize_acp_permission_policy("plan")
    with pytest.raises(ValueError, match="Unsupported acp_permission_policy"):
        resolve_permission_response("auto_approve", [], {"tool": "write"})


def test_writable_policy_auto_approves_first_option() -> None:
    options: list[object] = [SimpleNamespace(option_id="allow_once")]
    response = resolve_permission_response("writable", options, {"tool": "write"})
    assert isinstance(response.outcome, AllowedOutcome)
    assert response.outcome.option_id == "allow_once"


def test_writable_policy_keeps_legacy_empty_options_behavior() -> None:
    response = resolve_permission_response("writable", [], {"tool": "write"})
    assert isinstance(response.outcome, AllowedOutcome)
    assert response.outcome.option_id == "allow_once"


def test_read_only_selects_reject_always_when_available() -> None:
    options: list[object] = [
        PermissionOption(kind="allow_once", name="Allow", option_id="allow_once"),
        PermissionOption(
            kind="reject_always",
            name="Reject always",
            option_id="reject_always",
        ),
        PermissionOption(kind="reject_once", name="Reject", option_id="reject_once"),
    ]
    response = resolve_permission_response("read_only", options, {"kind": "write"})
    assert isinstance(response.outcome, AllowedOutcome)
    assert response.outcome.option_id == "reject_always"


def test_read_only_selects_reject_once_when_always_is_absent() -> None:
    options: list[object] = [
        PermissionOption(kind="allow_once", name="Allow", option_id="allow_once"),
        PermissionOption(kind="reject_once", name="Reject", option_id="reject_once"),
    ]
    response = resolve_permission_response("read_only", options, {"kind": "write"})
    assert isinstance(response.outcome, AllowedOutcome)
    assert response.outcome.option_id == "reject_once"


@pytest.mark.asyncio
async def test_read_only_falls_back_to_cancelled_without_deny_option() -> None:
    bridge = _OpenHandsACPBridge(permission_policy="read_only")
    response = await bridge.request_permission(
        [SimpleNamespace(option_id="allow_once")],
        "sess-1",
        {"title": "Write", "kind": "write"},
    )
    assert isinstance(response.outcome, DeniedOutcome)
    assert response.outcome.outcome == "cancelled"


@pytest.mark.asyncio
async def test_read_only_policy_denies_unknown_permissions() -> None:
    bridge = _OpenHandsACPBridge(permission_policy="read_only")
    response = await bridge.request_permission([], "sess-1", {"kind": "unknown"})
    assert isinstance(response.outcome, DeniedOutcome)


def test_read_only_session_mode_fails_closed_for_unverified_providers() -> None:
    with pytest.raises(ValueError, match="write paths are unverified"):
        resolve_session_mode_for_policy(
            "read_only",
            provider_key="codex",
            explicit_mode=None,
            default_session_mode="agent-full-access",
        )
    with pytest.raises(ValueError, match="write paths are unverified"):
        resolve_session_mode_for_policy(
            "read_only",
            provider_key=None,
            explicit_mode=None,
            default_session_mode=None,
        )


def test_read_only_refuses_bypass_session_mode() -> None:
    with pytest.raises(ValueError, match="bypasses permission prompts"):
        resolve_session_mode_for_policy(
            "read_only",
            provider_key="claude-code",
            explicit_mode="bypassPermissions",
            default_session_mode="bypassPermissions",
        )


def test_read_only_claude_uses_permission_requesting_mode() -> None:
    assert (
        resolve_session_mode_for_policy(
            "read_only",
            provider_key="claude-code",
            explicit_mode=None,
            default_session_mode="bypassPermissions",
        )
        == "default"
    )


def test_policy_instances_are_isolated_per_bridge() -> None:
    writable_bridge = _OpenHandsACPBridge(permission_policy="writable")
    read_only_bridge = _OpenHandsACPBridge(permission_policy="read_only")
    assert writable_bridge.permission_policy == "writable"
    assert read_only_bridge.permission_policy == "read_only"

    writable_response = resolve_permission_response(
        "writable",
        [SimpleNamespace(option_id="allow_once")],
        {"tool": "write"},
    )
    read_only_response = resolve_permission_response(
        "read_only",
        [SimpleNamespace(option_id="allow_once")],
        {"tool": "write"},
    )

    assert isinstance(writable_response.outcome, AllowedOutcome)
    assert isinstance(read_only_response.outcome, DeniedOutcome)


def _make_state(tmp_path, agent: ACPAgent) -> ConversationState:
    return ConversationState.create(
        id=uuid.uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(tmp_path)),
        persistence_dir=str(tmp_path / "persist"),
    )


def _start_acp_server_with_mocked_transport(
    agent: ACPAgent,
    tmp_path,
    *,
    agent_name: str = "claude-agent-acp",
    capture_env: dict[str, str] | None = None,
) -> MagicMock:
    conn = MagicMock()
    init_response = MagicMock()
    init_response.agent_info = MagicMock()
    init_response.agent_info.name = agent_name
    init_response.agent_info.version = "1.0"
    init_response.auth_methods = []
    conn.initialize = AsyncMock(return_value=init_response)
    new_response = MagicMock()
    new_response.session_id = "sess-1"
    new_response.modes = _claude_session_modes()
    conn.new_session = AsyncMock(return_value=new_response)
    conn.load_session = AsyncMock(return_value=MagicMock())
    conn.set_session_mode = AsyncMock()
    conn.set_session_model = AsyncMock()
    conn.authenticate = AsyncMock()
    conn.close = AsyncMock()

    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()

    async def _fake_create_subprocess_exec(*_args, env=None, **_kwargs):
        if capture_env is not None and env is not None:
            capture_env.update(env)
        return mock_process

    async def _fake_filter(_src, _dst):
        return None

    state = _make_state(tmp_path, agent)
    agent._executor = AsyncExecutor()
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent.asyncio.create_subprocess_exec",
                    new=_fake_create_subprocess_exec,
                )
            )
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent.ClientSideConnection",
                    return_value=conn,
                )
            )
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent._filter_jsonrpc_lines",
                    new=_fake_filter,
                )
            )
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent.asyncio.StreamReader",
                    return_value=MagicMock(),
                )
            )
            agent._start_acp_server(state)
    finally:
        agent._executor.close(timeout=1.0)
    return conn


def test_start_acp_server_wires_configured_policy_onto_bridge(tmp_path) -> None:
    agent = ACPAgent(
        acp_command=_CLAUDE_COMMAND,
        acp_permission_policy="read_only",
    )
    conn = _start_acp_server_with_mocked_transport(agent, tmp_path)
    assert agent._client is not None
    assert agent._client.permission_policy == "read_only"
    conn.set_session_mode.assert_awaited_once_with(
        mode_id="default",
        session_id="sess-1",
    )


def test_read_only_unknown_command_fails_closed_before_subprocess(tmp_path) -> None:
    agent = ACPAgent(acp_command=["echo"], acp_permission_policy="read_only")
    state = _make_state(tmp_path, agent)
    with pytest.raises(ValueError, match="write paths are unverified"):
        agent._start_acp_server(state)
    assert agent._client is None


def test_read_only_codex_fails_closed_before_subprocess(tmp_path) -> None:
    agent = ACPAgent(
        acp_command=["npx", "-y", "@agentclientprotocol/codex-acp"],
        acp_permission_policy="read_only",
    )
    state = _make_state(tmp_path, agent)
    with pytest.raises(ValueError, match="write paths are unverified"):
        agent._start_acp_server(state)


@pytest.mark.asyncio
async def test_concurrent_bridges_do_not_share_permission_policy() -> None:
    async def decision(policy: str) -> str:
        bridge = _OpenHandsACPBridge(permission_policy=policy)  # type: ignore[arg-type]
        response = await bridge.request_permission(
            [SimpleNamespace(option_id="allow_once")],
            "sess",
            {"tool": "write"},
        )
        return response.outcome.outcome

    outcomes = await asyncio.gather(
        decision("writable"),
        decision("read_only"),
    )
    assert outcomes == ["selected", "cancelled"]


def test_settings_create_agent_forwards_permission_policy() -> None:
    from openhands.sdk.settings import ACPAgentSettings

    agent = ACPAgentSettings(
        acp_server="claude-code",
        acp_permission_policy="read_only",
    ).create_agent()
    assert agent.acp_permission_policy == "read_only"


def test_writable_codex_defaults_remain_auto_approve() -> None:
    from openhands.sdk.settings import ACPAgentSettings

    agent = ACPAgentSettings(acp_server="codex").create_agent()
    response = resolve_permission_response(
        agent.acp_permission_policy,
        [SimpleNamespace(option_id="allow_once")],
        {"tool": "bash"},
    )
    assert isinstance(response.outcome, AllowedOutcome)
    assert agent.acp_permission_policy == "writable"


def test_extract_session_modes_from_typed_new_session() -> None:
    from acp.schema import NewSessionResponse

    response = NewSessionResponse(
        session_id="sess-1",
        modes=_claude_session_modes("default"),
    )
    current, available = _extract_session_modes(response)
    assert current == "default"
    assert available == {"default", "bypassPermissions"}


def test_extract_session_modes_ignores_magicmock_auto_attrs() -> None:
    current, available = _extract_session_modes(MagicMock())
    assert current is None
    assert available == set()


@pytest.mark.asyncio
async def test_read_only_fails_closed_when_mode_is_not_advertised() -> None:
    conn = MagicMock()
    conn.set_session_mode = AsyncMock()
    client = _OpenHandsACPBridge(permission_policy="read_only")
    with pytest.raises(ACPSessionModeError, match="did not advertise"):
        await _apply_acp_session_mode(
            conn,
            client,
            policy="read_only",
            mode_id="default",
            agent_name="claude-agent-acp",
            session_id="sess-1",
            session_response=MagicMock(),
        )
    conn.set_session_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_accepts_advertised_current_mode() -> None:
    from acp.schema import NewSessionResponse

    conn = MagicMock()
    conn.set_session_mode = AsyncMock()
    client = _OpenHandsACPBridge(permission_policy="read_only")
    await _apply_acp_session_mode(
        conn,
        client,
        policy="read_only",
        mode_id="default",
        agent_name="claude-agent-acp",
        session_id="sess-1",
        session_response=NewSessionResponse(
            session_id="sess-1",
            modes=_claude_session_modes("default"),
        ),
    )
    conn.set_session_mode.assert_awaited_once_with(
        mode_id="default",
        session_id="sess-1",
    )


@pytest.mark.asyncio
async def test_read_only_fails_closed_without_mode_confirmation() -> None:
    from acp.schema import NewSessionResponse

    conn = MagicMock()
    conn.set_session_mode = AsyncMock()
    client = _OpenHandsACPBridge(permission_policy="read_only")
    with pytest.raises(ACPSessionModeError, match="did not confirm"):
        await _apply_acp_session_mode(
            conn,
            client,
            policy="read_only",
            mode_id="default",
            agent_name="claude-agent-acp",
            session_id="sess-1",
            session_response=NewSessionResponse(
                session_id="sess-1",
                modes=_claude_session_modes("bypassPermissions"),
            ),
        )


@pytest.mark.asyncio
async def test_read_only_confirms_mode_via_current_mode_update() -> None:
    from acp.schema import NewSessionResponse

    conn = MagicMock()
    client = _OpenHandsACPBridge(permission_policy="read_only")

    async def _set_mode(*, mode_id: str, session_id: str) -> None:
        await client.session_update(
            session_id,
            CurrentModeUpdate(
                session_update="current_mode_update",
                current_mode_id=mode_id,
            ),
        )

    conn.set_session_mode = AsyncMock(side_effect=_set_mode)
    await _apply_acp_session_mode(
        conn,
        client,
        policy="read_only",
        mode_id="default",
        agent_name="claude-agent-acp",
        session_id="sess-1",
        session_response=NewSessionResponse(
            session_id="sess-1",
            modes=_claude_session_modes("bypassPermissions"),
        ),
    )
    assert client.get_current_mode_id("sess-1") == "default"


@pytest.mark.asyncio
async def test_writable_does_not_require_advertised_modes() -> None:
    conn = MagicMock()
    conn.set_session_mode = AsyncMock()
    client = _OpenHandsACPBridge(permission_policy="writable")
    await _apply_acp_session_mode(
        conn,
        client,
        policy="writable",
        mode_id="bypassPermissions",
        agent_name="claude-agent-acp",
        session_id="sess-1",
        session_response=MagicMock(),
    )
    conn.set_session_mode.assert_awaited_once_with(
        mode_id="bypassPermissions",
        session_id="sess-1",
    )


def test_writable_unknown_runtime_name_does_not_use_command_provider_mode(
    tmp_path,
) -> None:
    agent = ACPAgent(acp_command=_CLAUDE_COMMAND)
    conn = _start_acp_server_with_mocked_transport(
        agent, tmp_path, agent_name="custom-acp"
    )
    conn.set_session_mode.assert_not_awaited()


def _select_config_option(
    option_id: str,
    current_value: str,
    values: list[str] | None = None,
) -> SessionConfigOptionSelect:
    choices = values or [current_value]
    return SessionConfigOptionSelect(
        id=option_id,
        name=option_id,
        type="select",
        current_value=current_value,
        options=[SessionConfigSelectOption(name=v, value=v) for v in choices],
    )


def _start_acp_server_with_session_config(
    agent: ACPAgent,
    tmp_path,
    *,
    agent_name: str = "claude-agent-acp",
    modes: SessionModeState | None = None,
    config_options: list[SessionConfigOptionSelect] | None = None,
    replace_mode_on_config: str | None = None,
    emit_mode_after_config: str | None = None,
) -> MagicMock:
    conn = MagicMock()
    init_response = MagicMock()
    init_response.agent_info = MagicMock()
    init_response.agent_info.name = agent_name
    init_response.agent_info.version = "1.0"
    init_response.auth_methods = []
    conn.initialize = AsyncMock(return_value=init_response)
    current = list(config_options or [])
    conn.new_session = AsyncMock(
        return_value=NewSessionResponse(
            session_id="sess-1",
            modes=modes,
            config_options=list(current) or None,
        )
    )
    conn.load_session = AsyncMock(return_value=MagicMock())
    conn.set_session_mode = AsyncMock()
    conn.set_session_model = AsyncMock()
    conn.authenticate = AsyncMock()
    conn.close = AsyncMock()

    async def _set_config_option(
        *, config_id: str, session_id: str, value: str | bool
    ) -> SetSessionConfigOptionResponse:
        str_value = str(value)
        for i, option in enumerate(current):
            if option.id != config_id:
                continue
            choices = [select_option.value for select_option in option.options]
            if str_value not in choices:
                choices = [*choices, str_value]
            current[i] = SessionConfigOptionSelect(
                id=option.id,
                name=option.name,
                type="select",
                current_value=str_value,
                options=[SessionConfigSelectOption(name=v, value=v) for v in choices],
            )
            break
        else:
            raise AssertionError(f"unknown config option {config_id}")
        if replace_mode_on_config is not None:
            client = agent._client
            assert client is not None
            await client.session_update(
                session_id,
                CurrentModeUpdate(
                    session_update="current_mode_update",
                    current_mode_id=replace_mode_on_config,
                ),
            )
        elif emit_mode_after_config is not None:
            client = agent._client
            assert client is not None
            await client.session_update(
                session_id,
                CurrentModeUpdate(
                    session_update="current_mode_update",
                    current_mode_id=emit_mode_after_config,
                ),
            )
        return SetSessionConfigOptionResponse(config_options=list(current))

    conn.set_config_option = AsyncMock(side_effect=_set_config_option)

    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()

    async def _fake_create_subprocess_exec(*_args, env=None, **_kwargs):
        return mock_process

    async def _fake_filter(_src, _dst):
        return None

    state = _make_state(tmp_path, agent)
    agent._executor = AsyncExecutor()
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent.asyncio.create_subprocess_exec",
                    new=_fake_create_subprocess_exec,
                )
            )
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent.ClientSideConnection",
                    return_value=conn,
                )
            )
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent._filter_jsonrpc_lines",
                    new=_fake_filter,
                )
            )
            stack.enter_context(
                patch(
                    "openhands.sdk.agent.acp_agent.asyncio.StreamReader",
                    return_value=MagicMock(),
                )
            )
            agent._start_acp_server(state)
    finally:
        agent._executor.close(timeout=1.0)
    return conn


@pytest.mark.parametrize(
    "config_id",
    ["mode", "permissionMode", "permission_mode", "session-mode", "session_mode"],
)
def test_permission_mode_config_ids_are_detected(config_id: str) -> None:
    assert is_permission_mode_config_option(config_id)


def test_unrelated_config_ids_are_not_permission_mode_options() -> None:
    assert not is_permission_mode_config_option("effort")
    assert not is_permission_mode_config_option("model")
    assert not is_permission_mode_config_option("fast")


def test_read_only_rejects_permission_mode_config_option_ids() -> None:
    with pytest.raises(ValueError, match="permission/session mode"):
        assert_config_options_compatible_with_permission_policy(
            "read_only",
            {"mode": "bypassPermissions"},
        )
    with pytest.raises(ValueError, match="permission/session mode"):
        assert_config_options_compatible_with_permission_policy(
            "read_only",
            {"permissionMode": "acceptEdits"},
        )


def test_read_only_allows_unrelated_config_values_that_look_like_modes() -> None:
    assert_config_options_compatible_with_permission_policy(
        "read_only",
        {"effort": "default", "plan": "acceptEdits"},
    )


def test_writable_allows_permission_mode_config_options() -> None:
    assert_config_options_compatible_with_permission_policy(
        "writable",
        {"mode": "bypassPermissions"},
    )


@pytest.mark.parametrize(
    "requested",
    [
        {"mode": "bypassPermissions"},
        {"mode": "acceptEdits"},
        {"permissionMode": "bypassPermissions"},
    ],
)
def test_read_only_startup_rejects_bypass_mode_config_options(
    tmp_path, requested: dict[str, str]
) -> None:
    agent = ACPAgent(
        acp_command=_CLAUDE_COMMAND,
        acp_permission_policy="read_only",
        acp_config_options=requested,
    )
    state = _make_state(tmp_path, agent)
    with pytest.raises(ValueError, match="permission/session mode"):
        agent._start_acp_server(state)
    assert agent._client is None


def test_read_only_startup_applies_unrelated_config_options(tmp_path) -> None:
    agent = ACPAgent(
        acp_command=_CLAUDE_COMMAND,
        acp_permission_policy="read_only",
        acp_config_options={"effort": "medium"},
    )
    conn = _start_acp_server_with_session_config(
        agent,
        tmp_path,
        modes=_claude_session_modes("default"),
        config_options=[
            _select_config_option("effort", "low", ["low", "medium", "high"]),
        ],
        emit_mode_after_config="default",
    )
    conn.set_session_mode.assert_awaited_once_with(
        mode_id="default",
        session_id="sess-1",
    )
    conn.set_config_option.assert_awaited_once_with(
        config_id="effort",
        session_id="sess-1",
        value="medium",
    )


def test_read_only_startup_fails_without_post_config_mode_evidence(tmp_path) -> None:
    agent = ACPAgent(
        acp_command=_CLAUDE_COMMAND,
        acp_permission_policy="read_only",
        acp_config_options={"effort": "medium"},
    )
    with pytest.raises(ACPSessionModeError, match="did not prove"):
        _start_acp_server_with_session_config(
            agent,
            tmp_path,
            modes=_claude_session_modes("default"),
            config_options=[
                _select_config_option("effort", "low", ["low", "medium", "high"]),
            ],
        )


def test_read_only_startup_fails_if_config_replaces_session_mode(tmp_path) -> None:
    agent = ACPAgent(
        acp_command=_CLAUDE_COMMAND,
        acp_permission_policy="read_only",
        acp_config_options={"effort": "medium"},
    )
    with pytest.raises(ACPSessionModeError, match="did not prove"):
        _start_acp_server_with_session_config(
            agent,
            tmp_path,
            modes=_claude_session_modes("default"),
            config_options=[
                _select_config_option("effort", "low", ["low", "medium", "high"]),
            ],
            replace_mode_on_config="bypassPermissions",
        )


def test_read_only_startup_fails_if_config_state_includes_bypass_mode(
    tmp_path,
) -> None:
    agent = ACPAgent(
        acp_command=_CLAUDE_COMMAND,
        acp_permission_policy="read_only",
        acp_config_options={"effort": "medium"},
    )
    with pytest.raises(ACPSessionModeError, match="configuration option"):
        _start_acp_server_with_session_config(
            agent,
            tmp_path,
            modes=_claude_session_modes("default"),
            config_options=[
                _select_config_option("effort", "low", ["low", "medium", "high"]),
                _select_config_option(
                    "mode",
                    "bypassPermissions",
                    ["default", "bypassPermissions", "acceptEdits"],
                ),
            ],
            emit_mode_after_config="default",
        )


def test_writable_startup_still_applies_bypass_mode_config_option(tmp_path) -> None:
    agent = ACPAgent(
        acp_command=["echo"],
        acp_config_options={"mode": "bypassPermissions"},
    )
    conn = _start_acp_server_with_session_config(
        agent,
        tmp_path,
        agent_name="custom-acp",
        config_options=[
            _select_config_option(
                "mode",
                "default",
                ["default", "bypassPermissions", "acceptEdits"],
            ),
        ],
    )
    conn.set_session_mode.assert_not_awaited()
    conn.set_config_option.assert_awaited_once_with(
        config_id="mode",
        session_id="sess-1",
        value="bypassPermissions",
    )


@pytest.mark.asyncio
async def test_apply_config_rejects_mode_option_under_read_only() -> None:
    conn = MagicMock()
    conn.set_config_option = AsyncMock()
    client = _OpenHandsACPBridge(permission_policy="read_only")
    with pytest.raises(ValueError, match="permission/session mode"):
        await _apply_session_config_options(
            conn,
            client,
            "claude-agent-acp",
            "sess-1",
            {"mode": "acceptEdits"},
            [
                _select_config_option(
                    "mode",
                    "default",
                    ["default", "acceptEdits"],
                )
            ],
            required_session_mode="default",
        )
    conn.set_config_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_config_fails_closed_without_fresh_mode_evidence() -> None:
    conn = MagicMock()
    client = _OpenHandsACPBridge(permission_policy="read_only")
    await client.session_update(
        "sess-1",
        CurrentModeUpdate(
            session_update="current_mode_update",
            current_mode_id="default",
        ),
    )

    async def _set_config_option(
        *, config_id: str, session_id: str, value: str | bool
    ) -> SetSessionConfigOptionResponse:
        return SetSessionConfigOptionResponse(
            config_options=[
                _select_config_option("effort", str(value), ["low", "medium", "high"]),
            ]
        )

    conn.set_config_option = AsyncMock(side_effect=_set_config_option)
    with pytest.raises(ACPSessionModeError, match="did not prove"):
        await _apply_session_config_options(
            conn,
            client,
            "claude-agent-acp",
            "sess-1",
            {"effort": "medium"},
            [_select_config_option("effort", "low", ["low", "medium", "high"])],
            required_session_mode="default",
        )


@pytest.mark.asyncio
async def test_read_only_config_accepts_fresh_mode_after_writes() -> None:
    conn = MagicMock()
    client = _OpenHandsACPBridge(permission_policy="read_only")
    await client.session_update(
        "sess-1",
        CurrentModeUpdate(
            session_update="current_mode_update",
            current_mode_id="default",
        ),
    )

    async def _set_config_option(
        *, config_id: str, session_id: str, value: str | bool
    ) -> SetSessionConfigOptionResponse:
        await client.session_update(
            session_id,
            CurrentModeUpdate(
                session_update="current_mode_update",
                current_mode_id="default",
            ),
        )
        return SetSessionConfigOptionResponse(
            config_options=[
                _select_config_option("effort", str(value), ["low", "medium", "high"]),
            ]
        )

    conn.set_config_option = AsyncMock(side_effect=_set_config_option)
    await _apply_session_config_options(
        conn,
        client,
        "claude-agent-acp",
        "sess-1",
        {"effort": "medium"},
        [_select_config_option("effort", "low", ["low", "medium", "high"])],
        required_session_mode="default",
    )
    conn.set_config_option.assert_awaited_once()


def test_permission_logs_omit_tool_call_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {
        "tool_call_id": "call-1",
        "title": "Bash",
        "kind": "execute",
        "raw_input": {
            "command": "cat /secrets/oauth.txt",
            "path": "/home/user/.claude/.credentials.json",
            "token": "sk-live-secret-value",
        },
    }
    with caplog.at_level("INFO", logger="openhands.sdk.agent.acp_permission_policy"):
        resolve_permission_response("read_only", [], payload)
        resolve_permission_response(
            "writable",
            [SimpleNamespace(option_id="allow_once")],
            payload,
        )
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "cat /secrets/oauth.txt" not in combined
    assert "/home/user/.claude/.credentials.json" not in combined
    assert "sk-live-secret-value" not in combined
    assert "raw_input" not in combined
    assert "call-1" in combined
