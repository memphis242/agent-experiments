#!/usr/bin/env python3
"""Enumerate arms x tasks x reps and run up to --parallel runs at once.

A cell whose run directory already holds a run.json with status "completed"
is skipped. Extra arguments after `--` are passed through to run.py.
"""
from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

import arms
import run

RUN_PY = Path(__file__).resolve().parent / "run.py"


def is_completed(runs_dir: Path, arm: str, task: str, rep: int) -> bool:
    for path in runs_dir.glob(f"{arm}-{task}-r{rep}-*/run.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("status") == "completed":
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


def build_cells(arm_ids: Sequence[str], tasks: Sequence[str], reps: Sequence[int]) -> tuple[tuple[str, str, int], ...]:
    return tuple(itertools.product(arm_ids, tasks, reps))


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--arms", nargs="+", default=list(arms.ARMS), choices=list(arms.ARMS))
    p.add_argument("--tasks", nargs="+", required=True)
    p.add_argument("--reps", type=int, default=3, help="repetitions 1..N")
    p.add_argument("--runs-dir", type=Path, default=run.DEFAULT_RUNS_DIR)
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument("--dry-run", action="store_true", help="list cells, do not run")
    p.add_argument("passthrough", nargs=argparse.REMAINDER, help="after `--`: extra run.py arguments")
    a = p.parse_args(argv)
    if a.parallel < 1 or a.reps < 1:
        p.error("--parallel and --reps must be >= 1")
    extra = a.passthrough[1:] if a.passthrough[:1] == ["--"] else a.passthrough
    cells = build_cells(a.arms, a.tasks, range(1, a.reps + 1))
    todo = [c for c in cells if not is_completed(a.runs_dir, *c)]
    print(f"{len(cells)} cells, {len(cells) - len(todo)} already completed, {len(todo)} to run")

    def command(cell: tuple[str, str, int]) -> list[str]:
        arm, task, rep = cell
        return [sys.executable, str(RUN_PY), "--arm", arm, "--task", task, "--rep", str(rep),
                "--runs-dir", str(a.runs_dir), *extra]

    if a.dry_run:
        for cell in todo:
            print(" ".join(command(cell)))
        return 0

    def execute(cell: tuple[str, str, int]) -> tuple[tuple[str, str, int], int]:
        proc = subprocess.run(command(cell), capture_output=True, text=True, check=False)
        tag = "-".join(map(str, cell))
        print(f"[{tag}] exit {proc.returncode}: {(proc.stdout.strip() or proc.stderr.strip()).splitlines()[-1:]}",
              flush=True)
        return cell, proc.returncode

    with ThreadPoolExecutor(max_workers=a.parallel) as pool:
        results = list(pool.map(execute, todo))
    failed = [c for c, rc in results if rc != 0]
    for arm, task, rep in failed:
        print(f"not completed: {arm} {task} rep {rep} (rerun the matrix to retry)", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
