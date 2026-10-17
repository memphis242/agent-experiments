#!/usr/bin/env bash
# Self-test of the pack: run it on an unmodified clone of the mission template and verify the outcome.
# Usage: selftest.sh [template-repo]   (default /workspace/agent-experiments-fixture/mission-template)
# Expected: run.sh exits 1; out.json is valid; the build, unit-test and coverage checks pass (nothing was
# changed); every feature check fails with non-empty feedback; no infrastructure error.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="${1:-/workspace/agent-experiments-fixture/mission-template}"
base="${HK_SELFTEST_TMPDIR:-/workspace/tmp-acceptance}"
mkdir -p "$base" 2>/dev/null || base="${TMPDIR:-/tmp}"
tmp="$(mktemp -d "$base/selftest.XXXXXX")" || { echo "selftest: cannot create scratch directory" >&2; exit 2; }
trap 'rm -rf "$tmp"' EXIT

echo "selftest: cloning $template"
git clone --quiet "$template" "$tmp/repo" || { echo "selftest: clone failed" >&2; exit 2; }

start=$SECONDS
bash "$here/run.sh" "$tmp/repo" "$tmp/out.json"
rc=$?
echo "selftest: run.sh exit status $rc after $((SECONDS - start)) s"

python3 - "$rc" "$tmp/out.json" <<'PY'
import json
import sys

rc = int(sys.argv[1])
out = sys.argv[2]
problems: list[str] = []

if rc != 1:
    problems.append(f"run.sh exit status {rc}, wanted 1")
try:
    doc = json.load(open(out))
except (OSError, ValueError) as exc:
    print(f"selftest: out.json unreadable: {exc}")
    sys.exit(1)

assert set(doc) == {"passed", "checks"}, sorted(doc)
if doc["passed"] is not False:
    problems.append("top-level passed is not false")
checks = doc["checks"]
if not checks:
    problems.append("no checks")
ids = [c["id"] for c in checks]
if len(set(ids)) != len(ids):
    problems.append("duplicate check ids")
for c in checks:
    if set(c) != {"id", "passed", "feedback", "detail"}:
        problems.append(f"{c.get('id')}: wrong keys {sorted(c)}")

# Nothing in the template was changed, so these hold already.
must_pass = {"build-no-warnings", "unit-tests-pass", "coverage-not-below-baseline"}
by_id = {c["id"]: c for c in checks}
for cid in sorted(must_pass):
    if cid not in by_id or by_id[cid]["passed"] is not True:
        problems.append(f"{cid} should pass on the template")
for c in checks:
    if c["id"] in must_pass:
        continue
    if c["passed"]:
        problems.append(f"{c['id']} passed on the unmodified template")
    elif not c["feedback"].strip():
        problems.append(f"{c['id']} failed without feedback")
    for bad in ("acceptance/", "run.sh", "hk_hidden"):
        if bad in c["feedback"]:
            problems.append(f"{c['id']} feedback mentions {bad}")

print(f"{'id':32} {'passed':6} feedback")
for c in checks:
    print(f"{c['id']:32} {str(c['passed']):6} {c['feedback'][:70]!r}")
if problems:
    print("selftest: FAILED")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("selftest: OK")
PY
exit $?
