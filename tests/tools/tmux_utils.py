"""Helpers for tests that inspect the isolated tmux server."""

import subprocess

from openhands.tools.terminal.constants import TMUX_SOCKET_NAME


def _tmux_session_count() -> int:
    """Return the number of sessions on the current OpenHands tmux server."""
    try:
        result = subprocess.run(
            [
                "tmux",
                "-L",
                TMUX_SOCKET_NAME,
                "list-sessions",
                "-F",
                "#{session_name}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        return 0
    return len(result.stdout.splitlines()) if result.returncode == 0 else 0
