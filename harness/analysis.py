"""Cost, behavior, validation, and totals sections of a run record.

Shared by run.py (after each round) and reanalyze.py (from archived files).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import arms
import cost
import transcript

HYPOTHETICAL_NOTE = ("The none, 5m, 1h, and uniform_1h scenarios are computed from transcripts only and "
                     "exclude untranscribed requests (compaction summaries).")


def reconcile(record: dict[str, Any], requests: Sequence[cost.Request]) -> dict[str, Any]:
    """Cumulative transcript-billed cost through each round against the CLI's cumulative
    figure (report only; the difference is mostly untranscribed compaction requests)."""
    rounds = [r for r in record["rounds"] if r.get("started_epoch") is not None]
    if not rounds:
        return {}
    billed = cost.billed_by_round(requests, [r["started_epoch"] for r in rounds])
    rows = []
    cumulative = 0.0
    for r, usd in zip(rounds, billed):
        cumulative += usd
        cli = (r.get("cli") or {}).get("total_cost_usd")  # cumulative for the session
        diff = None if cli is None else cumulative - cli
        rows.append({"round": r["round"], "billed_usd": round(usd, 6),
                     "billed_cumulative_usd": round(cumulative, 6), "cli_cumulative_usd": cli,
                     "diff_usd": None if diff is None else round(diff, 6),
                     "rel_diff": None if not cli else round(diff / cli, 6)})
    return {"rounds": rows, "total_billed_usd": round(sum(billed), 6)}


def summarize_delegation(record: dict[str, Any], behavior: dict[str, Any]) -> None:
    """Spawns per round, assigned by the spawn's transcript timestamp."""
    rounds = [r for r in record["rounds"] if r.get("started_epoch") is not None]
    for r in rounds:
        r["delegation"] = {"spawns": 0, "by_type": {}}
    for call in behavior["subagent_spawn_calls"]:
        ts = call.get("timestamp")
        if ts is None or not rounds:
            continue
        owner = rounds[0]
        for r in rounds:
            if ts + 1.0 >= r["started_epoch"]:
                owner = r
        owner["delegation"]["spawns"] += 1
        by_type = owner["delegation"]["by_type"]
        by_type[call["subagent_type"]] = by_type.get(call["subagent_type"], 0) + 1


def session_model_usage(record: dict[str, Any]) -> dict[str, Any] | None:
    """modelUsage is cumulative for the session on resumed rounds: use the last one reported."""
    for r in reversed(record["rounds"]):
        usage = (r.get("cli") or {}).get("modelUsage")
        if isinstance(usage, dict) and usage:
            return usage
    return None


def update_record(record: dict[str, Any], mains: Sequence[Path], sub_dirs: Sequence[Path],
                  resume_indices: Sequence[int], gaps_path: Path, seed: int,
                  definition_models: dict[str, str]) -> None:
    """Recompute cost, behavior, validation, and totals in place; failures go to analysis_error."""
    try:
        gaps: tuple[float, ...] | None = None
        warnings: list[str] = []
        if resume_indices:
            if gaps_path.is_file():
                gaps = cost.load_gaps(gaps_path)
            else:
                warnings.append(f"pause samples {gaps_path} missing; scenarios computed without pauses")
        requests = cost.load_requests(list(mains), list(sub_dirs))
        pauses = cost.sample_pauses(resume_indices, gaps, seed) if resume_indices and gaps else {}
        report = cost.compute_cost(requests, pauses)
        data = {**report.to_dict(), "primary_scenario": cost.PRIMARY_SCENARIO, "seed": seed,
                "scenarios_note": HYPOTHETICAL_NOTE, "reconciliation": reconcile(record, requests)}
        transcript_billed = data["scenarios"]["billed"]["total_usd"]
        data["billed_transcript_usd"] = transcript_billed
        usage = session_model_usage(record)
        if usage:
            mu = cost.billed_from_model_usage(report, usage, arms.MAIN_MODEL)
            data["scenarios"]["billed"]["total_usd"] = round(mu["total_usd"], 6)
            data["scenarios"]["billed"]["by_model"] = {k: round(v, 6) for k, v in sorted(mu["by_model"].items())}
            data["untranscribed"] = mu["untranscribed"]
            data["billed_basis"] = "cli_model_usage"
        else:
            data["billed_basis"] = "transcript"
            warnings.append("CLI modelUsage missing; billed cost is transcript-only and omits compaction requests")
        data["warnings"] = warnings
        record["cost"] = data
        behavior = transcript.summarize_behavior(list(mains), list(sub_dirs))
        behavior["agent_definition_models"] = definition_models
        peaks = cost.context_peaks(requests)
        behavior["max_context_main"] = peaks["main"]
        behavior["max_context_subagent"] = peaks["subagent"]
        record["behavior"] = behavior
        summarize_delegation(record, behavior)
        arm = arms.get_arm(record["arm"])
        forbidden = list(arms.DENIED_TOOLS) + (["Agent"] if arm.subagent_model is None else [])
        violations = transcript.validate_models(list(mains), list(sub_dirs), arms.MAIN_MODEL, arm.subagent_model,
                                                arms.EFFORT_BY_MODEL, forbidden)
        inventory = [{**{k: v for k, v in i.items() if k != "path"},
                      "max_context": peaks["by_thread"].get(Path(str(i["path"])).stem)}
                     for i in transcript.subagent_inventory(list(sub_dirs))]
        record["validation"] = {"ok": not violations, "violations": violations, "subagents": inventory}
        record["analysis_error"] = None
    except (cost.CostError, transcript.TranscriptError, OSError) as exc:
        record["analysis_error"] = str(exc)
    rounds = record["rounds"]
    billed = ((record.get("cost") or {}).get("scenarios", {}).get(cost.PRIMARY_SCENARIO, {}).get("total_usd"))
    cli = [(r.get("cli") or {}).get("total_cost_usd") for r in rounds]
    record["totals"] = {
        "billed_usd": billed,
        # The CLI figure is cumulative for the session on resumed rounds; stored as reported.
        "cli_reported_by_round_usd": cli,
        "cli_reported_cumulative_usd": next((c for c in reversed(cli) if c is not None), None),
        "turns": sum((r.get("cli") or {}).get("num_turns") or 0 for r in rounds),
        "api_time_ms": sum((r.get("cli") or {}).get("duration_api_ms") or 0 for r in rounds),
        "cli_duration_ms": sum((r.get("cli") or {}).get("duration_ms") or 0 for r in rounds),
        "wall_time_s": round(sum(r["wall_time_s"] for r in rounds), 3),
    }
