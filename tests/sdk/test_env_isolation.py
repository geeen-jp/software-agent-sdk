"""`make validate` must run in an allowlisted environment, not inherit the parent's."""

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

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
    "TMUX_TMPDIR",
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
    assert {"PATH", "HOME", "TMPDIR", "TMUX_TMPDIR", "CI", "LC_ALL"} <= names
    assert SENTINEL not in result.stdout + result.stderr


def test_validate_env_uses_throwaway_home_and_tmpdir(tmp_path: Path):
    inspect_environment = (
        "import os, stat; "
        "print(os.environ['HOME']); "
        "print(os.environ['TMPDIR']); "
        "print(os.environ['TMUX_TMPDIR']); "
        "print(stat.S_IMODE(os.stat(os.environ['TMUX_TMPDIR']).st_mode))"
    )
    result = run_in_validate_env(tmp_path, sys.executable, "-c", inspect_environment)
    home, tmpdir, tmux_tmpdir = (Path(p) for p in result.stdout.splitlines()[:3])
    tmux_mode = int(result.stdout.splitlines()[3])
    assert home != tmp_path / "real-home"
    assert home.parent == tmpdir.parent
    assert home.parent.parent == tmp_path
    assert tmux_tmpdir == home.parent / "tmux"
    assert tmux_tmpdir != tmpdir
    assert tmux_mode == 0o700
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


def test_validate_env_reaps_orphaned_sandbox_processes(tmp_path: Path):
    """Orphans (here: one ignoring SIGTERM) must be gone before the sandbox is."""
    pid_file = tmp_path / "orphan.pid"
    orphan = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    spawn = (
        "import pathlib, subprocess, sys; "
        f"p = subprocess.Popen([sys.executable, '-c', {orphan!r}], "
        "start_new_session=True, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    result = run_in_validate_env(tmp_path, sys.executable, "-c", spawn)
    assert result.returncode == 0

    orphan_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _is_running(orphan_pid):
        time.sleep(0.1)
    assert not _is_running(orphan_pid)
    assert not list(tmp_path.glob("oh-validate.*"))


def test_validate_env_keeps_command_exit_status(tmp_path: Path):
    result = subprocess.run(
        ["sh", "scripts/validate-env.sh", "sh", "-c", "exit 3"],
        cwd=REPO_ROOT,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 3


def _is_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
