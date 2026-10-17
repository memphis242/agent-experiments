#!/usr/bin/env bash
# Build the flattened cFS mission template: one plain git repository, no
# submodules, no nested .git entries, with fixture/mission/ overlaid on top.
#
# Usage: build-template.sh [TEMPLATE_DIR]
#   TEMPLATE_DIR defaults to /workspace/agent-experiments-fixture/mission-template
#
# The GitHub clone (with submodules) is cached under CACHE_DIR; re-running
# reuses it and does not contact GitHub. Override with CFS_CACHE_DIR.
# Re-running replaces TEMPLATE_DIR entirely.
set -euo pipefail

readonly CFS_URL="https://github.com/nasa/cFS.git"
readonly CFS_COMMIT="3ce9b2734f3979935b7ab93acee1758fc96cbb93"
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TEMPLATE_DIR="${1:-/workspace/agent-experiments-fixture/mission-template}"
readonly CACHE_DIR="${CFS_CACHE_DIR:-/workspace/agent-experiments-cache}"
readonly CACHE_CLONE="${CACHE_DIR}/cFS-${CFS_COMMIT:0:12}"

die() { echo "build-template: error: $*" >&2; exit 1; }

[[ -d "${HERE}/mission" ]] || die "missing overlay directory ${HERE}/mission"
[[ -n "${TEMPLATE_DIR}" && "${TEMPLATE_DIR}" != "/" ]] || die "invalid template path"

# 1. Populate the cache: a full clone with submodules at the pinned commit.
#    A marker file is written last so an interrupted clone is redone.
if [[ ! -f "${CACHE_CLONE}/.cache-complete" ]]; then
    echo "build-template: populating cache ${CACHE_CLONE}"
    mkdir -p "${CACHE_DIR}"
    rm -rf "${CACHE_CLONE}"
    git clone "${CFS_URL}" "${CACHE_CLONE}"
    git -C "${CACHE_CLONE}" checkout --detach "${CFS_COMMIT}"
    git -C "${CACHE_CLONE}" submodule update --init --recursive
    touch "${CACHE_CLONE}/.cache-complete"
fi

# 2. Verify the cache is at the pinned bundle commit and that every submodule
#    commit matches the record in PINNED_COMMITS.md.
[[ "$(git -C "${CACHE_CLONE}" rev-parse HEAD)" == "${CFS_COMMIT}" ]] \
    || die "cache clone is not at ${CFS_COMMIT}; delete ${CACHE_CLONE} and re-run"
while read -r sha path; do
    grep -q "${sha}" "${HERE}/PINNED_COMMITS.md" \
        || die "submodule commit ${sha} (${path}) is not recorded in PINNED_COMMITS.md"
done < <(git -C "${CACHE_CLONE}" submodule status --recursive | sed 's/^[-+U ]//' | awk '{print $1, $2}')

# 3. Copy into a staging directory beside the destination, then flatten.
mkdir -p "$(dirname "${TEMPLATE_DIR}")"
readonly STAGE="${TEMPLATE_DIR}.staging.$$"
trap 'rm -rf "${STAGE}"' EXIT
rm -rf "${STAGE}"
cp -a "${CACHE_CLONE}" "${STAGE}"
rm -f "${STAGE}/.cache-complete"

# Remove every nested .git directory or file (submodule gitlinks), and the
# now-meaningless submodule manifest.
find "${STAGE}" -name .git -prune -exec rm -rf {} +
find "${STAGE}" -name .gitmodules -type f -delete
[[ -z "$(find "${STAGE}" -name .git -print -quit)" ]] || die "nested .git entries remain"

# 4. One repository. The upstream tree is staged with -f so nothing tracked
#    upstream is dropped by an upstream .gitignore.
git -C "${STAGE}" init --quiet --initial-branch=main
git -C "${STAGE}" add -f -A

# 5. Overlay the agent-facing files (README, tools/, ctf/, ...). Nested .git
#    entries inside the overlay (e.g. a cloned framework) are removed so the
#    result stays a single repository; the overlay's own .gitignore files are
#    honored when staging it.
cp -a "${HERE}/mission/." "${STAGE}/"
find "${STAGE}" -name .git -not -path "${STAGE}/.git" -prune -exec rm -rf {} +
git -C "${STAGE}" add -A
git -C "${STAGE}" \
    -c user.name="Mission Template" -c user.email="mission-template@example.invalid" \
    commit --quiet -m "Mission template"
[[ -z "$(find "${STAGE}" -name .git -not -path "${STAGE}/.git" -print -quit)" ]] \
    || die "nested .git entries remain"

# 6. Atomically replace the destination.
rm -rf "${TEMPLATE_DIR}"
mv "${STAGE}" "${TEMPLATE_DIR}"
trap - EXIT

echo "template: ${TEMPLATE_DIR}"
echo "size:     $(du -sh "${TEMPLATE_DIR}" | cut -f1) total, $(du -sh --exclude=.git "${TEMPLATE_DIR}" | cut -f1) working tree"
echo "commit:   $(git -C "${TEMPLATE_DIR}" rev-parse --short HEAD)"
