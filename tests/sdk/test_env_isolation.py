"""`make validate` must run in an allowlisted environment, not inherit the parent's."""

import os
import subprocess
from pathlib import Path

from tests.conftest import REPO_ROOT


SENTINEL = "dummy-sentinel-do-not-leak"
POLLUTED_ENV = {
    "OPENHANDS_SESSION_API_KEY": SENTINEL,
    "OPENHANDS_CODEX_AUTH_SOURCE": SENTINEL,
    "OH_PERSISTENCE_DIR": SENTINEL,
    "OH_SECRETS_DIR": SENTINEL,
    "CLAUDE_CODE_OAUTH_TOKEN": SENTINEL,
    "OPENAI_API_KEY": SENTINEL,
    "CODEX_HOME": SENTINEL,
    "CURSOR_API_KEY": SENTINEL,
    "GH_TOKEN": SENTINEL,
    "GITHUB_TOKEN": SENTINEL,
    "CLOUD_AGENT_INJECTED_SECRET_NAMES": SENTINEL,
    "SOME_UNLISTED_SECRET": SENTINEL,
}
ALLOWED_NAMES = {
    "PATH",
    "LANG",
    "HOME",
    "TMPDIR",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "PRE_COMMIT_HOME",
    "CI",
}


def run_in_validate_env(
    tmp_path: Path, *command: str
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        **POLLUTED_ENV,
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(tmp_path),
        "HOME": str(tmp_path / "real-home"),
    }
    return subprocess.run(
        ["sh", "scripts/validate-env.sh", *command],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_validate_env_contains_only_allowlisted_names(tmp_path: Path):
    result = run_in_validate_env(tmp_path, "env")
    names = {line.split("=", 1)[0] for line in result.stdout.splitlines()}
    locale_names = {name for name in names if name.startswith("LC_")}
    assert names <= ALLOWED_NAMES | locale_names
    assert {"PATH", "HOME", "TMPDIR", "CI", "LC_ALL"} <= names
    assert SENTINEL not in result.stdout + result.stderr


def test_validate_env_uses_throwaway_home_and_tmpdir(tmp_path: Path):
    result = run_in_validate_env(tmp_path, "sh", "-c", 'echo "$HOME" "$TMPDIR"')
    home, tmpdir = (Path(p) for p in result.stdout.split())
    assert home != tmp_path / "real-home"
    assert home.parent == tmpdir.parent
    assert home.parent.parent == tmp_path
    assert not home.parent.exists()


def test_make_validate_runs_steps_in_validate_env():
    result = subprocess.run(
        ["make", "-n", "validate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("sh scripts/validate-env.sh ")
    assert "validate-steps" in result.stdout


def test_make_validate_steps_run_in_order_through_suite_helper():
    result = subprocess.run(
        ["make", "-n", "validate-steps"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    commands = [line.removesuffix(" && \\") for line in result.stdout.splitlines()]
    assert all(c.startswith("sh scripts/validate-suite.sh ") for c in commands)
    names = [c.split()[2] for c in commands]
    assert names == [
        "sync",
        "sdk",
        "agent_server",
        "workspace",
        "cross",
        "tools",
        "pyright",
        "pre-commit",
        "git-diff-check",
        "git-diff-exit-code",
    ]
