"""Harness for loopback Agent Server + deterministic ACP stub integration tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from openhands.sdk.settings.model import AGENT_SETTINGS_SCHEMA_VERSION
from openhands.workspace.docker.workspace import find_available_tcp_port


_STUB_SCRIPT = (
    Path(__file__).resolve().parent.parent / "fixtures" / "acp_deterministic_stub.py"
)
_TRACE_REL = Path(".agent_tmp") / "acp_stub_trace.jsonl"


@contextmanager
def loopback_agent_server(
    tmp_path: Path,
    *,
    conversations_subdir: str = "conversations",
    workspace_subdir: str = "workspace",
    clean: bool = True,
) -> Generator[dict[str, Any]]:
    """Launch a real ``openhands.agent_server`` subprocess on loopback."""
    conversations_path = tmp_path / conversations_subdir
    workspace_path = tmp_path / workspace_subdir
    if clean:
        if conversations_path.exists():
            shutil.rmtree(conversations_path)
        if workspace_path.exists():
            shutil.rmtree(workspace_path)
    conversations_path.mkdir(parents=True, exist_ok=True)
    workspace_path.mkdir(parents=True, exist_ok=True)

    cfg = {
        "session_api_keys": [],
        "conversations_path": str(conversations_path),
        "workspace_path": str(workspace_path),
    }
    cfg_file = tmp_path / "agent_server_config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    port = find_available_tcp_port()
    host = "127.0.0.1"
    base_url = f"http://{host}:{port}"
    env = {
        **os.environ,
        "OPENHANDS_AGENT_SERVER_CONFIG_PATH": str(cfg_file),
        "LOG_JSON": "false",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openhands.agent_server",
            "--host",
            host,
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for_http_ok(f"{base_url}/ready", process=process, timeout=30.0)
        yield {
            "base_url": base_url,
            "process": process,
            "conversations_path": conversations_path,
            "workspace_path": workspace_path,
            "config_path": cfg_file,
        }
    finally:
        _stop_process(process)


def _wait_for_http_ok(
    url: str,
    *,
    process: subprocess.Popen[str],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(
                f"Agent server exited before {url} became ready: {stderr}"
            )
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {url}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def build_acp_agent_settings_payload(
    *,
    stub_script: Path | None = None,
    acp_env: dict[str, str] | None = None,
    acp_model: str = "composer-2.5",
) -> dict[str, Any]:
    script = stub_script or _STUB_SCRIPT
    payload: dict[str, Any] = {
        "schema_version": AGENT_SETTINGS_SCHEMA_VERSION,
        "agent_kind": "acp",
        "acp_server": "custom",
        "acp_command": [sys.executable, str(script.resolve())],
        "acp_model": acp_model,
        "acp_config_options": {"fast": "false"},
        "acp_client_capabilities": {
            "field_meta": {"parameterizedModelPicker": True},
        },
    }
    if acp_env:
        payload["acp_env"] = acp_env
    return payload


def build_start_conversation_payload(
    *,
    conversation_id: UUID,
    workspace_dir: Path,
    acp_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    workspace_dir = workspace_dir.resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    marker = workspace_dir / "workspace-write-check.txt"
    marker.write_text("writable\n", encoding="utf-8")
    return {
        "conversation_id": str(conversation_id),
        "agent_settings": build_acp_agent_settings_payload(acp_env=acp_env),
        "workspace": {"working_dir": str(workspace_dir)},
    }


def wait_for_execution_status(
    client: httpx.Client,
    conversation_id: UUID | str,
    status: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    conv_id = str(conversation_id)
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/conversations/{conv_id}")
        response.raise_for_status()
        last = response.json()
        if last.get("execution_status") == status:
            return last
        time.sleep(0.1)
    raise AssertionError(
        f"Conversation {conv_id} did not reach status {status!r}; last={last}"
    )


def run_conversation_turn(
    client: httpx.Client,
    conversation_id: UUID | str,
    message: str,
) -> None:
    conv_id = str(conversation_id)
    msg_resp = client.post(
        f"/api/conversations/{conv_id}/events",
        json={
            "content": [{"type": "text", "text": message}],
            "run": False,
        },
    )
    msg_resp.raise_for_status()
    run_resp = client.post(f"/api/conversations/{conv_id}/run")
    run_resp.raise_for_status()


def read_stub_trace(workspace_dir: Path) -> list[dict[str, Any]]:
    trace_file = workspace_dir / _TRACE_REL
    if not trace_file.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in trace_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def trace_methods(workspace_dir: Path) -> list[str]:
    return [event["method"] for event in read_stub_trace(workspace_dir)]
