"""Tests for bounded tmux command execution."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openhands.tools.terminal.terminal import tmux_server
from openhands.tools.terminal.terminal.tmux_server import BoundedTmuxServer


def _stub_tmux_process(monkeypatch, process):
    monkeypatch.setattr(tmux_server.shutil, "which", lambda _: "/usr/bin/tmux")
    popen = Mock(return_value=process)
    monkeypatch.setattr(
        tmux_server,
        "subprocess",
        SimpleNamespace(
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
            Popen=popen,
        ),
    )
    return popen


def test_tmux_command_timeout_kills_and_reaps_process(monkeypatch):
    process = Mock(returncode=-9)

    def communicate(timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired(["tmux"], timeout)
        return "", ""

    process.communicate.side_effect = communicate
    popen = _stub_tmux_process(monkeypatch, process)
    server = BoundedTmuxServer(socket_name="unit-test", command_timeout=10.0)

    with server.deadline_after(0.5):
        with pytest.raises(TimeoutError, match="capture-pane.*timed out"):
            server.cmd("capture-pane", "-p")

    timeout = process.communicate.call_args_list[0].kwargs["timeout"]
    assert 0 < timeout <= 0.5
    assert process.communicate.call_count == 2
    process.kill.assert_called_once_with()
    popen.assert_called_once_with(
        ["/usr/bin/tmux", "-Lunit-test", "capture-pane", "-p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="backslashreplace",
    )


def test_tmux_command_preserves_lib_tmux_output_parsing(monkeypatch):
    process = Mock(
        communicate=Mock(return_value=("first\nsecond\n\n", "warning\n\n")),
        returncode=23,
    )
    _stub_tmux_process(monkeypatch, process)
    server = BoundedTmuxServer(socket_name="unit-test")

    result = server.cmd("list-sessions")

    assert result.stdout == ["first", "second"]
    assert result.stderr == ["warning"]
    assert result.returncode == 23
