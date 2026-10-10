"""Shared timing checkpoint writer for the validation wrapper and pytest hooks."""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime


def format_checkpoint(
    suite_name: str,
    event: str,
    status: str | None = None,
) -> str:
    """Format one machine-readable wall-clock and monotonic checkpoint."""
    wall_clock = datetime.now(UTC).isoformat(timespec="microseconds")
    monotonic = time.monotonic_ns() / 1_000_000_000
    line = (
        f"VALIDATE_TIMING name={suite_name} event={event} "
        f"wall_clock_utc={wall_clock} monotonic_seconds={monotonic:.9f}"
    )
    if status is not None:
        line += f" status={status}"
    return line


def write_checkpoint(
    fd: int,
    suite_name: str,
    event: str,
    status: str | None = None,
) -> None:
    """Write one checkpoint to the wrapper's timing stream."""
    os.write(fd, f"\n{format_checkpoint(suite_name, event, status)}\n".encode())


if __name__ == "__main__":
    _, suite_name, event, *status_args = sys.argv
    status = status_args[0] if status_args and status_args[0] else None
    print(f"\n{format_checkpoint(suite_name, event, status)}", flush=True)
