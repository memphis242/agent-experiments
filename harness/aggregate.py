#!/usr/bin/env python3
"""Aggregate run.json files into Markdown tables.

Primary metric per arm x task: total billed spend over ALL attempts (passes,
cap hits, usage-limit drops, CLI errors, infrastructure errors) divided by the
number of passes. The conditional median and range over passing attempts, the
attempt and pass counts, and attrition are reported next to it.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

import cost


def load_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """Every attempt that left a run.json, whatever its status."""
    runs: list[dict[str, Any]] = []
    for path in sorted(runs_dir.glob("*/run.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"aggregate: skipping unreadable {path}: {exc}", file=sys.stderr)
            continue
        data["_dir"] = path.parent.name
        runs.append(data)
    return runs


def scenario_cost(run: dict[str, Any], scenario: str) -> float | None:
    report = run.get("cost")
    if not report:
        return None
    return report["scenarios"].get(scenario, {}).get("total_usd")


def _med_range(values: Sequence[float], fmt: str) -> str:
    if not values:
        return "-"
    return f"{fmt.format(statistics.median(values))} ({fmt.format(min(values))}-{fmt.format(max(values))})"


def hash_warnings(runs: Sequence[dict[str, Any]]) -> list[str]:
    """Runs whose arm config hash differs from their arm's majority, or whose harness SHA
    differs from the overall majority."""
    warnings: list[str] = []
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_arm[r.get("arm", "?")].append(r)
    for arm, group in sorted(by_arm.items()):
        counts = Counter(r.get("config_sha256") for r in group)
        majority = counts.most_common(1)[0][0]
        for r in group:
            if r.get("config_sha256") != majority:
                warnings.append(f"run {r['_dir']} ({arm}): config_sha256 differs from the arm's majority")
    shas = Counter((r.get("provenance") or {}).get("harness_sha") for r in runs)
    if shas:
        majority = shas.most_common(1)[0][0]
        for r in runs:
            if (r.get("provenance") or {}).get("harness_sha") != majority:
                warnings.append(f"run {r['_dir']}: harness SHA differs from the majority ({str(majority)[:12]})")
    return warnings


def render(runs: Sequence[dict[str, Any]], scenario: str) -> str:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        groups[(r["arm"], r["task"])].append(r)
    lines = [
        f"Cost scenario: `{scenario}`. Spend is summed over all attempts; USD per pass is the primary metric. "
        "Conditional cost is median (min-max) over passing attempts.",
        "",
        "| Arm | Task | Attempts | Passes | Attrition | Spend USD | USD per pass | Conditional USD | Rounds | Spawns | Compactions |",
        "|-----|------|---------:|-------:|-----------|----------:|-------------:|-----------------|--------|--------|-------------|",
    ]
    for (arm, task), group in sorted(groups.items()):
        passed = [r for r in group if r.get("status") == "completed" and r.get("passed")]
        spend = sum(scenario_cost(r, scenario) or 0.0 for r in group)
        pass_costs = [c for c in (scenario_cost(r, scenario) for r in passed) if c is not None]
        statuses = Counter(("pass" if r in passed else "cap_hit" if r.get("cap_hit") else r.get("status", "?"))
                           for r in group)
        attrition = ", ".join(f"{k} {v}" for k, v in sorted(statuses.items()) if k != "pass") or "none"
        per_pass = f"{spend / len(passed):.2f}" if passed else "-"
        behaviors = [(r.get("behavior") or {}) for r in group]
        lines.append(
            f"| {arm} | {task} | {len(group)} | {len(passed)} | {attrition} | {spend:.2f} | {per_pass} | "
            f"{_med_range(pass_costs, '{:.2f}')} | {_med_range([r.get('rounds_used', 0) for r in group], '{:.0f}')} | "
            f"{_med_range([b.get('subagent_spawns', 0) for b in behaviors], '{:.0f}')} | "
            f"{_med_range([b.get('compaction_events', 0) for b in behaviors], '{:.0f}')} |")
    if len(lines) == 4:
        lines.append("| - | - | 0 | 0 | - | - | - | - | - | - | - |")
    problems = [f"run {r['_dir']}: analysis_error: {r['analysis_error']}" for r in runs if r.get("analysis_error")]
    problems += hash_warnings(runs)
    if problems:
        lines += ["", "Warnings:", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs-dir", type=Path, default=Path("/workspace/agent-experiments-runs"))
    p.add_argument("--scenario", choices=cost.SCENARIOS, default=cost.PRIMARY_SCENARIO)
    a = p.parse_args(argv)
    if not a.runs_dir.is_dir():
        print(f"aggregate: error: runs dir {a.runs_dir} does not exist", file=sys.stderr)
        return 1
    sys.stdout.write(render(load_runs(a.runs_dir), a.scenario))
    return 0


if __name__ == "__main__":
    sys.exit(main())
