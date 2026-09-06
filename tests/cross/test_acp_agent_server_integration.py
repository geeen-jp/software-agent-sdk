"""Agent Server loopback integration tests for ACP (Issue #6 Phase H).

Exercises a real ``openhands.agent_server`` subprocess on loopback with a
deterministic stdio ACP stub. No paid provider credentials are required.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.cross.acp_agent_server_harness import (
    build_start_conversation_payload,
    loopback_agent_server,
    read_stub_trace,
    run_conversation_turn,
    trace_methods,
    wait_for_execution_status,
)


@pytest.fixture
def project_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace


def test_loopback_agent_server_ready_and_server_info(tmp_path: Path) -> None:
    with loopback_agent_server(tmp_path) as env:
        with httpx.Client(base_url=env["base_url"], timeout=10.0) as client:
            ready = client.get("/ready")
            assert ready.status_code == 200
            assert ready.json()["status"] == "ready"

            info = client.get("/server_info")
            assert info.status_code == 200
            payload = info.json()
            assert payload["title"] == "OpenHands Agent Server"
            assert isinstance(payload.get("usable_tools"), list)


def test_acp_conversation_create_query_final_response(
    tmp_path: Path,
    project_workspace: Path,
) -> None:
    conversation_id = uuid4()
    with loopback_agent_server(tmp_path) as env:
        with httpx.Client(base_url=env["base_url"], timeout=30.0) as client:
            create = client.post(
                "/api/conversations",
                json=build_start_conversation_payload(
                    conversation_id=conversation_id,
                    workspace_dir=project_workspace,
                ),
            )
            assert create.status_code == 201, create.text
            info = create.json()
            assert info["id"] == str(conversation_id)
            assert info["workspace"]["working_dir"] == str(project_workspace.resolve())
            assert info["agent"]["acp_server"] == "custom"
            assert info["agent"]["acp_model"] == "composer-2.5"
            assert info["agent"]["acp_config_options"] == {"fast": "false"}
            assert info["agent"]["acp_client_capabilities"]["_meta"] == {
                "parameterizedModelPicker": True
            }

            run_conversation_turn(client, conversation_id, "hello-acp")
            wait_for_execution_status(client, conversation_id, "finished")

            query = client.get(f"/api/conversations/{conversation_id}")
            query.raise_for_status()
            queried = query.json()
            assert queried["id"] == str(conversation_id)
            assert queried["execution_status"] == "finished"
            assert queried.get("current_model_id") == "composer-2.5"

            final = client.get(
                f"/api/conversations/{conversation_id}/agent_final_response"
            )
            final.raise_for_status()
            assert final.json()["response"] == "stub-response:hello-acp"

    trace = read_stub_trace(project_workspace)
    methods = trace_methods(project_workspace)
    assert "new_session" in methods
    assert methods.count("set_session_model") >= 1
    assert any(
        event.get("method") == "set_config_option"
        and event.get("config_id") == "fast"
        and event.get("value") is False
        for event in trace
    )
    prompt_index = methods.index("prompt")
    config_indices = [
        index
        for index, method in enumerate(methods)
        if method in {"set_session_model", "set_config_option"}
    ]
    assert config_indices and max(config_indices) < prompt_index


def test_acp_same_identity_restart_reconcile(
    tmp_path: Path,
    project_workspace: Path,
) -> None:
    conversation_id = uuid4()
    payload = build_start_conversation_payload(
        conversation_id=conversation_id,
        workspace_dir=project_workspace,
    )

    with loopback_agent_server(tmp_path) as env:
        with httpx.Client(base_url=env["base_url"], timeout=30.0) as client:
            create = client.post("/api/conversations", json=payload)
            assert create.status_code == 201
            run_conversation_turn(client, conversation_id, "persist-me")
            wait_for_execution_status(client, conversation_id, "finished")
            first_final = client.get(
                f"/api/conversations/{conversation_id}/agent_final_response"
            )
            first_final.raise_for_status()
            assert first_final.json()["response"] == "stub-response:persist-me"

    with loopback_agent_server(tmp_path, clean=False) as env:
        with httpx.Client(base_url=env["base_url"], timeout=30.0) as client:
            restart = client.post("/api/conversations", json=payload)
            assert restart.status_code == 200, restart.text
            info = restart.json()
            assert info["id"] == str(conversation_id)
            assert info["workspace"]["working_dir"] == str(project_workspace.resolve())

            final = client.get(
                f"/api/conversations/{conversation_id}/agent_final_response"
            )
            final.raise_for_status()
            assert final.json()["response"] == "stub-response:persist-me"

            run_conversation_turn(client, conversation_id, "after-restart")
            wait_for_execution_status(client, conversation_id, "finished")
            refreshed = client.get(
                f"/api/conversations/{conversation_id}/agent_final_response"
            )
            refreshed.raise_for_status()
            assert refreshed.json()["response"] == "stub-response:after-restart"

    methods = trace_methods(project_workspace)
    assert "load_session" in methods


def test_acp_interrupt_observes_pause(
    tmp_path: Path,
    project_workspace: Path,
) -> None:
    conversation_id = uuid4()
    with loopback_agent_server(tmp_path) as env:
        with httpx.Client(base_url=env["base_url"], timeout=30.0) as client:
            create = client.post(
                "/api/conversations",
                json=build_start_conversation_payload(
                    conversation_id=conversation_id,
                    workspace_dir=project_workspace,
                    acp_env={"ACP_STUB_PROMPT_DELAY_SECS": "8"},
                ),
            )
            assert create.status_code == 201, create.text

            msg_resp = client.post(
                f"/api/conversations/{conversation_id}/events",
                json={
                    "content": [{"type": "text", "text": "slow prompt"}],
                    "run": False,
                },
            )
            msg_resp.raise_for_status()
            run_resp = client.post(f"/api/conversations/{conversation_id}/run")
            run_resp.raise_for_status()

            wait_for_execution_status(client, conversation_id, "running", timeout=20.0)

            interrupt = client.post(f"/api/conversations/{conversation_id}/interrupt")
            assert interrupt.status_code == 200, interrupt.text

            paused = wait_for_execution_status(
                client, conversation_id, "paused", timeout=20.0
            )
            assert paused["execution_status"] == "paused"

            events = client.get(
                f"/api/conversations/{conversation_id}/events/search",
                params={"kind": "openhands.sdk.event.user_action.InterruptEvent"},
            )
            events.raise_for_status()
            assert events.json()["items"]

    trace = read_stub_trace(project_workspace)
    assert any(event.get("method") == "cancel" for event in trace)
