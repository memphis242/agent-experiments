#!/usr/bin/env bash
# Self-test of the STOR acceptance pack.
#
#   selftest.sh                     unmodified template: the pack must exit 1, write a valid result, report no
#                                   infrastructure error, pass the build and existing-unit-test checks, and
#                                   fail every STOR check with feedback.
#   selftest.sh --with-reference    additionally apply a reference STOR implementation (selftest/reference) to a
#                                   second clone; every check must then pass. Needs python3.12 and network
#                                   access for ctf/setup_ctf.sh.
#
# Environment: TEMPLATE (default /workspace/agent-experiments-fixture/mission-template).
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="${TEMPLATE:-/workspace/agent-experiments-fixture/mission-template}"
with_ref=0
[ "${1:-}" = "--with-reference" ] && with_ref=1
work="$(mktemp -d "${TMPDIR:-/tmp}/stor-selftest-XXXXXX")"
trap 'rm -rf "$work"' EXIT
status=0
fail() { echo "SELFTEST FAIL: $*"; status=1; }

[ -d "$template" ] || { echo "template $template not found" >&2; exit 2; }

summarize() { # <json>
    python3 - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("passed:", d["passed"], " checks:", len(d["checks"]))
for c in d["checks"]:
    print(("  ok     " if c["passed"] else "  FAILED ") + c["id"])
PY
}

echo "== mode 1: unmodified template"
git clone -q "$template" "$work/plain" || { echo "cannot clone $template" >&2; exit 2; }
start=$SECONDS
bash "$here/run.sh" "$work/plain" "$work/plain.json" > "$work/plain.log" 2>&1
rc=$?
echo "run.sh exit: $rc  ($((SECONDS - start)) s)"
if [ "$rc" -ne 1 ]; then
    fail "expected exit 1, got $rc"
    tail -n 20 "$work/plain.log"
fi
if [ -f "$work/plain.json" ]; then
    summarize "$work/plain.json"
    python3 - "$work/plain.json" <<'PY' || status=1
import json, re, sys
d = json.load(open(sys.argv[1]))
ok = True
def bad(msg):
    global ok
    ok = False
    print("SELFTEST FAIL:", msg)
assert set(d) == {"passed", "checks"}, "top-level keys"
checks = {c["id"]: c for c in d["checks"]}
if d["passed"] is not False:
    bad("passed must be false")
if len(checks) != len(d["checks"]) or not d["checks"]:
    bad("check ids must be unique and non-empty")
for c in d["checks"]:
    if set(c) != {"id", "passed", "feedback", "detail"}:
        bad(f"{c['id']}: keys {sorted(c)}")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", c["id"]):
        bad(f"{c['id']}: not kebab-case")
    if re.search(r"acceptance/|test_[A-Za-z0-9_]*\.py|[A-Za-z0-9_]*_test\.py|run\.sh", c["feedback"]):
        bad(f"{c['id']}: feedback mentions the pack")
must_pass = {"build-no-warnings", "unit-tests-pass"}
for cid in must_pass:
    if cid not in checks or not checks[cid]["passed"]:
        bad(f"{cid} must pass on the template: {checks.get(cid, {}).get('detail')}")
for cid, c in checks.items():
    if cid in must_pass:
        continue
    if c["passed"]:
        bad(f"{cid} passed on a tree without STOR")
    elif not c["feedback"].strip():
        bad(f"{cid} fails without feedback")
sys.exit(0 if ok else 1)
PY
else
    fail "no result file was written"
fi
if grep -qi "infrastructure error" "$work/plain.log"; then
    fail "infrastructure error reported"
    grep -i "infrastructure error" "$work/plain.log"
fi

if [ "$with_ref" -eq 1 ]; then
    echo "== mode 2: template plus the reference implementation"
    git clone -q "$template" "$work/ref" || exit 2
    ref="$here/selftest/reference"
    ( cd "$work/ref" && git apply "$ref/sample_defs.patch" ) || { fail "sample_defs.patch does not apply"; }
    cp -r "$ref/apps/stor" "$work/ref/apps/stor"
    mkdir -p "$work/ref/docs/reports" "$work/ref/ctf/ccdd/json" "$work/ref/ctf/tests"
    cp "$ref/docs/reports/stor.md" "$work/ref/docs/reports/"
    cp "$ref"/ctf/ccdd/json/stor_*.json "$work/ref/ctf/ccdd/json/"
    cp "$ref/ctf/tests/stor_basic.json" "$work/ref/ctf/tests/"
    python3 - "$work/ref/ctf/ccdd/json" <<'PY'
import json, sys
d = sys.argv[1]
mids = json.load(open(f"{d}/mission_mids.json"))
have = {m["mid_name"] for m in mids[0]["mids"]}
for n, v in [("STOR_CMD_MID", "0x18C4"), ("STOR_SEND_HK_MID", "0x18C5"), ("STOR_WAKEUP_MID", "0x18C6"),
             ("STOR_HK_TLM_MID", "0x08C4"), ("STOR_LIST_TLM_MID", "0x08C5"), ("STOR_STATS_TLM_MID", "0x08C6")]:
    if n not in have:
        mids[0]["mids"].append({"mid_name": n, "mid_value": v})
json.dump(mids, open(f"{d}/mission_mids.json", "w"), indent=2)
const = json.load(open(f"{d}/mission_constants.json"))
have = {c["constant_name"] for c in const}
for n, v in [("STOR_PATH_LEN", "64"), ("STOR_MAX_ENTRIES", "32"), ("STOR_LIST_ENTRIES_PER_PKT", "4")]:
    if n not in have:
        const.append({"constant_name": n, "constant_value": v})
json.dump(const, open(f"{d}/mission_constants.json", "w"), indent=2)
PY
    if [ -f "$work/ref/ctf/setup_ctf.sh" ]; then
        bash "$work/ref/ctf/setup_ctf.sh" > "$work/ctf-setup.log" 2>&1 || { fail "ctf/setup_ctf.sh failed"; tail -n 5 "$work/ctf-setup.log"; }
    fi
    start=$SECONDS
    bash "$here/run.sh" "$work/ref" "$work/ref.json" > "$work/ref.log" 2>&1
    rc=$?
    echo "run.sh exit: $rc  ($((SECONDS - start)) s)"
    [ -f "$work/ref.json" ] && summarize "$work/ref.json"
    grep "^coverage:" "$work/ref.log"
    [ "$rc" -eq 0 ] || { fail "the reference implementation did not pass (exit $rc)"; tail -n 20 "$work/ref.log"; }
fi

[ "$status" -eq 0 ] && echo "SELFTEST OK" || echo "SELFTEST FAILED"
exit "$status"
