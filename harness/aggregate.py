#!/usr/bin/env python3
"""Aggregate run.json files into a Markdown table.

Cost per completed task is the chosen scenario's total USD of runs whose
layer 1 acceptance passed. Runs that hit the round cap or failed are counted
in the 'runs' and 'passed' columns but not in the cost statistics.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

import cost


def load_runs(runs_dir: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for path in sorted(runs_dir.glob("*/run.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"aggregate: skipping unreadable {path}: {exc}", file=sys.stderr)
            continue
        if data.get("status") == "completed":
            runs.append(data)
    return runs


def _scenario_cost(run: dict[str, Any], scenario: str) -> float | None:
    report = run.get("cost")
    if not report:
        return None
    return report["scenarios"][scenario]["total_usd"]


def _med_range(values: Sequence[float], fmt: str) -> str:
    if not values:
        return "-"
    return f"{fmt.format(statistics.median(values))} ({fmt.format(min(values))}-{fmt.format(max(values))})"


def render(runs: Sequence[dict[str, Any]], scenario: str) -> str:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        groups[(r["arm"], r["task"])].append(r)
    lines = [
        f"Cost scenario: `{scenario}`. Cost columns are median (min-max) USD over runs that passed layer 1.",
        "",
        "| Arm | Task | Runs | Passed | Cost USD | Rounds | Spawns | Compactions |",
        "|-----|------|-----:|-------:|----------|-------:|--------|-------------|",
    ]
    for (arm, task), group in sorted(groups.items()):
        passed = [r for r in group if r.get("passed")]
        costs = [c for c in (_scenario_cost(r, scenario) for r in passed) if c is not None]
        spawns = [(r.get("behavior") or {}).get("subagent_spawns", 0) for r in group]
        comps = [(r.get("behavior") or {}).get("compaction_events", 0) for r in group]
        rounds = [r.get("rounds_used", 0) for r in group]
        lines.append(
            f"| {arm} | {task} | {len(group)} | {len(passed)} | {_med_range(costs, '{:.2f}')} | "
            f"{_med_range(rounds, '{:.0f}')} | {_med_range(spawns, '{:.0f}')} | {_med_range(comps, '{:.0f}')} |")
    if len(lines) == 4:
        lines.append("| - | - | 0 | 0 | - | - | - | - |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs-dir", type=Path, default=Path("/workspace/agent-experiments-runs"))
    p.add_argument("--scenario", choices=cost.SCENARIOS, default="1h")
    a = p.parse_args(argv)
    if not a.runs_dir.is_dir():
        print(f"aggregate: error: runs dir {a.runs_dir} does not exist", file=sys.stderr)
        return 1
    sys.stdout.write(render(load_runs(a.runs_dir), a.scenario))
    return 0


if __name__ == "__main__":
    sys.exit(main())
