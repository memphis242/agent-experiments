#!/usr/bin/env python3
"""Run one group of live checks inside an isolated group (isolation.wrap) and write their results as JSON.

Usage: live_worker.py <spec.json> <result.json>

The spec names the built tree, this group's scratch directory, the review's wall-clock deadline, the checks
(in order: checks of one group share nodes through Ctx.cache), the directory where node start-up failures are
recorded for every group, and the state of the CTF set-up made by the parent.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PACK = Path(__file__).resolve().parent
sys.path.insert(0, str(PACK))

import check  # noqa: E402
import ctf_checks as cc  # noqa: E402


def main() -> int:
    spec_ = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    ctx = check.Ctx(Path(spec_["repo"]), Path(spec_["work"]), time.monotonic() + (spec_["deadline_wall"] - time.time()))
    ctx.work.mkdir(parents=True, exist_ok=True)
    ctx.tree = Path(spec_["tree"])
    ctx.build_ok = True
    ctx.boot_fail_dir = Path(spec_["boot_fail_dir"])
    if spec_.get("ctf_root"):
        env = cc.CtfEnv(ctx)
        env.root = Path(spec_["ctf_root"])
        env.ctf = env.root / "ctf"
        env.setup_done = True
        env.setup_ok = bool(spec_["ctf_setup_ok"])
        env.setup_detail = spec_.get("ctf_setup_detail", "")
        ctx.ctf = env
    results = check.Results()
    ids = list(spec_["checks"])
    assert ids and all(check.BY_ID[cid].lane == "isolated" for cid in ids)

    def save() -> None:
        # Written after every check, so that the checks finished before a crash or a kill keep their results.
        tmp = out.with_suffix(".tmp")
        with results.lock:
            tmp.write_text(json.dumps({"items": results.items, "ran": sorted(results.ran)}))
        tmp.replace(out)

    try:
        for i, cid in enumerate(ids):
            try:
                check.run_one(ctx, check.BY_ID[cid], results)
            except check.BudgetExceeded:
                for rest in ids[i + 1:]:
                    results.put(rest, False, check.BY_ID[rest].feedback, "not run: the review's time budget ran out", ran=False)
                break
            finally:
                save()
    finally:
        ctx.shutdown_session()
        save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
