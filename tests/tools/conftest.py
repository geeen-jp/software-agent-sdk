"""Shared fixtures for tool tests."""

import os
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from openhands.tools.terminal.constants import TMUX_SOCKET_NAME


@pytest.fixture
def isolated_tmux_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use a per-test tmux socket and stop it during fallback cleanup."""
    with tempfile.TemporaryDirectory(prefix="oh-tmux-") as sandbox:
        tmux_tmpdir = Path(sandbox) / "tmux"
        tmux_tmpdir.mkdir(mode=0o700)
        tmux_tmpdir.chmod(0o700)
        monkeypatch.setenv("TMUX_TMPDIR", str(tmux_tmpdir))
        try:
            yield
        finally:
            try:
                subprocess.run(
                    ["tmux", "-L", TMUX_SOCKET_NAME, "kill-server"],
                    env={**os.environ, "TMUX_TMPDIR": str(tmux_tmpdir)},
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
