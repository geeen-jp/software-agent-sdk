"""Utility functions for extracting agent responses from conversation events."""

import json
from collections.abc import Sequence

from openhands.sdk.event import ACPToolCallEvent, ActionEvent, MessageEvent
from openhands.sdk.event.base import Event
from openhands.sdk.llm.message import content_to_str
from openhands.sdk.tool.builtins.finish import FinishAction, FinishTool


_NO_RESPONSE_FROM_ACP = "(No response from ACP server)"
_STRUCTURED_OUTPUT_TITLE = "structuredoutput"


def _get_completed_structured_output(events: Sequence[Event]) -> str:
    """Return the latest completed ACP structured result as JSON text.

    Claude ACP exposes native JSON-schema output as a completed
    ``StructuredOutput`` tool-call update. ACPAgent still emits its normal
    FinishAction delimiter, but some ACP versions put the placeholder
    ``(No response from ACP server)`` in that action instead of copying the
    structured payload into assistant text. Keep this provider transport
    detail here so callers continue to consume one final-response surface.
    """
    for event in reversed(events):
        if not isinstance(event, ACPToolCallEvent):
            continue
        if event.title.strip().lower() != _STRUCTURED_OUTPUT_TITLE:
            continue
        if (event.status or "").strip().lower() != "completed":
            continue
        if not isinstance(event.raw_input, dict):
            continue
        try:
            return json.dumps(
                event.raw_input, ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError):
            continue
    return ""


def get_agent_final_response(events: Sequence[Event]) -> str:
    """Extract the final response from the agent.

    An agent can end a conversation in two ways:
    1. By calling the finish tool
    2. By returning a text message with no tool calls

    Args:
        events: List of conversation events to search through.

    Returns:
        The final response message from the agent, or empty string if not found.
    """
    # Find the last finish action or message event from the agent
    for event in reversed(events):
        # Case 1: finish tool call
        if (
            isinstance(event, ActionEvent)
            and event.source == "agent"
            and event.tool_name == FinishTool.name
        ):
            # Extract message from finish tool call
            if event.action is not None and isinstance(event.action, FinishAction):
                message = event.action.message
                if message != _NO_RESPONSE_FROM_ACP:
                    return message
                structured = _get_completed_structured_output(events)
                return structured or message
            else:
                break
        # Case 2: text message with no tool calls (MessageEvent)
        elif isinstance(event, MessageEvent) and event.source == "agent":
            text_parts = content_to_str(event.llm_message.content)
            return "".join(text_parts)
    return ""
