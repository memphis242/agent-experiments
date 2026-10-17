#!/usr/bin/env bash
# Self-test of the acceptance pack against a reference tool and a broken tool.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pack="$(dirname "$here")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
status=0

make_repo() { # <dir> <tool-source>
    mkdir -p "$1/tools/tests"
    cp "$2" "$1/tools/utlog2junit.py"
    printf '# utlog2junit\n\nUsage: utlog2junit.py [--junit OUT.xml] [--markdown OUT.md] [--strict] PATH\n' > "$1/tools/README.md"
    cat > "$1/tools/tests/test_stub.py" <<'PY'
import unittest

class Stub(unittest.TestCase):
    def test_a(self) -> None: self.assertTrue(True)
    def test_b(self) -> None: self.assertTrue(True)
    def test_c(self) -> None: self.assertTrue(True)
    def test_d(self) -> None: self.assertTrue(True)
    def test_e(self) -> None: self.assertTrue(True)
PY
}

failed_ids() { python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(" ".join(c["id"] for c in d["checks"] if not c["passed"]))' "$1"; }

echo "== mode 1: reference implementation must pass everything"
make_repo "$work/good" "$here/ref_utlog2junit.py"
bash "$pack/run.sh" "$work/good" "$work/good.json"; rc=$?
echo "run.sh exit: $rc"
python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("passed:", d["passed"], "checks:", len(d["checks"]))
for c in d["checks"]:
    if not c["passed"]: print("FAILED", c["id"], c["detail"])' "$work/good.json"
if [ "$rc" -ne 0 ] || ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["passed"] is True else 1)' "$work/good.json"; then
    echo "SELFTEST FAIL: reference did not pass"; status=1
fi

echo "== mode 2: broken tool (exit 0 always, no error case, no TSF) must fail the right checks"
sed -e 's/return 1 if bad else 0/return 0/' \
    -e 's/if not log.complete:$/if False:/' -e 's/(0 if log.complete else 1)/0/' \
    -e 's/if key in ("FAIL", "TSF", "TTF"):/if key in ("FAIL",):/' \
    "$here/ref_utlog2junit.py" > "$work/broken.py"
cmp -s "$work/broken.py" "$here/ref_utlog2junit.py" && { echo "SELFTEST FAIL: sed did not alter tool"; status=1; }
make_repo "$work/bad" "$work/broken.py"
bash "$pack/run.sh" "$work/bad" "$work/bad.json"; rc=$?
echo "run.sh exit: $rc"
ids="$(failed_ids "$work/bad.json")"
echo "failed ids: $ids"
[ "$rc" -eq 1 ] || { echo "SELFTEST FAIL: expected exit 1"; status=1; }
for want in failing-log-exit-1 truncated-log-error-case truncated-log-exit-1 tsf-counted-as-failure; do
    case " $ids " in *" $want "*) ;; *) echo "SELFTEST FAIL: $want should fail"; status=1 ;; esac
done
for must_pass in tool-exists usage-no-output-flag-exit-2 real-dir-exit-0 mir-property strict-rejects-unknown-exit-2; do
    case " $ids " in *" $must_pass "*) echo "SELFTEST FAIL: $must_pass should pass"; status=1 ;; esac
done

echo "== mode 3: missing tool"
mkdir -p "$work/none"
bash "$pack/run.sh" "$work/none" "$work/none.json"; rc=$?
echo "run.sh exit: $rc; failed: $(failed_ids "$work/none.json" | wc -w) of $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["checks"]))' "$work/none.json")"
[ "$rc" -eq 1 ] || { echo "SELFTEST FAIL: expected exit 1"; status=1; }

[ "$status" -eq 0 ] && echo "SELFTEST OK" || echo "SELFTEST FAILED"
exit "$status"
