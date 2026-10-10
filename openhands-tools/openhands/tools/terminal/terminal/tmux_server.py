"""Bounded tmux command execution for terminal backends."""

from __future__ import annotations

import contextvars
import logging
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import libtmux
from libtmux import exc
from libtmux.common import tmux_cmd

from openhands.tools.terminal.constants import TMUX_COMMAND_TIMEOUT_SECONDS


logger = logging.getLogger(__name__)


class _BoundedTmuxCommand(tmux_cmd):
    """Run a tmux command with a bounded wait and libtmux-compatible results."""

    def __init__(
        self,
        *args: Any,
        timeout: float,
        command_name: str,
        operation_deadline: bool,
    ) -> None:
        tmux_bin = shutil.which("tmux")
        if not tmux_bin:
            raise exc.TmuxCommandNotFound

        command = [str(tmux_bin), *(str(arg) for arg in args)]
        self.cmd = command
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="backslashreplace",
            )
            try:
                stdout, stderr = self.process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                with suppress(ProcessLookupError):
                    self.process.kill()
                self.process.communicate()
                reason = "operation deadline" if operation_deadline else "command limit"
                raise TimeoutError(
                    f"tmux command {command_name!r} timed out after "
                    f"{timeout:.2f} seconds ({reason})"
                ) from error
            returncode = self.process.returncode
        except Exception:
            logger.exception("Exception running tmux command %r", command_name)
            raise

        self.returncode = returncode
        stdout_lines = stdout.split("\n")
        while stdout_lines and stdout_lines[-1] == "":
            stdout_lines.pop()

        self.stderr = list(filter(None, stderr.split("\n")))
        if "has-session" in command and self.stderr and not stdout_lines:
            self.stdout = [self.stderr[0]]
        else:
            self.stdout = stdout_lines


class BoundedTmuxServer(libtmux.Server):
    """A libtmux server that bounds each client process and operation."""

    def __init__(
        self,
        *args: Any,
        command_timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._command_timeout = command_timeout
        self._command_deadline: contextvars.ContextVar[float | None] = (
            contextvars.ContextVar(f"tmux_deadline_{id(self)}", default=None)
        )

    @contextmanager
    def deadline_after(self, timeout: float) -> Iterator[None]:
        """Limit commands in this context to the remaining operation budget."""
        requested_deadline = time.monotonic() + timeout
        current_deadline = self._command_deadline.get()
        if current_deadline is not None:
            requested_deadline = min(requested_deadline, current_deadline)
        token = self._command_deadline.set(requested_deadline)
        try:
            yield
        finally:
            self._command_deadline.reset(token)

    def cmd(
        self,
        cmd: str,
        *args: Any,
        target: str | int | None = None,
    ) -> tmux_cmd:
        """Execute tmux with the same argv and result format as libtmux."""
        server_args: list[str | int] = [cmd]
        if self.socket_name:
            server_args.insert(0, f"-L{self.socket_name}")
        if self.socket_path:
            server_args.insert(0, f"-S{self.socket_path}")
        if self.config_file:
            server_args.insert(0, f"-f{self.config_file}")
        if self.colors:
            if self.colors == 256:
                server_args.insert(0, "-2")
            elif self.colors == 88:
                server_args.insert(0, "-8")
            else:
                raise exc.UnknownColorOption

        command_args = ["-t", str(target), *args] if target is not None else [*args]
        deadline = self._command_deadline.get()
        timeout = self._command_timeout
        operation_deadline = False
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"tmux command {cmd!r} exceeded its operation deadline"
                )
            if remaining < timeout:
                timeout = remaining
                operation_deadline = True

        return _BoundedTmuxCommand(
            *server_args,
            *command_args,
            timeout=timeout,
            command_name=cmd,
            operation_deadline=operation_deadline,
        )
