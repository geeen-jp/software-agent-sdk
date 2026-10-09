"""Helpers to reap the process groups a terminal's shell left behind.

Interactive bash puts every foreground and background job into its own process
group, so signalling only the shell's group leaves jobs such as servers alive.
"""

import os
import signal
import subprocess
import time


def descendant_process_groups(root_pid: int) -> set[int]:
    """Return the process groups of ``root_pid``'s live descendants.

    Must be called while the descendants are still attached to ``root_pid``;
    once the shell exits they are re-parented and can no longer be found.
    """
    ps = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid="],
        capture_output=True,
        text=True,
        timeout=5,
    )
    children: dict[int, list[tuple[int, int]]] = {}
    for line in ps.stdout.splitlines():
        pid, ppid, pgid = (int(field) for field in line.split())
        children.setdefault(ppid, []).append((pid, pgid))

    groups: set[int] = set()
    pending = [root_pid]
    while pending:
        for pid, pgid in children.get(pending.pop(), []):
            groups.add(pgid)
            pending.append(pid)
    groups.discard(os.getpgrp())
    return groups


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_groups(pgids: set[int], grace: float = 1.0) -> None:
    """SIGTERM the groups, then SIGKILL whatever survives ``grace`` seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pgid in pgids:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and any(_alive(p) for p in pgids):
            time.sleep(0.05)
