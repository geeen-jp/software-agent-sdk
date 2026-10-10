#!/bin/sh
# Usage: validate-suite.sh NAME COMMAND [ARG...]
# Runs COMMAND, passes its output and exit status through, and prints
# `SUITE` lines (start, then pass/fail/interrupted with seconds and the
# pytest summary counts) so a slow, failing or hung suite can be identified
# from the log alone.
set -u

name="$1"
shift

log="$(mktemp "${TMPDIR:-/tmp}/oh-suite.XXXXXX")"
rc_file="$log.rc"
timing_log="$log.timing"
timing_fifo="$log.timing.fifo"
script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
timing_helper="$script_dir/../tests/validate_timing.py"
trap 'rm -f "$log" "$rc_file" "$timing_log" "$timing_fifo"' EXIT
start="$(date +%s)"

emit_timing() {
    python3 "$timing_helper" "$name" "$1" "${2:-}"
}

report() {
    tests="$(sed -nE 's/^=+ (.*) in [0-9.]+s.*/\1/p' "$log" | tail -n 1)"
    echo "SUITE name=$name status=$1 rc=$2 seconds=$(($(date +%s) - start)) tests=\"$tests\""
}

descendants() {
    for child in $(pgrep -P "$1"); do
        echo "$child"
        descendants "$child"
    done
}

# On cancel/timeout the runner sends a signal. SIGABRT makes python
# descendants (pytest enables faulthandler) dump the stack of the stuck test.
on_signal() {
    report interrupted "$1"
    for pid in $(descendants $$); do
        case "$(ps -o comm= -p "$pid")" in python*) kill -ABRT "$pid" ;; esac
    done 2>/dev/null
    sleep 2
    exit "$1"
}
trap 'on_signal 130' INT
trap 'on_signal 143' TERM HUP

echo "SUITE name=$name status=start"
# Stream timing records independently so pytest's output capture cannot hide
# its session hooks. Descriptor 3 stays open until after the suite's tee EOF.
mkfifo "$timing_fifo"
tee "$timing_log" <"$timing_fifo" &
timing_tee_pid=$!
exec 3>"$timing_fifo"
# A background job of a non-interactive shell has SIGINT/SIGQUIT ignored and
# `trap -` cannot undo that in dash; without this the suite inherits it and
# Ctrl+C tests fail.
{
    emit_timing command_launch >&3
    env --default-signal=INT,QUIT \
        OH_VALIDATE_TIMING_FD=3 \
        OH_VALIDATE_SUITE_NAME="$name" \
        "$@" 2>&1
    child_rc=$?
    emit_timing child_exit "$child_rc" >&3
    echo "$child_rc" >"$rc_file"
} | tee "$log" &
suite_tee_pid=$!
wait "$suite_tee_pid"
suite_tee_rc=$?
emit_timing tee_eof "$suite_tee_rc" >&3
exec 3>&-
wait "$timing_tee_pid"
rc="$(cat "$rc_file" 2>/dev/null || echo 1)"
if [ "$rc" -eq 0 ]; then
    report pass 0
else
    report fail "$rc"
fi
exit "$rc"
