#!/usr/bin/env bash
# Reference of the C.1 run script (self-test fixture): CFS_CPU selects the processor; cpu2 gets a private
# /dev/shm (its own OSAL RAM disk) through an unprivileged user and mount namespace.
set -euo pipefail
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CPU="${CFS_CPU:-cpu1}"
case "$CPU" in cpu1|cpu2) ;; *) echo "run_cfs: error: CFS_CPU must be cpu1 or cpu2" >&2; exit 1 ;; esac
readonly EXE_DIR="${ROOT}/build-native_std/exe/${CPU}"
if [[ "$CPU" == cpu1 ]]; then DEF_RUN="${ROOT}/build-native_std/run"; else DEF_RUN="${ROOT}/build-native_std/run/cpu2"; fi
readonly RUN_DIR="${CFS_RUN_DIR:-$DEF_RUN}"
readonly LOG="${RUN_DIR}/cfs.log" PIDFILE="${RUN_DIR}/cfs.pid" TIMEOUT="${CFS_TIMEOUT:-30}"
readonly MARKER="CFE_ES_Main entering OPERATIONAL state"
[[ -x "${EXE_DIR}/core-${CPU}" ]] || { echo "run_cfs: error: ${EXE_DIR}/core-${CPU} not found" >&2; exit 1; }
mkdir -p "${RUN_DIR}"
if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then echo "run_cfs: error: already running" >&2; exit 1; fi
: > "${LOG}"
cd "${EXE_DIR}"
if [[ "$CPU" == cpu2 ]]; then
    u="$(id -u)"; g="$(id -g)"
    setsid unshare -Urm --propagation private sh -c "mount -t tmpfs tmpfs /dev/shm && exec unshare -U --map-user=$u --map-group=$g ./core-cpu2" >> "${LOG}" 2>&1 < /dev/null &
else
    setsid ./core-cpu1 >> "${LOG}" 2>&1 < /dev/null &
fi
readonly PID=$!
echo "${PID}" > "${PIDFILE}"
deadline=$((SECONDS + TIMEOUT))
while (( SECONDS < deadline )); do
    grep -qF "${MARKER}" "${LOG}" && { echo "${PID}"; exit 0; }
    kill -0 "${PID}" 2>/dev/null || { echo "run_cfs: error: exited early" >&2; tail -n 20 "${LOG}" >&2; exit 1; }
    sleep 0.2
done
echo "run_cfs: error: not OPERATIONAL within ${TIMEOUT}s" >&2
exit 1
