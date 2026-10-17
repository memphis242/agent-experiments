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
for tool in python3 make cmake gcc gcov; do
    command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 2; }
done
rm -f "$out"

# Private network, mount and process namespaces keep UDP 1234/2234, /dev/shm and the
# process table (CTF stops cFS with pkill) away from other cFS instances on the host. The outer namespace (mapped root) prepares
# loopback and /dev/shm; the inner one maps the caller's own uid back so cFS runs
# unprivileged, as it does on the host. Without namespace support the checks
# run on the host.
uid="$(id -u)"; gid="$(id -g)"
isolate=(unshare -rnmpf --mount-proc python3 -I "$here/ns_setup.py" -- unshare -U "--map-user=$uid" "--map-group=$gid")
launcher=(env ACCEPT_ISOLATED=0)
if [ "${ACCEPT_NO_NAMESPACE:-0}" != "1" ] && command -v unshare >/dev/null && "${isolate[@]}" true 2>/dev/null; then
    launcher=("${isolate[@]}" env ACCEPT_ISOLATED=1)
fi

# 19 minutes: the harness allows 20.
timeout -k 10 1140 "${launcher[@]}" python3 -I "$here/check.py" "$repo" "$out"
rc=$?
case "$rc" in
    0|1) [ -f "$out" ] || exit 2; exit "$rc" ;;
    *) exit 2 ;;
esac
