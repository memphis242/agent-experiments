#!/usr/bin/env bash
# Start cFS (core-cpu1) in the background and wait until it is operational.
# Prints the PID on success. Exits non-zero, printing the log tail, otherwise.
#
# Environment overrides:
#   CFS_RUN_DIR   directory for cfs.log and cfs.pid (default: <tree>/build-native_std/run)
#   CFS_TIMEOUT   seconds to wait for OPERATIONAL (default: 30)
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly EXE_DIR="${ROOT}/build-native_std/exe/cpu1"
readonly RUN_DIR="${CFS_RUN_DIR:-${ROOT}/build-native_std/run}"
readonly LOG="${RUN_DIR}/cfs.log"
readonly PIDFILE="${RUN_DIR}/cfs.pid"
readonly TIMEOUT="${CFS_TIMEOUT:-30}"
readonly MARKER="CFE_ES_Main entering OPERATIONAL state"

fail() { echo "run_cfs: error: $*" >&2; exit 1; }

[[ -x "${EXE_DIR}/core-cpu1" ]] \
    || fail "${EXE_DIR}/core-cpu1 not found; build with: make native_std.prep && make -j16 native_std.install"

mkdir -p "${RUN_DIR}"
if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    fail "cFS already running (PID $(cat "${PIDFILE}")); run tools/stop_cfs.sh first"
fi
rm -f "${PIDFILE}"
: > "${LOG}"

cd "${EXE_DIR}"
setsid ./core-cpu1 >> "${LOG}" 2>&1 < /dev/null &
readonly PID=$!
echo "${PID}" > "${PIDFILE}"

deadline=$((SECONDS + TIMEOUT))
while (( SECONDS < deadline )); do
    if grep -qF "${MARKER}" "${LOG}"; then
        echo "${PID}"
        exit 0
    fi
    if ! kill -0 "${PID}" 2>/dev/null; then
        rm -f "${PIDFILE}"
        echo "run_cfs: error: core-cpu1 exited before reaching OPERATIONAL; log tail:" >&2
        tail -n 40 "${LOG}" >&2
        exit 1
    fi
    sleep 0.2
done

echo "run_cfs: error: no '${MARKER}' within ${TIMEOUT}s; log tail:" >&2
tail -n 40 "${LOG}" >&2
"${ROOT}/tools/stop_cfs.sh" >&2 || true
exit 1
