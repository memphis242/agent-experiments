#!/usr/bin/env bash
# Self-test of the pack, with a negative and a positive control:
#   1. an unmodified clone of the mission template: run.sh exits 1; out.json is valid; the build, unit-test and
#      coverage checks pass (nothing was changed); every feature check fails with non-empty feedback.
#   2. the template plus the reference solution (selftest/reference/hk_staleness.patch, the committed work of a
#      run that passed every check in its first round): run.sh exits 0 and every check passes.
# Neither may report an infrastructure error. Each clone gets CTF through ctf/setup_ctf.sh, as the harness does,
# and run.sh runs inside the harness isolation wrapper (private network, mount, IPC and PID namespaces) when
# pasta and unshare are installed, with ACCEPT_NO_NAMESPACE=1 and no OAuth token, as the harness runs it.
# Usage: selftest.sh [template-repo]   (default /workspace/agent-experiments-fixture/mission-template)
set -u
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness="$(cd "$here/../../../harness" 2>/dev/null && pwd)"
template="${1:-/workspace/agent-experiments-fixture/mission-template}"
reference="$here/selftest/reference/hk_staleness.patch"
base="${HK_SELFTEST_TMPDIR:-/workspace/tmp-acceptance}"
mkdir -p "$base" || { echo "selftest: cannot create $base" >&2; exit 2; }
tmp="$(mktemp -d "$base/selftest.XXXXXX")" || { echo "selftest: cannot create scratch directory" >&2; exit 2; }
trap 'rm -rf "$tmp"' EXIT
[ -f "$reference" ] || { echo "selftest: $reference missing" >&2; exit 2; }

isolated=0
if [ -n "$harness" ] && command -v pasta >/dev/null && command -v unshare >/dev/null; then
    isolated=1
else
    echo "selftest: pasta/unshare or the harness not found; running without isolation (do not run cFS in parallel)"
fi

# run_pack <repo> <out.json> <log>: run.sh the way the harness does; prints the exit status.
run_pack() {
    if [ "$isolated" -eq 1 ]; then
        ACCEPT_NO_NAMESPACE=1 python3 - "$harness" "$here/run.sh" "$1" "$2" > "$3" 2>&1 <<'PY'
import subprocess, sys
sys.path.insert(0, sys.argv[1])
from run import wrap_netns
sys.exit(subprocess.call(wrap_netns(["bash", *sys.argv[2:]])))
PY
    else
        bash "$here/run.sh" "$1" "$2" > "$3" 2>&1
    fi
    echo $?
}

# clone <dir>: a clone of the template with CTF set up.
clone() {
    git clone --quiet "$template" "$1" || { echo "selftest: clone failed" >&2; exit 2; }
    bash "$1/ctf/setup_ctf.sh" > "$1.ctf-setup.log" 2>&1 \
        || { echo "selftest: ctf/setup_ctf.sh failed:" >&2; tail -n 5 "$1.ctf-setup.log" >&2; exit 2; }
}

echo "selftest: negative control (unmodified template) and positive control (reference solution), in parallel"
clone "$tmp/plain"
clone "$tmp/ref"
git -C "$tmp/ref" apply --binary "$reference" || { echo "selftest: the reference patch does not apply" >&2; exit 2; }
git -C "$tmp/ref" add -A
git -C "$tmp/ref" -c user.name=selftest -c user.email=selftest@localhost commit --quiet -m "Reference solution"

start=$SECONDS
HK_ACCEPT_TMPDIR="$base" run_pack "$tmp/plain" "$tmp/plain.json" "$tmp/plain.log" > "$tmp/plain.rc" &
HK_ACCEPT_TMPDIR="$base" run_pack "$tmp/ref" "$tmp/ref.json" "$tmp/ref.log" > "$tmp/ref.rc" &
wait
echo "selftest: both runs finished after $((SECONDS - start)) s"

python3 - "$tmp" <<'PY'
import json
import re
import sys
from pathlib import Path

tmp = Path(sys.argv[1])
problems: list[str] = []


def load(name: str) -> tuple[int, dict | None]:
    rc = int((tmp / f"{name}.rc").read_text().strip() or -1)
    try:
        return rc, json.loads((tmp / f"{name}.json").read_text())
    except (OSError, ValueError) as exc:
        problems.append(f"{name}: out.json unreadable: {exc}")
        return rc, None


def validate(name: str, doc: dict) -> dict[str, dict]:
    if set(doc) != {"passed", "checks"}:
        problems.append(f"{name}: top-level keys {sorted(doc)}")
    checks = doc.get("checks", [])
    if not checks:
        problems.append(f"{name}: no checks")
    ids = [c.get("id") for c in checks]
    if len(set(ids)) != len(ids):
        problems.append(f"{name}: duplicate check ids")
    for c in checks:
        if set(c) != {"id", "passed", "feedback", "detail"}:
            problems.append(f"{name}: {c.get('id')}: wrong keys {sorted(c)}")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", str(c.get("id"))):
            problems.append(f"{name}: {c.get('id')}: not kebab-case")
        for bad in ("acceptance/", "run.sh", "hk_hidden", "baseline_coverage"):
            if bad in c.get("feedback", ""):
                problems.append(f"{name}: {c['id']} feedback mentions {bad}")
        if not c.get("passed") and not c.get("feedback", "").strip():
            problems.append(f"{name}: {c['id']} failed without feedback")
    if doc.get("passed") is not all(c.get("passed") for c in checks):
        problems.append(f"{name}: top-level passed disagrees with the checks")
    return {c["id"]: c for c in checks}


def show(name: str, by_id: dict[str, dict]) -> None:
    print(f"-- {name}")
    print(f"{'id':32} {'passed':6} feedback")
    for c in by_id.values():
        print(f"{c['id']:32} {str(c['passed']):6} {c['feedback'][:70]!r}")


rc, doc = load("plain")
if rc != 1:
    problems.append(f"plain: run.sh exit status {rc}, wanted 1")
if doc is not None:
    by_id = validate("plain", doc)
    show("negative control", by_id)
    # Nothing in the template was changed, so these hold already.
    must_pass = {"build-no-warnings", "unit-tests-pass", "coverage-not-below-baseline"}
    for cid in sorted(must_pass):
        if cid not in by_id or by_id[cid]["passed"] is not True:
            problems.append(f"plain: {cid} should pass on the template: {by_id.get(cid, {}).get('detail', '')[:300]}")
    for cid, c in by_id.items():
        if cid not in must_pass and c["passed"]:
            problems.append(f"plain: {cid} passed on the unmodified template")

rc, doc = load("ref")
if rc != 0:
    problems.append(f"ref: run.sh exit status {rc}, wanted 0")
if doc is not None:
    by_id = validate("ref", doc)
    show("positive control", by_id)
    for cid, c in by_id.items():
        if not c["passed"]:
            problems.append(f"ref: {cid} failed on the reference solution: {c['detail'][:400]}")

for name in ("plain", "ref"):
    log = (tmp / f"{name}.log").read_text(errors="replace")
    if "infrastructure error" in log.lower():
        problems.append(f"{name}: infrastructure error reported: {log[-600:]}")
if problems:
    print("selftest: FAILED")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("selftest: OK")
PY
exit $?
