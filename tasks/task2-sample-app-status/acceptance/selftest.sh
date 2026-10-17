#!/usr/bin/env bash
# Self-test of the Task 2 acceptance pack.
#
#   template   an unmodified mission template: build and unit tests pass, every
#              feature check fails with feedback, no infrastructure error.
#   reference  the template plus a known-good implementation (selftest/reference):
#              every check passes.
#
# Usage: selftest.sh [template|reference]...   (default: both)
# Environment:
#   TEMPLATE   mission template repository (default /workspace/agent-experiments-fixture/mission-template)
#   SELFTEST_CTF_CACHE  where the CTF checkout and virtualenv are cached between runs
#   FIXTURE    fixture overlay applied on top of it (default: <repo>/fixture/mission when this
#              pack sits in the experiment repository), so the clone matches what agents get
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="${TEMPLATE:-/workspace/agent-experiments-fixture/mission-template}"
fixture="${FIXTURE:-$here/../../../fixture/mission}"
scratch_base="${SELFTEST_TMP:-/workspace/tmp-acceptance}"
ctf_cache="${SELFTEST_CTF_CACHE:-/workspace/tmp-acceptance/task2-selftest-ctf}"
mkdir -p "$scratch_base"
modes=("$@")
[ "${#modes[@]}" -gt 0 ] || modes=(template reference)

work="$(mktemp -d "$scratch_base/task2-selftest-XXXXXX")"
trap 'rm -rf "$work"' EXIT

make_clone() {   # make_clone <dir> <mode>
    git clone --quiet "$template" "$1"
    if [ -d "$fixture" ]; then
        rsync -a --exclude CTF --exclude venv --exclude results --exclude __pycache__ "$fixture"/ "$1"/
    fi
    # The harness runs ctf/setup_ctf.sh (network needed) before the agent starts. Do the same
    # once and reuse the result, since the checks run without network access.
    if [ ! -d "$ctf_cache/CTF" ]; then
        bash "$1/ctf/setup_ctf.sh" >&2
        mkdir -p "$ctf_cache"
        rsync -a "$1/ctf/CTF" "$1/ctf/venv" "$ctf_cache"/
    else
        rsync -a "$ctf_cache/CTF" "$ctf_cache/venv" "$1/ctf"/
    fi
    if [ "$2" = reference ]; then
        rsync -a "$here/selftest/reference"/ "$1"/
    fi
}

verify() {   # verify <mode> <exit-code> <out.json>
    python3 -I - "$1" "$2" "$3" <<'PY'
import json, re, sys

mode, rc, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
problems: list[str] = []

def need(cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)

want_rc = 1 if mode == "template" else 0
need(rc == want_rc, f"exit status {rc}, wanted {want_rc} (2 would be an infrastructure error)")
try:
    doc = json.load(open(path))
except (OSError, ValueError) as exc:
    print(f"FAIL: out.json unreadable: {exc}")
    sys.exit(1)

need(set(doc) == {"passed", "checks"}, f"top-level keys {sorted(doc)}")
checks = doc.get("checks", [])
need(isinstance(checks, list) and len(checks) > 0, "checks empty")
ids = [c.get("id") for c in checks]
need(len(ids) == len(set(ids)), "duplicate check ids")
leak = re.compile(r"acceptance/|\btest_[A-Za-z0-9_]*\.py\b|\b[A-Za-z0-9_]*_test\.py\b|\brun\.sh\b")
for c in checks:
    need(set(c) == {"id", "passed", "feedback", "detail"}, f"check keys {sorted(c)}")
    need(bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", str(c.get("id")))), f"bad id {c.get('id')!r}")
    need(isinstance(c.get("passed"), bool), f"{c.get('id')}: passed not boolean")
    if not c.get("passed"):
        need(bool(str(c.get("feedback", "")).strip()), f"{c.get('id')}: failing check without feedback")
    need(not leak.search(str(c.get("feedback", ""))), f"{c.get('id')}: feedback leaks a pack detail")
    need(chr(0x2014) not in str(c.get("feedback", "")) + str(c.get("detail", "")), f"{c.get('id')}: em dash")
by_id = {c["id"]: c for c in checks}
need(doc.get("passed") == all(c["passed"] for c in checks), "top-level passed disagrees with the checks")

expected_ids = {"build-no-warnings", "unit-tests-pass", "coverage-not-below-baseline", "hidden-coverage-test",
                "to-lab-subscription", "status-packet-default-divisor", "set-rate-valid", "set-rate-invalid",
                "reset-counters", "process-count", "ccdd-definitions", "ctf-test-passes", "report-present"}
need(set(by_id) == expected_ids, f"check ids differ: missing {sorted(expected_ids - set(by_id))}, extra {sorted(set(by_id) - expected_ids)}")

if mode == "template":
    keep = {"build-no-warnings", "unit-tests-pass", "coverage-not-below-baseline"}
    for cid in sorted(expected_ids):
        if cid in by_id:
            need(by_id[cid]["passed"] == (cid in keep), f"{cid}: passed={by_id[cid]['passed']}, wanted {cid in keep}")
else:
    for cid, c in by_id.items():
        need(c["passed"], f"{cid}: failed: {c['detail'][:200]!r}")

for cid in sorted(by_id):
    c = by_id[cid]
    print(f"  {'PASS' if c['passed'] else 'FAIL'}  {cid}")
    if not c["passed"]:
        print(f"        feedback: {c['feedback']}")
        print(f"        detail  : {c['detail'][:220].replace(chr(10), ' / ')}")
if problems:
    print("SELFTEST PROBLEMS:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print(f"selftest ({mode}): OK")
PY
}

status=0
for mode in "${modes[@]}"; do
    echo "=== selftest: $mode ==="
    dir="$work/$mode"
    make_clone "$dir/repo" "$mode"
    start=$SECONDS
    rc=0
    bash "$here/run.sh" "$dir/repo" "$dir/out.json" || rc=$?
    echo "run.sh exit status $rc after $((SECONDS - start)) s"
    if [ ! -f "$dir/out.json" ]; then
        echo "FAIL: no out.json (exit status $rc)"
        status=1
        continue
    fi
    verify "$mode" "$rc" "$dir/out.json" || status=1
done
exit "$status"
