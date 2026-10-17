#!/usr/bin/env bash
# Run CTF test scripts against a built cFS tree.
#
# Usage: run_ctf.sh [--config FILE] <test.json|test_dir>...
#
# Environment (all optional):
#   CFS_ROOT         cFS source+build tree        (default: parent of this ctf/ directory)
#   CFS_RUN_DIR      dir with core-cpu1 and cf/   (default: $CFS_ROOT/build-native_std/exe/cpu1)
#   CTF_RESULTS_DIR  results/log root             (default: $CFS_ROOT/ctf/results)
#   CTF_BUILD_CFS    "true" to rebuild first      (default: false)
#   CTF_BUILD_CMD    rebuild command, run in CFS_ROOT (default: make install)
#   CTF_SKIP_PORT_CHECK  "1" skips the free-port check (e.g. inside a private network namespace)
#   CTF_ATTACH       "1" to talk to an already-running cFS: skips the free-port check (tests then omit StartCfs)
# Exit status: 0 when every test passes, non-zero otherwise (also on setup errors).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/configs/mission.ini"
if [ "${1:-}" = "--config" ]; then
  [ $# -ge 2 ] || { echo "run_ctf.sh: --config needs a file" >&2; exit 2; }
  CONFIG="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2
fi
[ $# -ge 1 ] || { echo "usage: run_ctf.sh [--config FILE] <test.json|test_dir>..." >&2; exit 2; }

die() { echo "run_ctf.sh: ERROR: $*" >&2; exit 2; }

[ -x "$HERE/venv/bin/python" ] && [ -f "$HERE/CTF/ctf" ] \
  || die "CTF is not set up; run $HERE/setup_ctf.sh first."

export CFS_ROOT="${CFS_ROOT:-$(dirname "$HERE")}"
[ -d "$CFS_ROOT" ] || die "CFS_ROOT=$CFS_ROOT is not a directory; set CFS_ROOT to your cFS tree."
CFS_ROOT="$(cd "$CFS_ROOT" && pwd)"; export CFS_ROOT
export CFS_RUN_DIR="${CFS_RUN_DIR:-$CFS_ROOT/build-native_std/exe/cpu1}"
[ -x "$CFS_RUN_DIR/core-cpu1" ] \
  || die "$CFS_RUN_DIR/core-cpu1 not found or not executable; build cFS or set CFS_RUN_DIR."
export CTF_CCDD_DIR="$HERE/ccdd/json"
export CTF_HEADER_DIR="$HERE/headers"
export CTF_BUILD_CFS="${CTF_BUILD_CFS:-false}"
export CTF_BUILD_CMD="${CTF_BUILD_CMD:-make install}"

RESULTS_ROOT="${CTF_RESULTS_DIR:-$CFS_ROOT/ctf/results}"
mkdir -p "$RESULTS_ROOT"
export CTF_RESULTS_DIR="$(cd "$RESULTS_ROOT" && pwd)"

# Test paths are resolved before changing into the CTF checkout (CTF's
# relative plugin and header paths need it as the working directory).
TESTS=()
for t in "$@"; do
  [ -e "$t" ] || die "test script not found: $t"
  TESTS+=("$(cd "$(dirname "$t")" && pwd)/$(basename "$t")")
done

# Another cFS on this host would hold CI_LAB's UDP port 1234 and make every command go to the wrong instance.
if [ "${CTF_ATTACH:-0}" != 1 ] && [ "${CTF_SKIP_PORT_CHECK:-0}" != 1 ] && command -v ss >/dev/null 2>&1 && ss -H -lun 2>/dev/null | grep -Eq '[:.]1234[[:space:]]'; then
  die "UDP port 1234 is already in use (another cFS running?); stop it first."
fi

# CTF shells out to pgrep/pkill; the wrappers make that work when /bin/sh is bash (see shims/bin/pgrep).
export PATH="$HERE/shims/bin:$PATH"

echo "CTF config : $CONFIG"
echo "cFS run dir: $CFS_RUN_DIR"
echo "Results in : $CTF_RESULTS_DIR/Run_<date>_<time>/  (CTF_Log_File.log, regression_results_summary.*, cfs stdout)"

cd "$HERE/CTF"
set +e
"$HERE/venv/bin/python" ctf --config_file "$CONFIG" "${TESTS[@]}"
STATUS=$?
set -e

# CTF stops cFS in ShutdownCfs. If the run aborted first, stop only the instance started from CFS_RUN_DIR.
[ "${CTF_ATTACH:-0}" = 1 ] || for pid in $(pgrep -x core-cpu1 2>/dev/null || true); do
  [ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" = "$CFS_RUN_DIR" ] && kill "$pid" 2>/dev/null || true
done

LATEST="$(ls -1dt "$CTF_RESULTS_DIR"/Run_* 2>/dev/null | head -1 || true)"
if [ -n "$LATEST" ]; then
  echo "Latest run : $LATEST"
  [ -f "$LATEST/results_summary.txt" ] && cat "$LATEST/results_summary.txt"
fi
if [ "$STATUS" -ne 0 ]; then echo "run_ctf.sh: CTF reported failure (exit $STATUS)" >&2; fi
exit "$STATUS"
