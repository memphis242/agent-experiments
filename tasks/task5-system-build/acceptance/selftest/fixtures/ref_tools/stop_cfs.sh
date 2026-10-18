#!/usr/bin/env bash
# Reference of the C.1 stop script (self-test fixture).
set -euo pipefail
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CPU="${CFS_CPU:-cpu1}"
if [[ "$CPU" == cpu1 ]]; then DEF_RUN="${ROOT}/build-native_std/run"; else DEF_RUN="${ROOT}/build-native_std/run/cpu2"; fi
readonly PIDFILE="${CFS_RUN_DIR:-$DEF_RUN}/cfs.pid"
[[ -f "${PIDFILE}" ]] || exit 0
pid="$(cat "${PIDFILE}")"
kill -TERM "$pid" 2>/dev/null || true
for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
kill -KILL "$pid" 2>/dev/null || true
rm -f "${PIDFILE}"
