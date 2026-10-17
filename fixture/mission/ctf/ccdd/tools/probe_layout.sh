#!/usr/bin/env bash
# Compile and run layout_probe.c against a built cFS tree and print the result.
#
# Usage: probe_layout.sh [CFS_ROOT]
#   CFS_ROOT   cFS source+build tree (default: $CFS_ROOT, else the parent of ctf/)
#   BUILD_DIR  build directory name under CFS_ROOT (default: build-native_std)
#   CPU        target cpu build to take include paths from (default: cpu1)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFS_ROOT="$(cd "${1:-${CFS_ROOT:-$HERE/../../..}}" && pwd)"
BUILD_DIR="${BUILD_DIR:-build-native_std}"
CPU="${CPU:-cpu1}"
FLAGS="$CFS_ROOT/$BUILD_DIR/native/default_$CPU/apps/sample_app/CMakeFiles/sample_app.dir/flags.make"
[ -f "$FLAGS" ] || { echo "probe_layout.sh: ERROR: $FLAGS not found; is $CFS_ROOT built?" >&2; exit 1; }

# Include paths and defines are taken from how the build compiled sample_app,
# so macros resolve to exactly what the flight software sees.
INCLUDES="$(sed -n 's/^C_INCLUDES = //p' "$FLAGS")"
DEFINES="$(sed -n 's/^C_DEFINES = //p' "$FLAGS")"
EXTRA=""
for app in to_lab ci_lab hk; do EXTRA="$EXTRA -I$CFS_ROOT/apps/$app/fsw/inc"; done
# Generated per-app headers (msgids, msgstruct) live next to the sources or in the build inc dir.
for dir in "$CFS_ROOT"/apps/*/config; do EXTRA="$EXTRA -I$dir"; done

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
# shellcheck disable=SC2086
cc -std=gnu99 -w ${CC_DEBUG:-} $DEFINES $INCLUDES $EXTRA -o "$OUT/probe" "$HERE/layout_probe.c"
"$OUT/probe"
