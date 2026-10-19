#!/usr/bin/env bash
# Usage: run.sh <repo-path> <out.json>
# Exit 0 every check passed, 1 a check failed, 2 infrastructure error found before the agent's code was used.
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
# From here on the agent's code is used: every outcome is a result file with exit 0 or 1, never exit 2.
fallback() { # <detail>: a valid result when check.py could not write one
    python3 -I - "$out" "$1" <<'PY'
import json, sys
fb = ("This requirement could not be verified because the review ran out of time: the build, the unit tests, the cFS "
      "startups with STOR or the CTF test took far longer than expected, so check that none of them hangs.")
json.dump({"passed": False, "checks": [{"id": "review-completed", "passed": False, "feedback": fb,
                                         "detail": sys.argv[2]}]}, open(sys.argv[1], "w"), indent=2)
PY
}
trap 'fallback "run.sh was interrupted"; exit 1' TERM INT
# check.py keeps its own budget (36 min) and writes its result on SIGTERM; the total stays under the harness's 40 min.
timeout --preserve-status --kill-after=30 2220 python3 -I -B "$here/check.py" "$repo" "$out"
rc=$?
if [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]; then
    if python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["passed"] == (sys.argv[2] == "0") else 1)' "$out" "$rc" 2>/dev/null; then
        exit "$rc"
    fi
    fallback "check.py exited $rc without a consistent result file"
    exit 1
fi
if [ "$rc" -eq 2 ] && [ ! -f "$out" ]; then
    exit 2  # check.py refused to start (arguments, tools, scratch space): the agent's code was not used
fi
fallback "check.py ended with status $rc (killed after the time limit, or crashed)"
exit 1
