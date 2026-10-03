"""Tests for the Codex per-turn outputSchema bridge."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openhands.sdk.agent import acp_codex_output_schema
from openhands.sdk.agent.acp_codex_output_schema import (
    CODEX_BRIDGE_PATH_ENV,
    CODEX_OUTPUT_SCHEMA_ENV,
    CODEX_PATH_ENV,
    CODEX_RUNTIME_PATH_ENV,
    build_codex_output_schema_env,
    ensure_bridge_script,
)


requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires node")


def _node_path() -> str:
    node = shutil.which("node")
    assert node is not None
    return node


@requires_node
def test_bridge_attaches_schema_to_every_turn_start(tmp_path):
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    title_schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    runtime = tmp_path / "fake-codex"
    args_path = tmp_path / "args.json"
    runtime.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "with open(os.environ['CODEX_TEST_ARGS_PATH'], 'w') as args_file:\n"
        "    json.dump(sys.argv[1:], args_file)\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write(line)\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    runtime.chmod(0o700)

    input_lines = [
        json.dumps({"id": 1, "method": "turn/start", "params": {"prompt": "first"}}),
        json.dumps(
            {
                "id": 2,
                "method": "turn/start",
                "params": {"outputSchema": title_schema, "prompt": "title"},
            },
            separators=(",", ":"),
        ),
        json.dumps({"id": 3, "method": "thread/start", "params": {}}),
        "not-json",
        json.dumps({"id": 4, "method": "turn/start", "params": {"outputSchema": None}}),
        json.dumps({"id": 5, "method": "turn/start", "params": {"prompt": "retry"}}),
    ]
    env = {
        **os.environ,
        CODEX_RUNTIME_PATH_ENV: str(runtime),
        CODEX_OUTPUT_SCHEMA_ENV: json.dumps(schema),
        "CODEX_TEST_ARGS_PATH": str(args_path),
    }
    result = subprocess.run(
        [_node_path(), str(ensure_bridge_script()), "app-server"],
        input="".join(f"{line}\n" for line in input_lines),
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    output_lines = result.stdout.splitlines()
    assert len(output_lines) == len(input_lines)
    for index in (0, 4, 5):
        message = json.loads(output_lines[index])
        assert message["params"]["outputSchema"] == schema
    for index in (1, 2, 3):
        assert output_lines[index] == input_lines[index]
    assert json.loads(args_path.read_text(encoding="utf-8")) == ["app-server"]


@pytest.mark.parametrize(
    ("missing_env", "expected_message"),
    [
        (CODEX_RUNTIME_PATH_ENV, "OH_CODEX_RUNTIME_PATH is not set"),
        (CODEX_OUTPUT_SCHEMA_ENV, "OH_CODEX_OUTPUT_SCHEMA is not set"),
    ],
)
@requires_node
def test_bridge_fails_closed_without_runtime_or_schema(missing_env, expected_message):
    env = {
        **os.environ,
        CODEX_RUNTIME_PATH_ENV: "/missing/codex",
        CODEX_OUTPUT_SCHEMA_ENV: json.dumps({"type": "object"}),
    }
    env.pop(missing_env)
    result = subprocess.run(
        [_node_path(), str(ensure_bridge_script()), "app-server"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert expected_message in result.stderr


def test_build_env_routes_codex_path_through_bridge():
    schema = {"type": "object", "properties": {"result": {"type": "string"}}}
    before = copy.deepcopy(schema)
    env = {CODEX_PATH_ENV: "/opt/node/bin/codex"}

    result = build_codex_output_schema_env(schema, env)

    assert result[CODEX_PATH_ENV] == result[CODEX_BRIDGE_PATH_ENV]
    assert Path(result[CODEX_PATH_ENV]).is_file()
    assert result[CODEX_RUNTIME_PATH_ENV] == "/opt/node/bin/codex"
    assert json.loads(result[CODEX_OUTPUT_SCHEMA_ENV]) == schema
    assert schema == before


def test_build_env_leaves_runtime_unset_when_unknown(monkeypatch):
    monkeypatch.setattr(
        acp_codex_output_schema.shutil,
        "which",
        lambda _command, path=None: None,
    )

    result = build_codex_output_schema_env({"type": "object"}, {"PATH": ""})

    assert CODEX_RUNTIME_PATH_ENV not in result
