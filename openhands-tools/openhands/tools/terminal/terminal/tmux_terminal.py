"""Tmux-based terminal backend implementation."""

import time
import uuid
from collections.abc import Mapping
from contextlib import suppress

import libtmux

from openhands.sdk.logger import get_logger
from openhands.tools.terminal.constants import (
    HISTORY_LIMIT,
    TMUX_INITIALIZE_TIMEOUT_SECONDS,
    TMUX_READ_SCREEN_TIMEOUT_SECONDS,
    TMUX_SESSION_HEIGHT,
    TMUX_SESSION_WIDTH,
    TMUX_SOCKET_NAME,
)
from openhands.tools.terminal.env import (
    build_terminal_env,
    normalize_terminal_env,
)
from openhands.tools.terminal.metadata import CmdOutputMetadata
from openhands.tools.terminal.terminal import TerminalInterface
from openhands.tools.terminal.terminal.interface import parse_ctrl_key
from openhands.tools.terminal.terminal.process_groups import (
    descendant_process_groups,
    terminate_process_groups,
)
from openhands.tools.terminal.terminal.tmux_server import BoundedTmuxServer


logger = get_logger(__name__)

# Map normalized special key names to tmux key names.
_TMUX_SPECIALS: dict[str, str] = {
    "ENTER": "Enter",
    "TAB": "Tab",
    "BS": "BSpace",
    "ESC": "Escape",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "HOME": "Home",
    "END": "End",
    "PGUP": "PPage",
    "PGDN": "NPage",
    "C-L": "C-l",
    "C-D": "C-d",
    "C-C": "C-c",
}


class TmuxTerminal(TerminalInterface):
    """Tmux-based terminal backend.

    This backend uses tmux to provide a persistent terminal session
    with full screen capture and history management capabilities.
    """

    PS1: str
    server: BoundedTmuxServer
    session: libtmux.Session
    window: libtmux.Window
    pane: libtmux.Pane

    def __init__(
        self,
        work_dir: str,
        username: str | None = None,
        env: Mapping[str, str] | None = None,
    ):
        super().__init__(work_dir, username)
        self.PS1 = CmdOutputMetadata.to_ps1_prompt()
        self._env = normalize_terminal_env(env)

    def initialize(self) -> None:
        """Initialize the tmux terminal session."""
        if self._initialized:
            return

        session: libtmux.Session | None = None
        try:
            env = build_terminal_env(self._env)
            # Disable interactive pagers (git, man, systemctl, ...) so commands that
            # auto-launch `less` on a TTY don't capture the pane and wedge the session.
            env.setdefault("GIT_PAGER", "cat")
            env.setdefault("PAGER", "cat")
            # Use a dedicated socket to isolate OpenHands sessions from the user's tmux
            self.server = BoundedTmuxServer(
                socket_name=TMUX_SOCKET_NAME,
                environment=env,
            )
            _shell_command = "/bin/bash"
            if self.username in ["root", "openhands"]:
                # This starts a non-login (new) shell for the given user
                _shell_command = f"su {self.username} -"

            logger.debug("Initializing tmux terminal with command: %s", _shell_command)
            session_name = f"openhands-{self.username}-{uuid.uuid4()}"
            with self.server.deadline_after(TMUX_INITIALIZE_TIMEOUT_SECONDS):
                session = self.server.new_session(
                    session_name=session_name,
                    start_directory=self.work_dir,
                    kill_session=True,
                    x=TMUX_SESSION_WIDTH,
                    y=TMUX_SESSION_HEIGHT,
                )
                self.session = session
                for k, v in env.items():
                    session.set_environment(k, v)

                # Set history limit to a large number to avoid losing history
                # https://unix.stackexchange.com/questions/43414/tmux-unlimited-history
                session.set_option("history-limit", str(HISTORY_LIMIT))
                session.history_limit = str(HISTORY_LIMIT)

                # Create a new pane because the initial pane's history limit is 2000.
                initial_window = session.active_window
                self.window = session.new_window(
                    window_name="terminal",
                    window_shell=_shell_command,
                    start_directory=self.work_dir,
                )
                active_pane = self.window.active_pane
                assert active_pane is not None, "Window should have an active pane"
                self.pane = active_pane
                logger.debug(
                    "pane: %s; history_limit: %s", self.pane, session.history_limit
                )

                # Configure bash to use simple PS1 and disable PS2
                # Disable history expansion to avoid ! mangling
                prompt_setup = (
                    f"set +H; export PROMPT_COMMAND='export PS1=\"{self.PS1}\"'; "
                    'export PS2=""'
                )
                self.pane.send_keys(prompt_setup)
                time.sleep(0.1)  # Wait for command to take effect

                self._initialized = True
                self.clear_screen()
                initial_window.kill()
                logger.debug(
                    "Tmux terminal initialized with work dir: %s", self.work_dir
                )
        except Exception:
            self._initialized = False
            if session is not None:
                with suppress(Exception):
                    session.kill()
            raise

    def pane_job_groups(self) -> set[int]:
        """Process groups of the jobs running under this pane's shell."""
        with suppress(Exception):
            pane_pid = self.pane.pane_pid
            assert pane_pid is not None
            return descendant_process_groups(int(pane_pid))
        return set()

    def close(self) -> None:
        """Clean up the tmux session."""
        if self._closed:
            return
        job_groups = self.pane_job_groups()
        try:
            if hasattr(self, "session"):
                self.session.kill()
        except Exception as e:
            # Session might already be dead/killed externally
            # (e.g., "can't find session" error from tmux)
            # Also handles ImportError during Python shutdown
            logger.debug(f"Error closing tmux session (may already be dead): {e}")
        terminate_process_groups(job_groups)
        self._closed: bool = True

    def send_keys(self, text: str, enter: bool = True) -> None:
        """Send text/keys to the tmux pane.

        Supports:
          - Plain text (uses literal paste; preserves spaces/newlines)
          - Named specials: ENTER, TAB, BS, ESC, UP, DOWN, LEFT, RIGHT,
            HOME, END, PGUP, PGDN, C-L, C-D, C-C
          - Generic Ctrl sequences: C-a..C-z, CTRL-x, CTRL+x

        Args:
            text: Text or key sequence to send
            enter: Whether to send Enter key after the text.
                   Ignored for special/ctrl keys.
        """
        if not self._initialized or not isinstance(self.pane, libtmux.Pane):
            raise RuntimeError("Tmux terminal is not initialized")

        # Map normalized names to tmux key names
        upper = text.strip().upper()

        # 1) Named specials
        if upper in _TMUX_SPECIALS:
            self.pane.send_keys(_TMUX_SPECIALS[upper], enter=False)
            return

        # 2) Generic Ctrl-<letter>
        ctrl = parse_ctrl_key(text)
        if ctrl is not None:
            self.pane.send_keys(ctrl, enter=False)
            return

        # 3) Plain text — use literal=True so tmux doesn't split on
        #    whitespace or interpret special tokens.
        self.pane.send_keys(text, enter=False, literal=True)
        if enter and not text.endswith("\n"):
            self.pane.send_keys("Enter", enter=False)

    def read_screen(self) -> str:
        """Read the current tmux pane content.

        Returns:
            Current visible content of the tmux pane
        """
        if not self._initialized or not isinstance(self.pane, libtmux.Pane):
            raise RuntimeError("Tmux terminal is not initialized")

        with self.server.deadline_after(TMUX_READ_SCREEN_TIMEOUT_SECONDS):
            content = "\n".join(
                map(
                    # avoid double newlines
                    lambda line: line.rstrip(),
                    self.pane.cmd("capture-pane", "-J", "-pS", "-").stdout,
                )
            )
        return content

    def clear_screen(self) -> None:
        """Clear the tmux pane screen and history.

        We intentionally avoid sending ``C-l`` (Ctrl+L) because the form-feed
        control character (``^L``) can leak into the shell input buffer over SSH
        connections.

        Instead, we run the ``clear`` command to clear the visible screen, then
        use tmux's ``clear-history`` to remove the scrollback buffer.
        """
        if not self._initialized or not isinstance(self.pane, libtmux.Pane):
            raise RuntimeError("Tmux terminal is not initialized")

        self.pane.send_keys("clear", enter=True)
        time.sleep(0.1)
        self.pane.cmd("clear-history")

    def interrupt(self) -> bool:
        """Send interrupt signal (Ctrl+C) to the tmux pane.

        Returns:
            True if interrupt was sent successfully, False otherwise
        """
        if not self._initialized or not isinstance(self.pane, libtmux.Pane):
            return False
        try:
            self.pane.send_keys("C-c", enter=False)
            return True
        except Exception as e:
            logger.error(f"Failed to interrupt command: {e}", exc_info=True)
            return False

    def is_running(self) -> bool:
        """Check if a command is currently running.

        For tmux, we determine this by checking if the terminal
        is ready for new commands (ends with prompt).
        """
        if not self._initialized:
            return False

        try:
            content = self.read_screen()
            # If the screen ends with our PS1 prompt, no command is running
            from openhands.tools.terminal.constants import CMD_OUTPUT_PS1_END

            return not content.rstrip().endswith(CMD_OUTPUT_PS1_END.rstrip())
        except Exception:
            return False
