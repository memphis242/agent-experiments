"""Cost, behavior, validation, and totals sections of a run record.

Shared by run.py (after each round) and reanalyze.py (from archived files).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import arms
import claude_calls
import codex
import cost
import transcript

HYPOTHETICAL_NOTE = ("The none, 5m, 1h, and uniform_1h scenarios are computed from transcripts only and "
                     "exclude untranscribed requests (compaction summaries).")
CODEX_MAIN_NOTE = ("A Codex main agent's cache writes carry no 5m/1h split and no pauses are simulated, so "
                   "the 5m, 1h, and uniform_1h scenarios equal billed; none reprices cache tokens at the input "
                   "price. All figures come from the session logs and exclude untranscribed requests "
                   "(compaction); exec_usage_reconciliation compares them with the CLI's own totals.")


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


def cache_ttl_report(requests: Sequence[cost.Request], expected_main: str,
                     expected_subagent: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Cache-write TTL actually used per thread kind, plus a cache_ttl violation per mismatched kind.

    A request mismatches when it writes cache with the TTL other than the one expected for
    its thread kind. Requests whose usage carried no 1h/5m split are counted as unsplit and
    never judged. Reported, never acted on."""
    ttls = {"1h": "cache_write_1h_tokens", "5m": "cache_write_5m_tokens"}
    expected = {"main": expected_main, "subagent": expected_subagent}
    assert set(expected.values()) <= set(ttls)
    summary: dict[str, Any] = {}
    violations: list[dict[str, Any]] = []
    for source, want in expected.items():
        group = [r for r in requests if r.source == source]
        other = "5m" if want == "1h" else "1h"
        broken = [r for r in group if r.ttl_split_known and getattr(r, ttls[other]) > 0]
        stamps = [r.timestamp for r in broken if r.timestamp > 0]
        first = (datetime.fromtimestamp(min(stamps), timezone.utc).isoformat().replace("+00:00", "Z")
                 if stamps else None)
        summary[source] = {
            "expected": want,
            "write_1h_tokens": sum(r.cache_write_1h_tokens for r in group),
            "write_5m_tokens": sum(r.cache_write_5m_tokens for r in group),
            "requests_with_5m_write": sum(1 for r in group if r.cache_write_5m_tokens > 0),
            "mismatched_requests": len(broken),
            "unsplit_requests": sum(1 for r in group if not r.ttl_split_known),
            "first_mismatch_timestamp": first,
        }
        if broken:
            violations.append({"kind": "cache_ttl", "source": source, "file": "-", "expected": want,
                               "observed": other, "count": len(broken), "first_timestamp": first})
    return summary, violations


def exec_usage_reconciliation(record: dict[str, Any], requests: Sequence[cost.Request]) -> dict[str, Any]:
    """Token totals of the session logs against the totals `codex exec` reported. Each round
    reports its thread's cumulative usage, so the last round of each thread counts. Report
    only: the difference is requests the logs omit."""
    last: dict[str, dict[str, Any]] = {}
    for r in record["rounds"]:
        reported = r.get("codex_cli") or {}
        if reported.get("session_id"):
            last[reported["session_id"]] = reported.get("usage") or {}
    exec_usage = {key: sum(usage.get(key) or 0 for usage in last.values()) for key in codex.USAGE_KEYS}
    logged = {"input_tokens": sum(cost.context_tokens(r) for r in requests),
              "cached_input_tokens": sum(r.cache_read_tokens for r in requests),
              "output_tokens": sum(r.output_tokens for r in requests)}
    return {"exec_usage": exec_usage, "logged_usage": logged,
            "diff": {key: exec_usage[key] - logged[key] for key in logged}}


def update_record(record: dict[str, Any], mains: Sequence[Path], sub_dirs: Sequence[Path],
                  resume_indices: Sequence[int], gaps_path: Path, seed: int,
                  definition_models: dict[str, str], codex_home: Path | None = None,
                  subagent_home: Path | None = None, claude_dir: Path | None = None) -> None:
    """Recompute cost, behavior, validation, and totals in place; failures go to analysis_error.

    With codex_home (a Codex-subagent arm), the headless Codex subagents' requests join the
    Claude requests as subagent requests, and their calls count as spawns. For a Codex-main
    arm there are no Claude Code main transcripts (mains and sub_dirs are empty): the Codex session
    logs in codex_home hold the main agent's requests, and the CLI's own cost figures do not exist.
    The subagents of a Codex-main arm are separate sessions: Codex sessions in subagent_home (their
    own CODEX_HOME), or Claude Code sessions recorded under claude_dir by claude-agent."""
    arm = arms.get_arm(record["arm"])
    assert (codex_home is not None) == arm.uses_codex
    assert (subagent_home is not None) == (arm.codex_main and arm.codex)
    assert (claude_dir is not None) == arm.claude_agents
    assert not arm.codex_main or not (mains or sub_dirs)
    main_turns: int | None = None
    try:
        gaps: tuple[float, ...] | None = None
        warnings: list[str] = []
        if resume_indices and not arm.codex_main:
            if gaps_path.is_file():
                gaps = cost.load_gaps(gaps_path)
            else:
                warnings.append(f"pause samples {gaps_path} missing; scenarios computed without pauses")
        claude_requests = cost.load_requests(list(mains), list(sub_dirs))
        if arm.codex_main:
            assert not claude_requests
            main_requests = codex.load_requests(codex_home, "main")
            main_turns = len(main_requests)
            if subagent_home is not None:
                agent_requests = codex.load_requests(subagent_home, "subagent")
            elif claude_dir is not None:
                agent_requests = claude_calls.load_requests(claude_dir)
                claude_requests = agent_requests
            else:
                agent_requests = ()
            requests = main_requests + agent_requests
        else:
            requests = claude_requests + (codex.load_requests(codex_home) if codex_home is not None else ())
        pauses = cost.sample_pauses(resume_indices, gaps, seed) if resume_indices and gaps else {}
        report = cost.compute_cost(requests, pauses)
        data = {**report.to_dict(), "primary_scenario": cost.PRIMARY_SCENARIO, "seed": seed,
                "scenarios_note": CODEX_MAIN_NOTE if arm.codex_main else HYPOTHETICAL_NOTE,
                "reconciliation": {} if arm.codex_main else reconcile(record, claude_requests)}
        transcript_billed = data["scenarios"]["billed"]["total_usd"]
        data["billed_transcript_usd"] = transcript_billed
        if arm.codex_main:
            data["billed_basis"] = "codex_rollout"
            reconciliation = data["exec_usage_reconciliation"] = exec_usage_reconciliation(record, main_requests)
            if any(v > 0 for v in reconciliation["diff"].values()):
                warnings.append(f"codex exec reported more usage than the session logs hold ({reconciliation['diff']}); "
                                "billed cost omits those requests")
            if claude_dir is not None:
                # Requests a Claude subagent call made without transcribing them count as billed subagent spend.
                gap = claude_calls.untranscribed(claude_dir)
                billed = data["scenarios"]["billed"]
                for model, found in gap.items():
                    billed["by_model"][model] = round(billed["by_model"].get(model, 0.0) + found["usd"], 6)
                added = sum(found["usd"] for found in gap.values())
                billed["by_source"]["subagent"] = round(billed["by_source"]["subagent"] + added, 6)
                billed["total_usd"] = round(billed["total_usd"] + added, 6)
                data["untranscribed"] = gap
                data["billed_basis"] = "codex_rollout+claude_model_usage"
        elif usage := session_model_usage(record):
            mu = cost.billed_from_model_usage(report, usage, arm.main_model)
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
        ttl, ttl_violations = cache_ttl_report(claude_requests, arms.PROMPT_CACHE_TTL, arms.SUBAGENT_PROMPT_CACHE_TTL)
        behavior["cache_ttl"] = ttl
        behavior["cache_ttl_drop"] = ttl["main"]["mismatched_requests"] > 0
        codex_violations: list[dict[str, Any]] = []
        if codex_home is not None:
            assert arm.uses_codex
            summary = codex.summarize(codex_home)
            behavior["codex"] = summary
            if summary["unpriced_usage_records"]:
                warnings.append(f"{summary['unpriced_usage_records']} Codex usage record(s) without a response id "
                                "left unpriced")
            if summary["truncated_logs"]:
                warnings.append(f"Codex session log(s) cut off mid-line, last line ignored: {summary['truncated_logs']}")
            behavior["compaction_events"] += summary["compaction_events"]
            if arm.codex_main:
                assert summary["calls"] == 0, "a Codex main agent starts no codex-agent calls in its own home"
                codex_violations = codex.validate_main(summary, codex.read_rollouts(codex_home), arm.main_model,
                                                       arm.main_effort, record["session_ids"])
            else:
                behavior["subagent_spawn_calls"] = behavior["subagent_spawn_calls"] + codex.spawn_calls(summary)
                behavior["subagent_spawns"] += summary["calls"]
                by_type = dict(behavior["subagent_spawns_by_type"])
                for agent, n in summary["calls_by_agent"].items():
                    by_type[agent] = by_type.get(agent, 0) + n
                behavior["subagent_spawns_by_type"] = dict(sorted(by_type.items()))
                codex_violations = codex.validate(codex_home, summary, {n: arm.model_for(n) for n in arm.agents},
                                                  arm.subagent_effort)
        agent_violations: list[dict[str, Any]] = []
        if subagent_home is not None:
            sub = behavior["codex_subagents"] = codex.summarize(subagent_home)
            if sub["unpriced_usage_records"]:
                warnings.append(f"{sub['unpriced_usage_records']} Codex subagent usage record(s) without a response "
                                "id left unpriced")
            if sub["truncated_logs"]:
                warnings.append(f"Codex subagent session log(s) cut off mid-line, last line ignored: {sub['truncated_logs']}")
            agent_violations = codex.validate(subagent_home, sub, {n: arm.model_for(n) for n in arm.agents},
                                              arm.subagent_effort)
        elif claude_dir is not None:
            sub = behavior["claude_agents"] = claude_calls.summarize(claude_dir)
            agent_violations = claude_calls.validate(
                claude_dir, sub, {n: arm.model_for(n) for n in arm.agents}, arm.subagent_effort,
                [*arms.DENIED_TOOLS, "Agent"])
        if subagent_home is not None or claude_dir is not None:
            behavior["subagent_spawn_calls"] = (codex.spawn_calls(sub) if subagent_home is not None
                                                else claude_calls.spawn_calls(sub))
            behavior["subagent_spawns"] = sub["calls"]
            behavior["subagent_spawns_by_type"] = dict(sub["calls_by_agent"])
            behavior["compaction_events"] += sub["compaction_events"]
        record["behavior"] = behavior
        summarize_delegation(record, behavior)
        forbidden = list(arms.DENIED_TOOLS) + (["Agent"] if arm.subagent_model is None or arm.codex else [])
        violations = transcript.validate_models(list(mains), list(sub_dirs), arm.main_model,
                                                arm.claude_subagent_models, arms.effort_map(arm), forbidden,
                                                {a: arm.model_for(a) for a in arm.agents} if arm.multi_model else None)
        violations += ttl_violations + codex_violations + agent_violations
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
    # Without a Claude CLI there is no reported cost, turn count, or API time; a Codex main
    # agent's turns are its model requests in the session logs.
    record["totals"] = {
        "billed_usd": billed,
        # The CLI figure is cumulative for the session on resumed rounds; stored as reported.
        "cli_reported_by_round_usd": cli,
        "cli_reported_cumulative_usd": next((c for c in reversed(cli) if c is not None), None),
        "turns": main_turns if arm.codex_main else sum((r.get("cli") or {}).get("num_turns") or 0 for r in rounds),
        "api_time_ms": None if arm.codex_main else sum((r.get("cli") or {}).get("duration_api_ms") or 0 for r in rounds),
        "cli_duration_ms": None if arm.codex_main else sum((r.get("cli") or {}).get("duration_ms") or 0 for r in rounds),
        "wall_time_s": round(sum(r["wall_time_s"] for r in rounds), 3),
    }
