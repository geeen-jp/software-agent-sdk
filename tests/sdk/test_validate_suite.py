"""`scripts/validate-suite.sh` must make a slow, failing or hung suite identifiable."""

import re
import signal
import subprocess
import sys
from datetime import datetime

from tests.conftest import REPO_ROOT


SCRIPT = "scripts/validate-suite.sh"
TIMING_PATTERN = re.compile(
    r"^VALIDATE_TIMING name=(?P<name>\S+) event=(?P<event>\S+) "
    r"wall_clock_utc=(?P<wall_clock>\S+) "
    r"monotonic_seconds=(?P<monotonic>\d+\.\d+)"
    r"(?: status=(?P<status>-?\d+))?$",
    re.MULTILINE,
)


def run_suite(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", SCRIPT, "demo", *command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def timing_records(output: str) -> list[dict[str, str | None]]:
    """Parse and validate the wrapper's timing checkpoints."""
    records = [match.groupdict() for match in TIMING_PATTERN.finditer(output)]
    for record in records:
        wall_clock = record["wall_clock"]
        monotonic = record["monotonic"]
        assert wall_clock is not None
        assert monotonic is not None
        datetime.fromisoformat(wall_clock.replace("Z", "+00:00"))
        float(monotonic)
    return records


def monotonic_values(records: list[dict[str, str | None]]) -> list[float]:
    values = []
    for record in records:
        monotonic = record["monotonic"]
        assert monotonic is not None
        values.append(float(monotonic))
    return values


def test_validate_timing_hook_probe():
    """A tiny pytest target for checking the env-gated lifecycle hooks."""


def test_validate_suite_reports_pass_with_pytest_counts():
    result = run_suite("echo", "=== 916 passed, 44 skipped in 437.78s (0:07:17) ===")
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "SUITE name=demo status=start"
    assert re.fullmatch(
        r'SUITE name=demo status=pass rc=0 seconds=\d+ tests="916 passed, 44 skipped"',
        lines[-1],
    )
    records = timing_records(result.stdout)
    assert [record["event"] for record in records] == [
        "command_launch",
        "child_exit",
        "tee_eof",
    ]
    assert records[1]["status"] == "0"
    assert monotonic_values(records) == sorted(monotonic_values(records))


def test_validate_suite_keeps_exit_status_and_reports_failure():
    result = run_suite("sh", "-c", "echo boom; exit 3")
    assert result.returncode == 3
    assert "boom" in result.stdout
    assert re.search(
        r'^SUITE name=demo status=fail rc=3 seconds=\d+ tests=""$',
        result.stdout,
        re.MULTILINE,
    )
    records = timing_records(result.stdout)
    assert [record["event"] for record in records] == [
        "command_launch",
        "child_exit",
        "tee_eof",
    ]
    assert records[1]["status"] == "3"


def test_validate_suite_records_pytest_session_timing():
    result = run_suite(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests/sdk/test_validate_suite.py::test_validate_timing_hook_probe",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    records = timing_records(result.stdout)
    assert [record["event"] for record in records] == [
        "command_launch",
        "pytest_session_start",
        "pytest_session_finish",
        "child_exit",
        "tee_eof",
    ]
    assert records[2]["status"] == "0"
    monotonic_times = monotonic_values(records)
    assert monotonic_times == sorted(monotonic_times)


def test_validate_suite_does_not_start_the_command_with_sigint_ignored():
    result = run_suite(
        sys.executable,
        "-c",
        "import signal; print(signal.getsignal(signal.SIGINT) is signal.SIG_IGN)",
    )
    assert result.returncode == 0
    assert "False" in result.stdout.splitlines()


def test_validate_suite_names_the_suite_when_terminated():
    hang = (
        "import faulthandler, time; faulthandler.enable(); "
        "print('ready', flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen(
        ["sh", SCRIPT, "demo", sys.executable, "-c", hang],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "SUITE name=demo status=start"
    observed_lines = []
    while not observed_lines or observed_lines[-1] != "ready":
        line = proc.stdout.readline().strip()
        if line:
            observed_lines.append(line)
    assert any("event=command_launch" in line for line in observed_lines[:-1])
    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode == 143
    assert "SUITE name=demo status=interrupted rc=143" in output
    assert "Current thread" in output
