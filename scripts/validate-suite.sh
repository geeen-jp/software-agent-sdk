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
trap 'rm -f "$log" "$rc_file"' EXIT
start="$(date +%s)"

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
# A background job of a non-interactive shell has SIGINT/SIGQUIT ignored and
# `trap -` cannot undo that in dash; without this the suite inherits it and
# Ctrl+C tests fail.
{ env --default-signal=INT,QUIT "$@" 2>&1; echo $? >"$rc_file"; } | tee "$log" &
wait $!
rc="$(cat "$rc_file" 2>/dev/null || echo 1)"
if [ "$rc" -eq 0 ]; then
    report pass 0
else
    report fail "$rc"
fi
exit "$rc"
