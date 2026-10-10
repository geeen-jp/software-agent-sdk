"""Tests for TerminalTool subclass."""

import platform
import tempfile
from collections.abc import Sequence
from uuid import uuid4

import pytest
from pydantic import SecretStr

from openhands.sdk.agent import Agent
from openhands.sdk.conversation.state import ConversationState
from openhands.sdk.llm import LLM
from openhands.sdk.workspace import LocalWorkspace
from openhands.tools.terminal import (
    TerminalAction,
    TerminalObservation,
    TerminalTool,
)
from tests.tools.tmux_utils import _tmux_session_count


def _close_tools(tools: Sequence[TerminalTool]) -> None:
    for tool in tools:
        if tool.executor is not None:
            tool.executor.close()


def _create_test_conv_state(temp_dir: str) -> ConversationState:
    """Helper to create a test conversation state."""
    llm = LLM(model="gpt-4o-mini", api_key=SecretStr("test-key"), usage_id="test-llm")
    agent = Agent(llm=llm, tools=[])
    return ConversationState.create(
        id=uuid4(),
        agent=agent,
        workspace=LocalWorkspace(working_dir=temp_dir),
    )


def test_bash_tool_initialization():
    """Test that TerminalTool initializes correctly."""
    with tempfile.TemporaryDirectory() as temp_dir:
        session_count_before = _tmux_session_count()
        tools: Sequence[TerminalTool] = ()
        try:
            conv_state = _create_test_conv_state(temp_dir)
            tools = TerminalTool.create(conv_state)
            tool = tools[0]

            assert tool.name == "terminal"
            assert tool.executor is not None
            assert tool.action_type == TerminalAction
            expected_session_count = session_count_before + int(
                getattr(tool.executor, "is_pooled", False)
            )
            assert _tmux_session_count() == expected_session_count
        finally:
            _close_tools(tools)

        assert _tmux_session_count() == session_count_before


def test_bash_tool_with_username():
    """Test that TerminalTool initializes correctly with username."""
    with tempfile.TemporaryDirectory() as temp_dir:
        session_count_before = _tmux_session_count()
        tools: Sequence[TerminalTool] = ()
        try:
            conv_state = _create_test_conv_state(temp_dir)
            tools = TerminalTool.create(conv_state, username="testuser")
            tool = tools[0]

            assert tool.name == "terminal"
            assert tool.executor is not None
            assert tool.action_type == TerminalAction
            expected_session_count = session_count_before + int(
                getattr(tool.executor, "is_pooled", False)
            )
            assert _tmux_session_count() == expected_session_count
        finally:
            _close_tools(tools)

        assert _tmux_session_count() == session_count_before


def test_bash_tool_execution():
    """Test that TerminalTool can execute commands."""
    with tempfile.TemporaryDirectory() as temp_dir:
        tools: Sequence[TerminalTool] = ()
        try:
            conv_state = _create_test_conv_state(temp_dir)
            tools = TerminalTool.create(conv_state)
            tool = tools[0]

            action = TerminalAction(command="echo 'Hello, World!'")
            result = tool(action)

            assert result is not None
            assert isinstance(result, TerminalObservation)
            assert "Hello, World!" in result.text
        finally:
            _close_tools(tools)


def test_bash_tool_working_directory():
    """Test that TerminalTool respects the working directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        tools: Sequence[TerminalTool] = ()
        try:
            conv_state = _create_test_conv_state(temp_dir)
            tools = TerminalTool.create(conv_state)
            tool = tools[0]

            action = TerminalAction(command="pwd")
            result = tool(action)

            assert isinstance(result, TerminalObservation)
            assert temp_dir in result.text
        finally:
            _close_tools(tools)


def test_bash_tool_to_openai_tool():
    """Test that TerminalTool can be converted to OpenAI tool format."""
    with tempfile.TemporaryDirectory() as temp_dir:
        session_count_before = _tmux_session_count()
        tools: Sequence[TerminalTool] = ()
        try:
            conv_state = _create_test_conv_state(temp_dir)
            tools = TerminalTool.create(conv_state)
            tool = tools[0]

            openai_tool = tool.to_openai_tool()

            assert openai_tool["type"] == "function"
            assert openai_tool["function"]["name"] == "terminal"
            assert "description" in openai_tool["function"]
            assert "parameters" in openai_tool["function"]
            expected_session_count = session_count_before + int(
                getattr(tool.executor, "is_pooled", False)
            )
            assert _tmux_session_count() == expected_session_count
        finally:
            _close_tools(tools)

        assert _tmux_session_count() == session_count_before


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="This test uses POSIX shell environment variable syntax.",
)
def test_terminal_tool_client_env_is_session_scoped_and_schema_hidden(monkeypatch):
    """Test that client env config reaches the shell without becoming action input."""
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setenv("OH_CLIENT_ENV_TEST", "parent-value")
        tools: Sequence[TerminalTool] = ()
        try:
            conv_state = _create_test_conv_state(temp_dir)
            tools = TerminalTool.create(
                conv_state,
                terminal_type="subprocess",
                env={"OH_CLIENT_ENV_TEST": "client-value"},
            )
            tool = tools[0]
            assert tool.executor is not None

            properties = tool.action_type.model_json_schema()["properties"]
            assert "env" not in properties

            action = TerminalAction(command='printf "%s" "$OH_CLIENT_ENV_TEST"')
            result = tool(action)
            assert isinstance(result, TerminalObservation)
            assert "client-value" in result.text
            assert "parent-value" not in result.text

            tool(TerminalAction(command="", reset=True))
            result_after_reset = tool(action)
            assert isinstance(result_after_reset, TerminalObservation)
            assert "client-value" in result_after_reset.text
        finally:
            _close_tools(tools)


def test_terminal_tool_client_env_rejects_invalid_names():
    """Test that client env keys must be valid shell environment names."""
    with tempfile.TemporaryDirectory() as temp_dir:
        conv_state = _create_test_conv_state(temp_dir)
        with pytest.raises(ValueError, match="Invalid terminal environment"):
            TerminalTool.create(conv_state, env={"INVALID-NAME": "value"})
