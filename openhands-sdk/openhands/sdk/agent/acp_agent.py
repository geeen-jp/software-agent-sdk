"""ACPAgent — an AgentBase subclass that delegates to an ACP server.

The Agent Client Protocol (ACP) lets OpenHands power conversations using
ACP-compatible servers (Claude Code, Gemini CLI, etc.) instead of direct
LLM calls.  The ACP server manages its own LLM, tools, and execution;
the ACPAgent relays user messages and collects the response. OpenHands
can still append prompt-only context, such as a skill catalog, to the
user message before it is sent to the ACP server.

Unlike the built-in Agent, one ACP ``step()`` maps to one complete remote
assistant turn. ACPAgent therefore emits a terminal ``FinishAction`` at the
end of each step to delimit that completed turn for downstream consumers.

See https://agentclientprotocol.com/protocol/overview
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import inspect
import json
import os
import re
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Collection, Generator, Iterable, Mapping
from concurrent.futures import Future
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple
from unittest.mock import Mock

from acp.client.connection import ClientSideConnection
from acp.exceptions import RequestError as ACPRequestError
from acp.helpers import image_block, text_block
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ClientCapabilities,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    ImageContentBlock,
    McpServerStdio,
    PromptResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionModelState,
    SseMcpServer,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)
from acp.transports import default_environment
from pydantic import Field, PrivateAttr, SecretStr, field_serializer, field_validator

from openhands.sdk.agent.acp_claude_auth import (
    CLAUDE_CONFIG_DIR_ENV,
    CLAUDE_CREDENTIALS_FILENAME,
    CLAUDE_CREDENTIALS_SECRET_NAME,
    CLAUDE_OAUTH_ENV_NAMES,
    CLAUDE_OAUTH_TOKEN_ENV,
    CLAUDE_PAYG_CONFLICTING_ENV,
    claude_subscription_auth_active,
    create_claude_config_runtime_dir,
    discard_claude_oauth_runtime_dir,
    is_valid_claude_oauth_credentials,
    seed_claude_oauth_credentials,
    track_claude_oauth_credentials_from_file,
    unlink_claude_oauth_credentials,
)
from openhands.sdk.agent.acp_file_credentials import (
    ACPFileCredentialLifecycle,
    codex_auth_file_is_chatgpt,
    create_file_credential_lifecycle,
    write_secret_file,
)
from openhands.sdk.agent.acp_models import ACPModelInfo
from openhands.sdk.agent.acp_permission_policy import (
    ACPPermissionPolicy,
    assert_config_options_compatible_with_permission_policy,
    initial_agent_mode_env_for_policy,
    is_codex_mode_config_option,
    is_permission_mode_config_option,
    normalize_acp_permission_policy,
    resolve_permission_response,
    resolve_session_mode_for_policy,
)
from openhands.sdk.agent.acp_tracing import ACPTurnTrace
from openhands.sdk.agent.base import AgentBase
from openhands.sdk.context import AgentContext
from openhands.sdk.conversation.secret_registry import SecretRegistry
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.credential import (
    CredentialBindingError,
    CredentialSyncError,
    VersionedCredentialBinding,
)
from openhands.sdk.event import (
    ACPToolCallEvent,
    ActionEvent,
    MessageEvent,
    ObservationEvent,
    SystemPromptEvent,
)
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.llm import LLM, ImageContent, Message, MessageToolCall, TextContent
from openhands.sdk.logger import get_logger
from openhands.sdk.mcp.config import MCPServer
from openhands.sdk.observability.laminar import maybe_init_laminar, observe
from openhands.sdk.secret import SecretSource
from openhands.sdk.settings.acp_providers import (
    CLAUDE_AGENT_ACP_VERSION,
    ACPFileSecretSpec,
    _build_session_meta,
    default_acp_file_secrets,
    detect_acp_provider_by_agent_name,
    detect_acp_provider_by_command,
    resolve_acp_package_version,
    resolve_acp_runtime_version,
    resolve_effective_acp_provider_key,
    validate_claude_structured_output_schema,
)
from openhands.sdk.settings.structured_output import StructuredOutputConfig
from openhands.sdk.tool import Tool  # noqa: TC002
from openhands.sdk.tool.builtins.finish import FinishAction, FinishObservation
from openhands.sdk.utils import maybe_truncate
from openhands.sdk.utils.pydantic_secrets import serialize_secret


# Released ACP exposes select/boolean option variants directly, not a generic
# SessionConfigOption RootModel wrapper.
SessionConfigOption = SessionConfigOptionSelect | SessionConfigOptionBoolean


logger = get_logger(__name__)
maybe_init_laminar()


if TYPE_CHECKING:
    from openhands.sdk.conversation import (
        ConversationCallbackType,
        ConversationState,
        ConversationTokenCallbackType,
        LocalConversation,
    )


# Maximum seconds to wait for a UsageUpdate notification after prompt()
# returns. The ACP server writes UsageUpdate to the wire before the
# PromptResponse, so under normal conditions the notification handler
# completes almost immediately. This timeout is a safety net for slow
# or remote servers.
_USAGE_UPDATE_TIMEOUT: float = float(os.environ.get("ACP_USAGE_UPDATE_TIMEOUT", "2.0"))

# Retry configuration for transient ACP connection errors.
# These errors can occur when the connection drops mid-conversation but the
# session state is still valid on the server side.
_ACP_PROMPT_MAX_RETRIES: int = int(os.environ.get("ACP_PROMPT_MAX_RETRIES", "3"))
_ACP_PROMPT_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 30.0)  # seconds

# Exception types that indicate transient connection issues worth retrying
_RETRIABLE_CONNECTION_ERRORS = (OSError, ConnectionError, BrokenPipeError, EOFError)

# JSON-RPC error codes from the ACP server that are transient and worth
# retrying.  These map to server-side failures (HTTP 500 equivalents) where
# the session state is still valid but the request failed.
# -32603 = "Internal error" (JSON-RPC spec) — covers ACP server crashes,
#          upstream model 500s, and transient infrastructure errors.
_RETRIABLE_SERVER_ERROR_CODES: frozenset[int] = frozenset({-32603})

# Maximum characters for ACP tool call content — matches MAX_CMD_OUTPUT_SIZE
# used by the terminal tool and the default max_message_chars in LLM config.
MAX_ACP_CONTENT_CHARS: int = 30_000

# Env vars that conflict with Claude Code's OAuth/subscription channel.
# Strip PAYG/proxy vars when that channel is active. Host CLAUDE_CONFIG_DIR
# alone must not disable API/proxy auth or other providers' credentials.
# CLAUDE_CREDENTIALS_JSON is an SDK input secret; the subprocess never
# receives the JSON blob.
_CLAUDE_OAUTH_CONFLICTING_ENV: frozenset[str] = CLAUDE_PAYG_CONFLICTING_ENV

# Limit for asyncio.StreamReader buffers used by the ACP subprocess pipes.
# The default (64 KiB) is too small for session_update notifications that
# carry large tool-call outputs (e.g. file contents, test results).  When
# a single JSON-RPC line exceeds the limit, readline() raises
# LimitOverrunError, silently killing the filter/receive pipeline and
# leaving the prompt() future unresolved forever.  100 MiB is a pragmatic
# compatibility limit for current ACP servers, not an endorsement of huge
# JSON-RPC payloads; the long-term fix is protocol-level chunking/streaming
# for large tool output.
_STREAM_READER_LIMIT: int = 100 * 1024 * 1024  # 100 MiB

# Bound on each await performed while tearing down a partially initialized
# ACP subprocess.  Initialization can fail after the process is spawned but
# before its handles reach the ACPAgent attributes, so that teardown is the
# only thing able to reap it — and it must not hang the failing init_state
# call on a subprocess that is already wedged or gone.
_ACP_INIT_ABORT_TIMEOUT: float = float(os.environ.get("ACP_INIT_ABORT_TIMEOUT", "5.0"))

# Bound on each ACP runtime teardown await (process wait, cancelled reader
# tasks, connection close).  ``AsyncExecutor.run_async(..., timeout=)`` uses
# ``anyio.fail_after``, which cannot return until the cancelled await actually
# exits — so the await itself must also yield at this deadline.
_ACP_RUNTIME_SHUTDOWN_TIMEOUT: float = float(
    os.environ.get("ACP_RUNTIME_SHUTDOWN_TIMEOUT", "5.0")
)

# Minimum interval between on_activity heartbeat signals (seconds).
# Throttled to avoid excessive calls while still keeping the idle timer
# well below the ~20 min runtime-api kill threshold.
_ACTIVITY_SIGNAL_INTERVAL: float = 30.0

# After a timeout/cancellation, wait briefly for the ACP prompt task to react
# to session/cancel before rewiring callbacks for the next turn.
_ACP_CANCEL_DRAIN_TIMEOUT: float = float(
    os.environ.get("ACP_CANCEL_DRAIN_TIMEOUT", "2.0")
)

# ACP tool-call statuses that represent a terminal outcome.  Non-terminal
# statuses (``pending``, ``in_progress``) mean the call is still in flight
# and, if the turn aborts before it reaches a terminal state, the live-
# emitted event on state.events will otherwise be orphaned forever.
_TERMINAL_TOOL_CALL_STATUSES: frozenset[str] = frozenset({"completed", "failed"})


class _PromptDrainResult(NamedTuple):
    drained: bool
    completed: bool
    response: PromptResponse | None
    error: BaseException | None


# Stable identifier stamped onto the sentinel LLM so downstream code
# (e.g. title_utils) can detect "this LLM cannot be called" without
# relying on the model name — which we overwrite with the real model
# once ``acp_model`` is known, so logs and serialized state show the
# actual model rather than "acp-managed".
ACP_SENTINEL_USAGE_ID = "acp-managed"

# Last N chars of an ACP session id shown in logs — enough entropy to correlate
# across log lines for one conversation but not enough to brute-force the full id.
_SESSION_ID_LOG_SUFFIX_LEN: Final[int] = 8


def _fingerprint_session_id(session_id: str | None) -> str:
    """Render an ACP session id as a short, non-reversible fingerprint."""
    if session_id is None:
        return "<none>"
    if len(session_id) <= _SESSION_ID_LOG_SUFFIX_LEN:
        return "<short>"
    return f"...{session_id[-_SESSION_ID_LOG_SUFFIX_LEN:]}"


def _make_dummy_llm() -> LLM:
    """Create a dummy LLM that should never be called directly."""
    return LLM(model="acp-managed", usage_id=ACP_SENTINEL_USAGE_ID)


# ---------------------------------------------------------------------------
# ACP Client implementation
# ---------------------------------------------------------------------------


# ACP auth method ID → environment variables that supply the credential.
# These IDs are the contract advertised by the pinned Codex ACP adapter. Keep
# this table exact: accepting arbitrary/legacy-looking IDs can select a billing
# path the adapter did not actually offer.
_AUTH_METHOD_ENV_MAP: dict[str, tuple[str, ...]] = {
    "api-key": ("CODEX_API_KEY", "OPENAI_API_KEY"),
    "gemini-api-key": ("GEMINI_API_KEY",),
}


class ACPAuthSelectionError(RuntimeError):
    """The ACP server's advertised authentication contract cannot be met."""


_ACP_DIAGNOSTIC_TAIL_CHARS = 4_000
_ACP_OFFERED_AUTH_MAX_IDS = 16
_ACP_OFFERED_AUTH_ID_CHARS = 64
_ACP_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)(['\"]?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|password)['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+"
    ),
)


def _has_usable_env_value(env: dict[str, str], name: str) -> bool:
    value = env.get(name)
    return isinstance(value, str) and bool(value.strip())


def _sanitize_acp_diagnostic(
    value: object, mask: Callable[[str], str] | None = None
) -> str:
    """Return a bounded diagnostic string without credential-shaped values."""
    text = str(value)
    if mask is not None:
        text = mask(text)
    for pattern in _ACP_SECRET_PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    if len(text) > _ACP_DIAGNOSTIC_TAIL_CHARS:
        text = text[-_ACP_DIAGNOSTIC_TAIL_CHARS:]
    return text


async def _capture_acp_stderr(stream: Any, tail: list[str]) -> None:
    """Keep only a bounded in-memory stderr tail for startup diagnostics."""
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
            if isinstance(line, bytes):
                line = line.decode(errors="replace")
            tail[0] = (tail[0] + str(line))[-_ACP_DIAGNOSTIC_TAIL_CHARS:]
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("ACP stderr capture stopped", exc_info=True)


def _acp_startup_log_extra(
    *,
    phase: str,
    launcher: str,
    provider_key: str | None,
    package_version: str | None,
    adapter_name: str | None,
    adapter_version: str | None,
    runtime_version: str | None,
    rpc: str | None,
    stage: str | None,
    exit_code: int | None,
    stderr_tail: str,
    detail: str,
) -> dict[str, object]:
    """Structured, bounded startup diagnostics for logging."""
    return {
        "phase": phase,
        "launcher": launcher,
        "provider_key": provider_key,
        "package_version": package_version,
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "runtime_version": runtime_version,
        "rpc": rpc,
        "stage": stage,
        "exit_code": exit_code,
        "stderr_tail": stderr_tail,
        "detail": detail,
    }


def _bound_offered_auth_ids(auth_methods: list[Any]) -> list[str]:
    """Return a bounded, sanitized list of advertised ACP auth method IDs."""
    ids: list[str] = []
    for method in auth_methods:
        raw = getattr(method, "id", None)
        if not raw:
            continue
        text = _sanitize_acp_diagnostic(str(raw))[:_ACP_OFFERED_AUTH_ID_CHARS]
        if text and text not in ids:
            ids.append(text)
        if len(ids) >= _ACP_OFFERED_AUTH_MAX_IDS:
            break
    return ids


def _select_auth_method(
    auth_methods: list[Any],
    env: dict[str, str],
) -> str | None:
    """Pick an auth method whose required credentials are present.

    Returns the ``id`` of the first matching method, or ``None`` if no
    supported credential source is available (the server may not require auth).

    ChatGPT subscription credentials in the effective ``CODEX_HOME`` are
    checked first so they take precedence over explicit API keys. When
    ``CODEX_HOME`` is isolated, the effective file is conversation-scoped and
    the host user's ``~/.codex/auth.json`` is never consulted.
    """
    if not isinstance(auth_methods, list):
        return None
    method_ids = {m.id for m in auth_methods}
    if "chat-gpt" in method_ids and codex_auth_file_is_chatgpt(env):
        return "chat-gpt"
    for method_id, env_vars in _AUTH_METHOD_ENV_MAP.items():
        if method_id not in method_ids:
            continue
        if isinstance(env_vars, str):
            env_vars = (env_vars,)
        if any(_has_usable_env_value(env, env_var) for env_var in env_vars):
            return method_id
    return None


def _should_defer_codex_chatgpt_auth(
    *,
    is_codex: bool,
    method_id: str,
    env: dict[str, str],
    has_codex_api_key: bool,
) -> bool:
    """Let codex-acp validate file-backed ChatGPT auth during session creation.

    codex-acp's explicit ``authenticate(chat-gpt)`` path asks the app-server to
    refresh the account and falls back to browser login when that refresh does
    not return an account. A file-backed ChatGPT credential is already the
    isolated runtime's auth source, so session creation must perform the
    non-refreshing auth check instead. API-key and all non-Codex paths retain
    the normal explicit-auth behavior.
    """
    return (
        is_codex
        and method_id == "chat-gpt"
        and not has_codex_api_key
        and codex_auth_file_is_chatgpt(env)
    )


class ACPSessionConfigError(RuntimeError):
    """A requested ACP session configuration could not be applied or verified.

    Raised during initialization so the session is never prompted with a
    configuration the ACP server did not confirm.
    """


class ACPSessionModelError(RuntimeError):
    """A requested ACP session model could not be applied or verified.

    Raised when the server accepts a model switch but the authoritative
    effective model cannot be confirmed or does not match the request.
    """


class ACPSessionModeError(RuntimeError):
    """A required ACP session mode could not be advertised or confirmed."""


def _classify_acp_init_error(exc: BaseException) -> str:
    """Map a cold-start failure to a structured ``ConversationErrorEvent`` code."""
    if isinstance(exc, ACPAuthSelectionError):
        return "ACPAuthError"
    if isinstance(exc, TimeoutError):
        return "ACPStartupTimeout"
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return "ACPSpawnError"
    return "ACPInitError"


# Session config-option id that selects the model on ACP servers that drive
# model selection through ``configOptions`` / ``session/set_config_option``
# (codex-acp, claude-agent-acp 0.44+) rather than the UNSTABLE ``models``
# capability + ``session/set_model`` (gemini-cli, older codex/claude).
_MODEL_CONFIG_OPTION_ID = "model"
_CLAUDE_MODE_CONFIG_OPTION_ID = "mode"
_CODEX_REASONING_EFFORTS: Final[frozenset[str]] = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max"}
)


def _codex_model_config_options(model: str) -> tuple[tuple[str, str], ...]:
    """Map combined Canvas Codex model IDs to codex-acp config options."""
    base_model, sep, effort = model.rpartition("/")
    if sep and base_model and effort in _CODEX_REASONING_EFFORTS:
        return (
            (_MODEL_CONFIG_OPTION_ID, base_model),
            ("reasoning_effort", effort),
        )
    return ((_MODEL_CONFIG_OPTION_ID, model),)


def _model_config_options(
    agent_name: str | None,
    model: str,
) -> tuple[tuple[str, str], ...]:
    provider = detect_acp_provider_by_agent_name(agent_name or "")
    if provider is not None and provider.key == "codex":
        return _codex_model_config_options(model)
    return ((_MODEL_CONFIG_OPTION_ID, model),)


def _model_config_option_from_options(
    options: list[SessionConfigOption] | None,
) -> Any | None:
    """Return the ``model`` select option from a complete config option state."""
    for raw in _iter_session_config_options(options):
        opt = getattr(raw, "root", raw)
        if (
            getattr(opt, "type", None) == "select"
            and getattr(opt, "id", None) == _MODEL_CONFIG_OPTION_ID
        ):
            return opt
    return None


def _model_config_option(response: Any) -> Any | None:
    """Return the ``model`` ``configOptions`` select off a session response."""
    return _model_config_option_from_options(getattr(response, "config_options", None))


_META_MODEL_ID_KEYS: Final[tuple[str, ...]] = (
    "current_model_id",
    "model_id",
    "model",
)


def _codex_reasoning_effort_from_options(
    options: list[SessionConfigOption] | None,
) -> str | None:
    for raw in _iter_session_config_options(options):
        effort_opt = getattr(raw, "root", raw)
        if getattr(effort_opt, "id", None) != "reasoning_effort":
            continue
        effort = getattr(effort_opt, "current_value", None)
        if isinstance(effort, str) and effort in _CODEX_REASONING_EFFORTS:
            return effort
    return None


def _strip_codex_effort_suffix(model_id: str) -> str:
    base, sep, effort = model_id.rpartition("/")
    if sep and base and effort in _CODEX_REASONING_EFFORTS:
        return base
    return model_id


def _effective_model_from_config_options(
    options: list[SessionConfigOption] | None,
    agent_name: str | None,
) -> str | None:
    """Reconstruct the effective model id from a complete config option state."""
    opt = _model_config_option_from_options(options)
    if opt is None:
        return None
    base = getattr(opt, "current_value", None)
    if not isinstance(base, str) or not base:
        return None
    provider = detect_acp_provider_by_agent_name(agent_name or "")
    if provider is not None and provider.key == "codex":
        effort = _codex_reasoning_effort_from_options(options)
        if effort is not None:
            return f"{base}/{effort}"
    return base


def _surfaced_current_model_id(
    current_model_id: str | None,
    agent_name: str | None,
    options: list[SessionConfigOption] | None,
) -> str | None:
    """Refresh the published model id from verified post-config session state."""
    reconstructed = _effective_model_from_config_options(options, agent_name)
    if reconstructed is not None:
        return reconstructed
    if current_model_id is None or not options:
        return current_model_id
    provider = detect_acp_provider_by_agent_name(agent_name or "")
    if provider is None or provider.key != "codex":
        return current_model_id
    effort = _codex_reasoning_effort_from_options(options)
    if effort is None:
        return current_model_id
    return f"{_strip_codex_effort_suffix(current_model_id)}/{effort}"


def _requested_model_matches_observed(
    requested: str,
    observed: str,
    *,
    agent_name: str | None,
    via_config_option: bool,
) -> bool:
    if requested == observed:
        return True
    if not via_config_option:
        return False
    provider = detect_acp_provider_by_agent_name(agent_name or "")
    if provider is None or provider.key != "codex":
        return False
    base, sep, effort = requested.rpartition("/")
    if sep and effort in _CODEX_REASONING_EFFORTS:
        return False
    observed_base, observed_sep, observed_effort = observed.rpartition("/")
    return bool(
        observed_sep
        and observed_effort in _CODEX_REASONING_EFFORTS
        and observed_base == requested
    )


def _is_claude_exact_model_request(
    agent_name: str | None,
    requested_model: str | None,
) -> bool:
    provider = detect_acp_provider_by_agent_name(agent_name or "")
    return bool(
        provider is not None
        and provider.key == "claude-code"
        and requested_model
        and requested_model.startswith("claude-")
    )


def _is_qualified_claude_exact_model(
    agent_name: str | None,
    adapter_version: str | None,
    requested_model: str | None,
) -> bool:
    """Identify the pinned Claude path that needs same-turn model proof.

    Claude model-picker values such as ``opus`` and ``sonnet`` are selectors,
    not exact identities. Explicit Claude model IDs are intentionally detected
    by their concrete ``claude-`` spelling; no alias table is introduced.
    """
    return bool(
        _is_claude_exact_model_request(agent_name, requested_model)
        and adapter_version == CLAUDE_AGENT_ACP_VERSION
    )


def _should_emit_claude_raw_sdk_messages(
    agent_name: str | None,
    adapter_version: str | None,
    permission_policy: ACPPermissionPolicy,
) -> bool:
    """Enable root-model provenance for every qualified read-only Claude session.

    A session may begin with a selector and later switch to an exact model.
    Raw SDK emission is session-scoped in claude-agent-acp, so enabling it only
    for the initial exact request would make that valid runtime-switch path
    unverifiable. The bridge retains provenance only while an exact turn is
    active; non-exact turns therefore incur no retained raw payload.
    """
    provider = detect_acp_provider_by_agent_name(agent_name or "")
    return bool(
        normalize_acp_permission_policy(permission_policy) == "read_only"
        and provider is not None
        and provider.key == "claude-code"
        and adapter_version == CLAUDE_AGENT_ACP_VERSION
    )


def _resolve_effective_model(
    *,
    requested: str,
    agent_name: str | None,
    session_id: str,
    via_config_option: bool,
    response: Any | None = None,
    config_options: list[SessionConfigOption] | None = None,
    client: _OpenHandsACPBridge | None = None,
    accept_routing_selector: bool = False,
) -> str:
    """Return the reported model/selector or fail closed.

    ``accept_routing_selector`` is used only by the qualified Claude exact
    runtime-switch path.  Claude 0.81.0 can accept a concrete model request
    while returning its picker alias (for example ``opus``); that selector is
    diagnostic state only and the next turn must still pass the same-turn
    served-model gate.
    """
    effective: str | None = None

    if response is not None:
        extracted, _, _ = _extract_session_models(response)
        if extracted:
            effective = extracted

    if effective is None and response is not None:
        meta = getattr(response, "field_meta", None)
        if isinstance(meta, dict):
            models_block = meta.get("models")
            if models_block is not None:
                extracted, _, _ = _extract_session_models(
                    type("_ModelCarrier", (), {"models": models_block})()
                )
                if extracted:
                    effective = extracted
            if effective is None:
                for key in _META_MODEL_ID_KEYS:
                    value = meta.get(key)
                    if isinstance(value, str) and value:
                        effective = value
                        break

    if effective is None:
        options = config_options
        if options is None and client is not None:
            options = client.get_config_options(session_id)
        if via_config_option or options:
            effective = _effective_model_from_config_options(options, agent_name)

    if effective is None:
        raise ACPSessionModelError(
            f"ACP server {agent_name!r} session {session_id} did not report an "
            f"authoritative model after requesting {requested!r}; the requested "
            "model cannot be verified."
        )
    if not accept_routing_selector and not _requested_model_matches_observed(
        requested,
        effective,
        agent_name=agent_name,
        via_config_option=via_config_option,
    ):
        raise ACPSessionModelError(
            f"ACP server {agent_name!r} session {session_id} reports effective "
            f"model {effective!r} after requesting {requested!r}."
        )
    return effective


async def _apply_acp_model(
    conn: ClientSideConnection,
    session_id: str,
    model: str,
    *,
    agent_name: str | None = None,
    via_config_option: bool,
    client: _OpenHandsACPBridge | None = None,
    accept_routing_selector: bool = False,
) -> str:
    """Apply ``model`` and return its reported selector/effective model id.

    The qualified Claude exact runtime-switch path may return a selector alias;
    that path passes ``accept_routing_selector=True`` and relies on the next
    completed turn for exact execution authority.
    """
    if via_config_option:
        last_options: list[SessionConfigOption] | None = None
        for config_id, value in _model_config_options(agent_name, model):
            response = await conn.set_config_option(
                config_id=config_id, value=value, session_id=session_id
            )
            last_options = response.config_options
        if client is not None and last_options:
            # Selecting a model can reveal dependent options (for example
            # Gemini reasoning_effort or Muse context/effort). Preserve the
            # complete response as the authoritative state for the following
            # config pass; the server may not emit a ConfigOptionUpdate for
            # this response.
            client.record_config_options(session_id, last_options)
        return _resolve_effective_model(
            requested=model,
            agent_name=agent_name,
            session_id=session_id,
            via_config_option=True,
            config_options=last_options,
            client=client,
            accept_routing_selector=accept_routing_selector,
        )

    response = await conn.set_session_model(model_id=model, session_id=session_id)
    return _resolve_effective_model(
        requested=model,
        agent_name=agent_name,
        session_id=session_id,
        via_config_option=False,
        response=response,
        client=client,
        accept_routing_selector=accept_routing_selector,
    )


def _usable_models(infos: Iterable[ACPModelInfo]) -> list[ACPModelInfo]:
    """Drop entries without a usable ``model_id``."""
    return [info for info in infos if info.model_id]


def _extract_session_models(
    response: Any,
    *,
    default_via_config_option: bool = False,
) -> tuple[str | None, list[ACPModelInfo] | None, bool]:
    """Extract model state off a session response in a single scan."""
    if response is None:
        return None, None, default_via_config_option
    opt = _model_config_option(response)
    if opt is not None:
        current = getattr(opt, "current_value", None)
        current = current if isinstance(current, str) and current else None
        options = getattr(opt, "options", None)
        if not isinstance(options, list):
            options = []
        usable = _usable_models(
            ACPModelInfo.from_protocol(o, id_attr="value") for o in options
        )
        return current, usable, True
    models = getattr(response, "models", None)
    if models is not None:
        current = getattr(models, "current_model_id", None)
        current = current if isinstance(current, str) and current else None
        raw = getattr(models, "available_models", None)
        if not isinstance(raw, list):
            raw = []
        usable = _usable_models(ACPModelInfo.from_protocol(m) for m in raw)
        return current, usable, False
    return None, None, default_via_config_option


def _extract_session_modes(response: Any) -> tuple[str | None, set[str]]:
    """Return (current_mode_id, available_mode_ids) from a session response."""
    if response is None:
        return None, set()
    modes = getattr(response, "modes", None)
    if modes is None:
        return None, set()
    available_raw = getattr(modes, "available_modes", None)
    if not isinstance(available_raw, list):
        return None, set()
    available: set[str] = set()
    for mode in available_raw:
        mode_id = getattr(mode, "id", None)
        if isinstance(mode_id, str) and mode_id:
            available.add(mode_id)
    current = getattr(modes, "current_mode_id", None)
    current = current if isinstance(current, str) and current else None
    return current, available


async def _apply_acp_session_mode(
    conn: ClientSideConnection,
    client: _OpenHandsACPBridge,
    *,
    policy: str,
    mode_id: str | None,
    agent_name: str,
    session_id: str,
    session_response: Any,
) -> None:
    """Set the session mode; read_only also requires advertise + confirm."""
    if mode_id is None:
        return
    current_mode_id, available = _extract_session_modes(session_response)
    require_confirm = normalize_acp_permission_policy(policy) == "read_only"
    if require_confirm and mode_id not in available:
        raise ACPSessionModeError(
            f"ACP server {agent_name!r} session {session_id} did not advertise "
            f"required read_only session mode {mode_id!r}; "
            f"available modes: {sorted(available)}."
        )
    logger.info("Setting ACP session mode: %s", mode_id)
    try:
        await conn.set_session_mode(mode_id=mode_id, session_id=session_id)
    except ACPSessionModeError:
        raise
    except Exception as exc:
        if require_confirm:
            raise ACPSessionModeError(
                f"ACP server {agent_name!r} session {session_id} failed to set "
                f"read_only session mode {mode_id!r}: {exc}"
            ) from exc
        raise
    if not require_confirm:
        return
    observed = client.get_current_mode_id(session_id)
    if observed == mode_id or (observed is None and current_mode_id == mode_id):
        return
    raise ACPSessionModeError(
        f"ACP server {agent_name!r} session {session_id} did not confirm "
        f"read_only session mode {mode_id!r} "
        f"(advertised current={current_mode_id!r}, observed={observed!r})."
    )


def _iter_session_config_options(
    options: list[SessionConfigOption] | None,
) -> Iterable[SessionConfigOption]:
    if not isinstance(options, list):
        return []
    return options


def _verify_read_only_mode_after_config(
    client: _OpenHandsACPBridge,
    *,
    policy: str,
    required_mode_id: str | None,
    agent_name: str,
    session_id: str,
    config_options: list[SessionConfigOption] | None,
    provider_key: str | None = None,
    adapter_version: str | None = None,
    mode_generation_before: int = 0,
    require_fresh_mode: bool = False,
    config_options_authoritative: bool = False,
) -> None:
    """Fail closed if session configuration replaced the verified read_only mode.

    Codex ACP 1.1.7 updates ``agentMode`` in memory and exposes it as config
    option ``mode``; it does not emit ``current_mode_update``. Cursor likewise
    keeps the confirmed ``ask`` mode effective while updating model-dependent
    options without emitting a second mode notification. Those providers may
    prove ``read_only`` from their provider-specific effective state instead of
    requiring a fresh mode notification. The qualified Claude ACP 0.81.0
    returns a complete
    ``configOptions`` snapshot from ``session/set_config_option``; its ``mode``
    option mirrors the effective session mode, so that provider-specific
    snapshot is the qualified proof when no fresh mode notification is emitted.
    This exception is qualified only for the adapter version pinned by the
    provider registry; unknown or different versions still require a fresh
    ``CurrentModeUpdate`` after config writes.
    """
    if (
        normalize_acp_permission_policy(policy) != "read_only"
        or required_mode_id is None
    ):
        return
    observed = client.get_current_mode_id(session_id)
    generation = client.get_current_mode_generation(session_id)
    provider = detect_acp_provider_by_agent_name(agent_name)
    effective_provider_key = provider.key if provider is not None else provider_key
    if observed is not None and observed != required_mode_id:
        if require_fresh_mode:
            raise ACPSessionModeError(
                f"ACP server {agent_name!r} session {session_id} did not prove "
                f"read_only session mode {required_mode_id!r} after applying "
                f"session configuration (observed={observed!r})."
            )
        raise ACPSessionModeError(
            f"ACP server {agent_name!r} session {session_id} replaced "
            f"read_only session mode {required_mode_id!r} with {observed!r} "
            "after applying session configuration."
        )
    codex_mode_value: str | None = None
    saw_codex_mode = False
    for option in (
        *_iter_session_config_options(config_options),
        *_iter_session_config_options(client.get_config_options(session_id)),
    ):
        option_id = getattr(option, "id", None)
        if is_codex_mode_config_option(option_id):
            saw_codex_mode = True
            codex_mode_value = _config_option_current_value(option)
        if not is_permission_mode_config_option(option_id):
            continue
        current = _config_option_current_value(option)
        # Cursor returns the mode config option as part of the complete model
        # config state, but that value can lag the authoritative
        # session/set_mode result (for example it may still say ``agent``
        # after the confirmed mode is ``ask``). Cursor's current-mode
        # observation above is the authority for this provider.
        if effective_provider_key == "cursor":
            continue
        if current != required_mode_id:
            raise ACPSessionModeError(
                f"ACP server {agent_name!r} session {session_id} configuration "
                f"option {option_id!r}={current!r} replaced read_only session "
                f"mode {required_mode_id!r}."
            )
    if require_fresh_mode:
        if effective_provider_key == "codex":
            if not saw_codex_mode or codex_mode_value != required_mode_id:
                raise ACPSessionModeError(
                    f"ACP server {agent_name!r} session {session_id} did not "
                    f"prove read_only session mode {required_mode_id!r} after "
                    "applying session configuration "
                    f"(mode={codex_mode_value!r}, observed={observed!r})."
                )
            return
        if effective_provider_key == "cursor":
            if observed != required_mode_id:
                raise ACPSessionModeError(
                    f"ACP server {agent_name!r} session {session_id} did not "
                    f"confirm read_only session mode {required_mode_id!r} "
                    f"after applying session configuration (observed={observed!r})."
                )
            return
        if (
            effective_provider_key == "claude-code"
            and adapter_version == CLAUDE_AGENT_ACP_VERSION
        ):
            if generation > mode_generation_before and observed == required_mode_id:
                return
            if not config_options_authoritative:
                raise ACPSessionModeError(
                    f"ACP server {agent_name!r} session {session_id} did not "
                    f"return authoritative Claude config state to prove "
                    f"read_only session mode {required_mode_id!r} after applying "
                    "session configuration."
                )
            mode_options = [
                option
                for option in _iter_session_config_options(config_options)
                if getattr(option, "id", None) == _CLAUDE_MODE_CONFIG_OPTION_ID
            ]
            observed_mode = (
                _config_option_current_value(mode_options[0])
                if len(mode_options) == 1
                else None
            )
            if observed_mode != required_mode_id:
                raise ACPSessionModeError(
                    f"ACP server {agent_name!r} session {session_id} did not "
                    f"prove Claude read_only session mode {required_mode_id!r} "
                    "from the complete configOptions response "
                    f"(mode={observed_mode!r})."
                )
            return
        if generation <= mode_generation_before or observed != required_mode_id:
            raise ACPSessionModeError(
                f"ACP server {agent_name!r} session {session_id} did not prove "
                f"read_only session mode {required_mode_id!r} after applying "
                f"session configuration (observed={observed!r})."
            )


async def _maybe_set_session_model(
    conn: ClientSideConnection,
    agent_name: str,
    session_id: str,
    acp_model: str | None,
    *,
    via_config_option: bool = False,
    model_state: SessionModelState | None = None,
    apply_requested: bool = False,
    client: _OpenHandsACPBridge | None = None,
    defer_exact_model_verification: bool = False,
) -> str | None:
    """Apply the *initial* session model right after session creation.

    Registry providers are routed by name.  Servers outside the registry are
    routed by capability: *model_state* is the ``models`` field the ACP server
    returned from ``new_session`` / ``load_session``, and its presence is the
    protocol-level signal that the server supports ``session/set_model``.
    When *apply_requested* is set (session init with an explicit ``acp_model``),
    the requested model is pushed even if the server did not advertise model
    state in the session response.

    Returns the verified effective model id, or ``None`` when no override was
    applied.  Raises when *apply_requested* is set and the model cannot be
    applied or verified.  The qualified Claude exact-model path deliberately
    defers the final identity check until its PromptResponse: Claude's
    session/config selector is only routing evidence, while the session
    metadata request is the caller intent.
    """
    if not acp_model:
        return None
    if defer_exact_model_verification:
        selector_model = (
            _effective_model_from_config_options(
                client.get_config_options(session_id), agent_name
            )
            if client is not None
            else None
        )
        logger.info(
            "Deferring exact Claude model verification until the completed turn: "
            "requested=%s selector=%s",
            acp_model,
            selector_model,
        )
        return None
    provider = detect_acp_provider_by_agent_name(agent_name)
    if provider is not None:
        if not provider.supports_set_session_model:
            if apply_requested:
                raise ACPSessionModelError(
                    f"ACP provider {provider.key!r} does not support applying "
                    f"requested model {acp_model!r} on session {session_id}."
                )
            return None
        try:
            return await _apply_acp_model(
                conn,
                session_id,
                acp_model,
                agent_name=agent_name,
                via_config_option=via_config_option,
                client=client,
            )
        except ACPSessionModelError:
            if apply_requested:
                raise
            logger.warning(
                "Could not verify model %r on ACP server %s; "
                "the session will use the server default",
                acp_model,
                agent_name,
            )
            return None
        except ACPRequestError as e:
            if apply_requested:
                raise ACPSessionModelError(
                    f"ACP server {agent_name!r} rejected model {acp_model!r}: {e}"
                ) from e
            logger.warning(
                "Could not set model %r on ACP server %s (%s); "
                "the session will use the server default",
                acp_model,
                agent_name,
                e,
            )
            return None
    if model_state is not None or apply_requested:
        try:
            return await _apply_acp_model(
                conn,
                session_id,
                acp_model,
                agent_name=agent_name,
                via_config_option=via_config_option,
                client=client,
            )
        except ACPSessionModelError:
            if apply_requested:
                raise
            logger.warning(
                "Could not verify model %r on ACP server %s; "
                "the session will use the server default",
                acp_model,
                agent_name,
            )
            return None
        except ACPRequestError as e:
            if apply_requested:
                raise ACPSessionModelError(
                    f"ACP server {agent_name!r} rejected model {acp_model!r}: {e}"
                ) from e
            logger.warning(
                "Could not set model %r on ACP server %s (%s); "
                "the session will use the server default",
                acp_model,
                agent_name,
                e,
            )
            return None
    if apply_requested:
        raise ACPSessionModelError(
            f"ACP server {agent_name!r} session {session_id} did not advertise "
            f"model state and cannot apply requested model {acp_model!r}."
        )
    return None


async def _reapply_session_model_on_resume(
    conn: ClientSideConnection,
    agent_name: str,
    session_id: str,
    acp_model: str | None,
    *,
    via_config_option: bool,
    config_options: list[SessionConfigOption] | None = None,
    client: _OpenHandsACPBridge | None = None,
) -> str | None:
    """Reapply or verify the persisted model on a *resumed* session.

    When the provider supports runtime switching, the requested model is
    pushed and verified.  Otherwise the authoritative session state from
    ``load_session`` is checked against the request.  Any mismatch or missing
    evidence raises before prompt execution.
    """
    if not acp_model:
        return None
    provider = detect_acp_provider_by_agent_name(agent_name)
    if provider is None or provider.supports_runtime_model_switch:
        try:
            return await _apply_acp_model(
                conn,
                session_id,
                acp_model,
                agent_name=agent_name,
                via_config_option=via_config_option,
                client=client,
            )
        except ACPRequestError as e:
            raise ACPSessionModelError(
                f"ACP server {agent_name!r} rejected model {acp_model!r}: {e}"
            ) from e
    return _resolve_effective_model(
        requested=acp_model,
        agent_name=agent_name,
        session_id=session_id,
        via_config_option=via_config_option,
        config_options=config_options,
        client=client,
    )


def _config_option_current_value(
    option: SessionConfigOption,
) -> str:
    """Normalize an ACP config option's current value to a string."""
    value = option.current_value
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _config_option_values(
    options: list[SessionConfigOption] | None,
) -> dict[str, str]:
    """Map ``option id -> current value`` for a complete config option state."""
    return {
        option.id: _config_option_current_value(option)
        for option in _iter_session_config_options(options)
    }


_CANONICAL_BOOLEAN_REQUESTS = frozenset({"true", "false"})


def _config_options_index(
    options: list[SessionConfigOption] | None,
) -> dict[str, SessionConfigOption]:
    return {option.id: option for option in _iter_session_config_options(options)}


def _parse_boolean_request(value: str) -> bool:
    normalized = value.lower()
    if normalized not in _CANONICAL_BOOLEAN_REQUESTS:
        raise ACPSessionConfigError(
            f"Invalid boolean configuration request {value!r}; "
            "expected 'true' or 'false'."
        )
    return normalized == "true"


def _config_option_wire_value(
    option: SessionConfigOption,
    requested: str,
) -> str | bool:
    if isinstance(option, SessionConfigOptionBoolean):
        return _parse_boolean_request(requested)
    return requested


def _config_option_matches_request(
    option: SessionConfigOption,
    requested: str,
) -> bool:
    observed = _config_option_current_value(option)
    if isinstance(option, SessionConfigOptionBoolean):
        return observed == requested.lower()
    return observed == requested


def _resolve_config_options_state(
    client: _OpenHandsACPBridge,
    session_id: str,
    response_options: list[SessionConfigOption] | None,
) -> dict[str, SessionConfigOption]:
    by_id = _config_options_index(response_options)
    if by_id:
        return by_id
    return _config_options_index(client.get_config_options(session_id))


def _verify_requested_config_options(
    agent_name: str,
    session_id: str,
    requested: dict[str, str],
    state_by_id: dict[str, SessionConfigOption],
) -> None:
    for config_id, requested_value in requested.items():
        option = state_by_id.get(config_id)
        if option is None:
            raise ACPSessionConfigError(
                f"ACP server {agent_name!r} session {session_id} final "
                f"configuration state is missing requested option {config_id!r}; "
                f"available options: {sorted(state_by_id)}."
            )
        if not _config_option_matches_request(option, requested_value):
            current = _config_option_current_value(option)
            raise ACPSessionConfigError(
                f"ACP server {agent_name!r} session {session_id} final "
                f"configuration option {config_id!r}={current!r} does not match "
                f"requested {requested_value!r}."
            )


async def _apply_session_config_options(
    conn: ClientSideConnection,
    client: _OpenHandsACPBridge,
    agent_name: str,
    session_id: str,
    requested: dict[str, str],
    initial_options: list[SessionConfigOption] | None,
    *,
    required_session_mode: str | None = None,
    provider_key: str | None = None,
    adapter_version: str | None = None,
) -> list[SessionConfigOption] | None:
    """Apply *requested* session config options and verify the observed state.

    Runs once per session — after ``new_session`` and after a successful
    ``load_session`` — so a resumed session is configured exactly like a fresh
    one, before any prompt is sent.

    Every requested option is set with ``set_config_option`` and then verified
    against the *complete* option state the server reports back.  The state
    returned by the response is preferred; the ``ConfigOptionUpdate``
    notification recorded for this exact session is only consulted when the
    response does not describe the option (some servers publish the new state
    asynchronously, and selecting one option can reveal another).  After all
    writes complete, every requested option is checked again against the final
    complete state for that same session.  Under ``read_only``, permission or
    session-mode option ids are refused up front, and the effective session
    mode is checked again after the writes.

    Raises:
        ACPSessionConfigError: if the server exposes no config options, a
            requested option is absent, ``set_config_option`` fails, no
            complete state comes back, or the reported value differs from the
            requested one.  Never returns while a requested option is
            unverified.
        ACPSessionModeError: if applying configuration replaced a verified
            ``read_only`` session mode.
        ValueError: if a requested option would select a permission/session
            mode under ``read_only``.

    Returns:
        The verified final option state after applying *requested*, or
        ``None`` when no options were requested.
    """
    assert_config_options_compatible_with_permission_policy(
        client.permission_policy,
        requested,
    )
    if not requested:
        _verify_read_only_mode_after_config(
            client,
            policy=client.permission_policy,
            required_mode_id=required_session_mode,
            agent_name=agent_name,
            session_id=session_id,
            config_options=initial_options,
            provider_key=provider_key,
            adapter_version=adapter_version,
        )
        return None

    mode_generation_before = client.get_current_mode_generation(session_id)
    require_fresh_mode = (
        normalize_acp_permission_policy(client.permission_policy) == "read_only"
        and required_session_mode is not None
    )

    state_by_id = _config_options_index(initial_options)
    observed_by_id = _config_options_index(client.get_config_options(session_id))
    if observed_by_id:
        # A model selection can replace the option catalog. Prefer that later,
        # complete state over the session/new snapshot so model-dependent
        # option type/choices cannot be taken from a stale pre-model state.
        state_by_id = {**state_by_id, **observed_by_id}
    if not state_by_id and not observed_by_id:
        raise ACPSessionConfigError(
            f"ACP server {agent_name!r} session {session_id} reported no session "
            f"configuration options, but {sorted(requested)} were requested."
        )

    last_response_options: list[SessionConfigOption] | None = None

    for config_id, requested_value in requested.items():
        option = state_by_id.get(config_id) or observed_by_id.get(config_id)
        if option is None:
            available = sorted(state_by_id or observed_by_id)
            raise ACPSessionConfigError(
                f"ACP server {agent_name!r} session {session_id} does not expose "
                f"configuration option {config_id!r}; available options: "
                f"{available}."
            )

        wire_value = _config_option_wire_value(option, requested_value)
        try:
            response = await conn.set_config_option(
                config_id=config_id,
                session_id=session_id,
                value=wire_value,
            )
        except ACPSessionConfigError:
            raise
        except Exception as e:
            raise ACPSessionConfigError(
                f"ACP server {agent_name!r} session {session_id} failed to set "
                f"configuration option {config_id!r}={requested_value!r}: {e}"
            ) from e

        last_response_options = response.config_options
        state_by_id = _resolve_config_options_state(
            client,
            session_id,
            response.config_options,
        )
        if not state_by_id:
            raise ACPSessionConfigError(
                f"ACP server {agent_name!r} session {session_id} returned no "
                f"configuration state after setting {config_id!r}="
                f"{requested_value!r}; the requested configuration cannot be "
                "verified."
            )

        current_option = state_by_id.get(config_id)
        if current_option is None or not _config_option_matches_request(
            current_option, requested_value
        ):
            current = (
                _config_option_current_value(current_option)
                if current_option is not None
                else None
            )
            raise ACPSessionConfigError(
                f"ACP server {agent_name!r} session {session_id} reports "
                f"configuration option {config_id!r}={current!r} after "
                f"requesting {requested_value!r}."
            )
        logger.info(
            "ACP session config option verified: %s=%s (session %s)",
            config_id,
            requested_value,
            session_id,
        )

    final_state = _resolve_config_options_state(
        client,
        session_id,
        last_response_options,
    )
    if not final_state:
        raise ACPSessionConfigError(
            f"ACP server {agent_name!r} session {session_id} returned no final "
            "configuration state; the requested configuration cannot be verified."
        )
    _verify_requested_config_options(
        agent_name,
        session_id,
        requested,
        final_state,
    )
    provider = detect_acp_provider_by_agent_name(agent_name)
    effective_provider_key = provider.key if provider is not None else provider_key
    qualified_claude_adapter = (
        effective_provider_key == "claude-code"
        and adapter_version == CLAUDE_AGENT_ACP_VERSION
    )
    _verify_read_only_mode_after_config(
        client,
        policy=client.permission_policy,
        required_mode_id=required_session_mode,
        agent_name=agent_name,
        session_id=session_id,
        config_options=(
            last_response_options
            if qualified_claude_adapter
            else list(final_state.values())
        ),
        provider_key=provider_key,
        adapter_version=adapter_version,
        mode_generation_before=mode_generation_before,
        require_fresh_mode=require_fresh_mode,
        config_options_authoritative=(
            qualified_claude_adapter and last_response_options is not None
        ),
    )
    return list(final_state.values())


def _extract_token_usage(
    response: Any,
) -> tuple[int, int, int, int, int]:
    """Extract token usage from an ACP PromptResponse.

    Returns (input_tokens, output_tokens, cache_read, cache_write, reasoning).

    Checks two locations:
    - claude-agent-acp, codex-acp: ``response.usage`` (standard ACP field)
    - gemini-cli: ``response._meta.quota.token_count`` (non-standard)
    """
    if response is not None and response.usage is not None:
        u = response.usage
        return (
            u.input_tokens,
            u.output_tokens,
            u.cached_read_tokens or 0,
            u.cached_write_tokens or 0,
            u.thought_tokens or 0,
        )
    if response is not None and response.field_meta is not None:
        quota = response.field_meta.get("quota", {})
        tc = quota.get("token_count", {})
        return (tc.get("input_tokens", 0), tc.get("output_tokens", 0), 0, 0, 0)
    return (0, 0, 0, 0, 0)


_SYNTHETIC_MODEL_RE = re.compile(r"^<[^>]+>$")


class _TurnModelEvidence(NamedTuple):
    """Minimum model provenance retained for one active ACP prompt attempt."""

    root_models: tuple[str, ...]
    delegated_models: tuple[str, ...]
    message_start_models: tuple[str, ...]
    invalid_root_evidence: bool
    invalid_turn_binding: bool


def _concrete_provider_model(value: Any) -> str | None:
    """Return a provider model id, rejecting empty and synthetic placeholders."""
    if not isinstance(value, str):
        return None
    model = value.strip()
    if not model or _SYNTHETIC_MODEL_RE.fullmatch(model):
        return None
    return model


def _raw_message_scope(
    message: Mapping[str, Any],
) -> Literal["root", "delegated", "unknown"]:
    """Classify a Claude SDK message using provider-owned ancestry markers.

    Claude Agent SDK 0.3.280 includes ``parent_tool_use_id`` on live SDK
    messages. ``parent_agent_id`` and ``isSidechain`` are optional across
    message kinds/versions, so they refine the classification when present but
    are not required for a live root assistant message.
    """
    if "parent_tool_use_id" not in message:
        return "unknown"
    if message["parent_tool_use_id"] is not None:
        return "delegated"
    if message.get("parent_agent_id") is not None:
        return "delegated"
    if message.get("isSidechain") is True:
        return "delegated"
    if "isSidechain" in message and message["isSidechain"] not in (False, None):
        return "unknown"
    return "root"


def _raw_user_message_scope(
    message: Mapping[str, Any],
) -> Literal["prompt", "tool_result", "unknown"]:
    """Classify a Claude SDK user message without retaining its content.

    Claude Agent SDK 0.3.280 emits user-role tool-result messages between
    root assistant messages. They have their own ``uuid`` and a null
    ``parent_tool_use_id``, so UUID alone is not a prompt-turn discriminator.
    The provider-owned ``message.content`` block type distinguishes the
    submitted prompt from those tool-result messages.
    """
    inner = message.get("message")
    if not isinstance(inner, Mapping) or inner.get("role") != "user":
        return "unknown"
    content = inner.get("content")
    if isinstance(content, str):
        return "prompt" if content.strip() else "unknown"
    if not isinstance(content, list) or not content:
        return "unknown"
    block_types: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            return "unknown"
        block_type = block.get("type")
        if not isinstance(block_type, str) or not block_type:
            return "unknown"
        block_types.append(block_type)

    def is_tool_result(block_type: str) -> bool:
        return block_type == "tool_result" or block_type.endswith("_tool_result")

    if all(is_tool_result(block_type) for block_type in block_types):
        return "tool_result"
    if any(is_tool_result(block_type) for block_type in block_types):
        return "unknown"
    return "prompt"


def _estimate_cost_from_tokens(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    """Estimate cost from token counts using LiteLLM's pricing database.

    Returns 0.0 if pricing is unavailable for the model.
    """
    try:
        import litellm

        cost_map = litellm.model_cost
        info = cost_map.get(model, {})
        input_cost = info.get("input_cost_per_token", 0) or 0
        output_cost = info.get("output_cost_per_token", 0) or 0
        return input_tokens * input_cost + output_tokens * output_cost
    except Exception:
        return 0.0


def _image_url_to_acp_block(url: str) -> ImageContentBlock | None:
    """Convert an image URL (data URI or plain URL) to an ACP ImageContentBlock.

    Data URIs (``data:<mime>;base64,<data>``) are parsed directly.
    Plain URLs are passed via the ``uri`` field with a generic MIME type.
    Returns ``None`` if the URL cannot be converted.
    """
    if url.startswith("data:"):
        # Parse data URI: data:<mime>;base64,<data>
        try:
            header, data = url.split(",", 1)
            mime_type = header.split(":", 1)[1].split(";", 1)[0]
            return image_block(data=data, mime_type=mime_type)
        except (ValueError, IndexError):
            logger.warning("Failed to parse data URI for ACP image block")
            return None
    # Plain URL — pass as uri with a generic MIME type; the ACP server
    # can fetch and detect the actual type.
    return image_block(data="", mime_type="image/png", uri=url)


def _serialize_tool_content(content: list[Any] | None) -> list[dict[str, Any]] | None:
    """Serialize ACP tool call content blocks to plain dicts for JSON storage."""
    if not content:
        return None
    result = []
    for content_block in content:
        block_dict = (
            content_block.model_dump(mode="json")
            if hasattr(content_block, "model_dump")
            else content_block
        )
        if (
            isinstance(block_dict, dict)
            and block_dict.get("type") == "text"
            and isinstance(block_dict.get("text"), str)
        ):
            block_dict = {
                **block_dict,
                "text": maybe_truncate(
                    block_dict["text"], truncate_after=MAX_ACP_CONTENT_CHARS
                ),
            }
        result.append(block_dict)
    return result


# The ACP MCP server union accepted by new_session() / load_session().
_ACPMcpServer = HttpMcpServer | SseMcpServer | McpServerStdio


def _remote_mcp_headers(server: MCPServer, name: str) -> list[HttpHeader]:
    """Convert remote MCP headers/auth into ACP's header-only representation."""
    headers = [
        HttpHeader(name=header_name, value=value.get_secret_value())
        for header_name, value in (server.headers or {}).items()
    ]

    auth_headers = server.auth.to_http_headers() if server.auth is not None else {}
    if auth_headers is None:
        logger.warning(
            "ACP MCP server %r uses unsupported remote MCP auth type %r; "
            "only header-compatible auth can be forwarded",
            name,
            type(server.auth).__name__,
        )
        return headers
    headers.extend(
        HttpHeader(name=header_name, value=value)
        for header_name, value in auth_headers.items()
    )
    return headers


def _acp_mcp_capabilities(init_response: Any) -> Any:
    """Return protocol MCP capabilities, ignoring incomplete test doubles.

    ``MagicMock`` is truthy and auto-creates child attributes. Reading
    ``agent_capabilities.mcp_capabilities.http`` on an unspecified mock
    therefore looks like ``http=True`` *and* takes ``unittest.mock``'s
    process-wide lock. An ACPAgent finalizer on another thread can call
    ``connection.close()`` on a different mock and deadlock that lock,
    which is what hung ``sdk-tests`` after initialize.
    """
    caps = getattr(init_response, "agent_capabilities", None)
    if caps is None or isinstance(caps, Mock):
        return None
    mcp_caps = getattr(caps, "mcp_capabilities", None)
    if mcp_caps is None or isinstance(mcp_caps, Mock):
        return None
    return mcp_caps


def _acp_mcp_transport_enabled(mcp_capabilities: Any, name: str) -> bool:
    """Whether the server advertised a boolean MCP transport capability."""
    if mcp_capabilities is None or isinstance(mcp_capabilities, Mock):
        return False
    return getattr(mcp_capabilities, name, False) is True


def _mcp_config_to_acp_servers(
    mcp_config: dict[str, MCPServer],
    mcp_capabilities: Any,
) -> list[_ACPMcpServer]:
    """Translate OpenHands MCP servers into ACP MCP server objects."""
    http_ok = _acp_mcp_transport_enabled(mcp_capabilities, "http")
    sse_ok = _acp_mcp_transport_enabled(mcp_capabilities, "sse")
    result: list[_ACPMcpServer] = []
    for name, server in mcp_config.items():
        if not server.enabled:
            continue
        if server.command:
            env = [
                EnvVariable(name=env_name, value=value.get_secret_value())
                for env_name, value in (server.env or {}).items()
            ]
            result.append(
                McpServerStdio(
                    name=name,
                    command=server.command,
                    args=list(server.args or []),
                    env=env,
                )
            )
        elif server.url:
            headers = _remote_mcp_headers(server, name)
            is_sse = server.effective_transport == "sse"
            if not (sse_ok if is_sse else http_ok):
                logger.warning(
                    "ACP server does not advertise %s MCP support; "
                    "dropping MCP server %r (%s)",
                    "SSE" if is_sse else "HTTP",
                    name,
                    server.url,
                )
                continue
            if is_sse:
                result.append(
                    SseMcpServer(type="sse", name=name, url=server.url, headers=headers)
                )
            else:
                result.append(
                    HttpMcpServer(
                        type="http", name=name, url=server.url, headers=headers
                    )
                )
        else:
            logger.warning(
                "Skipping ACP MCP server %r: needs a 'command' (stdio) or "
                "'url' (http/sse)",
                name,
            )
    return result


def _mask_json_value(value: Any, mask: Callable[[str], str]) -> Any:
    """Recursively apply *mask* to every string leaf of a JSON-like value."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: _mask_json_value(v, mask) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_json_value(v, mask) for v in value]
    return value


async def _filter_jsonrpc_lines(source: Any, dest: Any) -> None:
    """Read lines from *source* and forward only JSON-RPC lines to *dest*.

    Some ACP servers (e.g. ``claude-code-acp`` v0.1.x) emit log messages
    like ``[ACP] ...`` to stdout alongside JSON-RPC traffic.  This coroutine
    strips those non-protocol lines so the JSON-RPC connection is not confused.
    """
    try:
        while True:
            line = await source.readline()
            if not line:
                dest.feed_eof()
                break
            # JSON-RPC messages are single-line JSON objects containing
            # "jsonrpc". Filter out multi-line pretty-printed JSON from
            # debug logs that also start with '{'.
            stripped = line.lstrip()
            if stripped.startswith(b"{") and b'"jsonrpc"' in line:
                dest.feed_data(line)
            else:
                # Non-protocol stdout is intentionally not logged: providers
                # may echo prompts, environment values, or credentials there.
                logger.debug("ACP stdout contained non-protocol output")
    except Exception:
        logger.debug("_filter_jsonrpc_lines stopped", exc_info=True)
        dest.feed_eof()


def _signal_acp_process(process: Any, method: Literal["terminate", "kill"]) -> None:
    """Send ``terminate``/``kill`` to *process*, ignoring any failure.

    Deliberately synchronous: this is the one teardown step that still works
    when the coroutine doing the cleanup is itself being cancelled.
    """
    try:
        getattr(process, method)()
    except Exception as e:
        logger.debug("Error sending %s to ACP process: %s", method, e)


async def _close_acp_connection(connection: Any) -> None:
    """Close an ACP JSON-RPC connection from the executor portal loop.

    ``connection.close()`` must run inside the scheduled coroutine, not on the
    caller thread: ``run_async(conn.close)`` eagerly invokes ``close()`` while
    building the coroutine and can leave an unawaited mock/real coroutine if
    portal scheduling fails.
    """
    close_result = connection.close()
    if inspect.isawaitable(close_result):
        await close_result


async def _await_deadline(awaitable: Any, timeout: float) -> bool:
    """Wait up to *timeout* seconds; return whether *awaitable* completed.

    Unlike ``asyncio.wait_for`` / ``anyio.fail_after``, this does not keep
    waiting for an awaitable that ignores cancellation.  The deadline always
    returns control to the caller; a still-pending waiter is cancelled and
    abandoned.
    """
    if not inspect.isawaitable(awaitable):
        return True
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        raise
    if task not in done:
        task.cancel()
        return False
    if task.cancelled():
        raise asyncio.CancelledError
    exc = task.exception()
    if exc is not None:
        raise exc
    return True


async def _await_bounded(awaitable: Any, what: str) -> bool:
    """Await *awaitable* under ``_ACP_INIT_ABORT_TIMEOUT``; report completion.

    Teardown-only helper: a hung or already-broken transport must not keep a
    failing initialization alive, so a timeout, a transport error, or a
    non-awaitable is logged and reported as "did not complete" rather than
    raised.  Cancellation still propagates so the caller can escalate.
    """
    try:
        if await _await_deadline(awaitable, _ACP_INIT_ABORT_TIMEOUT):
            return True
        logger.debug("ACP init teardown: %s did not complete (timeout)", what)
        return False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("ACP init teardown: %s did not complete (%s)", what, e)
        return False


async def _abort_partial_acp_init(
    process: Any,
    conn: Any,
    filter_task: asyncio.Task | None,
) -> None:
    """Close the connection and reap an ACP subprocess whose init failed.

    Every step is best-effort and bounded, so the teardown can neither raise
    over the original failure nor stall it: the stdout filter task is
    cancelled, the JSON-RPC connection is closed, and the subprocess is
    terminated — escalating to ``kill`` if it does not exit in time.
    """
    if filter_task is not None:
        filter_task.cancel()
    if conn is not None:
        await _await_bounded(_close_acp_connection(conn), "closing the ACP connection")
    _signal_acp_process(process, "terminate")
    if await _await_bounded(process.wait(), "waiting for the ACP process to exit"):
        return
    _signal_acp_process(process, "kill")
    await _await_bounded(process.wait(), "reaping the killed ACP process")


class _OpenHandsACPBridge:
    """Bridge between OpenHands and ACP that accumulates session updates.

    Implements the ``Client`` protocol from ``agent_client_protocol``.

    Concurrency model — ``on_event`` / ``on_token`` / ``on_activity`` are
    fired synchronously from ``session_update``, which runs on the
    ``AsyncExecutor`` portal thread.  The guarantees that keep callbacks
    serialized within a single turn rely on the combination of two things,
    not the GIL alone:

    1. ``LocalConversation.run()`` calls ``agent.step(...)`` while holding
       the reentrant ``ConversationState`` lock (a ``FIFOLock``) — see
       ``local_conversation.py`` where ``self.agent.step(...)`` sits inside
       ``with self._state:``.  The caller thread owns that lock for the
       entire duration of ``step()``, so no other thread can append to
       ``state.events`` during the turn.
    2. ``portal.call(_prompt)`` blocks the caller thread until ``prompt()``
       returns.  Live ``on_event`` calls happen on the portal thread while
       the caller thread is parked inside ``portal.call()`` still owning
       the state lock; the final ``MessageEvent`` / ``FinishAction`` run
       on the caller thread after ``prompt()`` returns.  The two phases
       never overlap in time.

    The caller's state-lock ownership is what excludes *other* threads
    (hook workers, remote-conversation push layers, visualizers spawned
    elsewhere) from racing with either phase.  The ordering between the
    two phases is what keeps a single consumer's cross-callback state
    (e.g. hook processors that read-then-write) consistent.

    Two invariants callers rely on:

    * ``on_event`` handlers MUST NOT acquire the conversation state lock
      (``with conversation.state:``).  The bridge fires them on the portal
      thread while the caller thread is parked inside ``portal.call()``
      owning that lock, and ``FIFOLock`` is thread-bound — a lock-acquire
      on the portal thread would deadlock rather than re-enter.
    * Tool-call → final-message ordering depends on the ACP server
      draining every ``session_update`` notification for a turn *before*
      the prompt response returns.  Verified against
      ``claude-agent-acp@0.29.0``; servers that interleave trailing
      ``ToolCallProgress`` after the prompt response would invert the
      order a consumer sees, and dedupe-by-id+"last-seen wins" would
      treat the post-message event as authoritative.
    """

    def __init__(self, permission_policy: ACPPermissionPolicy = "writable") -> None:
        self.permission_policy: ACPPermissionPolicy = normalize_acp_permission_policy(
            permission_policy
        )
        self.accumulated_text: list[str] = []
        self.accumulated_thoughts: list[str] = []
        self.accumulated_tool_calls: list[dict[str, Any]] = []
        self.trace = ACPTurnTrace(acp_server=None, model_id=None)
        self.on_token: Any = None  # ConversationTokenCallbackType | None
        # Secret masker — set per turn by ACPAgent to
        # ``state.secret_registry.mask_secrets_in_output``. Applied to streamed
        # text chunks and tool-call raw_input/raw_output/content before they
        # reach ``on_token`` / ``on_event`` so a subprocess that echoes an
        # injected credential never lands in the (persisted, network-relayed)
        # event stream in cleartext. ``None`` ⇒ no-op (bridge used standalone).
        self.mask: Callable[[str], str] | None = None
        self.before_mask: Callable[[], None] | None = None
        self._masking_error: CredentialBindingError | None = None
        # Live event sink — fired from session_update as ACP tool-call
        # updates arrive, so the event stream reflects real subprocess
        # progress instead of a single end-of-turn burst. Set by
        # ACPAgent.step() for the duration of one prompt() round-trip.
        self.on_event: ConversationCallbackType | None = None
        # Activity heartbeat — called (throttled) during session_update to
        # signal that the ACP subprocess is still actively working.  Set by
        # ACPAgent.step() to keep the agent-server's idle timer alive.
        self.on_activity: Any = None  # Callable[[], None] | None
        self._last_activity_signal: float = float("-inf")
        # Monotonic timestamp of the most recent ``session_update``. Unlike the
        # throttled ``_last_activity_signal``, updated on *every* update so the
        # prompt idle-timeout watchdog sees real progress. Armed per turn via
        # ``arm_activity_clock``.
        self._last_activity_monotonic: float = float("-inf")
        # Telemetry state from UsageUpdate (persists across turns)
        self._last_cost: float = 0.0  # last cumulative cost seen
        self._last_cost_by_session: dict[str, float] = {}
        self._context_window: int = 0  # last context window seen
        self._context_window_by_session: dict[str, int] = {}
        # Per-turn synchronization for UsageUpdate notifications.
        self._turn_usage_updates: dict[str, Any] = {}
        self._usage_received: dict[str, asyncio.Event] = {}
        # Last complete session config option state observed per session id.
        # Keyed by session so an update for a different session can never
        # satisfy the verification of this one.
        self._config_options_by_session: dict[str, list[SessionConfigOption]] = {}
        self._current_mode_by_session: dict[str, str] = {}
        self._current_mode_generation_by_session: dict[str, int] = {}
        # Fork session state for ask_agent() — guarded by _fork_lock to
        # prevent concurrent ask_agent() calls from colliding.
        self._fork_lock = threading.Lock()
        self._fork_session_id: str | None = None
        self._fork_accumulated_text: list[str] = []
        # Exact-model provenance is deliberately separate from usage/cost
        # accounting. Only the active prompt attempt may populate it, and only
        # the minimal provider-owned model fields are retained.
        self._model_evidence_session_id: str | None = None
        self._model_evidence_root_models: list[str] = []
        self._model_evidence_delegated_models: list[str] = []
        self._model_evidence_message_start_models: list[str] = []
        self._model_evidence_invalid_root = False
        self._model_evidence_user_message_uuid: str | None = None
        self._model_evidence_invalid_turn_binding = False
        self._completed_model_evidence: _TurnModelEvidence | None = None
        self._model_evidence_lock = threading.Lock()

    def reset(self) -> None:
        self.accumulated_text.clear()
        self.accumulated_thoughts.clear()
        self.accumulated_tool_calls.clear()
        self.on_token = None
        self.on_event = None
        self.on_activity = None
        self._turn_usage_updates.clear()
        self._usage_received.clear()
        self._masking_error = None
        self.discard_turn_model_evidence()
        # Note: telemetry state (_last_cost, _context_window, _last_activity_signal,
        # etc.) is intentionally NOT cleared — it accumulates across turns.
        # Session config option state is likewise session-scoped, not per-turn.

    def begin_turn_model_evidence(self, session_id: str) -> None:
        """Start a fresh provenance scope for one prompt attempt."""
        self.discard_turn_model_evidence()
        with self._model_evidence_lock:
            self._model_evidence_session_id = session_id

    def finish_turn_model_evidence(self, session_id: str) -> None:
        """Close the active scope before usage waiters or late events run."""
        with self._model_evidence_lock:
            if self._model_evidence_session_id != session_id:
                return
            self._completed_model_evidence = _TurnModelEvidence(
                root_models=tuple(self._model_evidence_root_models),
                delegated_models=tuple(self._model_evidence_delegated_models),
                message_start_models=tuple(self._model_evidence_message_start_models),
                invalid_root_evidence=self._model_evidence_invalid_root,
                invalid_turn_binding=self._model_evidence_invalid_turn_binding,
            )
            self._model_evidence_session_id = None

    def pop_turn_model_evidence(self) -> _TurnModelEvidence | None:
        """Consume the completed evidence for exactly one prompt attempt."""
        with self._model_evidence_lock:
            evidence = self._completed_model_evidence
            self._completed_model_evidence = None
            return evidence

    def discard_turn_model_evidence(self) -> None:
        """Invalidate active and completed evidence, including on cancellation."""
        with self._model_evidence_lock:
            self._model_evidence_session_id = None
            self._model_evidence_root_models.clear()
            self._model_evidence_delegated_models.clear()
            self._model_evidence_message_start_models.clear()
            self._model_evidence_invalid_root = False
            self._model_evidence_user_message_uuid = None
            self._model_evidence_invalid_turn_binding = False
            self._completed_model_evidence = None

    def _record_raw_sdk_message(self, params: Mapping[str, Any]) -> None:
        with self._model_evidence_lock:
            self._record_raw_sdk_message_unlocked(params)

    def _root_message_belongs_to_turn(self, message: Mapping[str, Any]) -> bool:
        """Require the provider's current prompt UUID before accepting root data.

        Claude omits ``user_message_uuid`` on later root messages in a tool
        loop.  That omission is safe only after a prior root assistant or
        ``message_start`` frame has explicitly matched the current prompt.
        The first root frame must always carry the matching UUID.
        """
        prompt_uuid = self._model_evidence_user_message_uuid
        if prompt_uuid is None:
            self._model_evidence_invalid_turn_binding = True
            return False
        message_uuid = message.get("user_message_uuid")
        if message_uuid is None:
            if not (
                self._model_evidence_root_models
                or self._model_evidence_message_start_models
            ):
                self._model_evidence_invalid_turn_binding = True
                return False
            return True
        if message_uuid != prompt_uuid:
            self._model_evidence_invalid_turn_binding = True
            return False
        return True

    def _record_raw_sdk_message_unlocked(self, params: Mapping[str, Any]) -> None:
        """Extract provenance from one raw Claude SDK message without retaining it."""
        active_session_id = self._model_evidence_session_id
        if active_session_id is None or params.get("sessionId") != active_session_id:
            return
        sdk_message = params.get("message")
        if not isinstance(sdk_message, Mapping):
            return

        message_type = sdk_message.get("type")
        if message_type == "user":
            user_scope = _raw_user_message_scope(sdk_message)
            if user_scope == "tool_result":
                return
            if user_scope != "prompt":
                self._model_evidence_invalid_turn_binding = True
                return
            user_uuid = sdk_message.get("uuid")
            if not isinstance(user_uuid, str) or not user_uuid.strip():
                self._model_evidence_invalid_turn_binding = True
            elif self._model_evidence_user_message_uuid is None:
                self._model_evidence_user_message_uuid = user_uuid
            elif user_uuid != self._model_evidence_user_message_uuid:
                self._model_evidence_invalid_turn_binding = True
            return
        if message_type == "assistant":
            scope = _raw_message_scope(sdk_message)
            if scope == "delegated":
                inner = sdk_message.get("message")
                if isinstance(inner, Mapping):
                    model = _concrete_provider_model(inner.get("model"))
                    if model is not None:
                        self._model_evidence_delegated_models.append(model)
                return
            if scope != "root":
                self._model_evidence_invalid_root = True
                return
            if not self._root_message_belongs_to_turn(sdk_message):
                return
            inner = sdk_message.get("message")
            model = (
                _concrete_provider_model(inner.get("model"))
                if isinstance(inner, Mapping)
                else None
            )
            if model is None:
                self._model_evidence_invalid_root = True
            else:
                self._model_evidence_root_models.append(model)
            return

        if message_type != "stream_event":
            return
        event = sdk_message.get("event")
        if not isinstance(event, Mapping) or event.get("type") != "message_start":
            return
        scope = _raw_message_scope(sdk_message)
        if scope == "delegated":
            message_start = event.get("message")
            if isinstance(message_start, Mapping):
                model = _concrete_provider_model(message_start.get("model"))
                if model is not None:
                    self._model_evidence_delegated_models.append(model)
            return
        if scope != "root":
            # The consolidated assistant message remains the required
            # authority. A stream frame without a root discriminator is not
            # allowed to become authority and is ignored as a cross-check.
            return
        if not self._root_message_belongs_to_turn(sdk_message):
            return
        message_start = event.get("message")
        model = (
            _concrete_provider_model(message_start.get("model"))
            if isinstance(message_start, Mapping)
            else None
        )
        if model is None:
            self._model_evidence_invalid_root = True
        else:
            self._model_evidence_message_start_models.append(model)

    def arm_activity_clock(self) -> None:
        """Mark "now" as the last activity for the idle-timeout watchdog."""
        self._last_activity_monotonic = time.monotonic()

    def seconds_since_last_activity(self) -> float:
        """Seconds since the last ``session_update`` (or ``arm_activity_clock``)."""
        return time.monotonic() - self._last_activity_monotonic

    def prepare_usage_sync(self, session_id: str) -> asyncio.Event:
        """Prepare per-turn UsageUpdate synchronization for a session."""
        event = asyncio.Event()
        self._usage_received[session_id] = event
        self._turn_usage_updates.pop(session_id, None)
        return event

    def get_turn_usage_update(self, session_id: str) -> Any:
        """Return the latest UsageUpdate observed for the current turn."""
        return self._turn_usage_updates.get(session_id)

    def pop_turn_usage_update(self, session_id: str) -> Any:
        """Consume per-turn UsageUpdate synchronization state for a session."""
        self._usage_received.pop(session_id, None)
        return self._turn_usage_updates.pop(session_id, None)

    def get_config_options(self, session_id: str) -> list[SessionConfigOption] | None:
        """Return the last complete config option state seen for *session_id*."""
        return self._config_options_by_session.get(session_id)

    def record_config_options(
        self, session_id: str, options: list[SessionConfigOption]
    ) -> None:
        """Record a complete config state returned by a direct request."""
        self._config_options_by_session[session_id] = list(options)

    def get_current_mode_id(self, session_id: str) -> str | None:
        """Return the last confirmed session mode id for *session_id*."""
        return self._current_mode_by_session.get(session_id)

    def get_current_mode_generation(self, session_id: str) -> int:
        """Return how many CurrentModeUpdate notifications this session has seen."""
        return self._current_mode_generation_by_session.get(session_id, 0)

    def _mask_value(self, value: Any) -> Any:
        if self.mask is None:
            return value
        try:
            if self.before_mask is not None:
                self.before_mask()
            return _mask_json_value(value, self.mask)
        except CredentialBindingError as exc:
            if self._masking_error is None:
                self._masking_error = exc
            raise
        except Exception:
            logger.debug("secret masking failed", exc_info=True)
            return value

    def _mask_tool_call_entry(self, entry: dict[str, Any]) -> None:
        for key in ("title", "raw_input", "raw_output", "content"):
            if entry.get(key) is not None:
                entry[key] = self._mask_value(entry[key])

    def _raise_masking_error(self) -> None:
        if self._masking_error is not None:
            raise self._masking_error

    # -- Client protocol methods ------------------------------------------

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        logger.debug("ACP session_update: type=%s", type(update).__name__)
        self._last_activity_monotonic = time.monotonic()

        # Route fork session updates to the fork accumulator
        if self._fork_session_id is not None and session_id == self._fork_session_id:
            if isinstance(update, AgentMessageChunk):
                if isinstance(update.content, TextContentBlock):
                    self._fork_accumulated_text.append(
                        self._mask_value(update.content.text)
                    )
            return

        if isinstance(update, AgentMessageChunk):
            if isinstance(update.content, TextContentBlock):
                text = self._mask_value(update.content.text)
                self.accumulated_text.append(text)
                if self.on_token is not None:
                    try:
                        self.on_token(text)
                    except Exception:
                        logger.debug("on_token callback failed", exc_info=True)
            self._maybe_signal_activity()
        elif isinstance(update, AgentThoughtChunk):
            if isinstance(update.content, TextContentBlock):
                self.accumulated_thoughts.append(self._mask_value(update.content.text))
        elif isinstance(update, UsageUpdate):
            # Store the update for step()/ask_agent() to process in one place.
            self._context_window = update.size
            self._context_window_by_session[session_id] = update.size
            self._turn_usage_updates[session_id] = update
            event = self._usage_received.get(session_id)
            if event is not None:
                event.set()
        elif isinstance(update, ConfigOptionUpdate):
            # The Agent publishes the full option set on every change; record
            # it per session so initialization can verify requested options
            # even when the server answers set_config_option asynchronously.
            self._config_options_by_session[session_id] = list(update.config_options)
        elif isinstance(update, CurrentModeUpdate):
            current_mode_id = update.current_mode_id
            if isinstance(current_mode_id, str) and current_mode_id:
                self._current_mode_by_session[session_id] = current_mode_id
                self._current_mode_generation_by_session[session_id] = (
                    self._current_mode_generation_by_session.get(session_id, 0) + 1
                )
        elif isinstance(update, ToolCallStart):
            entry = {
                "tool_call_id": update.tool_call_id,
                "title": update.title,
                "tool_kind": update.kind,
                "status": update.status,
                "raw_input": update.raw_input,
                "raw_output": update.raw_output,
                "content": _serialize_tool_content(update.content),
            }
            self._mask_tool_call_entry(entry)
            self.accumulated_tool_calls.append(entry)
            self.trace.tool_started(entry)
            if entry.get("status") in _TERMINAL_TOOL_CALL_STATUSES:
                self.trace.tool_finished(entry)
            logger.debug("ACP tool call start: %s", update.tool_call_id)
            self._emit_tool_call_event(entry)
            self._maybe_signal_activity()
        elif isinstance(update, ToolCallProgress):
            target: dict[str, Any] | None = None
            prev_status: str | None = None
            for index, tc in enumerate(self.accumulated_tool_calls):
                if tc["tool_call_id"] == update.tool_call_id:
                    prev_status = tc.get("status")
                    updated = dict(tc)
                    if update.title is not None:
                        updated["title"] = update.title
                    if update.kind is not None:
                        updated["tool_kind"] = update.kind
                    if update.status is not None:
                        updated["status"] = update.status
                    if update.raw_input is not None:
                        updated["raw_input"] = update.raw_input
                    if update.raw_output is not None:
                        updated["raw_output"] = update.raw_output
                    if update.content is not None:
                        updated["content"] = _serialize_tool_content(update.content)
                    self._mask_tool_call_entry(updated)
                    self.accumulated_tool_calls[index] = updated
                    target = updated
                    break
            logger.debug("ACP tool call progress: %s", update.tool_call_id)
            became_terminal = (
                target is not None
                and target.get("status") in _TERMINAL_TOOL_CALL_STATUSES
                and prev_status not in _TERMINAL_TOOL_CALL_STATUSES
            )
            if target is not None and became_terminal:
                self.trace.tool_finished(target)
                self._emit_tool_call_event(target)
            self._maybe_signal_activity()
        else:
            logger.debug("ACP session update: %s", type(update).__name__)

    def _emit_tool_call_event(self, tc: dict[str, Any]) -> None:
        """Emit an ACPToolCallEvent reflecting the current state of ``tc``.

        Called from ``session_update`` on each ``ToolCallStart`` /
        ``ToolCallProgress`` so downstream consumers see tool cards appear
        and update as the subprocess runs.  The same ``tool_call_id`` is
        reused on every emission — consumers should dedupe by id and treat
        the last-seen event as authoritative.
        """
        if self.on_event is None:
            return
        try:
            raw_output = tc.get("raw_output")
            if isinstance(raw_output, str):
                raw_output = maybe_truncate(
                    raw_output, truncate_after=MAX_ACP_CONTENT_CHARS
                )
            event = ACPToolCallEvent(
                tool_call_id=tc["tool_call_id"],
                title=tc["title"],
                status=tc.get("status"),
                tool_kind=tc.get("tool_kind"),
                raw_input=tc.get("raw_input"),
                raw_output=raw_output,
                content=tc.get("content"),
                is_error=tc.get("status") == "failed",
            )
            self.on_event(event)
        except Exception:
            logger.debug("on_event callback failed", exc_info=True)

    def _maybe_signal_activity(self) -> None:
        """Signal activity to the agent-server's idle tracker (throttled).

        During conn.prompt(), ACP tool calls run inside the subprocess and
        never hit the agent-server's HTTP endpoints.  Without this heartbeat
        the server's idle_time grows unboundedly and the runtime-api kills
        the pod (default idle threshold ~20 min).

        Throttled to at most once per _ACTIVITY_SIGNAL_INTERVAL seconds to
        avoid excessive overhead on chatty ACP servers.
        """
        if self.on_activity is None:
            return
        now = time.monotonic()
        if now - self._last_activity_signal >= _ACTIVITY_SIGNAL_INTERVAL:
            self._last_activity_signal = now
            try:
                self.on_activity()
            except Exception:
                logger.debug("on_activity callback failed", exc_info=True)

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,  # noqa: ARG002
        tool_call: Any,
        **kwargs: Any,  # noqa: ARG002
    ) -> Any:
        """Apply the conversation-local ACP permission policy."""
        return resolve_permission_response(
            self.permission_policy,
            options,
            tool_call,
        )

    # fs/terminal methods — raise NotImplementedError; ACP server handles its own
    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> None:
        raise NotImplementedError("ACP server handles file operations")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError("ACP server handles file operations")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: Any = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError("ACP server handles terminal operations")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> Any:
        raise NotImplementedError("ACP server handles terminal operations")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> None:
        raise NotImplementedError("ACP server handles terminal operations")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> Any:
        raise NotImplementedError("ACP server handles terminal operations")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> None:
        raise NotImplementedError("ACP server handles terminal operations")

    async def ext_method(
        self,
        method: str,  # noqa: ARG002
        params: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        return {}

    async def ext_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        # The Python ACP client removes one leading underscore from extension
        # method names, so 0.81.0 arrives here as ``claude/sdkMessage``. Keep
        # the underscored spelling for direct/provider-shaped clients too.
        if method not in ("claude/sdkMessage", "_claude/sdkMessage"):
            return
        if isinstance(params, Mapping):
            self._record_raw_sdk_message(params)

    def on_connect(self, conn: Any) -> None:  # noqa: ARG002
        pass


# ---------------------------------------------------------------------------
# ACPAgent
# ---------------------------------------------------------------------------


class ACPAgent(AgentBase):
    """Agent that delegates to an ACP-compatible subprocess server."""

    # Override required fields with ACP-appropriate defaults
    llm: LLM = Field(default_factory=_make_dummy_llm)
    tools: list[Tool] = Field(default_factory=list)
    include_default_tools: list[str] = Field(default_factory=list)

    # ACP-specific configuration
    acp_command: list[str] = Field(
        ...,
        description=(
            "Command to start the ACP server, e.g."
            " ['npx', '-y', '@agentclientprotocol/claude-agent-acp']"
        ),
    )
    acp_server: str | None = Field(
        default=None,
        description=(
            "Provider registry key identifying which ACP CLI this agent runs "
            "('claude-code', 'codex', 'gemini-cli', or 'custom'); None when the "
            "agent is built directly rather than via ACPAgentSettings."
        ),
    )
    acp_args: list[str] = Field(
        default_factory=list,
        description="Additional arguments for the ACP server command",
    )
    acp_env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables for the ACP server process",
    )

    @field_serializer("acp_env", when_used="always")
    def _serialize_acp_env(self, value: dict[str, str], info):
        """Mask ``acp_env`` values via :func:`serialize_secret`."""
        return {k: serialize_secret(SecretStr(v), info) for k, v in value.items()}

    acp_session_mode: str | None = Field(
        default=None,
        description=(
            "Session mode ID to set after creating a session. "
            "If None (default), auto-detected from the ACP server type: "
            "'bypassPermissions' for claude-agent-acp, 'full-access' for codex-acp."
        ),
    )
    acp_prompt_timeout: float = Field(
        default=1800.0,
        description=(
            "Timeout in seconds for a single ACP prompt() call. "
            "Prevents indefinite hangs when the ACP server fails to respond."
        ),
    )
    acp_startup_timeout: float = Field(
        default=90.0,
        description=(
            "Timeout in seconds for ACP server startup: spawning the "
            "subprocess, the initialize/authenticate handshake, and "
            "new_session()/load_session()."
        ),
    )
    acp_model: str | None = Field(
        default=None,
        description=(
            "Model for the ACP server to use (e.g. 'claude-opus-4-6' or "
            "'gpt-5.4'). For Claude ACP, passed via session _meta. For Codex "
            "ACP, applied via the protocol-level set_session_model call. "
            "If None, the server picks its default."
        ),
    )
    structured_output: StructuredOutputConfig | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Provider-neutral structured-output configuration. The current "
            "implementation maps JSON Schema to Claude ACP session metadata."
        ),
    )
    acp_client_capabilities: ClientCapabilities | None = Field(
        default=None,
        description=(
            "Explicit ACP client capabilities to send with initialize(). "
            "When None (default) the argument is omitted and the ACP library's "
            "own default capabilities apply. Capability flags that are not part "
            "of the ACP schema travel through the protocol's extensibility "
            "field, e.g. ClientCapabilities(field_meta="
            "{'parameterizedModelPicker': True})."
        ),
    )
    acp_config_options: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Session configuration options to apply before the first prompt, "
            "as ACP config option id -> requested value (e.g. "
            "{'effort': 'medium', 'fast': 'false'}). Applied in order and "
            "verified against the option state the server reports, on both new "
            "and resumed sessions. Initialization fails if a requested option "
            "is missing or the session does not report the requested value."
        ),
    )
    acp_file_secrets: list[ACPFileSecretSpec] = Field(
        default_factory=lambda: list(default_acp_file_secrets()),
        description=(
            "Reserved 'file-content' credential secrets to materialise to disk "
            "before launching the subprocess."
        ),
    )
    acp_isolate_data_dir: bool = Field(
        default=False,
        description=(
            "Give the ACP subprocess a per-conversation CLI data/config root "
            "instead of the shared user HOME."
        ),
    )
    acp_permission_policy: ACPPermissionPolicy = Field(
        default="writable",
        description=(
            "Conversation-local permission policy for ACP sessions. "
            "``writable`` preserves the default auto-approve behavior. "
            "``read_only`` refuses unverified provider write paths and, for "
            "Claude Code, Codex, and Cursor, uses a provider-specific "
            "read-only session mode plus denied request_permission callbacks. "
            "Unsupported providers "
            "and unknown policy values fail closed."
        ),
    )

    @field_validator("acp_permission_policy")
    @classmethod
    def _validate_acp_permission_policy(cls, value: str) -> ACPPermissionPolicy:
        return normalize_acp_permission_policy(value)

    @field_validator("agent_context")
    @classmethod
    def _drop_project_skills(cls, value: AgentContext | None) -> AgentContext | None:
        """Clear ``load_project_skills`` — ACP CLIs read the repo themselves."""
        if value is None or not value.load_project_skills:
            return value
        return value.model_copy(update={"load_project_skills": False})

    def model_post_init(self, __context: object) -> None:
        super().model_post_init(__context)
        self._requested_model_id = self.acp_model
        self._runtime_model_override_active = False
        if self.structured_output is not None:
            configured_provider = resolve_effective_acp_provider_key(
                acp_server=self.acp_server,
                command=self.acp_command,
            )
            if configured_provider != "claude-code":
                raise ValueError(
                    "structured_output is unsupported for ACP provider "
                    f"{configured_provider!r}; only Claude ACP is qualified"
                )
            validate_claude_structured_output_schema(self.structured_output.schema)
        # Propagate the actual model name to the sentinel LLM and its
        # metrics so that logs, serialized state, and cost/token entries
        # show the real model instead of the "acp-managed" placeholder.
        # The ACP-sentinel marker lives on ``llm.usage_id`` and is
        # independent of the model name.
        if self.acp_model:
            self.llm.model = self.acp_model
            self.llm.metrics.model_name = self.acp_model
            if self.llm.metrics.accumulated_token_usage is not None:
                self.llm.metrics.accumulated_token_usage.model = self.acp_model

    # Private runtime state
    _executor: Any = PrivateAttr(default=None)
    _conn: Any = PrivateAttr(default=None)  # ClientSideConnection
    _session_id: str | None = PrivateAttr(default=None)
    _process: Any = PrivateAttr(default=None)  # asyncio subprocess
    _client: Any = PrivateAttr(default=None)  # _OpenHandsACPBridge
    _filtered_reader: Any = PrivateAttr(default=None)  # StreamReader
    _stdout_filter_task: Any = PrivateAttr(default=None)  # asyncio.Task
    _stderr_capture_task: Any = PrivateAttr(default=None)  # asyncio.Task
    _closed: bool = PrivateAttr(default=False)
    _working_dir: str = PrivateAttr(default="")
    _agent_name: str = PrivateAttr(
        default=""
    )  # ACP server name from InitializeResponse
    _agent_version: str = PrivateAttr(
        default=""
    )  # ACP server version from InitializeResponse
    _model_via_config_option: bool = PrivateAttr(default=False)
    _requested_model_id: str | None = PrivateAttr(default=None)
    _runtime_model_override_active: bool = PrivateAttr(default=False)
    _current_model_id: str | None = PrivateAttr(default=None)
    _session_mode_id: str | None = PrivateAttr(default=None)
    _served_model_id: str | None = PrivateAttr(default=None)
    _post_turn_model_verification_required: bool = PrivateAttr(default=False)
    _available_models: list[ACPModelInfo] | None = PrivateAttr(default=None)
    _model_override_applied: bool = PrivateAttr(default=False)
    _resumed_existing_session: bool = PrivateAttr(default=False)
    _restart_session_on_next_turn: bool = PrivateAttr(default=False)
    _file_credential_lifecycles: dict[str, ACPFileCredentialLifecycle] = PrivateAttr(
        default_factory=dict
    )
    _file_credential_bindings: dict[str, VersionedCredentialBinding] = PrivateAttr(
        default_factory=dict
    )
    _file_credential_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _file_credential_close_lock: threading.Lock = PrivateAttr(
        default_factory=threading.Lock
    )
    _replace_file_credentials_on_next_materialisation: set[str] = PrivateAttr(
        default_factory=set
    )
    _claude_config_runtime_dir: Path | None = PrivateAttr(default=None)
    _claude_oauth_source_digest: str | None = PrivateAttr(default=None)
    _secret_registry_for_masking: SecretRegistry | None = PrivateAttr(default=None)
    _startup_phase: str = PrivateAttr(default="not-started")
    _startup_rpc: str | None = PrivateAttr(default=None)
    _startup_stage: str = PrivateAttr(default="not-started")
    _startup_stderr_tail: str = PrivateAttr(default="")
    _startup_exit_code: int | None = PrivateAttr(default=None)
    _startup_provider_key: str | None = PrivateAttr(default=None)
    _startup_package_version: str | None = PrivateAttr(default=None)
    _startup_adapter_name: str | None = PrivateAttr(default=None)
    _startup_adapter_version: str | None = PrivateAttr(default=None)
    _startup_runtime_version: str | None = PrivateAttr(default=None)
    _atexit_callback: Callable[[], None] | None = PrivateAttr(default=None)
    # Suffix rendered once at session start from agent_context + secret_registry.
    _suffix_install_state: str = PrivateAttr(default="unused")
    _installed_suffix: str | None = PrivateAttr(default=None)
    # Callback to signal that the ACP subprocess is actively working.
    # Injected by the agent-server to call update_last_execution_time().
    _on_activity: Any = PrivateAttr(default=None)  # Callable[[], None] | None

    # -- Helpers -----------------------------------------------------------

    def _record_usage(
        self,
        response: PromptResponse | None,
        session_id: str,
        elapsed: float | None = None,
        usage_update: UsageUpdate | None = None,
    ) -> None:
        """Record cost, token usage, latency, and notify stats callback once.

        Args:
            response: The ACP PromptResponse (may carry a ``usage`` field).
            session_id: Session identifier used as the response_id for metrics.
            elapsed: Wall-clock seconds for this prompt round-trip (optional).
            usage_update: The synchronized ACP UsageUpdate for this turn, if any.
        """
        # -- Cost recording ---------------------------------------------------
        # claude-agent-acp, codex-acp: report cost via UsageUpdate notification
        # gemini-cli: does not send UsageUpdate (cost derived from tokens below)
        cost_recorded = False
        if usage_update is not None and usage_update.cost is not None:
            last_cost = self._client._last_cost_by_session.get(session_id, 0.0)
            delta = usage_update.cost.amount - last_cost
            if delta > 0:
                self.llm.metrics.add_cost(delta)
                cost_recorded = True
            self._client._last_cost_by_session[session_id] = usage_update.cost.amount
            self._client._last_cost = usage_update.cost.amount

        # -- Token usage recording --------------------------------------------
        input_tokens, output_tokens, cache_read, cache_write, reasoning = (
            _extract_token_usage(response)
        )
        if input_tokens or output_tokens:
            self.llm.metrics.add_token_usage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                reasoning_tokens=reasoning,
                context_window=self._client._context_window_by_session.get(
                    session_id, self._client._context_window
                ),
                response_id=session_id,
            )

        # -- Cost derivation from tokens --------------------------------------
        # gemini-cli: no UsageUpdate cost, so derive from token counts using
        # LiteLLM's model pricing database (same source the proxy uses).
        # claude-agent-acp, codex-acp: skipped since cost_recorded is True.
        requested_model = self._requested_model_for_turn()
        if not cost_recorded and (input_tokens or output_tokens) and requested_model:
            cost = _estimate_cost_from_tokens(
                requested_model, input_tokens, output_tokens
            )
            if cost > 0:
                self.llm.metrics.add_cost(cost)

        if not cost_recorded and not input_tokens and not output_tokens:
            # gemini-cli currently returns response.usage=None and
            # response.field_meta=None (ACP SDK strips _meta during
            # serialization). Tracked in google-gemini/gemini-cli#24280.
            logger.debug(
                "No usage data from ACP server %s — token/cost tracking unavailable",
                self._agent_name or "unknown",
            )

        if elapsed is not None:
            self.llm.metrics.add_response_latency(elapsed, session_id)

        if self.llm.telemetry._stats_update_callback is not None:
            try:
                self.llm.telemetry._stats_update_callback()
            except Exception:
                logger.debug("Stats update callback failed", exc_info=True)

    # -- Capability helpers ------------------------------------------------

    @property
    def supports_openhands_tools(self) -> bool:
        """``False`` — the ACP server manages its own toolset."""
        return False

    @property
    def supports_openhands_mcp(self) -> bool:
        """``False`` — OpenHands does not create in-process MCP tools here.

        ACP agents still honor ``mcp_config`` by forwarding configured servers
        to the ACP subprocess at session creation time.
        """
        return False

    @property
    def supports_condenser(self) -> bool:
        """``False`` — the ACP server manages its own context window."""
        return False

    @property
    def agent_kind(self) -> Literal["acp"]:
        """ACP agents have ``agent_kind == "acp"``."""
        return "acp"

    # -- ACP-specific runtime properties -----------------------------------

    @property
    def agent_name(self) -> str:
        """Name of the ACP server (from InitializeResponse.agent_info)."""
        return self._agent_name

    @property
    def agent_version(self) -> str:
        """Version of the ACP server (from InitializeResponse.agent_info)."""
        return self._agent_version

    @property
    def current_model_id(self) -> str | None:
        """The ACP session's routing/model selector.

        For the qualified Claude exact-model path this is deliberately not the
        execution authority: only the completed turn's provider-owned root
        assistant SDK messages can populate the served identity used by the
        success gate.
        """
        return self._current_model_id

    def _requested_model_for_turn(self) -> str | None:
        """Return the caller's model intent for the current runtime binding."""
        if not self._runtime_model_override_active and not self.has_live_acp_session:
            return self.acp_model
        return self._requested_model_id or self.acp_model

    @property
    def available_models(self) -> list[ACPModelInfo]:
        """Models the ACP server offers for this session."""
        return list(self._available_models or [])

    @property
    def supports_runtime_model_switch(self) -> bool:
        """Whether a live, mid-conversation model switch will be attempted."""
        if self._session_id is None:
            return False
        provider = detect_acp_provider_by_agent_name(self._agent_name)
        return provider is not None and provider.supports_runtime_model_switch

    @property
    def has_live_acp_session(self) -> bool:
        """Whether a live ACP session exists to act on right now."""
        return (
            self._conn is not None
            and self._session_id is not None
            and self._executor is not None
        )

    def get_all_llms(self) -> Generator[LLM]:
        yield self.llm

    # -- File credential lifecycle -----------------------------------------

    def activate_file_credential_binding(
        self,
        secret_name: str,
        binding: VersionedCredentialBinding,
    ) -> None:
        with self._file_credential_lock:
            if self._initialized or self._closed:
                raise RuntimeError(
                    "ACP credential bindings must be activated before use"
                )
            self._file_credential_bindings[secret_name] = binding

    def restart_for_updated_credentials(self, secret_names: Collection[str]) -> None:
        configured = {spec.secret_name for spec in self.acp_file_secrets}
        configured.add(CLAUDE_CREDENTIALS_SECRET_NAME)
        with self._file_credential_lock:
            self._replace_file_credentials_on_next_materialisation.update(
                configured.intersection(secret_names)
            )
        if self._initialized:
            self._restart_session_on_next_turn = True

    def _has_runtime_resources(self) -> bool:
        return (
            self._executor is not None
            or self._process is not None
            or self._conn is not None
            or bool(self._file_credential_lifecycles)
            or self._claude_config_runtime_dir is not None
        )

    def _register_atexit_cleanup(self, *, replace: bool = False) -> None:
        if self._atexit_callback is not None:
            if not replace:
                return
            atexit.unregister(self._atexit_callback)
        agent_ref = weakref.ref(self)

        def cleanup() -> None:
            agent = agent_ref()
            if agent is not None:
                agent._finalize()

        self._atexit_callback = cleanup
        atexit.register(cleanup)

    def _unregister_atexit_cleanup(self) -> None:
        callback = self._atexit_callback
        if callback is not None:
            atexit.unregister(callback)
            self._atexit_callback = None

    def _present_file_secret_names(self, state: ConversationState) -> set[str]:
        configured = {spec.secret_name for spec in self.acp_file_secrets}
        if not configured:
            return set()
        return set(state.secret_registry.secret_sources) & configured

    def _acp_file_secret_dir(self, state: ConversationState, subdir: str) -> Path:
        if state.persistence_dir:
            root = Path(state.persistence_dir) / "acp" / subdir
        else:
            root = Path(state.workspace.working_dir) / ".openhands" / "acp" / subdir
        return Path(os.path.abspath(root))

    def _ensure_claude_config_runtime_dir(self) -> Path:
        existing = self._claude_config_runtime_dir
        if existing is not None and existing.is_dir():
            return existing
        runtime_dir = create_claude_config_runtime_dir()
        self._claude_config_runtime_dir = runtime_dir
        return runtime_dir

    def _remove_durable_claude_oauth_credentials(
        self, state: ConversationState
    ) -> None:
        durable_dir = self._acp_file_secret_dir(state, "claude-code")
        unlink_claude_oauth_credentials(durable_dir)

    def _cleanup_claude_config_runtime(self, *, discard: bool) -> None:
        runtime_dir = self._claude_config_runtime_dir
        if runtime_dir is None:
            return
        if not discard:
            return
        discard_claude_oauth_runtime_dir(runtime_dir)
        self._claude_config_runtime_dir = None
        self._claude_oauth_source_digest = None

    def _explicit_claude_oauth_credentials(
        self, state: ConversationState
    ) -> str | None:
        registry_value = state.secret_registry.get_secret_value(
            CLAUDE_CREDENTIALS_SECRET_NAME
        )
        if is_valid_claude_oauth_credentials(registry_value):
            return str(registry_value)
        if self.agent_context and self.agent_context.secrets:
            secret = self.agent_context.secrets.get(CLAUDE_CREDENTIALS_SECRET_NAME)
            if secret is None:
                return None
            value = (
                secret.get_value() if isinstance(secret, SecretSource) else str(secret)
            )
            if is_valid_claude_oauth_credentials(value):
                return value
        return None

    def _isolate_acp_data_dir(
        self, state: ConversationState, env: dict[str, str]
    ) -> bool:
        provider = detect_acp_provider_by_command(self.acp_command)
        if provider is None or provider.data_dir_env_var is None:
            return False
        env_var = provider.data_dir_env_var
        oauth_file_channel = False
        if provider.key == "claude-code":
            if self._file_credential_bindings.get(CLAUDE_CREDENTIALS_SECRET_NAME):
                oauth_file_channel = self._materialise_bound_claude_oauth(state, env)
                data_dir = Path(env[CLAUDE_CONFIG_DIR_ENV])
            else:
                data_dir = self._ensure_claude_config_runtime_dir()
                with self._file_credential_lock:
                    replace_existing = (
                        CLAUDE_CREDENTIALS_SECRET_NAME
                        in self._replace_file_credentials_on_next_materialisation
                    )
                try:
                    result = seed_claude_oauth_credentials(
                        data_dir,
                        state.secret_registry,
                        replace_existing=replace_existing,
                        required=self.acp_permission_policy == "read_only",
                        last_source_digest=self._claude_oauth_source_digest,
                        explicit_credentials=self._explicit_claude_oauth_credentials(
                            state
                        ),
                    )
                except BaseException:
                    self._cleanup_claude_config_runtime(discard=True)
                    raise
                self._claude_oauth_source_digest = result.source_digest
                if replace_existing:
                    with self._file_credential_lock:
                        self._replace_file_credentials_on_next_materialisation.discard(
                            CLAUDE_CREDENTIALS_SECRET_NAME
                        )
                oauth_file_channel = result.seeded
            self._remove_durable_claude_oauth_credentials(state)
        else:
            data_dir = self._acp_file_secret_dir(state, provider.key)
            data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[env_var] = str(data_dir)
        return oauth_file_channel

    def _materialise_bound_claude_oauth(
        self, state: ConversationState, env: dict[str, str]
    ) -> bool:
        """Materialise the canonical Claude OAuth binding into a private dir."""
        binding = self._file_credential_bindings.get(CLAUDE_CREDENTIALS_SECRET_NAME)
        if binding is None:
            return False
        assert self._executor is not None
        lifecycle = create_file_credential_lifecycle(
            CLAUDE_CREDENTIALS_SECRET_NAME,
            binding,
            self._executor.run_async,
        )
        if lifecycle is None:
            raise CredentialSyncError("Claude OAuth credential binding is unsupported.")
        with self._file_credential_lock:
            if self._closed:
                raise CredentialSyncError("Credential binding is closed.")
        try:
            lifecycle.materialize(state.secret_registry, env)
            self._remove_durable_claude_oauth_credentials(state)
        except BaseException:
            lifecycle.discard()
            env.pop(CLAUDE_CONFIG_DIR_ENV, None)
            raise
        with self._file_credential_lock:
            closed = self._closed
            if not closed:
                self._file_credential_lifecycles[CLAUDE_CREDENTIALS_SECRET_NAME] = (
                    lifecycle
                )
                self._replace_file_credentials_on_next_materialisation.discard(
                    CLAUDE_CREDENTIALS_SECRET_NAME
                )
        if closed:
            lifecycle.discard()
            env.pop(CLAUDE_CONFIG_DIR_ENV, None)
            raise CredentialSyncError("Credential binding is closed.")
        return True

    def _materialise_file_secrets(
        self, state: ConversationState, env: dict[str, str]
    ) -> None:
        for spec in self.acp_file_secrets:
            name = spec.secret_name
            with self._file_credential_lock:
                replace_existing = (
                    name in self._replace_file_credentials_on_next_materialisation
                )
            binding = self._file_credential_bindings.get(name)
            assert self._executor is not None
            lifecycle = create_file_credential_lifecycle(
                name,
                binding,
                self._executor.run_async,
            )
            if lifecycle is not None:
                with self._file_credential_lock:
                    if self._closed:
                        raise CredentialSyncError("Credential binding is closed.")
                try:
                    lifecycle.materialize(state.secret_registry, env)
                    durable_path = (
                        self._acp_file_secret_dir(state, spec.subdir) / spec.filename
                    )
                    try:
                        durable_path.unlink(missing_ok=True)
                    except OSError as exc:
                        raise CredentialSyncError(
                            "Durable credential copy could not be removed."
                        ) from exc
                except BaseException:
                    env.pop("CODEX_HOME", None)
                    lifecycle.discard()
                    raise
                with self._file_credential_lock:
                    closed = self._closed
                    if not closed:
                        self._file_credential_lifecycles[name] = lifecycle
                        self._replace_file_credentials_on_next_materialisation.discard(
                            name
                        )
                if closed:
                    env.pop("CODEX_HOME", None)
                    lifecycle.discard()
                    raise CredentialSyncError("Credential binding is closed.")
                continue

            value = state.secret_registry.get_secret_value(name)
            if not value:
                continue
            directory = self._acp_file_secret_dir(state, spec.subdir)
            target = directory / spec.filename
            self._materialise_file_secret(
                spec,
                env,
                directory,
                target,
                value,
                replace_existing=replace_existing,
            )
            with self._file_credential_lock:
                self._replace_file_credentials_on_next_materialisation.discard(name)

    def _materialise_file_secret(
        self,
        spec: ACPFileSecretSpec,
        env: dict[str, str],
        directory: Path,
        target: Path,
        value: str,
        *,
        replace_existing: bool = False,
    ) -> None:
        name = spec.secret_name
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            directory.parent.chmod(0o700)
            preserve_existing = (
                not replace_existing and target.is_file() and target.stat().st_size > 0
            )
            if preserve_existing:
                target.chmod(0o600)
                logger.info(
                    "ACP file-secret %r already present at %s; preserving "
                    "(seed-if-absent)",
                    name,
                    target,
                )
            else:
                write_secret_file(target, value)
                logger.info("Materialised ACP file-secret %r -> %s", name, target)
        except (OSError, UnicodeError):
            logger.exception(
                "Failed to materialise ACP file-secret %r under %s",
                name,
                directory,
            )
            raise
        env[spec.env_var] = str(directory if spec.env_points_to == "dir" else target)
        for companion in spec.warn_if_unset:
            if not env.get(companion):
                logger.warning(
                    "ACP file-secret %r materialised but %s is unset; the "
                    "provider may fail to authenticate until it is configured",
                    name,
                    companion,
                )

    @staticmethod
    def _log_file_credential_failures(
        operation: str, failures: dict[str, Exception]
    ) -> None:
        for name, error in failures.items():
            logger.warning(
                "Failed to %s ACP file credential %r",
                operation,
                name,
                exc_info=(type(error), error, error.__traceback__),
            )

    @staticmethod
    def _raise_first_file_credential_failure(
        operation: str, failures: dict[str, Exception]
    ) -> None:
        if not failures:
            return
        first_name = next(iter(failures))
        remaining = dict(failures)
        first_error = remaining.pop(first_name)
        ACPAgent._log_file_credential_failures(operation, remaining)
        raise first_error

    def _sync_file_credentials_collect(self) -> dict[str, Exception]:
        failures: dict[str, Exception] = {}
        with self._file_credential_lock:
            lifecycles = tuple(self._file_credential_lifecycles.items())
        for name, lifecycle in lifecycles:
            try:
                lifecycle.flush()
            except Exception as error:
                failures[name] = error
        return failures

    def _sync_file_credentials(self) -> None:
        failures = self._sync_file_credentials_collect()
        self._raise_first_file_credential_failure("sync", failures)

    def _track_file_credentials_for_masking(self) -> None:
        with self._file_credential_lock:
            lifecycles = tuple(self._file_credential_lifecycles.items())
        for _name, lifecycle in lifecycles:
            try:
                lifecycle.track_current()
            except CredentialBindingError:
                raise
            except Exception as error:
                raise CredentialSyncError(
                    f"ACP file credential {_name!r} could not be synchronized."
                ) from error
        registry = self._secret_registry_for_masking
        runtime_dir = self._claude_config_runtime_dir
        if registry is None or runtime_dir is None:
            return
        track_claude_oauth_credentials_from_file(
            registry, runtime_dir / CLAUDE_CREDENTIALS_FILENAME
        )

    def _bind_file_credential_masking(self) -> None:
        client = self._client
        if client is None:
            return
        agent_ref = weakref.ref(self)

        def track_file_credentials() -> None:
            agent = agent_ref()
            if agent is not None:
                agent._track_file_credentials_for_masking()

        client.before_mask = track_file_credentials

    def _flush_file_credentials_blocking(self) -> None:
        failures = self._sync_file_credentials_collect()
        self._raise_first_file_credential_failure("sync", failures)

    def _release_file_credentials_collect(self) -> dict[str, Exception]:
        failures: dict[str, Exception] = {}
        with self._file_credential_lock:
            lifecycles = tuple(self._file_credential_lifecycles.items())
        for name, lifecycle in lifecycles:
            try:
                lifecycle.close()
            except Exception as error:
                failures[name] = error
            else:
                with self._file_credential_lock:
                    if self._file_credential_lifecycles.get(name) is lifecycle:
                        self._file_credential_lifecycles.pop(name)
        return failures

    def _startup_timeout_message(self) -> str:
        return (
            f"ACP startup timed out after {self.acp_startup_timeout:.0f}s "
            "waiting for the ACP server to spawn, authenticate, and "
            "create/load a session"
        )

    # -- Lifecycle ---------------------------------------------------------

    def init_state(
        self,
        state: ConversationState,
        on_event: ConversationCallbackType,
    ) -> None:
        """Spawn the ACP server and initialize a session."""
        if self.tools:
            raise NotImplementedError(
                "ACPAgent does not support custom tools; "
                "the ACP server manages its own tools"
            )
        if self.condenser is not None:
            raise NotImplementedError(
                "ACPAgent does not support condenser; "
                "the ACP server manages its own context"
            )
        if self.agent_context:
            self.agent_context.validate_acp_compatibility()

        # A writable Claude session cannot safely use the post-turn exact-model
        # contract: the first prompt would run before the served identity is
        # known. Reject the request before spawning/configuring a subprocess;
        # the qualified path is intentionally read_only-only.
        configured_provider_key = resolve_effective_acp_provider_key(
            acp_server=self.acp_server,
            command=self.acp_command,
        )
        if (
            configured_provider_key == "claude-code"
            and self.acp_model
            and self.acp_model.startswith("claude-")
            and self.acp_permission_policy != "read_only"
        ):
            raise ACPSessionModelError(
                "Qualified Claude exact-model execution requires a proven "
                "read_only session before prompting; writable/default sessions "
                f"cannot request {self.acp_model!r}."
            )

        from openhands.sdk.utils.async_executor import AsyncExecutor

        if self._executor is not None:
            self._cleanup()
        self._executor = AsyncExecutor()

        self._installed_suffix = self._render_suffix(state)
        prior_session_id = state.agent_state.get("acp_session_id")
        suffix_already_installed = bool(state.agent_state.get("acp_suffix_installed"))
        self._resumed_existing_session = bool(prior_session_id)

        try:
            self._start_acp_server(state)
        except Exception as e:
            detail = _sanitize_acp_diagnostic(
                str(e),
                state.secret_registry.mask_secrets_in_output,
            )
            stderr_tail = _sanitize_acp_diagnostic(
                self._startup_stderr_tail,
                state.secret_registry.mask_secrets_in_output,
            )
            adapter_name = _sanitize_acp_diagnostic(
                self._startup_adapter_name or "",
                state.secret_registry.mask_secrets_in_output,
            )
            adapter_version = _sanitize_acp_diagnostic(
                self._startup_adapter_version or "",
                state.secret_registry.mask_secrets_in_output,
            )
            runtime_version = _sanitize_acp_diagnostic(
                self._startup_runtime_version or "",
                state.secret_registry.mask_secrets_in_output,
            )
            package_version = _sanitize_acp_diagnostic(
                self._startup_package_version or "",
                state.secret_registry.mask_secrets_in_output,
            )
            rpc = _sanitize_acp_diagnostic(
                self._startup_rpc or "",
                state.secret_registry.mask_secrets_in_output,
            )
            stage = _sanitize_acp_diagnostic(
                self._startup_stage or "",
                state.secret_registry.mask_secrets_in_output,
            )
            logger.error(
                "ACP startup failed",
                extra=_acp_startup_log_extra(
                    phase=self._startup_phase,
                    launcher=Path(self.acp_command[0]).name,
                    provider_key=self._startup_provider_key,
                    package_version=package_version or None,
                    adapter_name=adapter_name or None,
                    adapter_version=adapter_version or None,
                    runtime_version=runtime_version or None,
                    rpc=rpc or None,
                    stage=stage or None,
                    exit_code=self._startup_exit_code,
                    stderr_tail=stderr_tail,
                    detail=detail,
                ),
            )
            try:
                self._cleanup()
            except Exception:
                logger.warning("Failed to clean up ACP resources", exc_info=True)
            if self._has_runtime_resources():
                self._register_atexit_cleanup(replace=True)
            try:
                state.execution_status = ConversationExecutionStatus.ERROR
                on_event(
                    ConversationErrorEvent(
                        source="agent",
                        code=_classify_acp_init_error(e),
                        detail=(
                            f"{detail[:500]} (phase={self._startup_phase}, "
                            f"provider_key={self._startup_provider_key or 'unknown'}, "
                            f"package_version={package_version or '<unavailable>'}, "
                            f"adapter_name={adapter_name or '<unavailable>'}, "
                            f"adapter_version={adapter_version or '<unavailable>'}, "
                            f"runtime_version={runtime_version or '<unavailable>'}, "
                            f"rpc={rpc or '<unavailable>'}, "
                            f"stage={stage or '<unavailable>'}, "
                            f"exit_code={self._startup_exit_code}, "
                            f"stderr={stderr_tail[:500] or '<empty>'})"
                        ),
                    )
                )
            except Exception:
                logger.exception("Failed to surface ACP init error to client")
            raise

        self._register_atexit_cleanup(replace=True)

        if self._session_id is not None:
            truly_resumed = (
                prior_session_id is not None and self._session_id == prior_session_id
            )
            self._resumed_existing_session = truly_resumed
        else:
            truly_resumed = self._resumed_existing_session

        self._initialized = True

        new_agent_state = {
            **state.agent_state,
            "acp_agent_name": self._agent_name,
            "acp_agent_version": self._agent_version,
            "acp_session_id": self._session_id,
            "acp_session_cwd": self._working_dir,
            "acp_supports_runtime_model_switch": self.supports_runtime_model_switch,
            "acp_model_via_config_option": self._model_via_config_option,
        }
        if not self._resumed_existing_session:
            new_agent_state.pop("acp_suffix_installed", None)
        override_attempted_not_applied = bool(self._requested_model_for_turn()) and (
            not self._model_override_applied
        )
        if self._current_model_id is not None:
            new_agent_state["acp_current_model_id"] = self._current_model_id
        elif (
            not truly_resumed
            or self._available_models is not None
            or override_attempted_not_applied
        ):
            new_agent_state.pop("acp_current_model_id", None)
        if self._available_models is not None:
            new_agent_state["acp_available_models"] = [
                m.model_dump() for m in self._available_models
            ]
        elif not truly_resumed:
            new_agent_state.pop("acp_available_models", None)
        state.agent_state = new_agent_state

        if self._installed_suffix:
            self._suffix_install_state = (
                "installed"
                if suffix_already_installed and self._resumed_existing_session
                else "pending_first_prompt"
            )

        on_event(
            SystemPromptEvent(
                source="agent",
                system_prompt=TextContent(
                    text=(
                        "This conversation is powered by an ACP server. "
                        "The system prompt and tools are managed by the "
                        "ACP server and are not available for display."
                    )
                ),
                dynamic_context=TextContent(text=self._installed_suffix)
                if self._installed_suffix
                else None,
                tools=[],
            )
        )

    def _render_suffix(self, state: ConversationState) -> str | None:
        """Render the system suffix once, including secrets from the registry."""
        file_secret_names = self._present_file_secret_names(state) | set(
            CLAUDE_OAUTH_ENV_NAMES
        )
        secret_infos = [
            info
            for info in state.secret_registry.get_secret_infos()
            if info.get("name") not in file_secret_names
        ]
        agent_context = self.agent_context
        if agent_context is None:
            if not secret_infos:
                return None
            agent_context = AgentContext(current_datetime=None)
        elif agent_context.secrets:
            agent_context = agent_context.model_copy(update={"secrets": {}})
        return agent_context.to_acp_prompt_context(additional_secret_infos=secret_infos)

    def _commit_suffix_installation(self, state: ConversationState) -> None:
        if self._suffix_install_state == "pending_first_prompt":
            self._suffix_install_state = "installed"
            state.agent_state = {
                **state.agent_state,
                "acp_suffix_installed": True,
            }

    def _mark_startup(
        self,
        phase: str,
        *,
        rpc: str | None = None,
        stage: str | None = None,
    ) -> None:
        self._startup_phase = phase
        self._startup_rpc = rpc
        self._startup_stage = stage if stage is not None else phase

    def _start_acp_server(self, state: ConversationState) -> None:
        """Start the ACP subprocess and initialize the session."""
        # ``LocalConversation.switch_acp_model`` replaces a pre-session agent
        # with ``model_copy(update={"acp_model": ...})``. Pydantic does not
        # rerun ``model_post_init`` for that copy, so a private requested-model
        # value copied from the construction-time agent must not shadow the new
        # persisted field. A live runtime switch explicitly activates the
        # private binding and is retained across a restart of that session.
        if not self._runtime_model_override_active:
            self._requested_model_id = self.acp_model
        self._post_turn_model_verification_required = False
        self._session_mode_id = None
        self._served_model_id = None
        self._mark_startup("preflight", rpc=None, stage="preflight")
        self._startup_stderr_tail = ""
        self._startup_exit_code = None
        self._startup_provider_key = resolve_effective_acp_provider_key(
            acp_server=self.acp_server,
            command=self.acp_command,
        )
        self._startup_package_version = resolve_acp_package_version(
            self.acp_command,
            provider_key=self._startup_provider_key,
        )
        self._startup_adapter_name = None
        self._startup_adapter_version = None
        self._startup_runtime_version = resolve_acp_runtime_version(
            self._startup_provider_key
        )
        command_provider = detect_acp_provider_by_command(self.acp_command)
        preflight_mode = resolve_session_mode_for_policy(
            self.acp_permission_policy,
            provider_key=command_provider.key if command_provider else None,
            explicit_mode=self.acp_session_mode,
            default_session_mode=(
                command_provider.default_session_mode if command_provider else None
            ),
        )
        assert_config_options_compatible_with_permission_policy(
            self.acp_permission_policy,
            self.acp_config_options,
        )
        client = _OpenHandsACPBridge(permission_policy=self.acp_permission_policy)
        self._client = client
        self._secret_registry_for_masking = state.secret_registry
        client.mask = state.secret_registry.mask_secrets_in_output
        self._bind_file_credential_masking()

        # Build environment: inherit current env + conversation secrets + ACP extras
        env = default_environment()
        env.update(os.environ)
        env.update(self.acp_env)
        command_is_claude = (
            command_provider is not None and command_provider.key == "claude-code"
        )
        excluded_secret_names = self._present_file_secret_names(state) | {
            CLAUDE_CREDENTIALS_SECRET_NAME
        }
        if not command_is_claude:
            excluded_secret_names = {*excluded_secret_names, CLAUDE_OAUTH_TOKEN_ENV}
        env.update(
            state.secret_registry.get_all_secrets_as_env_vars(
                exclude=excluded_secret_names
            )
        )
        claude_oauth_file_channel = False
        if self.acp_isolate_data_dir:
            claude_oauth_file_channel = self._isolate_acp_data_dir(state, env)
        elif (
            command_is_claude
            and CLAUDE_CREDENTIALS_SECRET_NAME in self._file_credential_bindings
        ):
            claude_oauth_file_channel = self._materialise_bound_claude_oauth(state, env)
        self._materialise_file_secrets(state, env)
        # Inject secrets from agent_context for keys not already present.
        if self.agent_context and self.agent_context.secrets:
            for name, secret in self.agent_context.secrets.items():
                if name in CLAUDE_OAUTH_ENV_NAMES and (
                    name == CLAUDE_CREDENTIALS_SECRET_NAME or not command_is_claude
                ):
                    continue
                if name not in env:
                    value = (
                        secret.get_value()
                        if isinstance(secret, SecretSource)
                        else str(secret)
                    )
                    if value:
                        env[name] = value
        # Strip CLAUDECODE so nested Claude Code instances don't refuse to start
        env.pop("CLAUDECODE", None)
        env.pop(CLAUDE_CREDENTIALS_SECRET_NAME, None)
        if not command_is_claude:
            for oauth_name in CLAUDE_OAUTH_ENV_NAMES:
                env.pop(oauth_name, None)

        # Strip PAYG/proxy vars when Claude subscription/OAuth is active so
        # they cannot silently win. Writable API/proxy stays intact when
        # OAuth is truly absent. File-channel sessions also drop the token
        # env var so the isolated credential file is the only channel.
        if command_is_claude and claude_subscription_auth_active(
            env, oauth_file_channel=claude_oauth_file_channel
        ):
            for conflict in _CLAUDE_OAUTH_CONFLICTING_ENV:
                env.pop(conflict, None)
            if claude_oauth_file_channel:
                env.pop(CLAUDE_OAUTH_TOKEN_ENV, None)

        env.update(
            initial_agent_mode_env_for_policy(
                self.acp_permission_policy,
                provider_key=command_provider.key if command_provider else None,
                mode_id=preflight_mode,
            )
        )

        command = self.acp_command[0]
        args = list(self.acp_command[1:]) + list(self.acp_args)

        working_dir = str(state.workspace.working_dir)

        self._resumed_existing_session = False

        # Prior ACP session id — survives agent-server restarts via
        # ConversationState.agent_state (serialized into base_state.json).
        # Its presence is the signal to resume; its absence means fresh start.
        # ACP servers key persistence by ``cwd``; if the workspace moved we
        # drop the id so we don't accidentally resume (or silently load) a
        # session the server associates with a different directory.
        prior_session_id: str | None = state.agent_state.get("acp_session_id")
        prior_session_cwd: str | None = state.agent_state.get("acp_session_cwd")
        if prior_session_id is not None and prior_session_cwd not in (
            None,
            working_dir,
        ):
            logger.warning(
                "ACP session %s was created with cwd=%s; current cwd=%s differs, "
                "starting a fresh session instead of resuming",
                prior_session_id,
                prior_session_cwd,
                working_dir,
            )
            prior_session_id = None

        has_codex_api_key = any(
            _has_usable_env_value(env, name)
            for name in ("CODEX_API_KEY", "OPENAI_API_KEY")
        )
        requested_model = self._requested_model_for_turn()

        async def _init() -> tuple[
            Any,
            Any,
            Any,
            str,
            str,
            str,
            str | None,
            list[ACPModelInfo] | None,
            bool,
        ]:
            # Spawn the subprocess directly so we can install a
            # filtering reader that skips non-JSON-RPC lines some
            # ACP servers (e.g. claude-code-acp v0.1.x) write to
            # stdout.
            self._mark_startup("spawn", rpc=None, stage="spawn")
            process = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_STREAM_READER_LIMIT,
            )
            assert process.stdin is not None
            assert process.stdout is not None

            # ``conn`` / ``filter_task`` stay None until they exist so the
            # teardown below only touches what was actually created.
            conn: ClientSideConnection | None = None
            filter_task: asyncio.Task | None = None
            stderr_tail = [""]
            stderr_task: asyncio.Task | None = None
            try:
                stderr_reader = getattr(process, "stderr", None)
                stderr_readline = getattr(stderr_reader, "readline", None)
                if inspect.iscoroutinefunction(stderr_readline):
                    stderr_task = asyncio.get_event_loop().create_task(
                        _capture_acp_stderr(stderr_reader, stderr_tail)
                    )
                # Wrap the subprocess stdout in a filtering reader that
                # only passes lines starting with '{' (JSON-RPC messages).
                filtered_reader = asyncio.StreamReader(limit=_STREAM_READER_LIMIT)
                filter_task = asyncio.get_event_loop().create_task(
                    _filter_jsonrpc_lines(process.stdout, filtered_reader)
                )

                conn = ClientSideConnection(
                    client,
                    process.stdin,  # write to subprocess
                    filtered_reader,  # read filtered output
                )

                # Track subprocess handles early so partial-init cleanup can
                # tear them down if a later handshake step fails.
                self._process = process
                self._conn = conn
                self._filtered_reader = filtered_reader
                self._stdout_filter_task = filter_task
                self._stderr_capture_task = stderr_task

                # Initialize the protocol and discover server identity.  The
                # capabilities argument is only sent when the caller supplied one,
                # so servers keep seeing the library defaults otherwise.
                self._mark_startup("initialize", rpc="initialize", stage="handshake")
                if self.acp_client_capabilities is not None:
                    init_response = await conn.initialize(
                        protocol_version=1,
                        client_capabilities=self.acp_client_capabilities,
                    )
                else:
                    init_response = await conn.initialize(protocol_version=1)
                agent_name = ""
                agent_version = ""
                if init_response.agent_info is not None:
                    agent_name = init_response.agent_info.name or ""
                    agent_version = init_response.agent_info.version or ""
                self._startup_adapter_name = agent_name or None
                self._startup_adapter_version = agent_version or None
                self._startup_provider_key = resolve_effective_acp_provider_key(
                    acp_server=self.acp_server,
                    command=self.acp_command,
                    agent_name=agent_name or None,
                )
                self._startup_package_version = resolve_acp_package_version(
                    self.acp_command,
                    provider_key=self._startup_provider_key,
                )
                self._startup_runtime_version = resolve_acp_runtime_version(
                    self._startup_provider_key
                )
                logger.info(
                    "ACP server initialized: agent_name=%r, agent_version=%r",
                    agent_name,
                    agent_version,
                )

                mcp_caps = _acp_mcp_capabilities(init_response)
                acp_mcp_servers = _mcp_config_to_acp_servers(self.mcp_config, mcp_caps)
                if acp_mcp_servers:
                    logger.info(
                        "Forwarding %d MCP server(s) to ACP session: %s",
                        len(acp_mcp_servers),
                        [s.name for s in acp_mcp_servers],
                    )

                # Authenticate if the server requires it.  Some ACP servers
                # (e.g. codex-acp) require an explicit authenticate call
                # before session creation.  We auto-detect the method from
                # the env vars that are available to the process.
                self._mark_startup("authenticate", rpc="authenticate", stage="select")
                auth_methods = init_response.auth_methods or []
                method_id = _select_auth_method(auth_methods, env)
                is_codex = (
                    self._startup_provider_key == "codex"
                    or (
                        command_provider is not None and command_provider.key == "codex"
                    )
                    or self.acp_server == "codex"
                )
                isolated_codex_without_api = bool(
                    is_codex and self.acp_isolate_data_dir and not has_codex_api_key
                )
                defer_codex_chatgpt_auth = bool(
                    method_id is not None
                    and _should_defer_codex_chatgpt_auth(
                        is_codex=is_codex,
                        method_id=method_id,
                        env=env,
                        has_codex_api_key=has_codex_api_key,
                    )
                )
                if method_id is None:
                    offered = _bound_offered_auth_ids(auth_methods)
                    if is_codex and auth_methods:
                        raise ACPAuthSelectionError(
                            "Codex advertised auth methods have no exact "
                            "supported credential match "
                            f"(offered={offered})."
                        )
                    if isolated_codex_without_api and not codex_auth_file_is_chatgpt(
                        env
                    ):
                        raise ACPAuthSelectionError(
                            "Codex ChatGPT subscription authentication is "
                            "required for isolated startup, but no usable "
                            "chat-gpt credential was materialized "
                            f"(offered={offered})."
                        )
                    if auth_methods:
                        logger.warning(
                            "ACP server offers auth methods %s but no matching "
                            "env var is set — session creation may fail",
                            offered,
                        )
                elif defer_codex_chatgpt_auth:
                    self._mark_startup(
                        "authenticate", rpc=None, stage="deferred_to_session"
                    )
                    logger.info(
                        "Deferring ACP authenticate RPC for Codex ChatGPT "
                        "file-backed auth; app-server will validate it during "
                        "session creation"
                    )
                else:
                    logger.info("Authenticating with ACP method: %s", method_id)
                    self._mark_startup(
                        "authenticate", rpc="authenticate", stage="authenticate"
                    )
                    auth_kwargs: dict[str, Any] = {}
                    # gemini-cli: pass gateway baseUrl to route API calls
                    # through LiteLLM proxy. claude-agent-acp and codex-acp
                    # read their provider base URL from env vars directly.
                    if method_id == "gemini-api-key":
                        provider = detect_acp_provider_by_agent_name(agent_name)
                        base_url_var = (
                            provider.base_url_env_var if provider is not None else None
                        )
                        if base_url_var:
                            base_url = env.get(base_url_var)
                            if base_url:
                                auth_kwargs["gateway"] = {"baseUrl": base_url}
                    await conn.authenticate(method_id=method_id, **auth_kwargs)

                # Resume the prior ACP session if we have its id.  If the server
                # has forgotten it (state wiped, new host, etc.) fall through to
                # new_session so the conversation still starts cleanly.
                #
                # We only swallow ACPRequestError here: that is the protocol-level
                # "I don't know this session" signal and is recoverable by
                # starting fresh.  Transport failures (broken pipe, EOF, timeout,
                # subprocess crash) propagate — there is no working connection to
                # fall back on, and the outer init_state handler cleans up.
                session_id: str | None = None
                reported_model_id: str | None = None
                available_models: list[ACPModelInfo] | None = None
                config_options: list[SessionConfigOption] | None = None
                load_response: Any = None
                if prior_session_id is not None:
                    try:
                        self._mark_startup(
                            "load_session", rpc="session/load", stage="load_session"
                        )
                        # claude-agent-acp 0.81.0 fingerprints session/load by
                        # cwd and mcpServers, not _meta, but its query options
                        # still need the caller's concrete model request after
                        # a resume. This carries intent/configuration only; a
                        # qualified Claude exact request is intentionally not
                        # treated as proven by load_session, and the resumed
                        # session's next turn must provide fresh evidence.
                        load_meta = _build_session_meta(
                            agent_name,
                            acp_model=requested_model,
                            structured_output=self.structured_output,
                            emit_raw_sdk_messages=_should_emit_claude_raw_sdk_messages(
                                agent_name,
                                self._startup_adapter_version,
                                self.acp_permission_policy,
                            ),
                        )
                        load_response = await conn.load_session(
                            cwd=working_dir,
                            session_id=prior_session_id,
                            mcp_servers=acp_mcp_servers,
                            **load_meta,
                        )
                        session_id = prior_session_id
                        self._resumed_existing_session = True
                        persisted_via_config_option = bool(
                            state.agent_state.get("acp_model_via_config_option", False)
                        )
                        (
                            reported_model_id,
                            available_models,
                            self._model_via_config_option,
                        ) = _extract_session_models(
                            load_response,
                            default_via_config_option=persisted_via_config_option,
                        )
                        config_options = load_response.config_options
                        logger.info(
                            "Resumed ACP session: %s (cwd=%s)",
                            _fingerprint_session_id(session_id),
                            working_dir,
                        )
                    except ACPRequestError as e:
                        logger.warning(
                            "ACP load_session(%s) failed (%s); starting fresh session",
                            _fingerprint_session_id(prior_session_id),
                            e,
                        )

                session_response: Any
                if session_id is None:
                    self._mark_startup(
                        "new_session", rpc="session/new", stage="new_session"
                    )
                    session_meta = _build_session_meta(
                        agent_name,
                        acp_model=requested_model,
                        structured_output=self.structured_output,
                        emit_raw_sdk_messages=_should_emit_claude_raw_sdk_messages(
                            agent_name,
                            self._startup_adapter_version,
                            self.acp_permission_policy,
                        ),
                    )
                    response = await conn.new_session(
                        cwd=working_dir,
                        mcp_servers=acp_mcp_servers,
                        **session_meta,
                    )
                    session_id = response.session_id
                    config_options = response.config_options
                    session_response = response
                    (
                        reported_model_id,
                        available_models,
                        self._model_via_config_option,
                    ) = _extract_session_models(response)
                    model_rpc = (
                        "session/set_config_option"
                        if self._model_via_config_option
                        else "session/set_model"
                    )
                    self._mark_startup("model", rpc=model_rpc, stage="apply_model")
                    defer_exact_model = _is_qualified_claude_exact_model(
                        agent_name,
                        self._startup_adapter_version,
                        requested_model,
                    )
                    self._post_turn_model_verification_required = defer_exact_model
                    effective_model_id = await _maybe_set_session_model(
                        conn,
                        agent_name,
                        session_id,
                        requested_model,
                        via_config_option=self._model_via_config_option,
                        model_state=getattr(response, "models", None),
                        apply_requested=bool(requested_model),
                        client=client,
                        defer_exact_model_verification=defer_exact_model,
                    )
                else:
                    model_rpc = (
                        "session/set_config_option"
                        if self._model_via_config_option
                        else "session/set_model"
                    )
                    self._mark_startup("model", rpc=model_rpc, stage="apply_model")
                    session_response = load_response
                    defer_exact_model = _is_qualified_claude_exact_model(
                        agent_name,
                        self._startup_adapter_version,
                        requested_model,
                    )
                    self._post_turn_model_verification_required = defer_exact_model
                    effective_model_id = (
                        None
                        if defer_exact_model
                        else await _reapply_session_model_on_resume(
                            conn,
                            agent_name,
                            session_id,
                            requested_model,
                            via_config_option=self._model_via_config_option,
                            config_options=config_options,
                            client=client,
                        )
                    )

                override_applied = effective_model_id is not None
                current_model_id = (
                    effective_model_id
                    if effective_model_id is not None
                    else reported_model_id
                )

                # Resolve the permission mode.  Known providers each have their
                # own mode ID (bypassPermissions, full-access, yolo …).
                # Unknown/custom servers get None — skip the call rather than
                # sending a provider-specific string they won't recognise.
                # Writable keeps the historical runtime-agent_name lookup and
                # does not fall back to the launch-command provider.
                # read_only may use the command provider because it already
                # failed closed before spawn when that provider is unverified.
                provider = detect_acp_provider_by_agent_name(agent_name)
                if self.acp_permission_policy == "read_only":
                    runtime_key = (
                        provider.key
                        if provider is not None
                        else (command_provider.key if command_provider else None)
                    )
                    runtime_default_mode = (
                        provider.default_session_mode
                        if provider is not None
                        else (
                            command_provider.default_session_mode
                            if command_provider
                            else None
                        )
                    )
                else:
                    runtime_key = provider.key if provider is not None else None
                    runtime_default_mode = (
                        provider.default_session_mode if provider is not None else None
                    )
                mode_id = resolve_session_mode_for_policy(
                    self.acp_permission_policy,
                    provider_key=runtime_key,
                    explicit_mode=self.acp_session_mode,
                    default_session_mode=runtime_default_mode,
                )
                self._mark_startup("mode", rpc="session/set_mode", stage="set_mode")
                await _apply_acp_session_mode(
                    conn,
                    client,
                    policy=self.acp_permission_policy,
                    mode_id=mode_id,
                    agent_name=agent_name,
                    session_id=session_id,
                    session_response=session_response,
                )
                self._session_mode_id = mode_id

                # Requested session configuration is applied and verified here —
                # part of initialization, so step() never prompts a session whose
                # configuration the server did not confirm.  Runs last so options
                # that depend on the selected model are resolved against the
                # server's post-model state.
                self._mark_startup(
                    "config", rpc="session/set_config_option", stage="set_config"
                )
                final_options = await _apply_session_config_options(
                    conn,
                    client,
                    agent_name,
                    session_id,
                    self.acp_config_options,
                    config_options,
                    required_session_mode=mode_id,
                    provider_key=runtime_key,
                    adapter_version=self._startup_adapter_version,
                )
                claude_exact_model = _is_claude_exact_model_request(
                    agent_name,
                    requested_model,
                )
                if claude_exact_model and not defer_exact_model:
                    raise ACPSessionModelError(
                        "Claude exact-model execution is not qualified for this "
                        "adapter version; refusing to prompt "
                        f"(requested={requested_model!r}, "
                        f"adapter_version={self._startup_adapter_version!r}, "
                        f"selector={current_model_id!r})."
                    )
                if defer_exact_model and self.acp_permission_policy != "read_only":
                    raise ACPSessionModelError(
                        "Qualified Claude exact-model execution requires a proven "
                        "read_only session before prompting "
                        f"(requested={requested_model!r}, "
                        f"selector={current_model_id!r})."
                    )
                current_model_id = _surfaced_current_model_id(
                    current_model_id, agent_name, final_options
                )

                self._mark_startup("ready", rpc=None, stage="ready")
                return (
                    conn,
                    process,
                    filtered_reader,
                    session_id,
                    agent_name,
                    agent_version,
                    current_model_id,
                    available_models,
                    override_applied,
                )
            except BaseException:
                self._startup_stderr_tail = stderr_tail[0]
                self._startup_exit_code = getattr(process, "returncode", None)
                # The subprocess is already running, but its handles have not
                # been published to the ACPAgent attributes yet — ``init_state``
                # cleanup cannot see them, so nothing else would ever reap the
                # process.  Tear it down here before the failure propagates.
                teardown = asyncio.ensure_future(
                    _abort_partial_acp_init(process, conn, filter_task)
                )
                try:
                    await asyncio.shield(teardown)
                except BaseException:
                    # Cancelled while unwinding: the shielded teardown keeps
                    # running, but the event loop may disappear with us, so
                    # kill the subprocess without awaiting anything.
                    _signal_acp_process(process, "kill")
                if stderr_task is not None:
                    await _await_bounded(stderr_task, "reading ACP stderr")
                    self._startup_stderr_tail = stderr_tail[0]
                self._startup_exit_code = getattr(process, "returncode", None)
                # Clear early-published handles so init_state's _cleanup does
                # not close/terminate resources _abort_partial_acp_init reaped.
                self._conn = None
                self._process = None
                self._filtered_reader = None
                self._stdout_filter_task = None
                self._stderr_capture_task = None
                # Re-raises the original failure, not anything from teardown.
                raise

        try:
            (
                self._conn,
                self._process,
                self._filtered_reader,
                self._session_id,
                self._agent_name,
                self._agent_version,
                self._current_model_id,
                self._available_models,
                self._model_override_applied,
            ) = self._executor.run_async(_init, timeout=self.acp_startup_timeout)
        except TimeoutError:
            raise TimeoutError(self._startup_timeout_message()) from None
        self._working_dir = working_dir
        if self._model_override_applied and self._current_model_id is not None:
            self.llm.model = self._current_model_id
            self.llm.metrics.model_name = self._current_model_id
            if self.llm.metrics.accumulated_token_usage is not None:
                self.llm.metrics.accumulated_token_usage.model = self._current_model_id
        self._flush_file_credentials_blocking()

    def _reset_client_for_turn(
        self,
        on_token: ConversationTokenCallbackType | None,
        on_event: ConversationCallbackType,
        prompt: Any = None,
        mask: Callable[[str], str] | None = None,
    ) -> None:
        """Reset per-turn client state and (re)wire live callbacks."""
        self._served_model_id = None
        self._client.trace.abandon()
        self._client.reset()
        self._client.trace = ACPTurnTrace(
            acp_server=self.acp_server,
            model_id=self._current_model_id,
            mask=mask,
        )
        self._client.trace.start_turn(prompt)
        self._client.on_token = on_token
        self._client.on_event = on_event
        self._client.on_activity = self._on_activity
        self._client.arm_activity_clock()

    def _verify_served_model_for_turn(self) -> None:
        """Gate a qualified Claude turn on its own served-model evidence.

        The selector kept in ``_current_model_id`` is diagnostic/routing state;
        it is never accepted as exact identity.  The response is checked before
        structured result consumption or FinishAction emission so provider
        output cannot become an SDK-successful turn first. Token/cost
        accounting remains independent telemetry. ``model_usage`` is likewise
        telemetry only: it has no root-agent or same-turn authority contract.
        """
        if not self._post_turn_model_verification_required:
            return
        requested_model = self._requested_model_for_turn()
        selector_model = self._current_model_id
        evidence = (
            self._client.pop_turn_model_evidence() if self._client is not None else None
        )
        root_models = evidence.root_models if evidence is not None else ()
        message_start_models = (
            evidence.message_start_models if evidence is not None else ()
        )
        observed_models = (*root_models, *message_start_models)
        if (
            evidence is None
            or evidence.invalid_root_evidence
            or evidence.invalid_turn_binding
            or not root_models
            or not observed_models
        ):
            self._served_model_id = None
            self._restart_session_on_next_turn = True
            raise ACPSessionModelError(
                "Claude ACP turn did not provide qualified same-turn root "
                f"evidence (requested={requested_model!r}, "
                f"selector={selector_model!r}, served='UNKNOWN')."
            )
        mismatched = next(
            (model for model in observed_models if model != requested_model), None
        )
        if requested_model is None or mismatched is not None:
            self._served_model_id = None
            self._restart_session_on_next_turn = True
            raise ACPSessionModelError(
                "Claude ACP turn served a model different from the request "
                f"(requested={requested_model!r}, selector={selector_model!r}, "
                f"served={mismatched or root_models[0]!r}, "
                f"root_models={list(root_models)!r})."
            )
        served_model = root_models[0]
        self._served_model_id = served_model
        logger.info(
            "Claude ACP same-turn root model verification passed: requested=%s "
            "selector=%s root_models=%s delegated_models=%s",
            requested_model,
            selector_model,
            list(root_models),
            list(evidence.delegated_models),
        )

    def _clear_turn_callbacks(self) -> None:
        """Unwire per-turn bridge callbacks so trailing updates are no-ops."""
        if self._client is None:
            return
        self._client.trace.abandon()
        self._client.on_event = None
        self._client.on_token = None
        self._client.on_activity = None

    def _cancel_inflight_tool_calls(self) -> None:
        """Emit a terminal ``failed`` ACPToolCallEvent for every tool call
        in the accumulator that has not reached a terminal status yet.
        """
        on_event = self._client.on_event
        self._clear_turn_callbacks()
        if on_event is None:
            return
        for tc in self._client.accumulated_tool_calls:
            status = tc.get("status")
            if status in _TERMINAL_TOOL_CALL_STATUSES:
                continue
            try:
                on_event(
                    ACPToolCallEvent(
                        tool_call_id=tc["tool_call_id"],
                        title=tc["title"],
                        status="failed",
                        tool_kind=tc.get("tool_kind"),
                        raw_input=tc.get("raw_input"),
                        raw_output=tc.get("raw_output"),
                        content=tc.get("content"),
                        is_error=True,
                    )
                )
            except Exception:
                logger.debug(
                    "Failed to emit supersede event for %s",
                    tc.get("tool_call_id"),
                    exc_info=True,
                )

    def _build_acp_prompt(
        self, event: MessageEvent
    ) -> list[TextContentBlock | ImageContentBlock] | None:
        """Build the ACP content blocks for one user turn."""
        message = event.to_llm_message()
        blocks: list[TextContentBlock | ImageContentBlock] = []
        for content in message.content:
            if isinstance(content, TextContent) and content.text.strip():
                blocks.append(text_block(content.text))
            elif isinstance(content, ImageContent):
                for url in content.image_urls:
                    acp_block = _image_url_to_acp_block(url)
                    if acp_block is not None:
                        blocks.append(acp_block)
        if (
            self._suffix_install_state == "pending_first_prompt"
            and self._installed_suffix
        ):
            blocks.append(text_block(self._installed_suffix))
        if not blocks:
            return None
        return blocks

    def _flush_inflight_tool_calls_as_completed(self) -> None:
        for tc in self._client.accumulated_tool_calls:
            if tc.get("status") in _TERMINAL_TOOL_CALL_STATUSES:
                continue
            title = tc.get("title")
            if isinstance(title, str) and title.casefold() == "structuredoutput":
                # A structured-output call without a terminal ACP update must
                # never be promoted as a usable final response.
                tc["status"] = "failed"
            else:
                tc["status"] = "completed"
            self._client._emit_tool_call_event(tc)

    async def _do_acp_prompt(
        self, prompt_blocks: list[TextContentBlock | ImageContentBlock]
    ) -> PromptResponse | None:
        """One ACP ``conn.prompt`` round-trip + UsageUpdate sync on the portal loop."""
        if self._conn is None or self._session_id is None:
            msg = "ACPAgent has no live ACP session; call init_state() first"
            raise RuntimeError(msg)
        session_id = self._session_id
        usage_sync = self._client.prepare_usage_sync(session_id)
        self._client.begin_turn_model_evidence(session_id)
        try:
            response = await self._conn.prompt(prompt_blocks, session_id)
        except BaseException:
            self._client.discard_turn_model_evidence()
            raise
        self._client.finish_turn_model_evidence(session_id)
        if self._client.get_turn_usage_update(session_id) is None:
            try:
                await asyncio.wait_for(usage_sync.wait(), timeout=_USAGE_UPDATE_TIMEOUT)
            except TimeoutError:
                logger.warning(
                    "UsageUpdate not received within %.1fs for session %s",
                    _USAGE_UPDATE_TIMEOUT,
                    session_id,
                )
        return response

    def _idle_timeout_message(self) -> str:
        return (
            f"ACP prompt timed out after {self.acp_prompt_timeout:.0f}s "
            "with no activity from the ACP server"
        )

    async def _await_with_idle_deadline(
        self,
        awaitable: Any,
        *,
        cancel_on_exit: bool,
    ) -> PromptResponse | None:
        """Await *awaitable*, aborting only after a stretch of inactivity."""
        idle_limit = self.acp_prompt_timeout
        fut = asyncio.ensure_future(awaitable)
        try:
            while True:
                remaining = idle_limit - self._client.seconds_since_last_activity()
                if remaining <= 0:
                    raise TimeoutError(self._idle_timeout_message())
                await asyncio.wait({fut}, timeout=remaining)
                if fut.done():
                    return fut.result()
                if self._client.seconds_since_last_activity() >= idle_limit:
                    raise TimeoutError(self._idle_timeout_message())
        finally:
            if cancel_on_exit and not fut.done():
                fut.cancel()

    async def _await_prompt_response_with_timeout(
        self,
        prompt_future: Future[PromptResponse | None],
    ) -> PromptResponse | None:
        return await self._await_with_idle_deadline(
            asyncio.wrap_future(prompt_future), cancel_on_exit=False
        )

    @staticmethod
    def _prompt_response_was_cancelled(response: PromptResponse | None) -> bool:
        return response is not None and response.stop_reason == "cancelled"

    def _finalize_successful_turn(
        self,
        response: PromptResponse | None,
        elapsed: float,
        state: ConversationState,
        on_event: ConversationCallbackType,
    ) -> None:
        """Post-prompt bookkeeping + FinishAction/Observation emission."""
        session_id = self._session_id or ""
        usage_update = self._client.pop_turn_usage_update(session_id)
        self._record_usage(
            response,
            session_id,
            elapsed=elapsed,
            usage_update=usage_update,
        )

        # Usage accounting is telemetry, not acceptance. Keep it independent
        # of the exact-model success gate so a provider response that is later
        # rejected for model provenance does not silently lose its token/cost
        # accounting. No successful turn event is emitted until this gate
        # passes.
        self._verify_served_model_for_turn()
        self._client._raise_masking_error()
        self._commit_suffix_installation(state)

        self._flush_inflight_tool_calls_as_completed()

        self._track_file_credentials_for_masking()
        mask = state.secret_registry.mask_secrets_in_output
        response_text = mask("".join(self._client.accumulated_text))
        thought_text = mask("".join(self._client.accumulated_thoughts))
        if not response_text:
            response_text = "(No response from ACP server)"

        self._client.trace.finish_turn(
            response_text, thought_text, self._client.accumulated_tool_calls
        )

        finish_action = FinishAction(message=response_text)
        tc_id = str(uuid.uuid4())
        action_event = ActionEvent(
            source="agent",
            thought=[],
            reasoning_content=thought_text or None,
            action=finish_action,
            tool_name="finish",
            tool_call_id=tc_id,
            tool_call=MessageToolCall(
                id=tc_id,
                name="finish",
                arguments=json.dumps({"message": response_text}),
                origin="completion",
            ),
            llm_response_id=str(uuid.uuid4()),
        )
        on_event(action_event)
        on_event(
            ObservationEvent(
                observation=FinishObservation.from_text(text=response_text),
                action_id=action_event.id,
                tool_name="finish",
                tool_call_id=tc_id,
            )
        )
        state.execution_status = ConversationExecutionStatus.FINISHED

    def _record_discarded_turn_usage(
        self,
        response: PromptResponse | None,
        elapsed: float,
    ) -> None:
        """Account a completed provider response that cancellation discarded.

        Cancellation remains the acceptance authority. Token/cost telemetry is
        independent, however, so a late response must not disappear from usage
        accounting merely because it cannot become a successful SDK turn.
        """
        session_id = self._session_id or ""
        usage_update = self._client.pop_turn_usage_update(session_id)
        self._record_usage(
            response,
            session_id,
            elapsed=elapsed,
            usage_update=usage_update,
        )

    def _emit_turn_timeout(
        self,
        elapsed: float,
        state: ConversationState,
        on_event: ConversationCallbackType,
    ) -> None:
        logger.error(
            "ACP prompt timed out after %.1fs with no activity for the last "
            "%.0fs. The ACP server may have stalled or failed to send the "
            "JSON-RPC response. Accumulated %d text chunks, %d tool calls.",
            elapsed,
            self.acp_prompt_timeout,
            len(self._client.accumulated_text),
            len(self._client.accumulated_tool_calls),
        )
        error_message = Message(
            role="assistant",
            content=[
                TextContent(
                    text=(
                        "ACP prompt timed out after "
                        f"{self.acp_prompt_timeout:.0f}s with no activity from "
                        "the agent. The agent may have stalled, or it may have "
                        "completed its work but the response was not received."
                    )
                )
            ],
        )
        self._cancel_inflight_tool_calls()
        on_event(MessageEvent(source="agent", llm_message=error_message))
        state.execution_status = ConversationExecutionStatus.ERROR

    def _emit_turn_error(
        self,
        exc: BaseException,
        state: ConversationState,
        on_event: ConversationCallbackType,
    ) -> None:
        error_str = str(exc)
        logger.error("ACP prompt failed: %s", exc, exc_info=True)
        self._cancel_inflight_tool_calls()
        on_event(
            MessageEvent(
                source="agent",
                llm_message=Message(
                    role="assistant",
                    content=[TextContent(text=f"ACP error: {error_str}")],
                ),
            )
        )
        is_aup = (
            "usage policy" in error_str.lower() or "content policy" in error_str.lower()
        )
        on_event(
            ConversationErrorEvent(
                source="agent",
                code="UsagePolicyRefusal" if is_aup else "ACPPromptError",
                detail=error_str[:500],
            )
        )
        state.execution_status = ConversationExecutionStatus.ERROR

    def _finalize_successful_turn_guarded(
        self,
        response: PromptResponse | None,
        elapsed: float,
        state: ConversationState,
        on_event: ConversationCallbackType,
    ) -> None:
        try:
            self._finalize_successful_turn(response, elapsed, state, on_event)
        except CredentialBindingError as exc:
            self._emit_turn_error(exc, state, on_event)
            self._restart_session_on_next_turn = True
            raise

    def _handle_cancelled_cleanup_interruption(
        self,
        prompt_future: Future[PromptResponse | None] | None,
        _elapsed: float,
        _state: ConversationState,
        _on_event: ConversationCallbackType,
    ) -> None:
        if prompt_future is not None and prompt_future.done():
            try:
                response = prompt_future.result()
            except BaseException:
                self._cancel_inflight_tool_calls()
                self._restart_session_on_next_turn = True
            else:
                if (
                    self._post_turn_model_verification_required
                    or self._prompt_response_was_cancelled(response)
                ):
                    # Qualified exact Claude cancellation is authoritative
                    # even when a valid-looking response raced with cancel.
                    if self._post_turn_model_verification_required:
                        self._record_discarded_turn_usage(response, _elapsed)
                    self._cancel_inflight_tool_calls()
                    self._restart_session_on_next_turn = True
                else:
                    self._finalize_successful_turn_guarded(
                        response,
                        _elapsed,
                        _state,
                        _on_event,
                    )
            return

        self._cancel_inflight_tool_calls()
        if prompt_future is not None:
            self._restart_session_on_next_turn = True

    async def _arequest_session_cancel(self) -> None:
        if self._conn is None or self._executor is None or self._session_id is None:
            return
        conn = self._conn
        session_id = self._session_id

        async def _cancel() -> None:
            result = conn.cancel(session_id)
            if inspect.isawaitable(result):
                await result

        try:
            future = self._executor.portal.start_task_soon(_cancel)
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=_ACP_CANCEL_DRAIN_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "Timed out sending ACP session cancel; restarting ACP session"
            )
            self._restart_session_on_next_turn = True
        except Exception:
            logger.warning("Failed to send ACP session cancel", exc_info=True)

    async def _drain_cancelled_prompt(
        self,
        future: Future[PromptResponse | None] | None,
    ) -> _PromptDrainResult:
        if future is None:
            return _PromptDrainResult(
                drained=True, completed=False, response=None, error=None
            )
        if future.cancelled():
            return _PromptDrainResult(
                drained=True, completed=False, response=None, error=None
            )
        if future.done():
            try:
                return _PromptDrainResult(
                    drained=True,
                    completed=True,
                    response=future.result(),
                    error=None,
                )
            except BaseException as exc:
                return _PromptDrainResult(
                    drained=True, completed=True, response=None, error=exc
                )
        try:
            response = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=_ACP_CANCEL_DRAIN_TIMEOUT,
            )
            return _PromptDrainResult(
                drained=True, completed=True, response=response, error=None
            )
        except asyncio.CancelledError:
            if future.cancelled():
                return _PromptDrainResult(
                    drained=False, completed=False, response=None, error=None
                )
            raise
        except TimeoutError:
            logger.warning(
                "Timed out waiting for cancelled ACP prompt to drain; "
                "the ACP session will be restarted before the next turn"
            )
            return _PromptDrainResult(
                drained=False, completed=False, response=None, error=None
            )
        except BaseException as exc:
            return _PromptDrainResult(
                drained=future.done(), completed=True, response=None, error=exc
            )

    def _restart_session_after_drain_timeout(
        self,
        state: ConversationState,
        on_event: ConversationCallbackType,
    ) -> None:
        logger.warning("Restarting ACP session after cancelled prompt drain timeout")
        self._clear_turn_callbacks()
        self._cleanup()
        self._initialized = False
        self.init_state(state, on_event=on_event)
        self._restart_session_on_next_turn = False

    async def _arestart_session_after_drain_timeout(
        self,
        state: ConversationState,
        on_event: ConversationCallbackType,
    ) -> None:
        await asyncio.to_thread(
            self._restart_session_after_drain_timeout, state, on_event
        )

    def _request_session_cancel(self) -> None:
        if self._conn is None or self._executor is None or self._session_id is None:
            return
        conn = self._conn
        session_id = self._session_id

        async def _cancel() -> None:
            result = conn.cancel(session_id)
            if inspect.isawaitable(result):
                await result

        try:
            self._executor.portal.start_task_soon(_cancel)
        except Exception:
            logger.warning("Failed to send ACP session cancel", exc_info=True)

    async def _flush_file_credentials(self) -> None:
        await asyncio.to_thread(self._flush_file_credentials_blocking)

    @observe(name="acp_agent.astep", ignore_inputs=["conversation", "on_event"])
    async def astep(
        self,
        conversation: LocalConversation,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None = None,
        prompt_message: MessageEvent | None = None,
    ) -> None:
        """Native-async variant of :meth:`step`.

        Schedules the ACP ``conn.prompt`` round-trip on the portal loop and
        awaits the result on the caller's event loop so post-prompt callbacks
        and state updates stay on ``LocalConversation.arun``'s task.
        """
        state = conversation.state

        if self._restart_session_on_next_turn:
            await self._arestart_session_after_drain_timeout(state, on_event)

        prompt_blocks: list[TextContentBlock | ImageContentBlock] | None = None
        if prompt_message is not None:
            prompt_blocks = self._build_acp_prompt(prompt_message)
        else:
            for event in reversed(list(state.events)):
                if isinstance(event, MessageEvent) and event.source == "user":
                    prompt_blocks = self._build_acp_prompt(event)
                    if prompt_blocks:
                        break
        if prompt_blocks is None:
            logger.warning("No user message found; finishing conversation")
            state.execution_status = ConversationExecutionStatus.FINISHED
            return

        mask = state.secret_registry.mask_secrets_in_output
        self._reset_client_for_turn(on_token, on_event, prompt_blocks, mask)

        t0 = time.monotonic()
        prompt_future: Future[PromptResponse | None] | None = None
        try:
            logger.info(
                "Sending ACP prompt (idle_timeout=%.0fs, blocks=%d, async)",
                self.acp_prompt_timeout,
                len(prompt_blocks),
            )
            portal = self._executor.portal

            response: PromptResponse | None = None
            max_retries = _ACP_PROMPT_MAX_RETRIES
            for attempt in range(max_retries + 1):
                try:
                    current_prompt_future: Future[PromptResponse | None] = (
                        portal.start_task_soon(
                            self._do_acp_prompt,
                            prompt_blocks,
                        )
                    )
                    prompt_future = current_prompt_future
                    response = await self._await_prompt_response_with_timeout(
                        current_prompt_future
                    )
                    break
                except TimeoutError:
                    raise
                except _RETRIABLE_CONNECTION_ERRORS as e:
                    if attempt < max_retries:
                        delay = _ACP_PROMPT_RETRY_DELAYS[
                            min(attempt, len(_ACP_PROMPT_RETRY_DELAYS) - 1)
                        ]
                        logger.warning(
                            "ACP prompt failed with retriable error "
                            "(attempt %d/%d), retrying in %.0fs: %s",
                            attempt + 1,
                            max_retries + 1,
                            delay,
                            e,
                        )
                        await asyncio.sleep(delay)
                        self._cancel_inflight_tool_calls()
                        self._reset_client_for_turn(
                            on_token, on_event, prompt_blocks, mask
                        )
                    else:
                        raise
                except ACPRequestError as e:
                    if (
                        e.code in _RETRIABLE_SERVER_ERROR_CODES
                        and attempt < max_retries
                    ):
                        delay = _ACP_PROMPT_RETRY_DELAYS[
                            min(attempt, len(_ACP_PROMPT_RETRY_DELAYS) - 1)
                        ]
                        logger.warning(
                            "ACP prompt failed with server error "
                            "(attempt %d/%d), retrying in %.0fs: [%d] %s",
                            attempt + 1,
                            max_retries + 1,
                            delay,
                            e.code,
                            e,
                        )
                        await asyncio.sleep(delay)
                        self._cancel_inflight_tool_calls()
                        self._reset_client_for_turn(
                            on_token, on_event, prompt_blocks, mask
                        )
                    else:
                        raise

            elapsed = time.monotonic() - t0
            logger.info("ACP prompt returned in %.1fs (async)", elapsed)
            with state:
                self._finalize_successful_turn(response, elapsed, state, on_event)
        except asyncio.CancelledError:
            # Cancellation wins the authority race. Prevent a provider event
            # arriving during cancel/drain from becoming this turn's proof.
            self._client.discard_turn_model_evidence()
            try:
                await self._arequest_session_cancel()
                drain_result = await self._drain_cancelled_prompt(prompt_future)
            except asyncio.CancelledError:
                self._client.discard_turn_model_evidence()
                with state:
                    elapsed = time.monotonic() - t0
                    self._handle_cancelled_cleanup_interruption(
                        prompt_future, elapsed, state, on_event
                    )
                raise
            with state:
                elapsed = time.monotonic() - t0
                if drain_result.completed and drain_result.error is None:
                    if self._post_turn_model_verification_required:
                        # Qualified exact Claude cancellation is authoritative
                        # even if valid root model evidence arrived late. Keep
                        # the historical completion behavior for other ACP
                        # providers and non-qualified sessions.
                        self._record_discarded_turn_usage(
                            drain_result.response, elapsed
                        )
                        self._cancel_inflight_tool_calls()
                        self._restart_session_on_next_turn = True
                    elif self._prompt_response_was_cancelled(drain_result.response):
                        self._cancel_inflight_tool_calls()
                        self._restart_session_on_next_turn = True
                    else:
                        self._finalize_successful_turn_guarded(
                            drain_result.response, elapsed, state, on_event
                        )
                    raise
                if drain_result.completed and drain_result.error is not None:
                    self._cancel_inflight_tool_calls()
                    self._restart_session_on_next_turn = True
                    raise
                self._cancel_inflight_tool_calls()
            if not drain_result.drained:
                self._restart_session_on_next_turn = True
            raise
        except TimeoutError:
            try:
                await self._arequest_session_cancel()
                drain_result = await self._drain_cancelled_prompt(prompt_future)
            except asyncio.CancelledError:
                self._client.discard_turn_model_evidence()
                with state:
                    elapsed = time.monotonic() - t0
                    self._handle_cancelled_cleanup_interruption(
                        prompt_future, elapsed, state, on_event
                    )
                raise
            with state:
                elapsed = time.monotonic() - t0
                if drain_result.completed and drain_result.error is None:
                    if self._prompt_response_was_cancelled(drain_result.response):
                        self._client.discard_turn_model_evidence()
                        if self._post_turn_model_verification_required:
                            self._record_discarded_turn_usage(
                                drain_result.response, elapsed
                            )
                        self._emit_turn_timeout(elapsed, state, on_event)
                        self._restart_session_on_next_turn = True
                    else:
                        try:
                            self._finalize_successful_turn_guarded(
                                drain_result.response, elapsed, state, on_event
                            )
                        except ACPSessionModelError as exc:
                            # The normal prompt path reaches the outer
                            # exception handler, but this is an exception
                            # handler's drain path. Surface the exact-model
                            # failure to the conversation before re-raising.
                            self._emit_turn_error(exc, state, on_event)
                            self._restart_session_on_next_turn = True
                            raise
                elif drain_result.completed and drain_result.error is not None:
                    self._client.discard_turn_model_evidence()
                    self._emit_turn_error(drain_result.error, state, on_event)
                    self._restart_session_on_next_turn = True
                else:
                    self._client.discard_turn_model_evidence()
                    self._emit_turn_timeout(elapsed, state, on_event)
                    self._restart_session_on_next_turn = True
        except Exception as e:
            with state:
                self._emit_turn_error(e, state, on_event)
            raise
        finally:
            self._clear_turn_callbacks()
            await self._flush_file_credentials()

    @observe(name="acp_agent.step", ignore_inputs=["conversation", "on_event"])
    def step(
        self,
        conversation: LocalConversation,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None = None,
    ) -> None:
        """Send the latest user message to the ACP server and emit the response."""
        state = conversation.state

        if self._restart_session_on_next_turn:
            self._restart_session_after_drain_timeout(state, on_event)

        # Find the latest user message. Conversation implementations already
        # attach per-turn AgentContext extensions to MessageEvent.extended_content;
        # MessageEvent.to_llm_message() merges those extensions with the user text.
        prompt_blocks = None
        for event in reversed(list(state.events)):
            if isinstance(event, MessageEvent) and event.source == "user":
                prompt_blocks = self._build_acp_prompt(event)
                if prompt_blocks:
                    break

        if prompt_blocks is None:
            logger.warning("No user message found; finishing conversation")
            state.execution_status = ConversationExecutionStatus.FINISHED
            return

        mask = state.secret_registry.mask_secrets_in_output
        self._reset_client_for_turn(on_token, on_event, prompt_blocks, mask)

        t0 = time.monotonic()
        try:

            async def _prompt() -> PromptResponse:
                usage_sync = self._client.prepare_usage_sync(self._session_id or "")
                session_id = self._session_id or ""
                self._client.begin_turn_model_evidence(session_id)
                try:
                    response = await self._conn.prompt(prompt_blocks, session_id)
                except BaseException:
                    self._client.discard_turn_model_evidence()
                    raise
                self._client.finish_turn_model_evidence(session_id)
                if self._client.get_turn_usage_update(self._session_id or "") is None:
                    try:
                        await asyncio.wait_for(
                            usage_sync.wait(), timeout=_USAGE_UPDATE_TIMEOUT
                        )
                    except TimeoutError:
                        logger.warning(
                            "UsageUpdate not received within %.1fs for session %s",
                            _USAGE_UPDATE_TIMEOUT,
                            self._session_id,
                        )
                return response

            # Send prompt to ACP server with retry logic for connection errors.
            # Transient connection failures (network blips, server restarts) are
            # retried to preserve session state and avoid losing progress.
            logger.info(
                "Sending ACP prompt (timeout=%.0fs, blocks=%d)",
                self.acp_prompt_timeout,
                len(prompt_blocks),
            )

            response: PromptResponse | None = None
            max_retries = _ACP_PROMPT_MAX_RETRIES

            for attempt in range(max_retries + 1):
                try:
                    response = self._executor.run_async(
                        _prompt, timeout=self.acp_prompt_timeout
                    )
                    break
                except TimeoutError:
                    raise
                except _RETRIABLE_CONNECTION_ERRORS as e:
                    if attempt < max_retries:
                        delay = _ACP_PROMPT_RETRY_DELAYS[
                            min(attempt, len(_ACP_PROMPT_RETRY_DELAYS) - 1)
                        ]
                        logger.warning(
                            "ACP prompt failed with retriable error (attempt %d/%d), "
                            "retrying in %.0fs: %s",
                            attempt + 1,
                            max_retries + 1,
                            delay,
                            e,
                        )
                        time.sleep(delay)
                        self._cancel_inflight_tool_calls()
                        self._reset_client_for_turn(
                            on_token, on_event, prompt_blocks, mask
                        )
                    else:
                        raise
                except ACPRequestError as e:
                    # Retry transient server errors (e.g. "Internal Server
                    # Error" from Gemini).  These are JSON-RPC -32603 errors
                    # that indicate a server-side failure, not a client bug.
                    if (
                        e.code in _RETRIABLE_SERVER_ERROR_CODES
                        and attempt < max_retries
                    ):
                        delay = _ACP_PROMPT_RETRY_DELAYS[
                            min(attempt, len(_ACP_PROMPT_RETRY_DELAYS) - 1)
                        ]
                        logger.warning(
                            "ACP prompt failed with server error (attempt %d/%d), "
                            "retrying in %.0fs: [%d] %s",
                            attempt + 1,
                            max_retries + 1,
                            delay,
                            e.code,
                            e,
                        )
                        time.sleep(delay)
                        self._cancel_inflight_tool_calls()
                        self._reset_client_for_turn(
                            on_token, on_event, prompt_blocks, mask
                        )
                    else:
                        raise

            elapsed = time.monotonic() - t0
            logger.info("ACP prompt returned in %.1fs", elapsed)

            session_id = self._session_id or ""
            usage_update = self._client.pop_turn_usage_update(session_id)
            self._record_usage(
                response,
                session_id,
                elapsed=elapsed,
                usage_update=usage_update,
            )
            self._verify_served_model_for_turn()

            # ACPToolCallEvents were already emitted live from
            # _OpenHandsACPBridge.session_update as each ToolCallStart /
            # ToolCallProgress notification arrived — no end-of-turn fan-out
            # here. FinishAction closes out the turn below.

            # Build response message
            response_text = "".join(self._client.accumulated_text)
            thought_text = "".join(self._client.accumulated_thoughts)

            if not response_text:
                response_text = "(No response from ACP server)"

            # ACP step() boundaries are full remote assistant turns, not
            # partial planning steps. Emit FinishAction to delimit that
            # completed turn for eval/remote consumers, matching #2190.
            finish_action = FinishAction(message=response_text)
            tc_id = str(uuid.uuid4())
            action_event = ActionEvent(
                source="agent",
                thought=[],
                reasoning_content=thought_text or None,
                action=finish_action,
                tool_name="finish",
                tool_call_id=tc_id,
                tool_call=MessageToolCall(
                    id=tc_id,
                    name="finish",
                    arguments=json.dumps({"message": response_text}),
                    origin="completion",
                ),
                llm_response_id=str(uuid.uuid4()),
            )
            on_event(action_event)
            on_event(
                ObservationEvent(
                    observation=FinishObservation.from_text(text=response_text),
                    action_id=action_event.id,
                    tool_name="finish",
                    tool_call_id=tc_id,
                )
            )

            state.execution_status = ConversationExecutionStatus.FINISHED

        except TimeoutError:
            self._client.discard_turn_model_evidence()
            elapsed = time.monotonic() - t0
            logger.error(
                "ACP prompt timed out after %.1fs (limit=%.0fs). "
                "The ACP server may have completed its work but failed to "
                "send the JSON-RPC response. Accumulated %d text chunks, "
                "%d tool calls.",
                elapsed,
                self.acp_prompt_timeout,
                len(self._client.accumulated_text),
                len(self._client.accumulated_tool_calls),
            )
            error_message = Message(
                role="assistant",
                content=[
                    TextContent(
                        text=(
                            f"ACP prompt timed out after {elapsed:.0f}s. "
                            "The agent may have completed its work but "
                            "the response was not received."
                        )
                    )
                ],
            )
            # Close any tool cards left in flight from the timed-out attempt.
            self._cancel_inflight_tool_calls()
            on_event(MessageEvent(source="agent", llm_message=error_message))
            state.execution_status = ConversationExecutionStatus.ERROR
        except Exception as e:
            self._client.discard_turn_model_evidence()
            logger.error("ACP prompt failed: %s", e, exc_info=True)
            error_str = str(e)

            # Close any tool cards left in flight before surfacing the error.
            self._cancel_inflight_tool_calls()

            # Emit error as an agent message (existing behavior, preserved for
            # consumers that inspect MessageEvents)
            error_message = Message(
                role="assistant",
                content=[TextContent(text=f"ACP error: {e}")],
            )
            on_event(MessageEvent(source="agent", llm_message=error_message))

            # Emit typed ConversationErrorEvent so RemoteConversation can
            # report the actual error detail via _get_last_error_detail()
            # instead of falling back to "Remote conversation ended with error"
            is_aup = (
                "usage policy" in error_str.lower()
                or "content policy" in error_str.lower()
            )
            on_event(
                ConversationErrorEvent(
                    source="agent",
                    code="UsagePolicyRefusal" if is_aup else "ACPPromptError",
                    detail=error_str[:500],
                )
            )

            state.execution_status = ConversationExecutionStatus.ERROR

            # Re-raise so LocalConversation.run()'s outer except handler
            # breaks the loop, emits ConversationErrorEvent, and raises
            # ConversationRunError — matching how the regular Agent works
            raise
        finally:
            self._clear_turn_callbacks()

    def ask_agent(self, question: str) -> str | None:
        """Fork the ACP session, prompt the fork, and return the response."""
        if self._conn is None:
            msg = "ACPAgent has no ACP connection; call init_state() first"
            raise RuntimeError(msg)
        if self._session_id is None:
            msg = "ACPAgent has no session ID; call init_state() first"
            raise RuntimeError(msg)

        client = self._client

        async def _fork_and_prompt() -> str:
            fork_response = await self._conn.fork_session(
                cwd=self._working_dir,
                session_id=self._session_id,
            )
            fork_session_id = fork_response.session_id

            client._fork_session_id = fork_session_id
            client._fork_accumulated_text.clear()
            try:
                fork_t0 = time.monotonic()
                usage_sync = client.prepare_usage_sync(fork_session_id)
                client.begin_turn_model_evidence(fork_session_id)
                try:
                    response = await self._conn.prompt(
                        [text_block(question)],
                        fork_session_id,
                    )
                except BaseException:
                    client.discard_turn_model_evidence()
                    raise
                client.finish_turn_model_evidence(fork_session_id)
                if client.get_turn_usage_update(fork_session_id) is None:
                    try:
                        await asyncio.wait_for(
                            usage_sync.wait(), timeout=_USAGE_UPDATE_TIMEOUT
                        )
                    except TimeoutError:
                        logger.warning(
                            "UsageUpdate not received within %.1fs for fork session %s",
                            _USAGE_UPDATE_TIMEOUT,
                            fork_session_id,
                        )
                fork_elapsed = time.monotonic() - fork_t0

                result = "".join(client._fork_accumulated_text)
                usage_update = client.pop_turn_usage_update(fork_session_id)
                self._record_usage(
                    response,
                    fork_session_id,
                    elapsed=fork_elapsed,
                    usage_update=usage_update,
                )
                self._verify_served_model_for_turn()
                return result
            finally:
                client.discard_turn_model_evidence()
                client._fork_session_id = None
                client._fork_accumulated_text.clear()

        with client._fork_lock:
            return self._executor.run_async(_fork_and_prompt)

    def assume_runtime_ownership_from(self, predecessor: ACPAgent) -> None:
        """Take sole cleanup ownership after a shallow :meth:`model_copy` handoff."""
        if predecessor._closed:
            return
        if not (
            predecessor.has_live_acp_session or predecessor._has_runtime_resources()
        ):
            return
        saved_atexit = self._atexit_callback
        try:
            self._register_atexit_cleanup(replace=True)
            self._bind_file_credential_masking()
            predecessor.release_runtime()
        except Exception:
            if self._atexit_callback is not saved_atexit:
                self._unregister_atexit_cleanup()
                self._atexit_callback = saved_atexit
                if saved_atexit is not None:
                    atexit.register(saved_atexit)
            raise

    def set_acp_model(self, model: str) -> str:
        """Switch the model on the running ACP session (mid-conversation).

        Returns:
            The verified effective model id reported by the ACP server/session.
        """
        if not model or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not self.has_live_acp_session:
            raise RuntimeError(
                "ACP session is not initialized; the model can only be switched "
                "after the conversation has started (first run())."
            )
        provider = detect_acp_provider_by_agent_name(self._agent_name)
        if provider is not None and not provider.supports_runtime_model_switch:
            raise ValueError(
                f"ACP provider '{provider.key}' does not support runtime model "
                "switching."
            )
        qualified_exact_model = _is_qualified_claude_exact_model(
            self._agent_name,
            self._startup_adapter_version,
            model,
        )
        if _is_claude_exact_model_request(self._agent_name, model) and not (
            qualified_exact_model
        ):
            raise ValueError(
                "Claude exact-model switching is not qualified for this adapter "
                f"version (requested={model!r}, "
                f"adapter_version={self._startup_adapter_version!r})."
            )
        if qualified_exact_model and self.acp_permission_policy != "read_only":
            raise ValueError(
                "Qualified Claude exact-model execution requires a proven "
                "read_only session before prompting."
            )
        assert self._conn is not None
        assert self._session_id is not None
        conn = self._conn
        session_id = self._session_id
        claude_read_only_config_boundary = (
            provider is not None
            and provider.key == "claude-code"
            and self._startup_adapter_version == CLAUDE_AGENT_ACP_VERSION
            and self.acp_permission_policy == "read_only"
        )
        claude_client = self._client if claude_read_only_config_boundary else None
        mode_generation_before = 0
        if claude_read_only_config_boundary:
            assert provider is not None
            if claude_client is None:
                raise RuntimeError(
                    "ACPAgent has no live client for Claude mode re-proof"
                )
            # Capture the generation before the model write so a fresh mode
            # notification cannot be mistaken for an old confirmation.
            mode_generation_before = claude_client.get_current_mode_generation(
                session_id
            )
        try:
            effective_model = self._executor.run_async(
                _apply_acp_model,
                conn,
                session_id,
                model,
                agent_name=self._agent_name,
                via_config_option=self._model_via_config_option,
                client=self._client,
                accept_routing_selector=qualified_exact_model,
                timeout=self.acp_prompt_timeout,
            )
        except TimeoutError:
            if claude_read_only_config_boundary:
                self._restart_session_on_next_turn = True
            raise
        except ACPSessionModelError as e:
            if claude_read_only_config_boundary:
                self._restart_session_on_next_turn = True
            method = (
                "set_config_option(model)"
                if self._model_via_config_option
                else "set_session_model"
            )
            raise ValueError(
                f"ACP server could not verify {method}(model={model!r}): {e}"
            ) from e
        except ACPRequestError as e:
            if claude_read_only_config_boundary:
                self._restart_session_on_next_turn = True
            if e.code in _RETRIABLE_SERVER_ERROR_CODES:
                raise
            method = (
                "set_config_option(model)"
                if self._model_via_config_option
                else "set_session_model"
            )
            raise ValueError(
                f"ACP server rejected {method}(model={model!r}): {e}"
            ) from e
        except Exception:
            if claude_read_only_config_boundary:
                self._restart_session_on_next_turn = True
            raise

        # A model config write is a post-mode configuration boundary.  On the
        # qualified Claude adapter, re-prove the independently enforced
        # read_only mode from the same complete response state before binding a
        # new requested model to the next turn.
        if claude_read_only_config_boundary:
            assert claude_client is not None
            config_options = claude_client.get_config_options(session_id)
            try:
                _verify_read_only_mode_after_config(
                    claude_client,
                    policy=self.acp_permission_policy,
                    required_mode_id=self._session_mode_id,
                    agent_name=self._agent_name,
                    session_id=session_id,
                    config_options=config_options,
                    provider_key="claude-code",
                    adapter_version=self._startup_adapter_version,
                    mode_generation_before=mode_generation_before,
                    require_fresh_mode=True,
                    config_options_authoritative=config_options is not None,
                )
            except ACPSessionModeError:
                # The provider has already received the model write, so do not
                # let a caller ignore a failed re-proof and prompt the now
                # unqualified session. Restarting before the next turn also
                # prevents the old model binding from being reused silently.
                self._restart_session_on_next_turn = True
                raise

        # A runtime switch starts a new provenance binding. The protocol
        # response may update the selector, but it is not a substitute for the
        # next turn's served-model evidence on the qualified Claude path.
        self._requested_model_id = model
        self._runtime_model_override_active = True
        self._post_turn_model_verification_required = qualified_exact_model
        self._served_model_id = None
        published_model = model if qualified_exact_model else effective_model
        self.llm.model = published_model
        self.llm.metrics.model_name = published_model
        if self.llm.metrics.accumulated_token_usage is not None:
            self.llm.metrics.accumulated_token_usage.model = published_model
        self._current_model_id = effective_model
        logger.info(
            "Switched ACP session model to %s (provider=%s, session=%s)",
            effective_model,
            provider.key if provider else "unknown",
            _fingerprint_session_id(self._session_id),
        )
        return effective_model

    def close(self) -> None:
        """Terminate the ACP subprocess and clean up resources."""
        with self._file_credential_close_lock:
            with self._file_credential_lock:
                if self._closed and not self._file_credential_lifecycles:
                    return
                self._closed = True
            failures = self._shutdown_runtime(discard_bindings=True)
            if not self._has_runtime_resources():
                self._unregister_atexit_cleanup()
            self._raise_first_file_credential_failure("close", failures)

    def _cleanup(self) -> None:
        failures = self._shutdown_runtime(discard_bindings=False)
        self._raise_first_file_credential_failure("restart", failures)

    def _shutdown_runtime(self, *, discard_bindings: bool) -> dict[str, Exception]:
        failures: dict[str, Exception] = {}
        if self._conn is not None and self._executor is not None:
            conn = self._conn
            try:
                self._executor.run_async(
                    _close_acp_connection,
                    conn,
                    timeout=_ACP_RUNTIME_SHUTDOWN_TIMEOUT,
                )
            except Exception as e:
                logger.debug("Error closing ACP connection: %s", e)
            self._conn = None

        process = self._process
        if process is not None:
            try:
                if process.returncode is None or not isinstance(
                    process.returncode, int
                ):
                    process.terminate()
                if self._executor is not None:
                    self._executor.run_async(
                        self._wait_for_process,
                        process,
                        timeout=_ACP_RUNTIME_SHUTDOWN_TIMEOUT,
                    )
            except Exception as e:
                logger.debug("Error terminating ACP process: %s", e)
                try:
                    process.kill()
                    if self._executor is not None:
                        self._executor.run_async(
                            self._wait_for_process,
                            process,
                            timeout=_ACP_RUNTIME_SHUTDOWN_TIMEOUT,
                        )
                except Exception as kill_error:
                    logger.debug("Error killing ACP process: %s", kill_error)
            self._process = None

        for task_attr in ("_stdout_filter_task", "_stderr_capture_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                if self._executor is not None:
                    try:
                        self._executor.run_async(
                            self._await_cancelled_task,
                            task,
                            timeout=_ACP_RUNTIME_SHUTDOWN_TIMEOUT,
                        )
                    except Exception as e:
                        logger.debug("Error stopping %s: %s", task_attr, e)
                setattr(self, task_attr, None)

        credential_failures = self._release_file_credentials_collect()
        failures.update(credential_failures)
        self._cleanup_claude_config_runtime(discard=discard_bindings)
        if discard_bindings:
            with self._file_credential_lock:
                if not credential_failures:
                    self._file_credential_bindings = {}
            self._secret_registry_for_masking = None

        if self._executor is not None and not credential_failures:
            try:
                self._executor.close(timeout=_ACP_RUNTIME_SHUTDOWN_TIMEOUT)
            except Exception as e:
                failures["ACP executor"] = e
            self._executor = None
        return failures

    @staticmethod
    async def _wait_for_process(process: asyncio.subprocess.Process) -> None:
        wait = getattr(process, "wait", None)
        if wait is None:
            return
        if inspect.iscoroutinefunction(wait):
            awaitable: Any = wait()
        elif inspect.iscoroutine(wait) or asyncio.isfuture(wait):
            awaitable = wait
        else:
            # asyncio.subprocess.Process.wait is a coroutine function. Anything
            # else (including MagicMock.wait) is not a process wait — do not
            # drive it on the portal or a worker thread.
            return
        if not await _await_deadline(awaitable, _ACP_RUNTIME_SHUTDOWN_TIMEOUT):
            raise TimeoutError("ACP process did not exit")

    @staticmethod
    async def _await_cancelled_task(task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await _await_deadline(task, _ACP_RUNTIME_SHUTDOWN_TIMEOUT)

    def release_runtime(self) -> None:
        """Disarm this agent's finalizer after handing its live ACP runtime to a
        shallow :meth:`~pydantic.BaseModel.model_copy`.
        """
        with self._file_credential_close_lock:
            self._unregister_atexit_cleanup()
            with self._file_credential_lock:
                self._file_credential_lifecycles = {}
                self._file_credential_bindings = {}
                self._claude_config_runtime_dir = None
                self._claude_oauth_source_digest = None
                self._secret_registry_for_masking = None
                self._closed = True

    def __del__(self) -> None:
        try:
            has_resources = self._has_runtime_resources()
        except Exception:
            return
        if not has_resources:
            return
        try:
            threading.Thread(
                target=self._finalize,
                name="acp-agent-finalizer",
                daemon=True,
            ).start()
        except Exception:
            self._finalize()

    def _finalize(self) -> None:
        try:
            conn = getattr(self, "_conn", None)
            process = getattr(self, "_process", None)
            if isinstance(conn, Mock) or isinstance(process, Mock):
                # Test doubles are not a live ACP subprocess. close() would
                # schedule MagicMock I/O on the portal from this destructor
                # thread and can deadlock unittest.mock's process-wide lock
                # against another agent's in-flight _init.
                self._conn = None
                self._process = None
                self._closed = True
                return
            self.close()
        except Exception:
            logger.warning("Failed to finalize ACPAgent resources", exc_info=True)
