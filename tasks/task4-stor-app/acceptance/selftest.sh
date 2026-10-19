#!/usr/bin/env bash
# Self-test of the STOR acceptance pack.
#
#   selftest.sh                     negative control: on the unmodified template the pack must exit 1, write a valid
#                                   result, pass the build and existing-unit-test checks, and fail every STOR check with
#                                   feedback. Also checks that every hidden-test behaviour tag has a feedback sentence.
#   selftest.sh --with-reference    additionally the positive control: the reference STOR (selftest/reference) on a
#                                   second clone must pass every check, hidden and live, each on its first attempt.
#   selftest.sh --variants          additionally every implementation under selftest/variants/<name>/ (patch.diff on
#                                   top of the reference, expect.json): a "variant" must pass every check it runs; a
#                                   "mutant" must fail each check in must_fail (with the expected feedback/detail
#                                   substrings) and pass every check not in must_fail or may_fail.
#   --skip-template                 leave out the negative control.
#
# Every pack run happens inside the harness isolation wrapper (pasta network namespace plus mount, IPC and PID
# namespaces), one namespace per run, so runs proceed in parallel (SELFTEST_JOBS, default 4) without sharing the
# fixed cFS UDP ports. SELFTEST_NO_NETNS=1 runs on the host network instead (one run at a time).
#
# Environment: TEMPLATE (default /workspace/agent-experiments-fixture/mission-template); CTF_CACHE, a directory with
# a set-up CTF/ and venv/ to copy instead of running ctf/setup_ctf.sh (which needs network access);
# SELFTEST_KEEP=1 keeps the work directory.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness="$(cd "$here/../../../harness" 2>/dev/null && pwd)"
template="${TEMPLATE:-/workspace/agent-experiments-fixture/mission-template}"
with_ref=0
variants=0
skip_template=0
for arg in "$@"; do
    case "$arg" in
        --with-reference) with_ref=1 ;;
        --variants) with_ref=1; variants=1 ;;
        --skip-template) skip_template=1 ;;
        *) echo "unknown argument $arg" >&2; exit 2 ;;
    esac
done
jobs_max="${SELFTEST_JOBS:-4}"
[ "${SELFTEST_NO_NETNS:-0}" = 1 ] && jobs_max=1
scratch_root="${ACCEPT_SCRATCH_ROOT:-/workspace/tmp-acceptance}"
mkdir -p "$scratch_root" || exit 2
work="$(mktemp -d "$scratch_root/stor-selftest-XXXXXX")"
if [ "${SELFTEST_KEEP:-0}" = 1 ]; then echo "work directory: $work"; else trap 'rm -rf "$work"' EXIT; fi
status=0
fail() { echo "SELFTEST FAIL: $*"; status=1; }

[ -d "$template" ] || { echo "template $template not found" >&2; exit 2; }
[ -n "$harness" ] && [ -f "$harness/run.py" ] || { echo "harness/run.py not found next to the task directory" >&2; exit 2; }

# run_pack <repo> <name>: run the pack (a copy without selftest/, as the harness does) on <repo>; writes
# $work/<name>.json, $work/<name>.log and $work/<name>.rc.
run_pack() {
    python3 - "$here" "$harness" "$1" "$work" "$2" <<'PY'
import os, shutil, subprocess, sys, time
from pathlib import Path
here, harness, repo, work, name = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5]
sys.path.insert(0, harness)
from run import wrap_netns  # noqa: E402
pack = work / f"pack-{name}"
shutil.rmtree(pack, ignore_errors=True)
shutil.copytree(here, pack, ignore=shutil.ignore_patterns("selftest", "__pycache__"))
env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
cmd = ["bash", str(pack / "run.sh"), repo, str(work / f"{name}.json")]
if os.environ.get("SELFTEST_NO_NETNS") != "1":
    env["ACCEPT_NO_NAMESPACE"] = "1"
    cmd = wrap_netns(cmd)
t0 = time.monotonic()
p = subprocess.run(cmd, cwd=pack, env=env, capture_output=True, text=True, errors="replace")
(work / f"{name}.log").write_text(f"--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}")
(work / f"{name}.rc").write_text(f"{p.returncode} {time.monotonic() - t0:.0f}\n")
shutil.rmtree(pack, ignore_errors=True)
PY
}

summarize() { # <json>
    python3 - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("passed:", d["passed"], " checks:", len(d["checks"]))
for c in d["checks"]:
    print(("  ok     " if c["passed"] else "  FAILED ") + c["id"] + ("" if c["passed"] else "  :: " + c["feedback"][:220]))
PY
}

# install_reference <repo>: the reference STOR, its sample_defs registration, CTF test, CCDD and report.
install_reference() {
    local dest="$1" ref="$here/selftest/reference"
    ( cd "$dest" && git apply "$ref/sample_defs.patch" ) || return 1
    cp -r "$ref/apps/stor" "$dest/apps/stor"
    mkdir -p "$dest/docs/reports" "$dest/ctf/ccdd/json" "$dest/ctf/tests"
    cp "$ref/docs/reports/stor.md" "$dest/docs/reports/"
    cp "$ref"/ctf/ccdd/json/stor_*.json "$dest/ctf/ccdd/json/"
    cp "$ref/ctf/tests/stor_basic.json" "$dest/ctf/tests/"
    python3 - "$dest/ctf/ccdd/json" <<'PY'
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
    ( cd "$dest" && git add -A && git -c user.name=selftest -c user.email=selftest@invalid commit -qm "reference STOR" ) \
        || return 1
}

# setup_ctf <repo>: CTF checkout and virtualenv, from CTF_CACHE when given (no network needed), else set up.
setup_ctf() {
    local dest="$1"
    if [ -n "${CTF_CACHE:-}" ] && [ -d "$CTF_CACHE/CTF" ] && [ -d "$CTF_CACHE/venv" ]; then
        cp -a "$CTF_CACHE/CTF" "$CTF_CACHE/venv" "$dest/ctf/"
    else
        bash "$dest/ctf/setup_ctf.sh" > "$dest.ctf-setup.log" 2>&1 || { tail -n 5 "$dest.ctf-setup.log"; return 1; }
    fi
}

# first_attempt_failures <log>: live checks that passed only on their retry.
first_attempt_failures() { grep -o '^\[[a-z0-9-]*\] FIRST ATTEMPT FAILED: .\{0,300\}' "$1" || true; }

pids=()
wait_slot() { while [ "$(jobs -rp | wc -l)" -ge "$jobs_max" ]; do sleep 2; done; }

if [ "$skip_template" -eq 0 ]; then
    echo "== negative control: unmodified template"
    git clone -q "$template" "$work/plain" || { echo "cannot clone $template" >&2; exit 2; }
    python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from hidden_glue import check_tag_coverage; from pathlib import Path; p = check_tag_coverage(Path(sys.argv[1])); print("\n".join(p)); sys.exit(1 if p else 0)' "$here" \
        || fail "hidden-test behaviour tags and feedback sentences do not match (see above)"
    wait_slot; run_pack "$work/plain" plain & pids+=($!)
fi

ref_ready=0
if [ "$with_ref" -eq 1 ]; then
    git clone -q "$template" "$work/ref" || exit 2
    if install_reference "$work/ref" && setup_ctf "$work/ref"; then
        ref_ready=1
        echo "== positive control: template plus the reference implementation"
        wait_slot; run_pack "$work/ref" ref & pids+=($!)
    else
        fail "the reference implementation could not be installed (sample_defs.patch or CTF set-up)"
    fi
fi

variant_names=()
if [ "$variants" -eq 1 ] && [ "$ref_ready" -eq 1 ]; then
    for vdir in "$here"/selftest/variants/*/; do
        [ -f "$vdir/expect.json" ] || continue
        name="$(basename "$vdir")"
        echo "== variant $name"
        rm -rf "$work/v-$name"
        cp -a "$work/ref" "$work/v-$name"
        if [ -s "$vdir/patch.diff" ] && ! ( cd "$work/v-$name" && git apply "$vdir/patch.diff" ); then
            fail "variant $name: patch.diff does not apply on top of the reference"
            continue
        fi
        only="$(python3 -c 'import json,sys; print(",".join(json.load(open(sys.argv[1])).get("only", [])))' "$vdir/expect.json")"
        variant_names+=("$name")
        wait_slot; STOR_PACK_ONLY="$only" run_pack "$work/v-$name" "v-$name" & pids+=($!)
    done
fi

for pid in "${pids[@]}"; do wait "$pid"; done

if [ "$skip_template" -eq 0 ]; then
    echo "== result: negative control"
    read -r rc secs < "$work/plain.rc"
    echo "run.sh exit: $rc  ($secs s)"
    [ "$rc" -eq 1 ] || { fail "template: expected exit 1, got $rc"; tail -n 20 "$work/plain.log"; }
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
if set(d) != {"passed", "checks"}:
    bad("top-level keys")
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
    if "\u2014" in c["feedback"] or "\u2014" in c["detail"]:
        bad(f"{c['id']}: em dash in feedback or detail")
if "review-completed" in checks:
    bad("the review did not complete: " + checks["review-completed"]["detail"])
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
        fail "template: no result file was written"
    fi
fi

if [ "$ref_ready" -eq 1 ]; then
    echo "== result: positive control"
    read -r rc secs < "$work/ref.rc"
    echo "run.sh exit: $rc  ($secs s)"
    [ -f "$work/ref.json" ] && summarize "$work/ref.json"
    grep "coverage: " "$work/ref.log" | head -n 1
    [ "$rc" -eq 0 ] || { fail "the reference implementation did not pass (exit $rc)"; grep -E '^\[[a-z0-9-]+\] FAILED' "$work/ref.log" | cut -c1-1500; }
    firsts="$(first_attempt_failures "$work/ref.log")"
    [ -z "$firsts" ] || { fail "the reference needed a retry (a flaky check or a flaky reference)"; echo "$firsts"; }
fi

for name in "${variant_names[@]}"; do
    vdir="$here/selftest/variants/$name"
    read -r rc secs < "$work/v-$name.rc"
    echo "== result: variant $name (run.sh exit $rc, $secs s)"
    if [ ! -f "$work/v-$name.json" ]; then
        fail "variant $name: no result file"; tail -n 20 "$work/v-$name.log"; continue
    fi
    python3 - "$vdir/expect.json" "$work/v-$name.json" "$work/v-$name.log" "$name" <<'PY' || status=1
import json, re, sys
exp, res, log, name = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), open(sys.argv[3]).read(), sys.argv[4]
checks = {c["id"]: c for c in res["checks"]}
only = set(exp.get("only", [])) or set(checks)
kind = exp.get("kind")
must_fail = set(exp.get("must_fail", []))
may_fail = set(exp.get("may_fail", []))
ok = True
def bad(msg):
    global ok
    ok = False
    print(f"SELFTEST FAIL: {name}: {msg}")
if kind not in ("variant", "mutant"):
    bad(f"expect.json kind {kind!r} is not variant or mutant")
if kind == "variant" and must_fail:
    bad("a variant must not list must_fail")
if kind == "mutant" and not must_fail:
    bad("a mutant must list at least one must_fail check")
for cid in sorted(must_fail | may_fail | set(exp.get("feedback_contains", {})) | set(exp.get("detail_contains", {}))):
    if cid not in checks:
        bad(f"unknown check id {cid!r} in expect.json")
    elif cid not in only:
        bad(f"{cid} is expected but not run (add it to only)")
for cid in sorted(only):
    c = checks.get(cid)
    if c is None:
        continue
    if cid in must_fail:
        if c["passed"]:
            bad(f"{cid} passed but the planted bug must make it fail")
    elif not c["passed"] and cid not in may_fail:
        bad(f"{cid} failed unexpectedly: {c['feedback'][:300]} || {c['detail'][:600]}")
for key, field in (("feedback_contains", "feedback"), ("detail_contains", "detail")):
    for cid, subs in exp.get(key, {}).items():
        c = checks.get(cid)
        if c is None or c["passed"]:
            continue
        for sub in subs:
            if sub not in c[field]:
                bad(f"{cid} {field} lacks {sub!r}; it is: {c[field][:500]}")
firsts = re.findall(r"^\[([a-z0-9-]+)\] FIRST ATTEMPT FAILED", log, flags=re.M)
if kind == "variant" and firsts:
    bad("checks passed only on their retry: " + ", ".join(firsts))
failed = sorted(cid for cid in only if cid in checks and not checks[cid]["passed"])
print(f"  {kind}: failed {failed or 'nothing'}" + (f"; first-attempt failures {firsts}" if firsts else ""))
sys.exit(0 if ok else 1)
PY
done

[ "$status" -eq 0 ] && echo "SELFTEST OK" || echo "SELFTEST FAILED"
exit "$status"
