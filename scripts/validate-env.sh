#!/bin/sh
# Runs "$@" in a clean environment (env -i) holding only the allowlist below
# and throwaway HOME/TMPDIR directories. Nothing else is inherited.
set -eu

cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
sandbox="$(mktemp -d "${TMPDIR:-/tmp}/oh-validate.XXXXXX")"

# Everything the run starts inherits TMPDIR=$sandbox/tmp (see env -i below), also
# after being re-parented away from this shell; `ps e` shows each environment.
sandbox_pids() {
    snapshot="$(ps -A eww -o pid= -o command= 2>/dev/null || true)"
    printf '%s\n' "$snapshot" |
        awk -v marker="TMPDIR=$sandbox/tmp" -v self="$$" \
            'index($0, marker) && $1 != self { print $1 }'
}

# TERM, then KILL. Returns non-zero while a process of the run is still alive.
reap_sandbox() {
    for signal in TERM KILL; do
        pids="$(sandbox_pids)"
        [ -n "$pids" ] || return 0
        # shellcheck disable=SC2086
        kill -"$signal" $pids 2>/dev/null || true
        tries=0
        while [ "$tries" -lt 20 ] && [ -n "$(sandbox_pids)" ]; do
            sleep 0.1
            tries=$((tries + 1))
        done
    done
    [ -z "$(sandbox_pids)" ]
}

cleanup() {
    if reap_sandbox; then
        rm -rf "$sandbox"
    else
        echo "validate-env: processes still alive; keeping $sandbox" >&2
    fi
}
trap cleanup EXIT
mkdir "$sandbox/home" "$sandbox/tmp" "$sandbox/tmux"
chmod 700 "$sandbox/tmux"

# LANG and LC_* are the only allowlisted names matched by prefix.
locale_vars="$(env | sed -n 's/^\(LC_[A-Za-z_]*=.*\)$/\1/p' | tr '\n' ' ')"

# $locale_vars is deliberately unquoted: one word per NAME=value pair.
# shellcheck disable=SC2086
env -i \
    PATH="$PATH" \
    LANG="${LANG:-C.UTF-8}" \
    $locale_vars \
    HOME="$sandbox/home" \
    TMPDIR="$sandbox/tmp" \
    TMUX_TMPDIR="$sandbox/tmux" \
    UV_CACHE_DIR="${UV_CACHE_DIR:-$cache_home/uv}" \
    UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$data_home/uv/python}" \
    PRE_COMMIT_HOME="${PRE_COMMIT_HOME:-$cache_home/pre-commit}" \
    CI=true \
    "$@"
