from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from pathlib import Path
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
        # Cursor's ACP ``ask`` mode disables edits/command execution.  The
        # command identity is recognized only for the exact cursor-agent
        # binary; arbitrary custom ACP commands remain fail-closed.
        "cursor": "ask",
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

    pinned codex-acp 2.1.0 applies ``set_session_mode`` in-memory and does not
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


def _tool_call_field(tool_call: object, *names: str) -> object | None:
    for name in names:
        if isinstance(tool_call, Mapping):
            value = tool_call.get(name)
        else:
            value = getattr(tool_call, name, None)
        if value is not None:
            return value
    return None


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


def _allow_option_id(options: list[object]) -> str | None:
    for option in options:
        if _permission_option_kind(option) == "allow_once":
            option_id = _permission_option_id(option)
            if option_id is not None:
                return option_id
    return None


_READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {"ls", "cat", "head", "tail", "wc", "pwd", "rg", "grep"}
)
_READ_ONLY_GIT_COMMANDS: frozenset[str] = frozenset(
    {"ls-files", "log", "show", "diff", "status"}
)
_FIND_SIDE_EFFECT_OPTIONS: frozenset[str] = frozenset(
    {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    }
)
_GIT_SIDE_EFFECT_OPTIONS: frozenset[str] = frozenset(
    {
        "-o",
        "-O",
        "--output",
        "--ext-diff",
        "--textconv",
        "--extcmd",
        "--open-files-in-pager",
        "--no-index",
        "--paginate",
        "--help",
        "-h",
    }
)
_READ_ONLY_COMMAND_GUIDANCE = (
    "Read-only shell policy: inspection commands may be combined with `;` or `&&` "
    "and with one or more `|` pipes. `cd` may target an existing directory inside "
    "the workspace. Start Git commands with `git --no-pager`; `git diff`, `git "
    "log`, and `git show` also require `--no-ext-diff --no-textconv` immediately "
    "after the subcommand. Start ripgrep with `rg --no-config`. Use only "
    "read-only inspection commands, and do not use redirects, command "
    "substitution, background jobs, or shell globs. If a command is denied, "
    "rewrite it to follow these rules and retry."
)


def _is_git_status_verbose_option(arg: str) -> bool:
    if arg.startswith("--"):
        option = arg[2:].partition("=")[0].removeprefix("no-")
        return bool(option) and "verbose".startswith(option)
    return arg.startswith("-") and "v" in arg[1:]


def _read_only_simple_command_denial_reason(command: str) -> str | None:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return "Use balanced shell quotes around command arguments."
    if not argv:
        return "Each command in a read-only command chain must be non-empty."

    executable, *args = argv
    if executable in _READ_ONLY_COMMANDS:
        if executable == "tail" and any(
            arg in {"-f", "-F", "--follow"}
            or arg.startswith("--follow=")
            or (
                arg.startswith("-")
                and not arg.startswith("--")
                and ("f" in arg[1:] or "F" in arg[1:])
            )
            for arg in args
        ):
            return "Do not use follow mode with `tail` in a read-only command."
        if executable == "rg":
            # Require this before any pattern/path so it cannot be mistaken
            # for an operand. It disables RIPGREP_CONFIG_PATH as well as the
            # default config file for this invocation.
            if not args or args[0] != "--no-config":
                return "Start ripgrep commands with `rg --no-config`."
            if any(
                arg
                in {
                    "-z",
                    "--search-zip",
                    "--pre",
                    "--pre-glob",
                    "--hostname-bin",
                    "--config",
                }
                or arg.startswith("--pre=")
                or arg.startswith("--pre-glob=")
                or arg.startswith("--hostname-bin=")
                or arg.startswith("--config=")
                or (arg.startswith("-") and not arg.startswith("--") and "z" in arg[1:])
                for arg in args[1:]
            ):
                return (
                    "Do not use ripgrep options that load config or run preprocessors."
                )
        return None

    if executable == "find":
        if any(
            arg in _FIND_SIDE_EFFECT_OPTIONS
            or any(arg.startswith(f"{option}=") for option in _FIND_SIDE_EFFECT_OPTIONS)
            for arg in args
        ):
            return (
                "Do not use `find` actions that execute commands, delete, or "
                "write files."
            )
        return None

    if executable == "git":
        # A command-configured pager can run an arbitrary program when git's
        # output is attached to a terminal. Keep this global option before the
        # subcommand so Git applies it to every approved inspection command.
        if not args or args[0] != "--no-pager":
            return (
                "Start Git commands with `git --no-pager` to disable configured pagers."
            )
        if len(args) < 2:
            return "Use an approved inspection subcommand after `git --no-pager`."
        subcommand, *git_args = args[1:]
        if subcommand not in _READ_ONLY_GIT_COMMANDS:
            return (
                "Use Git inspection subcommands: ls-files, log, show, diff, or status."
            )
        if subcommand == "status":
            # `git status -v` and `-vv` emit staged and working-tree patches,
            # which can run external diff and textconv helpers. Git accepts
            # abbreviated long options and clustered short options, so reject
            # every spelling of the verbose flag before the pathspec delimiter.
            status_options = (
                git_args[: git_args.index("--")] if "--" in git_args else git_args
            )
            if any(_is_git_status_verbose_option(arg) for arg in status_options):
                return "Do not use verbose Git status options in a read-only command."
        if subcommand in {"diff", "log", "show"}:
            # These commands can invoke diff.external / GIT_EXTERNAL_DIFF and
            # configured textconv drivers. Require the disabling flags
            # immediately after the subcommand, before any pathspec delimiter.
            if git_args[:2] != ["--no-ext-diff", "--no-textconv"]:
                return (
                    f"Use `git --no-pager {subcommand} --no-ext-diff "
                    "--no-textconv ...` "
                    "to disable external diff and textconv helpers."
                )
            git_args = git_args[2:]
        if any(
            arg in _GIT_SIDE_EFFECT_OPTIONS
            or arg.startswith("--output=")
            or arg.startswith("--ext-diff=")
            or arg.startswith("--textconv=")
            or arg.startswith("--extcmd=")
            or arg.startswith("--open-files-in-pager=")
            or (
                arg.startswith("-")
                and not arg.startswith("--")
                and arg[1:2] in {"o", "O"}
            )
            for arg in git_args
        ):
            return "Do not use Git options that write output or invoke external tools."
        return None

    return (
        "Use only read-only inspection commands such as ls, cat, head, tail, wc, "
        "pwd, rg, grep, find, and the approved Git inspection subcommands."
    )


def _has_unquoted_shell_expansion(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "*?[]{}~":
            return True
    return False


def _split_read_only_command(
    command: str,
) -> list[tuple[tuple[str, ...], str | None]] | None:
    """Split the supported shell subset into command pipelines."""
    groups: list[tuple[tuple[str, ...], str | None]] = []
    stages: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    last_separator: str | None = None
    separator_before: str | None = None
    index = 0

    while index < len(command):
        char = command[index]
        if char in "\n\r":
            return None
        if quote == '"' and char in {"$", "`"}:
            return None
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in {"$", "`", ">", "<", "(", ")"}:
            return None
        if char not in {"|", ";", "&"}:
            index += 1
            continue

        stage = command[start:index].strip()
        if not stage:
            return None
        stages.append(stage)
        if char == "|":
            if index + 1 < len(command) and command[index + 1] == "|":
                return None
            last_separator = "|"
            index += 1
        elif char == "&":
            if index + 1 >= len(command) or command[index + 1] != "&":
                return None
            groups.append((tuple(stages), separator_before))
            stages = []
            last_separator = "&&"
            separator_before = "&&"
            index += 2
        else:
            groups.append((tuple(stages), separator_before))
            stages = []
            last_separator = ";"
            separator_before = ";"
            index += 1
        start = index

    if quote is not None:
        return None
    final_stage = command[start:].strip()
    if final_stage:
        stages.append(final_stage)
        groups.append((tuple(stages), separator_before))
        return groups
    if last_separator == ";" and groups:
        return groups
    return None


def _cd_target(
    argv: list[str],
    *,
    workspace_root: str | None,
    workspace_home: str | None,
    current_directory: Path | None,
) -> tuple[Path | None, str | None]:
    if workspace_root is None:
        return None, "`cd` is allowed only when the workspace path is known."
    if len(argv) > 2 or (len(argv) == 2 and argv[1].startswith("-")):
        return None, "Use `cd` with no argument or one directory inside the workspace."

    try:
        root = Path(os.path.abspath(workspace_root))
        resolved_root = root.resolve()
        if len(argv) == 1:
            home = Path(workspace_home or os.path.expanduser("~"))
            target = home if home.is_absolute() else (current_directory or root) / home
        else:
            path = Path(argv[1])
            base = current_directory or root
            target = path if path.is_absolute() else base / path
        # Bash tracks PWD logically by default. Normalize `..` without resolving
        # symlinks so later relative cd commands follow the same path Bash uses.
        target = Path(os.path.abspath(target))
        resolved_target = target.resolve()
    except (OSError, RuntimeError):
        return None, "The `cd` target must resolve to a directory inside the workspace."
    if not resolved_target.is_relative_to(resolved_root):
        return None, "The `cd` target must stay inside the workspace."
    if not target.is_dir():
        return (
            None,
            "The `cd` target must be an existing directory inside the workspace.",
        )
    return target, None


def _read_only_command_denial_reason(
    command: object,
    *,
    workspace_root: str | None = None,
    workspace_home: str | None = None,
) -> str | None:
    if isinstance(command, list):
        if not command or not all(isinstance(part, str) for part in command):
            return "Provide a non-empty read-only shell command."
        command = shlex.join(command)
    if not isinstance(command, str) or not command:
        return "Provide a non-empty read-only shell command."
    if _has_unquoted_shell_expansion(command):
        return "Do not use shell glob expansion in a read-only command."
    groups = _split_read_only_command(command)
    if groups is None:
        return (
            "Combine commands only with `;`, `&&`, and `|`. Redirects, command "
            "substitution, background jobs, newlines, and other shell syntax are "
            "not allowed."
        )

    initial_directory = (
        Path(os.path.abspath(workspace_root)) if workspace_root is not None else None
    )
    # Each state records the shell's logical directory and whether the previous
    # command in the current && list may have succeeded. Keeping both states
    # preserves the fact that a failed left-hand command skips every remaining
    # command in that && list, including cd commands.
    directory_states: set[tuple[Path | None, bool]] = {(initial_directory, True)}
    for stages, separator_before in groups:
        if separator_before == "&&":
            executable_directories = {
                directory
                for directory, chain_active in directory_states
                if chain_active
            }
            next_states = {
                (directory, False)
                for directory, chain_active in directory_states
                if not chain_active
            }
        else:
            executable_directories = {directory for directory, _ in directory_states}
            next_states: set[tuple[Path | None, bool]] = set()

        if not executable_directories:
            directory_states = next_states
            continue

        parsed_stages: list[list[str]] = []
        for stage in stages:
            try:
                argv = shlex.split(stage, posix=True)
            except ValueError:
                return "Use balanced shell quotes around command arguments."
            parsed_stages.append(argv)

        if len(stages) > 1 and any(argv and argv[0] == "cd" for argv in parsed_stages):
            return (
                "Use `cd` as a standalone command before a `;` or `&&`, not in a pipe."
            )

        if len(stages) == 1 and parsed_stages[0] and parsed_stages[0][0] == "cd":
            for current_directory in executable_directories:
                if workspace_root is None:
                    return "`cd` is allowed only when the workspace path is known."
                target, denial_reason = _cd_target(
                    parsed_stages[0],
                    workspace_root=workspace_root,
                    workspace_home=workspace_home,
                    current_directory=current_directory,
                )
                if denial_reason is not None:
                    return denial_reason
                next_states.add((target, True))
        else:
            for stage in stages:
                denial_reason = _read_only_simple_command_denial_reason(stage)
                if denial_reason is not None:
                    return denial_reason
            # Inspection commands can fail or succeed, which determines whether
            # the next && group executes. They do not change the shell directory.
            for directory in executable_directories:
                next_states.add((directory, True))
                next_states.add((directory, False))

        directory_states = next_states
    return None


def _read_only_tool_call_denial_reason(
    tool_call: object,
    *,
    workspace_root: str | None = None,
    workspace_home: str | None = None,
) -> str | None:
    kind = _tool_call_field(tool_call, "kind")
    if kind == "read" or kind == "search":
        return None
    if kind != "execute":
        return "Only read, search, and approved read-only execute requests are allowed."
    raw_input = _tool_call_field(tool_call, "raw_input", "rawInput")
    if not isinstance(raw_input, Mapping):
        return "The execute request did not provide a command to check."
    return _read_only_command_denial_reason(
        raw_input.get("command"),
        workspace_root=workspace_root,
        workspace_home=workspace_home,
    )


def _permission_log_target(tool_call: object) -> str:
    """Return a non-payload identifier for permission logs."""
    for attr in ("tool_call_id", "title", "kind"):
        value = _tool_call_field(tool_call, attr)
        if isinstance(value, str) and value.strip():
            return value
    return type(tool_call).__name__


def resolve_permission_response(
    policy: str,
    options: list[object],
    tool_call: object,
    *,
    workspace_root: str | None = None,
    workspace_home: str | None = None,
) -> RequestPermissionResponse:
    """Apply the conversation-local ACP permission policy to one request."""
    normalized = normalize_acp_permission_policy(policy)
    target = _permission_log_target(tool_call)
    if normalized == "read_only":
        denial_reason = _read_only_tool_call_denial_reason(
            tool_call,
            workspace_root=workspace_root,
            workspace_home=workspace_home,
        )
        if denial_reason is None:
            allow_option_id = _allow_option_id(options)
            if allow_option_id is not None:
                logger.info(
                    "ACP allowing read-only permission under read_only policy: "
                    "%s (option: %s)",
                    target,
                    allow_option_id,
                )
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(
                        outcome="selected",
                        option_id=allow_option_id,
                    ),
                )
            denial_reason = (
                "The ACP request did not offer an `allow_once` option for this "
                "read-only command."
            )
        deny_option_id = _deny_option_id(options)
        if deny_option_id is not None:
            logger.info(
                "ACP denying permission under read_only policy: %s (%s; option: %s)",
                target,
                denial_reason,
                deny_option_id,
            )
            return RequestPermissionResponse(
                field_meta={"openhands/denial_reason": denial_reason},
                outcome=AllowedOutcome(outcome="selected", option_id=deny_option_id),
            )
        logger.info(
            "ACP denying permission under read_only policy: %s (%s)",
            target,
            denial_reason,
        )
        return RequestPermissionResponse(
            field_meta={"openhands/denial_reason": denial_reason},
            outcome=DeniedOutcome(outcome="cancelled"),
        )

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
