#!/usr/bin/env python3
"""Enumerate arms x tasks x reps and run up to --parallel runs at once.

A cell is skipped only if a run.json with status "completed" exists for it whose
config_sha256 equals the current arm root's and whose run-code hash (see
provenance.py) equals the current one; commits touching only docs, analysis or
this driver do not invalidate finished runs. Cells that ended in usage_limit (or any other status) rerun from
scratch. Once any run reports a usage limit no new cell is launched. Extra arguments after `--` are passed through to run.py.
"""
from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

import arms
import proc
import provenance
import run

TASKS = ("task1", "task2", "task3", "task4")
RUN_PY = Path(__file__).resolve().parent / "run.py"


def load_attempts(runs_dir: Path) -> list[dict]:
    attempts: list[dict] = []
    for path in sorted(runs_dir.glob("*/run.json")):
        try:
            attempts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return attempts


def is_completed(attempts: Sequence[dict], arm: str, task: str, rep: int, config_sha: str, run_code: str,
                 resolve: provenance.Resolver = provenance.run_code_sha) -> bool:
    """A cell is done only by a completed run made with the current arm config and run code.
    Runs with any other status (including usage_limit) are rerun from scratch."""
    assert run_code, "the current run-code hash must be known before skipping cells"
    return any(
        a.get("arm") == arm and a.get("task") == task and a.get("rep") == rep and a.get("status") == "completed"
        and a.get("config_sha256") == config_sha
        and provenance.same_run_code(provenance.recorded_run_code(a, resolve), run_code)
        for a in attempts)


def build_cells(arm_ids: Sequence[str], tasks: Sequence[str], reps: Sequence[int]) -> tuple[tuple[str, str, int], ...]:
    return tuple(itertools.product(arm_ids, tasks, reps))


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--arms", nargs="+", default=list(arms.ARMS), choices=list(arms.ARMS))
    p.add_argument("--tasks", nargs="+", default=list(TASKS), help="short task names (default: task1..task4)")
    p.add_argument("--reps", type=int, default=3, help="repetitions 1..N")
    p.add_argument("--runs-dir", type=Path, default=run.DEFAULT_RUNS_DIR)
    p.add_argument("--arms-dir", type=Path, default=arms.DEFAULT_ARMS_DIR)
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument("--dry-run", action="store_true", help="list cells, do not run")
    p.add_argument("passthrough", nargs=argparse.REMAINDER, help="after `--`: extra run.py arguments")
    a = p.parse_args(argv)
    if a.parallel < 1 or a.reps < 1:
        p.error("--parallel and --reps must be >= 1")
    extra = a.passthrough[1:] if a.passthrough[:1] == ["--"] else a.passthrough
    cells = build_cells(a.arms, a.tasks, range(1, a.reps + 1))
    harness_sha = run.harness_git()[0]
    run_code = provenance.run_code_sha("HEAD")
    if run_code is None:
        print("matrix: error: cannot hash the run-relevant harness files at HEAD", file=sys.stderr)
        return 1
    if provenance.run_code_dirty():
        print("matrix: warning: run-relevant harness files have uncommitted changes; "
              "runs will record a commit that does not describe them", file=sys.stderr)
    try:
        config_shas = {arm: run.hash_config_root(arms.arm_root(arm, a.arms_dir)) for arm in a.arms}
    except OSError as exc:
        print(f"matrix: error: cannot hash arm roots in {a.arms_dir}: {exc}", file=sys.stderr)
        return 1
    for arm, sha in config_shas.items():
        if not (a.arms_dir / arm / "settings.json").is_file():
            print(f"matrix: error: arm root for {arm} is missing in {a.arms_dir}; run harness/arms.py", file=sys.stderr)
            return 1
    attempts = load_attempts(a.runs_dir)
    todo = [c for c in cells if not is_completed(attempts, c[0], c[1], c[2], config_shas[c[0]], run_code)]
    print(f"{len(cells)} cells, {len(cells) - len(todo)} already completed with matching hashes "
          f"(harness {harness_sha[:12]}, run code {run_code[:12]}), {len(todo)} to run")

    def command(cell: tuple[str, str, int]) -> list[str]:
        arm, task, rep = cell
        return [sys.executable, str(RUN_PY), "--arm", arm, "--task", task, "--rep", str(rep),
                "--runs-dir", str(a.runs_dir), "--arms-dir", str(a.arms_dir), *extra]

    if a.dry_run:
        for cell in todo:
            print(" ".join(command(cell)))
        return 0

    usage_limit = threading.Event()

    def execute(cell: tuple[str, str, int]) -> tuple[tuple[str, str, int], int | None]:
        if usage_limit.is_set():
            return cell, None  # not launched
        done = proc.run_group(command(cell), cwd=None, env=None, timeout=None)
        tag = "-".join(map(str, cell))
        print(f"[{tag}] exit {done.returncode}: {(done.stdout.strip() or done.stderr.strip()).splitlines()[-1:]}",
              flush=True)
        if done.returncode == run.EXIT_USAGE_LIMIT:
            usage_limit.set()
        return cell, done.returncode

    with ThreadPoolExecutor(max_workers=a.parallel) as pool:
        results = list(pool.map(execute, todo))
    skipped = [c for c, rc in results if rc is None]
    failed = [c for c, rc in results if rc not in (0, None)]
    for arm, task, rep in failed:
        print(f"not completed: {arm} {task} rep {rep} (rerun the matrix to retry from scratch)", file=sys.stderr)
    if usage_limit.is_set():
        print(f"usage limit reached: {len(skipped)} cells were not launched. Wait for the limit to reset, then "
              f"rerun the same command; cells without a matching completed run (including those that hit the "
              f"limit) restart from scratch, and their spend stays recorded in their run directories.",
              file=sys.stderr)
        return run.EXIT_USAGE_LIMIT
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
