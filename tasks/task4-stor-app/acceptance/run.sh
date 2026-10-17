#!/usr/bin/env bash
# Usage: run.sh <repo-path> <out.json>
# Exit 0 every check passed, 1 a check failed, 2 infrastructure error.
set -u
if [ "$#" -ne 2 ]; then
    echo "usage: run.sh <repo-path> <out.json>" >&2
    exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$1" 2>/dev/null && pwd)" || { echo "repo $1 not found" >&2; exit 2; }
out="$(cd "$(dirname "$2")" 2>/dev/null && pwd)/$(basename "$2")" || { echo "bad out path" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 2; }
command -v timeout >/dev/null || { echo "timeout not found" >&2; exit 2; }
[ -f "$here/check.py" ] && [ -f "$here/hidden_tests/coveragetest_stor_hidden.c" ] \
    || { echo "pack files missing" >&2; exit 2; }
rm -f "$out"

# All scratch space of this invocation lives under one directory, so cleanup never touches another run.
scratch_root="${ACCEPT_SCRATCH_ROOT:-/workspace/tmp-acceptance}"
mkdir -p "$scratch_root" || { echo "cannot create $scratch_root" >&2; exit 2; }
scratch="$(mktemp -d "$scratch_root/stor-run-XXXXXX")" || { echo "cannot create scratch directory" >&2; exit 2; }
cleanup() {
    # Never leave a cFS running, whatever happened: stop those whose working directory is under our scratch.
    for pid in $(pgrep -x core-cpu1 2>/dev/null || true); do
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
        case "$cwd" in "$scratch"/*) kill -KILL "$pid" 2>/dev/null || true ;; esac
    done
    rm -rf "$scratch"
}
# The agent's code (build, cFS, test framework) runs under this script: hand it no credentials.
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
trap cleanup EXIT
trap 'exit 2' TERM INT

export STOR_PACK_TMP="$scratch"
# Each step enforces its own timeout; the total stays under 30 minutes.
timeout --kill-after=20 1750 python3 -I -B "$here/check.py" "$repo" "$out"
rc=$?
case "$rc" in
    0|1) [ -f "$out" ] || exit 2; exit "$rc" ;;
    *) exit 2 ;;
esac
