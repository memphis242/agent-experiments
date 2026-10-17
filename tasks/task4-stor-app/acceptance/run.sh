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
[ -f "$here/check.py" ] && [ -f "$here/hidden_tests/coveragetest_stor_hidden.c" ] \
    || { echo "pack files missing" >&2; exit 2; }
rm -f "$out"
# Each check enforces its own timeout; the total stays under 30 minutes.
timeout --kill-after=20 1750 python3 -I "$here/check.py" "$repo" "$out"
rc=$?
# Never leave a cFS running, whatever happened above.
for pid in $(pgrep -x core-cpu1 2>/dev/null || true); do
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    case "$cwd" in
        */stor-pack-*/tree/build-native_std/exe/cpu1) kill -KILL "$pid" 2>/dev/null || true ;;
    esac
done
case "$rc" in
    0|1) [ -f "$out" ] || exit 2; exit "$rc" ;;
    *) exit 2 ;;
esac
