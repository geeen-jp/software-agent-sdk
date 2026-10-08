#!/bin/sh
# Runs "$@" in a clean environment (env -i) holding only the allowlist below
# and throwaway HOME/TMPDIR directories. Nothing else is inherited.
set -eu

cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
sandbox="$(mktemp -d "${TMPDIR:-/tmp}/oh-validate.XXXXXX")"
trap 'rm -rf "$sandbox"' EXIT
mkdir "$sandbox/home" "$sandbox/tmp"

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
    UV_CACHE_DIR="${UV_CACHE_DIR:-$cache_home/uv}" \
    UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$data_home/uv/python}" \
    PRE_COMMIT_HOME="${PRE_COMMIT_HOME:-$cache_home/pre-commit}" \
    CI=true \
    "$@"
