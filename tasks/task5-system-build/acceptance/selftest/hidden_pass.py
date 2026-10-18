#!/usr/bin/env python3
"""Pass and fail paths of the component behaviour tests through the review's own glue (hidden_glue.py).

  hidden_pass.py <work-dir> [--mode reference|fake]

reference: a template clone with the partial reference components of selftest/hidden_reference (enough for the
           behaviour tests only) must pass every one of the eight checks;
fake:      the do-nothing stand-ins of fake_apps.py must fail every check with requirement feedback, never crash.
Prints one line per check and exits non-zero on any unexpected verdict.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
sys.path.insert(0, str(PACK))
sys.path.insert(0, str(HERE))

import fake_apps  # noqa: E402
from common import Fail  # noqa: E402
from hidden_glue import HIDDEN_CHECKS, HiddenTests  # noqa: E402

TEMPLATE = Path("/workspace/agent-experiments-fixture/mission-template")


def sh(tree: Path):  # noqa: ANN201
    def run(cmd: list[str], timeout: float, env=None, cwd=None):  # noqa: ANN001, ANN202
        return subprocess.run(cmd, cwd=cwd or tree, text=True, errors="replace", capture_output=True, timeout=timeout, env=env)
    return run


def make_tree(work: Path, mode: str) -> Path:
    tree = work / f"hidden-{mode}"
    if tree.exists():
        shutil.rmtree(tree)
    subprocess.run(["git", "clone", "-q", str(TEMPLATE), str(tree)], check=True)
    fake_apps.add(tree, hidden=False)
    if mode == "reference":
        osc = tree / "sample_defs" / "default_osconfig.cmake"
        osc.write_text(osc.read_text() + "\nset(OSAL_CONFIG_MAX_FILE_NAME 32)\n")
        for comp in ("apps/stor", "apps/cmdi", "apps/tlmo", "apps/gw", "apps/sfi", "libs/cmdval_lib"):
            shutil.rmtree(tree / comp)
            shutil.copytree(HERE / "hidden_reference" / comp, tree / comp)
    return tree


def main() -> int:
    work = Path(sys.argv[1]).resolve()
    mode = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--mode" else "reference"
    assert mode in ("reference", "fake")
    work.mkdir(parents=True, exist_ok=True)
    tree = make_tree(work, mode)
    t0 = time.monotonic()
    ht = HiddenTests(tree, PACK, sh(tree))
    ht.run_all(lambda want: want)
    bad = 0
    for cid in HIDDEN_CHECKS:
        try:
            ht.verdict(cid, f"[{cid}]", lambda want: want)
            verdict, text = "pass", ""
        except Fail as exc:
            verdict, text = "fail", f"{str(exc)[:200]} || feedback: {exc.feedback[:160] if exc.feedback else ''}"
        expected = "pass" if mode == "reference" else "fail"
        ok = verdict == expected and (mode == "reference" or "crash" not in text and "compile and link" not in text)
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'BAD '} {cid}: {verdict} {text}")
    for (comp, unit), res in sorted(ht.results.items()):
        print(f"     runner {comp}-{unit}: built {res.built} exit {res.returncode} cases {len(res.seen_cases)} "
              f"failed {len(res.failed_cases)}" + (f" build: {res.build_detail[:300]}" if not res.built else ""))
    print(f"hidden {mode}: {'OK' if bad == 0 else f'{bad} unexpected verdicts'} ({time.monotonic() - t0:.0f} s)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
