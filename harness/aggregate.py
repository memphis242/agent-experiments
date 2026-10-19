#!/usr/bin/env python3
"""Aggregate run.json files into Markdown tables.

Primary metric per arm x task: total billed spend over ALL attempts (passes,
cap hits, usage-limit drops, CLI errors, infrastructure errors) divided by the
number of passes, shown beside passes/attempts and min-to-max ranges over all
attempts of the cell.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

import cost
import provenance


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


def load_superseded(path: Path) -> dict[str, str]:
    """Run id -> reason, from a JSON object naming completed runs replaced by a rerun."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in data.items()):
        raise ValueError(f"{path}: expected an object mapping run ids to non-empty reasons")
    return data


def drop_superseded(runs: list[dict[str, Any]], path: Path | None, tool: str) -> list[dict[str, Any]]:
    """Runs without the ones `path` lists as superseded; the count dropped is reported on stderr."""
    if path is None:
        return runs
    superseded = load_superseded(path)
    kept = [r for r in runs if r["_dir"] not in superseded]
    print(f"{tool}: dropped {len(runs) - len(kept)} superseded run(s) listed in {path}", file=sys.stderr)
    return kept


def scenario_cost(run: dict[str, Any], scenario: str) -> float | None:
    report = run.get("cost")
    if not report:
        return None
    return report["scenarios"].get(scenario, {}).get("total_usd")


ARM_DESCRIPTIONS: dict[str, str] = {
    "A1": "solo Opus 5.5",
    "A2": "Opus 5.5 subagents, eager", "A3": "Opus 5.5 subagents, conservative",
    "A4": "Sonnet 5.5 subagents, eager", "A5": "Sonnet 5.5 subagents, conservative",
    "A6": "Sonnet 5 subagents, eager", "A7": "Sonnet 5 subagents, conservative",
    "A8": "Sonnet 4.6 subagents, eager", "A9": "Sonnet 4.6 subagents, conservative",
    "A10": "solo Sonnet 5.5, xhigh effort", "A11": "Sonnet 5.5 main, Haiku 5.5 implementer and tester",
    "A12": "solo Sonnet 5.5, high effort",
    "A13": "solo Opus 5.5, medium effort",
    "A14": "Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered",
    "A15": "Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered",
    "A16": "solo Haiku 5.5, high effort",
    "A17": "solo Haiku 5.5, xhigh effort",
    "A18": "solo Sonnet 5, medium effort",
    "A19": "solo Sonnet 5, high effort",
    "A20": "solo Sonnet 4.6, high effort",
}


def _range(values: Sequence[float], fmt: str) -> str:
    """'min to max', or a single value when they are equal."""
    if not values:
        return "-"
    low, high = fmt.format(min(values)), fmt.format(max(values))
    return low if low == high else f"{low} to {high}"


def hash_warnings(runs: Sequence[dict[str, Any]],
                  resolve: provenance.Resolver = provenance.run_code_sha) -> list[str]:
    """Runs whose arm config hash differs from their arm's majority, or whose run-code hash
    (the run-relevant harness files at the recorded commit) differs from the overall majority."""
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
    current = provenance.run_code_sha("HEAD") or ""
    codes = {r["_dir"]: provenance.recorded_run_code(r, resolve) for r in runs}
    # Reviewed-equivalent run code counts as the current one.
    codes = {d: (current if provenance.same_run_code(c, current) else c) for d, c in codes.items()}
    if codes:
        majority = Counter(codes.values()).most_common(1)[0][0]
        for r in runs:
            if codes[r["_dir"]] != majority:
                warnings.append(f"run {r['_dir']}: run-code hash differs from the majority ({str(majority)[:12]})")
    return warnings


def short_task(name: str) -> str:
    found = re.match(r"task\d+", name)
    return found.group(0) if found else name


def render(runs: Sequence[dict[str, Any]], scenario: str, tasks: Sequence[str] | None = None) -> str:
    wanted = {short_task(t) for t in tasks} if tasks else None
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        if wanted is None or r["task"] in wanted:
            groups[(r["arm"], r["task"])].append(r)
    lines = [
        f"Cost scenario: `{scenario}`. Spend per pass is total spend over all attempts divided by passes. "
        "Other columns are min to max over all attempts of the cell; turns are main-agent turns summed over rounds.",
        "",
        "| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |",
        "|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|",
    ]
    for (arm, task), group in sorted(groups.items()):
        passes = sum(1 for r in group if r.get("status") == "completed" and r.get("passed"))
        costs = [c for c in (scenario_cost(r, scenario) for r in group) if c is not None]
        per_pass = f"{sum(costs) / passes:.2f}" if passes else "-"
        totals = [(r.get("totals") or {}) for r in group]
        behaviors = [(r.get("behavior") or {}) for r in group]
        lines.append(
            f"| {arm} | {ARM_DESCRIPTIONS.get(arm, '-')} | {task} | {passes}/{len(group)} | {per_pass} | "
            f"{_range(costs, '{:.2f}')} | {f'{max(costs) - min(costs):.2f}' if costs else '-'} | "
            f"{_range([t['wall_time_s'] / 60 for t in totals if t.get('wall_time_s') is not None], '{:.1f}')} | "
            f"{_range([t['turns'] for t in totals if t.get('turns') is not None], '{:.0f}')} | "
            f"{_range([b['subagent_spawns'] for b in behaviors if 'subagent_spawns' in b], '{:.0f}')} | "
            f"{_range([r.get('rounds_used', 0) for r in group], '{:.0f}')} | "
            f"{_range([b['compaction_events'] for b in behaviors if 'compaction_events' in b], '{:.0f}')} | "
            f"{_range([b['max_context_main'] / 1000 for b in behaviors if b.get('max_context_main') is not None], '{:.1f}')} | "
            f"{_range([b['max_context_subagent'] / 1000 for b in behaviors if b.get('max_context_subagent') is not None], '{:.1f}')} |")
    if len(lines) == 4:
        lines.append("| - | - | - | 0/0 | - | - | - | - | - | - | - | - | - | - |")
    shown = [r for g in groups.values() for r in g]
    problems = [f"run {r['_dir']}: analysis_error: {r['analysis_error']}" for r in shown if r.get("analysis_error")]
    problems += hash_warnings(runs)
    drops = Counter(r["arm"] for r in shown if (r.get("behavior") or {}).get("cache_ttl_drop") is True)
    if drops:
        lines += ["", "Runs with a main-agent cache TTL drop (5m writes although 1h was expected): "
                  + ", ".join(f"{arm} {n}/{sum(1 for r in shown if r['arm'] == arm)}"
                              for arm, n in sorted(drops.items()))]
    if problems:
        lines += ["", "Warnings:", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs-dir", type=Path, default=Path("/workspace/agent-experiments-runs"))
    p.add_argument("--scenario", choices=cost.SCENARIOS, default=cost.PRIMARY_SCENARIO)
    p.add_argument("--task", nargs="+", help="only these tasks (task1..task4 or full directory names)")
    p.add_argument("--markdown-out", type=Path, help="also write the table to this file")
    p.add_argument("--drop-status", nargs="+", default=(), metavar="STATUS",
                   help="exclude attempts with these run statuses (for example cli_error for runs cut off "
                        "by a subscription limit and rerun from scratch); the count dropped is reported")
    p.add_argument("--superseded", type=Path, help="JSON object of run ids replaced by reruns, to exclude")
    a = p.parse_args(argv)
    if not a.runs_dir.is_dir():
        print(f"aggregate: error: runs dir {a.runs_dir} does not exist", file=sys.stderr)
        return 1
    try:
        runs = drop_superseded(load_runs(a.runs_dir), a.superseded, "aggregate")
    except (OSError, ValueError) as exc:
        print(f"aggregate: error: {exc}", file=sys.stderr)
        return 1
    dropped = [r for r in runs if r.get("status") in set(a.drop_status)]
    if dropped:
        runs = [r for r in runs if r not in dropped]
        print(f"aggregate: dropped {len(dropped)} attempt(s) with status in {sorted(set(a.drop_status))}",
              file=sys.stderr)
    text = render(runs, a.scenario, a.task)
    sys.stdout.write(text)
    if a.markdown_out:
        try:
            a.markdown_out.write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"aggregate: error: cannot write {a.markdown_out}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
