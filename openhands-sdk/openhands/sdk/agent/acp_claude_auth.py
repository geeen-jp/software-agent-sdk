from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from openhands.sdk.agent.acp_file_credentials import write_secret_file
from openhands.sdk.conversation.secret_registry import SecretRegistry
from openhands.sdk.credential import CredentialNeedsReauthentication
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

CLAUDE_CREDENTIALS_FILENAME = ".credentials.json"
CLAUDE_CREDENTIALS_SECRET_NAME = "CLAUDE_CREDENTIALS_JSON"
CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CLAUDE_OAUTH_ENV_NAMES = frozenset(
    {
        CLAUDE_CREDENTIALS_SECRET_NAME,
        CLAUDE_OAUTH_TOKEN_ENV,
    }
)

# PAYG / proxy / cloud-provider vars that must not outrank an active
# Claude subscription/OAuth channel.
CLAUDE_PAYG_CONFLICTING_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


class ClaudeOAuthSeedResult(NamedTuple):
    seeded: bool
    source_digest: str | None

    def __bool__(self) -> bool:
        return self.seeded


def is_valid_claude_oauth_credentials(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    refresh = oauth.get("refreshToken")
    access = oauth.get("accessToken")
    return (
        isinstance(refresh, str)
        and bool(refresh)
        and isinstance(access, str)
        and bool(access)
    )


def claude_oauth_source_digest(credentials: str) -> str:
    return hashlib.sha256(credentials.encode("utf-8")).hexdigest()


def claude_oauth_source_root(
    *,
    source_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve Claude OAuth files without freezing the user home at import time."""
    if source_root is not None:
        return source_root
    env = os.environ if environ is None else environ
    config_dir = env.get(CLAUDE_CONFIG_DIR_ENV)
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".claude"


def _read_credentials_file(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return value if is_valid_claude_oauth_credentials(value) else None


def track_claude_oauth_credentials_for_masking(
    secret_registry: SecretRegistry,
    credentials: str,
) -> None:
    """Track file- and secret-origin OAuth material for ACP output masking.

    ``SecretRegistry.get_secret_value`` only records the registered secret
    blob. Isolated Claude auth also reads ``.credentials.json``, and subprocess
    output may leak ``accessToken`` / ``refreshToken`` without the wrapping
    JSON. Track both the blob and those token substrings. The source file is
    not modified.
    """
    values: dict[str, str] = {}
    digest = claude_oauth_source_digest(credentials) if credentials else "empty"
    if credentials:
        values[f"{CLAUDE_CREDENTIALS_SECRET_NAME}.{digest}.json"] = credentials
    try:
        payload = json.loads(credentials)
    except (TypeError, ValueError):
        payload = None
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if isinstance(oauth, dict):
        for field in ("accessToken", "refreshToken"):
            token = oauth.get(field)
            if isinstance(token, str) and token:
                values[f"{CLAUDE_CREDENTIALS_SECRET_NAME}.{digest}.{field}"] = token
    secret_registry.track_exported_values(values)


def track_claude_oauth_credentials_from_file(
    secret_registry: SecretRegistry,
    path: Path,
) -> None:
    """Mask currently isolated Claude OAuth material, including runtime rotation.

    Read-time only: no polling and no credential contents in logs.
    """
    credentials = _read_credentials_file(path)
    if credentials is None:
        return
    track_claude_oauth_credentials_for_masking(secret_registry, credentials)


def create_claude_config_runtime_dir() -> Path:
    runtime_dir = Path(tempfile.mkdtemp(prefix="openhands-claude-acp-"))
    runtime_dir.chmod(0o700)
    return runtime_dir


def unlink_claude_oauth_credentials(directory: Path) -> None:
    target = directory / CLAUDE_CREDENTIALS_FILENAME
    try:
        target.unlink(missing_ok=True)
    except OSError:
        logger.debug("Isolated Claude OAuth credentials file could not be removed")


def discard_claude_oauth_runtime_dir(directory: Path) -> None:
    unlink_claude_oauth_credentials(directory)
    shutil.rmtree(directory, ignore_errors=True)


def resolve_claude_oauth_credentials(
    secret_registry: SecretRegistry,
    *,
    secret_name: str = CLAUDE_CREDENTIALS_SECRET_NAME,
    source_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    explicit_credentials: str | None = None,
) -> str:
    if isinstance(explicit_credentials, str) and is_valid_claude_oauth_credentials(
        explicit_credentials
    ):
        track_claude_oauth_credentials_for_masking(
            secret_registry, explicit_credentials
        )
        return explicit_credentials

    secret_value = secret_registry.get_secret_value(secret_name)
    if secret_value and is_valid_claude_oauth_credentials(secret_value):
        track_claude_oauth_credentials_for_masking(secret_registry, secret_value)
        return secret_value

    config_root = claude_oauth_source_root(source_root=source_root, environ=environ)
    file_value = _read_credentials_file(config_root / CLAUDE_CREDENTIALS_FILENAME)
    if file_value is not None:
        track_claude_oauth_credentials_for_masking(secret_registry, file_value)
        return file_value

    raise CredentialNeedsReauthentication(
        "Claude subscription/OAuth credentials are missing or invalid. "
        "Sign in with Claude Code before starting an isolated session."
    )


def _explicit_registry_credentials(secret_registry: SecretRegistry) -> str | None:
    secret_value = secret_registry.get_secret_value(CLAUDE_CREDENTIALS_SECRET_NAME)
    if secret_value and is_valid_claude_oauth_credentials(secret_value):
        return secret_value
    return None


def seed_claude_oauth_credentials(
    data_dir: Path,
    secret_registry: SecretRegistry,
    *,
    replace_existing: bool = False,
    required: bool = True,
    source_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    last_source_digest: str | None = None,
    explicit_credentials: str | None = None,
) -> ClaudeOAuthSeedResult:
    """Copy Claude OAuth state into an isolated config directory when available.

    Returns whether a valid OAuth credential file is present afterwards, plus
    the digest of the last *source* credentials (registry/host), not the
    possibly rotated isolated copy.

    When *required* is False, missing OAuth is not an error so writable
    sessions can keep using isolated config plus environment authentication.
    Explicit registry/supplied credentials replace a pre-existing isolated
    copy; an unchanged source leaves runtime-rotated tokens in place.
    """
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = data_dir / CLAUDE_CREDENTIALS_FILENAME
    existing = _read_credentials_file(target) if target.is_file() else None

    supplied = explicit_credentials
    if not is_valid_claude_oauth_credentials(supplied):
        supplied = _explicit_registry_credentials(secret_registry)

    if isinstance(supplied, str) and is_valid_claude_oauth_credentials(supplied):
        digest = claude_oauth_source_digest(supplied)
        source_changed = last_source_digest is not None and last_source_digest != digest
        should_write = replace_existing or existing is None or source_changed
        if should_write:
            write_secret_file(target, supplied)
            existing = supplied
        if existing is not None:
            track_claude_oauth_credentials_for_masking(secret_registry, existing)
        logger.info("Claude OAuth credentials are present in the isolated config")
        return ClaudeOAuthSeedResult(True, digest)

    if existing is not None and not replace_existing:
        track_claude_oauth_credentials_for_masking(secret_registry, existing)
        logger.info("Claude OAuth credentials already present in isolated config")
        return ClaudeOAuthSeedResult(True, last_source_digest)

    try:
        credentials = resolve_claude_oauth_credentials(
            secret_registry,
            source_root=source_root,
            environ=environ,
        )
    except CredentialNeedsReauthentication:
        if required:
            raise
        logger.info(
            "Claude OAuth credentials were not found; continuing with an "
            "isolated config directory and environment authentication."
        )
        return ClaudeOAuthSeedResult(False, last_source_digest)

    digest = claude_oauth_source_digest(credentials)
    write_secret_file(target, credentials)
    track_claude_oauth_credentials_for_masking(secret_registry, credentials)
    logger.info("Seeded Claude OAuth credentials into isolated config")
    return ClaudeOAuthSeedResult(True, digest)


def claude_subscription_auth_active(
    env: Mapping[str, str],
    *,
    oauth_file_channel: bool,
) -> bool:
    """True when Claude subscription/OAuth must take precedence over PAYG."""
    if oauth_file_channel:
        return True
    token = env.get(CLAUDE_OAUTH_TOKEN_ENV)
    if isinstance(token, str) and token.strip():
        return True
    return is_valid_claude_oauth_credentials(env.get(CLAUDE_CREDENTIALS_SECRET_NAME))
