#!/usr/bin/env bash
# Usage: run.sh <repo-path> <out.json>
# Exit 0 every check passed, 1 a check failed, 2 infrastructure error before the agent's code was used.
# The per-subsystem summary is written next to <out.json> as <out-stem>.subsystems.json.
# Budget: check.py stops starting work after 180 minutes; the harness's --acceptance-timeout must be at least 10920 s.
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
for f in check.py frames.py spec.py node.py hidden_glue.py hidden_tests/t5_model.h baseline_manifest.json.gz; do
    [ -f "$here/$f" ] || { echo "pack file $f missing" >&2; exit 2; }
done
rm -f "$out" "${out%.json}.subsystems.json"

scratch_root="${ACCEPT_SCRATCH_ROOT:-/workspace/tmp-acceptance}"
mkdir -p "$scratch_root" || { echo "cannot create $scratch_root" >&2; exit 2; }
scratch="$(mktemp -d "$scratch_root/t5-run-XXXXXX")" || { echo "cannot create scratch directory" >&2; exit 2; }
cleanup() {
    # Never leave a node running: stop every core-cpu1/core-cpu2 whose working directory is under our scratch.
    for name in core-cpu1 core-cpu2; do
        for pid in $(pgrep -x "$name" 2>/dev/null || true); do
            cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
            case "$cwd" in "$scratch"/*) kill -KILL "$pid" 2>/dev/null || true ;; esac
        done
    done
    [ "${T5_PACK_KEEP:-}" = 1 ] || rm -rf "$scratch"
}
# The agent's code (build, cFS, test framework) runs under this script: hand it no credentials.
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
trap cleanup EXIT
trap 'exit 2' TERM INT

export T5_PACK_TMP="$scratch"
timeout --kill-after=30 10920 python3 -I -B "$here/check.py" "$repo" "$out"
rc=$?
case "$rc" in
    0|1) [ -f "$out" ] || exit 2; exit "$rc" ;;
    *) exit 2 ;;
esac
