from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, cast

from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
)

from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

ACPPermissionPolicy = Literal["writable", "read_only"]
_DEFAULT_ACP_PERMISSION_POLICY: ACPPermissionPolicy = "writable"
_SUPPORTED_ACP_PERMISSION_POLICIES: frozenset[str] = frozenset(
    {"writable", "read_only"}
)
_BYPASS_SESSION_MODES: frozenset[str] = frozenset(
    {"bypassPermissions", "agent-full-access", "yolo", "dontAsk"}
)
_READ_ONLY_SESSION_MODES: Mapping[str, str] = MappingProxyType(
    {
        "claude-code": "default",
        "codex": "read-only",
    }
)
_CODEX_INITIAL_AGENT_MODE_ENV = "INITIAL_AGENT_MODE"
_CODEX_MODE_CONFIG_OPTION_ID = "mode"
_PERMISSION_MODE_CONFIG_IDS: frozenset[str] = frozenset(
    {
        "mode",
        "permissionmode",
        "sessionmode",
    }
)


def normalize_acp_permission_policy(value: str | None) -> ACPPermissionPolicy:
    """Return a supported policy or raise before a read-only run can start."""
    if value is None:
        return _DEFAULT_ACP_PERMISSION_POLICY
    if value not in _SUPPORTED_ACP_PERMISSION_POLICIES:
        raise ValueError(
            f"Unsupported acp_permission_policy '{value}'. "
            f"Supported values: {sorted(_SUPPORTED_ACP_PERMISSION_POLICIES)}"
        )
    return cast(ACPPermissionPolicy, value)


def resolve_session_mode_for_policy(
    policy: str,
    *,
    provider_key: str | None,
    explicit_mode: str | None,
    default_session_mode: str | None,
) -> str | None:
    """Choose a session mode that cannot silently bypass the permission policy."""
    normalized = normalize_acp_permission_policy(policy)
    if normalized == "writable":
        return explicit_mode or default_session_mode
    if provider_key is None or provider_key not in _READ_ONLY_SESSION_MODES:
        raise ValueError(
            "acp_permission_policy='read_only' is not supported for provider "
            f"{provider_key!r}; refusing to start because write paths are unverified."
        )
    required_mode = _READ_ONLY_SESSION_MODES[provider_key]
    if explicit_mode is None or explicit_mode == required_mode:
        return required_mode
    if explicit_mode in _BYPASS_SESSION_MODES:
        raise ValueError(
            f"acp_session_mode={explicit_mode!r} bypasses permission prompts "
            "and cannot be combined with acp_permission_policy='read_only'."
        )
    raise ValueError(
        f"acp_session_mode={explicit_mode!r} is not a verified read_only "
        f"enforcement mode for {provider_key!r}."
    )


def initial_agent_mode_env_for_policy(
    policy: str,
    *,
    provider_key: str | None,
    mode_id: str | None,
) -> dict[str, str]:
    """Env that makes Codex advertise the verified read_only mode at session/new.

    pinned codex-acp 1.1.7 applies ``set_session_mode`` in-memory and does not
    emit ``current_mode_update``. ``INITIAL_AGENT_MODE`` is the adapter's
    documented initial-mode contract, so session/new can advertise
    ``current_mode_id=read-only`` for the existing advertise+confirm check.
    """
    if (
        normalize_acp_permission_policy(policy) != "read_only"
        or provider_key != "codex"
        or not mode_id
    ):
        return {}
    return {_CODEX_INITIAL_AGENT_MODE_ENV: mode_id}


def is_codex_mode_config_option(config_id: object) -> bool:
    """Return True for Codex ``MODE_CONFIG_ID`` (``mode``), not other mode ids."""
    return isinstance(config_id, str) and config_id == _CODEX_MODE_CONFIG_OPTION_ID


def _normalize_permission_mode_config_id(config_id: object) -> str | None:
    if not isinstance(config_id, str):
        return None
    normalized = "".join(ch for ch in config_id.lower() if ch.isalnum())
    return normalized or None


def is_permission_mode_config_option(config_id: object) -> bool:
    """Return True when *config_id* selects ACP permission/session mode."""
    normalized = _normalize_permission_mode_config_id(config_id)
    return normalized is not None and normalized in _PERMISSION_MODE_CONFIG_IDS


def assert_config_options_compatible_with_permission_policy(
    policy: str,
    requested: Mapping[str, str] | None,
) -> None:
    """Refuse config options that can replace the verified read_only session mode."""
    normalized = normalize_acp_permission_policy(policy)
    if normalized != "read_only" or not requested:
        return
    for config_id, value in requested.items():
        if not isinstance(config_id, str) or not config_id.strip():
            raise ValueError(
                f"Malformed acp_config_options id {config_id!r} cannot be "
                "combined with acp_permission_policy='read_only'."
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Malformed acp_config_options[{config_id!r}]={value!r} cannot "
                "be combined with acp_permission_policy='read_only'."
            )
        if is_permission_mode_config_option(config_id):
            raise ValueError(
                f"acp_config_options[{config_id!r}]={value!r} selects a "
                "permission/session mode and cannot be combined with "
                "acp_permission_policy='read_only'."
            )


def _option_str(option: object, name: str) -> str | None:
    value = getattr(option, name, None)
    return value if isinstance(value, str) and value else None


def _permission_option_id(option: object) -> str | None:
    if isinstance(option, PermissionOption):
        return option.option_id or None
    return _option_str(option, "option_id")


def _permission_option_kind(option: object) -> str | None:
    if isinstance(option, PermissionOption):
        kind = option.kind
        return kind if isinstance(kind, str) and kind else None
    return _option_str(option, "kind")


def _deny_option_id(options: list[object]) -> str | None:
    ranked: list[tuple[int, str]] = []
    for option in options:
        kind = _permission_option_kind(option)
        option_id = _permission_option_id(option)
        if option_id is None:
            continue
        if kind == "reject_always" or option_id == "reject_always":
            ranked.append((0, option_id))
        elif kind == "reject_once" or option_id == "reject_once":
            ranked.append((1, option_id))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _permission_log_target(tool_call: object) -> str:
    """Return a non-payload identifier for permission logs."""
    for attr in ("tool_call_id", "title", "kind"):
        value = getattr(tool_call, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(tool_call, Mapping):
        for key in ("tool_call_id", "title", "kind"):
            value = tool_call.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return type(tool_call).__name__


def resolve_permission_response(
    policy: str,
    options: list[object],
    tool_call: object,
) -> RequestPermissionResponse:
    """Apply the conversation-local ACP permission policy to one request."""
    normalized = normalize_acp_permission_policy(policy)
    target = _permission_log_target(tool_call)
    if normalized == "read_only":
        deny_option_id = _deny_option_id(options)
        if deny_option_id is not None:
            logger.info(
                "ACP denying permission under read_only policy: %s (option: %s)",
                target,
                deny_option_id,
            )
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=deny_option_id),
            )
        logger.info(
            "ACP denying permission under read_only policy: %s",
            target,
        )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    option_id = "allow_once"
    if options:
        candidate = _permission_option_id(options[0])
        if candidate is not None:
            option_id = candidate
    logger.info(
        "ACP auto-approving permission: %s (option: %s)",
        target,
        option_id,
    )
    return RequestPermissionResponse(
        outcome=AllowedOutcome(outcome="selected", option_id=option_id),
    )
