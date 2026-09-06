"""Deterministic stdio ACP agent for Agent Server integration tests.

Speaks the released ACP 0.10.x JSON-RPC protocol over stdin/stdout. No paid
provider or network access is required. Observed calls are appended to
``<cwd>/.agent_tmp/acp_stub_trace.jsonl`` for post-run assertions.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, cast

from acp.agent.connection import AgentSideConnection
from acp.helpers import text_block
from acp.interfaces import Agent, Client
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    ModelInfo,
    NewSessionResponse,
    PromptResponse,
    ResumeSessionResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionModelState,
    SetSessionConfigOptionResponse,
    SetSessionModelResponse,
)
from acp.stdio import stdio_streams


SessionConfigOption = SessionConfigOptionSelect | SessionConfigOptionBoolean

_TRACE_NAME = "acp_stub_trace.jsonl"
_FAST_OPTION_ID = "fast"
_COMPOSER_MODEL_ID = "composer-2.5"


def _trace_path(cwd: str) -> Path:
    return Path(cwd) / ".agent_tmp" / _TRACE_NAME


def _append_trace(cwd: str, event: dict[str, Any]) -> None:
    path = _trace_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _default_config_options() -> list[SessionConfigOption]:
    return [
        SessionConfigOptionBoolean(
            id=_FAST_OPTION_ID,
            name=_FAST_OPTION_ID,
            type="boolean",
            current_value=True,
        )
    ]


def _default_models() -> SessionModelState:
    return SessionModelState(
        available_models=[
            ModelInfo(model_id=_COMPOSER_MODEL_ID, name="Composer 2.5"),
            ModelInfo(model_id="auto", name="Auto"),
        ],
        current_model_id="auto",
    )


def _option_with_value(
    option: SessionConfigOption, value: str | bool
) -> SessionConfigOption:
    if isinstance(option, SessionConfigOptionBoolean):
        bool_value = value if isinstance(value, bool) else value.lower() == "true"
        return SessionConfigOptionBoolean(
            id=option.id,
            name=option.name,
            type="boolean",
            current_value=bool_value,
        )
    str_value = str(value).lower() if isinstance(value, bool) else str(value)
    choices = [select_option.value for select_option in option.options]
    if str_value not in choices:
        choices = [*choices, str_value]
    return SessionConfigOptionSelect(
        id=option.id,
        name=option.name,
        type="select",
        current_value=str_value,
        options=[SessionConfigSelectOption(name=v, value=v) for v in choices],
    )


class DeterministicACPStubAgent:
    """Minimal cursor-like ACP server used by integration tests."""

    def __init__(self, conn: Client) -> None:
        self._conn = cast(AgentSideConnection, conn)
        self._session_id: str | None = None
        self._cwd: str | None = None
        self._config_options: list[SessionConfigOption] = _default_config_options()
        self._models: SessionModelState = _default_models()
        self._cancelled_sessions: set[str] = set()

    def on_connect(self, conn: Client) -> None:
        self._conn = cast(AgentSideConnection, conn)

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        trace_cwd = self._cwd or os.getcwd()
        _append_trace(
            trace_cwd,
            {
                "method": "initialize",
                "protocol_version": protocol_version,
                "client_capabilities": (
                    client_capabilities.model_dump(by_alias=True, exclude_none=True)
                    if client_capabilities is not None
                    else None
                ),
            },
        )
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=Implementation(
                name="cursor-agent",
                title="Deterministic ACP Stub",
                version="0.0-test",
            ),
            agent_capabilities=AgentCapabilities(load_session=True),
        )

    def _session_response_options(self) -> list[SessionConfigOption]:
        return list(self._config_options)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        self._cwd = cwd
        self._session_id = f"stub-{uuid.uuid4()}"
        _append_trace(
            cwd,
            {
                "method": "new_session",
                "cwd": cwd,
                "mcp_servers": len(mcp_servers or []),
                "session_id": self._session_id,
            },
        )
        return NewSessionResponse(
            session_id=self._session_id,
            config_options=self._session_response_options(),
            models=self._models,
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        self._cwd = cwd
        self._session_id = session_id
        _append_trace(
            cwd,
            {
                "method": "load_session",
                "cwd": cwd,
                "session_id": session_id,
                "mcp_servers": len(mcp_servers or []),
            },
        )
        return LoadSessionResponse(
            config_options=self._session_response_options(),
            models=self._models,
        )

    async def set_session_model(
        self, model_id: str, session_id: str, **kwargs: Any
    ) -> SetSessionModelResponse:
        _append_trace(
            self._cwd or os.getcwd(),
            {
                "method": "set_session_model",
                "model_id": model_id,
                "session_id": session_id,
            },
        )
        self._models = SessionModelState(
            available_models=list(self._models.available_models),
            current_model_id=model_id,
        )
        return SetSessionModelResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        updated: list[SessionConfigOption] = []
        found = False
        for option in self._config_options:
            if option.id == config_id:
                updated.append(_option_with_value(option, value))
                found = True
            else:
                updated.append(option)
        if not found:
            msg = f"unknown config option {config_id}"
            raise ValueError(msg)
        self._config_options = updated
        _append_trace(
            self._cwd or os.getcwd(),
            {
                "method": "set_config_option",
                "config_id": config_id,
                "session_id": session_id,
                "value": value,
                "config_options": {
                    option.id: option.current_value for option in updated
                },
            },
        )
        return SetSessionConfigOptionResponse(config_options=updated)

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        cwd = self._cwd or os.getcwd()
        user_text = ""
        for block in prompt:
            text = getattr(block, "text", None)
            if text:
                user_text = text
                break
        _append_trace(
            cwd,
            {
                "method": "prompt",
                "session_id": session_id,
                "message_id": message_id,
                "user_text": user_text,
            },
        )

        delay = float(os.environ.get("ACP_STUB_PROMPT_DELAY_SECS", "0"))
        if delay > 0:
            elapsed = 0.0
            step = 0.05
            while elapsed < delay:
                if session_id in self._cancelled_sessions:
                    return PromptResponse(stop_reason="cancelled")
                await asyncio.sleep(step)
                elapsed += step

        if session_id in self._cancelled_sessions:
            return PromptResponse(stop_reason="cancelled")

        response_text = f"stub-response:{user_text or 'empty'}"
        await self._conn.session_update(
            session_id,
            AgentMessageChunk(
                session_update="agent_message_chunk",
                content=text_block(response_text),
            ),
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._cancelled_sessions.add(session_id)
        _append_trace(
            self._cwd or os.getcwd(),
            {"method": "cancel", "session_id": session_id},
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None

    async def set_session_mode(
        self, mode_id: str, session_id: str, **kwargs: Any
    ) -> None:
        _append_trace(
            self._cwd or os.getcwd(),
            {
                "method": "set_session_mode",
                "mode_id": mode_id,
                "session_id": session_id,
            },
        )
        return None

    async def list_sessions(
        self,
        additional_directories: list[str] | None = None,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        return ListSessionsResponse(sessions=[])

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        return ForkSessionResponse(session_id=f"fork-{session_id}")

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        return ResumeSessionResponse()

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        _append_trace(
            self._cwd or os.getcwd(),
            {"method": "close_session", "session_id": session_id},
        )
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


def _build_agent(conn: Client) -> Agent:
    return cast(Agent, DeterministicACPStubAgent(cast(AgentSideConnection, conn)))


async def _main() -> None:
    reader, writer = await stdio_streams()
    conn = AgentSideConnection(
        _build_agent,
        writer,
        reader,
        listening=False,
        use_unstable_protocol=True,
    )
    await conn.listen()


if __name__ == "__main__":
    asyncio.run(_main())
