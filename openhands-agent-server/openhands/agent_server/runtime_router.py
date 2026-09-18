"""Runtime bootstrap readiness for the production-shaped Agent Server path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from openhands.agent_server.config import Config
from openhands.agent_server.dependencies import check_session_api_key
from openhands.agent_server.persistence import get_secrets_store
from openhands.sdk.agent.acp_claude_auth import (
    CLAUDE_CREDENTIALS_FILENAME,
    CLAUDE_CREDENTIALS_SECRET_NAME,
    CLAUDE_OAUTH_SOURCE_ROOT_ENV,
    is_valid_claude_oauth_credentials,
)
from openhands.sdk.agent.acp_file_credentials import (
    CODEX_AUTH_SECRET_NAME,
    CODEX_AUTH_SOURCE_ROOT_ENV,
    is_valid_codex_auth,
)


runtime_router = APIRouter(
    prefix="/runtime",
    tags=["Runtime"],
    dependencies=[Depends(check_session_api_key)],
)


class RuntimeReadinessResponse(BaseModel):
    """Safe, secret-free readiness information for a managed runtime."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"]
    agent_server_auth_mode: Literal["session-api-key", "none"]
    providers: dict[str, bool] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    build_git_sha: str


def _read_secret_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _source_value(env_name: str, filename: str) -> str | None:
    root = os.environ.get(env_name)
    if not root:
        return None
    return _read_secret_file(Path(root) / filename)


def _provider_readiness(config: Config) -> tuple[dict[str, bool], list[str]]:
    providers: dict[str, bool] = {}
    failures: list[str] = []
    store = get_secrets_store(config)

    claude = store.get_secret(CLAUDE_CREDENTIALS_SECRET_NAME)
    if not is_valid_claude_oauth_credentials(claude):
        claude = _source_value(
            CLAUDE_OAUTH_SOURCE_ROOT_ENV,
            CLAUDE_CREDENTIALS_FILENAME,
        )
    providers["claude-oauth"] = is_valid_claude_oauth_credentials(claude)
    if not providers["claude-oauth"]:
        failures.append("claude-oauth credential source is unavailable")

    codex = store.get_secret(CODEX_AUTH_SECRET_NAME)
    if not is_valid_codex_auth(codex):
        codex = _source_value(CODEX_AUTH_SOURCE_ROOT_ENV, "auth.json")
    providers["codex-chatgpt"] = is_valid_codex_auth(codex)
    if not providers["codex-chatgpt"]:
        failures.append("codex ChatGPT credential source is unavailable")
    return providers, failures


@runtime_router.get("/readiness", response_model=RuntimeReadinessResponse)
async def runtime_readiness(request: Request) -> RuntimeReadinessResponse:
    """Validate the managed Agent Server auth and provider prerequisites.

    The route is under the normal session-key dependency.  It reports only
    booleans and stable failure names; credential contents and paths are never
    returned.
    """
    config: Config = request.app.state.config
    providers, failures = _provider_readiness(config)
    auth_mode = "session-api-key" if config.session_api_keys else "none"
    if auth_mode != "session-api-key":
        failures.insert(0, "session API key authentication is disabled")
    return RuntimeReadinessResponse(
        status="ready" if not failures else "not_ready",
        agent_server_auth_mode=auth_mode,
        providers=providers,
        failures=failures,
        build_git_sha=os.environ.get("OPENHANDS_BUILD_GIT_SHA", "unknown"),
    )
