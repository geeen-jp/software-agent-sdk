"""`scripts/validate-suite.sh` must make a slow, failing or hung suite identifiable."""

import re
import signal
import subprocess
import sys

from tests.conftest import REPO_ROOT


SCRIPT = "scripts/validate-suite.sh"


def run_suite(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", SCRIPT, "demo", *command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_validate_suite_reports_pass_with_pytest_counts():
    result = run_suite("echo", "=== 916 passed, 44 skipped in 437.78s (0:07:17) ===")
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "SUITE name=demo status=start"
    assert re.fullmatch(
        r'SUITE name=demo status=pass rc=0 seconds=\d+ tests="916 passed, 44 skipped"',
        lines[-1],
    )


def test_validate_suite_keeps_exit_status_and_reports_failure():
    result = run_suite("sh", "-c", "echo boom; exit 3")
    assert result.returncode == 3
    assert "boom" in result.stdout
    assert re.search(
        r'^SUITE name=demo status=fail rc=3 seconds=\d+ tests=""$',
        result.stdout,
        re.MULTILINE,
    )


def test_validate_suite_names_the_suite_when_terminated():
    hang = "import faulthandler, time; faulthandler.enable(); time.sleep(60)"
    proc = subprocess.Popen(
        ["sh", SCRIPT, "demo", sys.executable, "-c", hang],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "SUITE name=demo status=start"
    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode == 143
    assert "SUITE name=demo status=interrupted rc=143" in output
    assert "Current thread" in output
