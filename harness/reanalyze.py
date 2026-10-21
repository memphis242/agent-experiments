#!/usr/bin/env python3
"""Recompute the cost, behavior, validation, and totals sections of existing run.json files.

Inputs are the archived transcripts under <run>/transcripts/ and the
cli-round*.stdout.json files; everything else in run.json is left intact. The
file is rewritten atomically and `reanalyzed_with` records the harness SHA.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import analysis
import arms
import codex
import run
import transcript


def load_cli_results(run_dir: Path) -> dict[int, dict[str, Any]]:
    results: dict[int, dict[str, Any]] = {}
    for path in sorted(run_dir.glob("cli-round*.stdout.json")):
        number = int(path.name.removeprefix("cli-round").split(".", 1)[0])
        try:
            results[number] = run.parse_cli_result(path.read_text(encoding="utf-8"))
        except (OSError, run.RunError):
            continue
    return results


def reanalyze_run(run_dir: Path, gaps_path: Path) -> tuple[float | None, float | None]:
    """Returns (billed before, billed after)."""
    path = run_dir / "run.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    before = (record.get("totals") or {}).get("billed_usd")
    cli_results = load_cli_results(run_dir)
    for entry in record["rounds"]:
        result = cli_results.get(entry["round"])
        if result and entry.get("cli") is not None:
            for key in ("total_cost_usd", "modelUsage", "usage", "num_turns", "duration_ms", "duration_api_ms"):
                if key in result:
                    entry["cli"][key] = result[key]
    transcripts = run_dir / "transcripts"
    codex_main = record.get("main_harness") == "codex"
    mains = [] if codex_main else sorted(transcripts.glob("*.jsonl"))
    if not mains and not codex_main:
        raise transcript.TranscriptError(f"no archived transcripts in {transcripts}")
    # Archived order follows the session order recorded in run.json.
    order = {sid: i for i, sid in enumerate(record.get("session_ids", []))}
    mains.sort(key=lambda p: order.get(p.stem, len(order)))
    sub_dirs = [transcripts / m.stem / "subagents" for m in mains]
    definition_models = (record.get("behavior") or {}).get("agent_definition_models", {})
    codex_home = (codex.home_for(run_dir) if codex_main or record.get("subagent_harness") == "codex" else None)
    arm = arms.get_arm(record["arm"])
    # Persisted shell-launcher runs keep their separate home; built-in children share the main home.
    candidate = codex.subagent_home_for(codex_home) if codex_home and arm.builtin_subagents else None
    subagent_home = candidate if candidate is not None and candidate.is_dir() else None
    claude_dir = run_dir / "config" / arms.CLAUDE_DIR if arm.claude_agents else None
    analysis.update_record(record, mains, sub_dirs, record.get("resume_request_indices", []),
                           gaps_path, (record.get("cost") or {}).get("seed", 0), definition_models, codex_home,
                           subagent_home, claude_dir)
    record["reanalyzed_with"] = run.harness_git()[0]
    run.write_json_atomic(path, record)
    return before, (record.get("totals") or {}).get("billed_usd")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("runs_dir", type=Path)
    p.add_argument("--run", help="only this run ID")
    p.add_argument("--gaps", type=Path, default=run.DEFAULT_GAPS)
    a = p.parse_args(argv)
    dirs = [a.runs_dir / a.run] if a.run else sorted(d.parent for d in a.runs_dir.glob("*/run.json"))
    failed = 0
    for d in dirs:
        try:
            before, after = reanalyze_run(d, a.gaps)
        except (OSError, json.JSONDecodeError, KeyError, transcript.TranscriptError) as exc:
            print(f"{d.name}: error: {exc}", file=sys.stderr)
            failed += 1
            continue
        fmt = lambda v: "-" if v is None else f"{v:.4f}"
        print(f"{d.name}: billed {fmt(before)} -> {fmt(after)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
