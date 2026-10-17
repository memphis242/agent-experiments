#!/usr/bin/env bash
# Usage: run.sh <repo-path> <out.json>
# Exit 0 every check passed, 1 a check failed, 2 infrastructure error.
# Total budget 25 minutes; cFS is always stopped and the scratch tree removed on exit.
set -u
if [ "$#" -ne 2 ]; then
    echo "usage: run.sh <repo-path> <out.json>" >&2
    exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$1" 2>/dev/null && pwd)" || { echo "repo $1 not found" >&2; exit 2; }
out="$(cd "$(dirname "$2")" 2>/dev/null && pwd)/$(basename "$2")" || { echo "bad out path" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 2; }
[ -f "$here/baseline_coverage.json" ] || { echo "baseline coverage missing" >&2; exit 2; }
[ -f "$here/hidden_tests/hk_hidden_tests.c" ] || { echo "hidden unit test missing" >&2; exit 2; }

# Scratch space for the copy of the repository: always under /workspace (the tree and its build are large).
HK_ACCEPT_TMPDIR="${HK_ACCEPT_TMPDIR:-/workspace/tmp-acceptance}"
case "$HK_ACCEPT_TMPDIR" in
    /workspace/*) ;;
    *) echo "HK_ACCEPT_TMPDIR must be under /workspace" >&2; exit 2 ;;
esac
export HK_ACCEPT_TMPDIR
mkdir -p "$HK_ACCEPT_TMPDIR" 2>/dev/null || { echo "cannot create $HK_ACCEPT_TMPDIR" >&2; exit 2; }
work="$(mktemp -d "$HK_ACCEPT_TMPDIR/run.XXXXXX")" || { echo "cannot create scratch directory" >&2; exit 2; }
export HK_ACCEPT_TMPDIR="$work"

cleanup() {
    # Stop any core-cpu1 whose working directory lies inside the scratch tree, then remove it.
    for pid in $(pgrep -x core-cpu1 2>/dev/null || true); do
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
        case "$cwd" in
            "$work"/*) kill -TERM "$pid" 2>/dev/null; sleep 0.5; kill -KILL "$pid" 2>/dev/null || true ;;
        esac
    done
    [ -n "${HK_ACCEPT_KEEP:-}" ] || rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 2' INT TERM

rm -f "$out"
timeout --kill-after=30 1500 python3 -I "$here/check.py" "$repo" "$out"
rc=$?
case "$rc" in
    0|1) [ -f "$out" ] || exit 2; exit "$rc" ;;
    *) exit 2 ;;
esac
