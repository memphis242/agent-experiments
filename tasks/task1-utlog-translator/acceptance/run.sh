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
[ -f "$here/fixtures/expected.json" ] || { echo "fixtures missing" >&2; exit 2; }
rm -f "$out"
python3 -I "$here/check.py" "$repo" "$out"
rc=$?
case "$rc" in
    0|1) [ -f "$out" ] || exit 2; exit "$rc" ;;
    *) exit 2 ;;
esac
