#!/usr/bin/env bash
# Stop the cFS instance started by run_cfs.sh, using its PID file.
# Exits 0 if cFS was stopped or was not running.
#
# Environment overrides: CFS_RUN_DIR (see run_cfs.sh).
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly RUN_DIR="${CFS_RUN_DIR:-${ROOT}/build-native_std/run}"
readonly PIDFILE="${RUN_DIR}/cfs.pid"

if [[ ! -f "${PIDFILE}" ]]; then
    echo "stop_cfs: no PID file; cFS not started by run_cfs.sh"
    exit 0
fi
pid="$(cat "${PIDFILE}")"
[[ "${pid}" =~ ^[0-9]+$ ]] || { echo "stop_cfs: error: bad PID file ${PIDFILE}" >&2; exit 1; }

if kill -0 "${pid}" 2>/dev/null; then
    kill -TERM "${pid}" 2>/dev/null || true
    for _ in $(seq 1 50); do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "${pid}" 2>/dev/null; then
        kill -KILL "${pid}" 2>/dev/null || true
        sleep 0.2
    fi
    kill -0 "${pid}" 2>/dev/null && { echo "stop_cfs: error: PID ${pid} would not stop" >&2; exit 1; }
    echo "stop_cfs: stopped PID ${pid}"
else
    echo "stop_cfs: PID ${pid} was not running"
fi
rm -f "${PIDFILE}"
