#!/usr/bin/env bash
# Self-test of the Task 5 acceptance pack. Exit 0 when every stage behaves as expected.
#
#   selftest.sh                 all stages
#   selftest.sh --quick         unit tests only
#
# Stages:
#   1. unit tests (selftest/test_*.py): frame codec against RFC 4231 and the CRC check value, deframer,
#      result schema against harness/acceptance.py, per-subsystem scoring, every check's decision logic on
#      fixtures, the frame-builder checks against a reference tools/udp_harness.py;
#   2. component behaviour tests through the review's glue: partial references (selftest/hidden_reference)
#      pass all eight checks, do-nothing stand-ins fail all eight without crashing;
#   3. live checks of S3, S4 and C.1 against fake nodes in a private network/mount namespace (all pass),
#      and with seeded faults in the fake (the targeted checks fail);
#   4. dry run of run.sh against the unmodified template inside the harness's isolation wrapper: exit 1, a
#      valid result, only the properties the template already has pass, every other check fails with
#      feedback and no pack error, the per-subsystem summary is written; its time is reported.
#
# Environment: TEMPLATE (default /workspace/agent-experiments-fixture/mission-template),
#              ACCEPT_SCRATCH_ROOT (default /workspace/tmp-acceptance).
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness="$(cd "$here/../../../harness" && pwd)"
template="${TEMPLATE:-/workspace/agent-experiments-fixture/mission-template}"
scratch_root="${ACCEPT_SCRATCH_ROOT:-/workspace/tmp-acceptance}"
mkdir -p "$scratch_root" || exit 2
work="$(mktemp -d "$scratch_root/t5-selftest-XXXXXX")"
trap '[ "${T5_SELFTEST_KEEP:-}" = 1 ] || rm -rf "$work"' EXIT
status=0
fail() { echo "SELFTEST FAIL: $*"; status=1; }

echo "== 1. unit tests"
( cd "$here" && python3 -B -m unittest discover -s selftest -p 'test_*.py' 2>&1 | tail -n 4 ) | tee "$work/unit.log"
grep -q "^OK" "$work/unit.log" || fail "unit tests"
[ "${1:-}" = "--quick" ] && { [ "$status" -eq 0 ] && echo "SELFTEST OK (quick)"; exit "$status"; }
[ -d "$template" ] || { echo "template $template not found" >&2; exit 2; }

echo "== 2. component behaviour tests"
for mode in reference fake; do
    python3 -B "$here/selftest/hidden_pass.py" "$work/hidden" --mode "$mode" > "$work/hidden-$mode.log" 2>&1 \
        || { fail "behaviour tests ($mode)"; cat "$work/hidden-$mode.log"; }
    tail -n 1 "$work/hidden-$mode.log"
done

echo "== 3. live checks against fake nodes"
python3 -B "$here/selftest/live_fake.py" "$work/live" > "$work/live.log" 2>&1 || { fail "live fake"; grep -E "^BAD" "$work/live.log"; }
tail -n 1 "$work/live.log"
for m in evict-newest format-as-crc budget-ignored max-age-ignored; do
    python3 -B "$here/selftest/live_fake.py" "$work/live" --mutant "$m" > "$work/live-$m.log" 2>&1 || fail "live fake mutant $m"
    tail -n 1 "$work/live-$m.log"
done

echo "== 4. dry run against the unmodified template"
git clone -q "$template" "$work/repo" || exit 2
cmd=(bash "$here/run.sh" "$work/repo" "$work/out.json")
if command -v pasta >/dev/null && command -v unshare >/dev/null; then
    mapfile -t wrapped < <(python3 - "$harness" "${cmd[@]}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from run import wrap_netns
for part in wrap_netns(sys.argv[2:]):
    print(part)
PY
)
    cmd=("${wrapped[@]}")
fi
start=$SECONDS
ACCEPT_NO_NAMESPACE=1 ACCEPT_SCRATCH_ROOT="$work/scratch" "${cmd[@]}" 2>&1 | cat > "$work/dry.log"
rc=${PIPESTATUS[0]}
echo "run.sh exit: $rc ($((SECONDS - start)) s)"
[ "$rc" -eq 1 ] || { fail "expected exit 1, got $rc"; tail -n 20 "$work/dry.log"; }
python3 - "$harness" "$work/out.json" <<'PY' || status=1
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import acceptance
out = Path(sys.argv[2])
d = json.loads(out.read_text())
res = acceptance.parse_result(d, 1)
ok = True
def bad(msg):
    global ok
    ok = False
    print("SELFTEST FAIL:", msg)
template_has = {"n-build-werror", "n-unit-tests", "n-change-scope", "s7-setup-fresh"}
for c in res.checks:
    if c.check_id in template_has:
        if not c.passed:
            bad(f"{c.check_id} must pass on the template: {c.detail[:300]}")
        continue
    if c.passed:
        bad(f"{c.check_id} passed on the template")
    for word in ("unexpected ", "infrastructure problem", "not run"):
        if word in c.detail:
            bad(f"{c.check_id}: {c.detail[:300]}")
side = out.with_name(out.stem + ".subsystems.json")
s = json.loads(side.read_text())
assert set(s["subsystems"]) == {"S1", "S2", "S3", "S4", "S5", "S6", "S7", "N"}
print(f"checks {len(res.checks)}, passing on the template {sorted(c.check_id for c in res.checks if c.passed)}, "
      f"elapsed {s['elapsed_s']} s")
fb = acceptance.compose_feedback(res.failures())
print(f"feedback message: {len(fb.splitlines())} lines; first item: {fb.splitlines()[2][:160]}")
sys.exit(0 if ok else 1)
PY

[ "$status" -eq 0 ] && echo "SELFTEST OK" || echo "SELFTEST FAILED"
exit "$status"
