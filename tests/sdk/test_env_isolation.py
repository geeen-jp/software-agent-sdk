"""The test suite must never inherit the agent-server's host-side env."""

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import AGENT_SERVER_ENV_VARS, REPO_ROOT, strip_agent_server_env


ISOLATED_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_AUTH_CODE",
    *AGENT_SERVER_ENV_VARS,
)


def test_strip_agent_server_env_removes_inherited_values(monkeypatch):
    for name in AGENT_SERVER_ENV_VARS:
        monkeypatch.setenv(name, "/dummy")
    strip_agent_server_env()
    assert [n for n in AGENT_SERVER_ENV_VARS if n in os.environ] == []


def test_pytest_run_strips_inherited_env(tmp_path: Path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import os\n"
        f"NAMES = {AGENT_SERVER_ENV_VARS!r}\n"
        "def test_probe():\n"
        "    assert [n for n in NAMES if n in os.environ] == []\n"
    )
    env = {**os.environ, **{name: "/dummy" for name in AGENT_SERVER_ENV_VARS}}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.conftest",
            "--rootdir",
            str(tmp_path),
            str(probe),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_make_validate_unsets_isolated_env():
    result = subprocess.run(
        ["make", "-n", "validate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    unset_line = next(
        line for line in result.stdout.splitlines() if line.startswith("unset ")
    )
    assert set(unset_line.removeprefix("unset ").split(";")[0].split()) == set(
        ISOLATED_ENV_VARS
    )
