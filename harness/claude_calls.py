"""Headless Claude Code subagents of a Codex-main arm: reading their call records and transcripts.

claude_agent.py records each call under <claude dir>/agent-calls/<call id>/call.json and its
session transcript under <claude dir>/projects/. This module prices those transcripts as subagent
requests, summarizes the calls, and checks that each ran on its agent's model and effort without a
denied tool, the way codex.py does for Codex sessions.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import cost
import transcript
from transcript import TranscriptError

CALLS_DIR = "agent-calls"


def call_records(claude_dir: Path) -> list[dict[str, Any]]:
    """call.json of every claude-agent call, oldest first; a call without one is reported as unreadable."""
    calls: list[dict[str, Any]] = []
    root = claude_dir / CALLS_DIR
    for directory in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            record = json.loads((directory / "call.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            record = {"agent": None, "unreadable": str(exc)}
        record["call_id"] = directory.name
        calls.append(record)
    return calls


def call_transcript(claude_dir: Path, call: Mapping[str, Any]) -> Path | None:
    """The session transcript of a finished call, or None when it reported no session or left none."""
    session = call.get("session_id")
    if not session:
        return None
    try:
        return transcript.find_main_transcript(claude_dir, Path(str(call.get("cwd") or ".")), str(session))
    except TranscriptError:
        return None


def transcript_paths(claude_dir: Path) -> list[Path]:
    """The calls' transcripts in call order."""
    found = (call_transcript(claude_dir, c) for c in call_records(claude_dir))
    return [p for p in found if p is not None]


def load_requests(claude_dir: Path) -> tuple[cost.Request, ...]:
    """One subagent request per model response in the calls' transcripts (Claude pricing, cache TTLs
    as recorded)."""
    return cost.load_subagent_files(transcript_paths(claude_dir))


def call_failed(call: Mapping[str, Any]) -> bool:
    """A call that ended without a report, other than one stopped by a signal (the main agent
    or the CLI stopping it). Calls with no end record are counted as unfinished instead."""
    return call.get("ended_epoch") is not None and bool(call.get("failed")) and not call.get("stopped_by_signal")


def summarize(claude_dir: Path) -> dict[str, Any]:
    """Calls by agent, tool use, compactions, and per-call timing, outcome, and context size."""
    calls = call_records(claude_dir)
    tools: Counter[str] = Counter()
    compactions = 0
    per_call = []
    for call in calls:
        path = call_transcript(claude_dir, call)
        requests = cost.load_subagent_files([path]) if path else ()
        if path:
            tools.update(transcript.tool_use_counts(path))
            compactions += transcript.compaction_count(path)
        start, end = call.get("started_epoch"), call.get("ended_epoch")
        per_call.append({
            "call_id": call["call_id"], "agent": call.get("agent"), "session_id": call.get("session_id"),
            "started_epoch": start, "wall_s": round(end - start, 3) if start and end else None,
            "exit_code": call.get("exit_code"), "failed": call_failed(call), "unfinished": end is None,
            "stopped_by_signal": call.get("stopped_by_signal"), "usage_limit": bool(call.get("usage_limit")),
            "transcript": path is not None, "requests": len(requests),
            "max_input_tokens": max((cost.context_tokens(r) for r in requests), default=0),
            "total_cost_usd": call.get("total_cost_usd"), "num_turns": call.get("num_turns")})
    return {"calls": len(calls),
            "calls_by_agent": dict(sorted(Counter(str(c.get("agent")) for c in calls).items())),
            "tool_calls": dict(sorted(tools.items())), "compaction_events": compactions,
            "usage_limit": any(c["usage_limit"] for c in per_call), "per_call": per_call}


def run_outcome(summary: Mapping[str, Any]) -> tuple[str, str] | None:
    """(status, error) when the run's Claude subagents make it invalid: a Claude usage limit
    ("usage_limit"), or a call that failed ("claude_error"); None otherwise. Such a run measured
    the main agent working around a missing subagent, so it is rerun rather than reported."""
    def detail(call: Mapping[str, Any]) -> str:
        return f"{call['call_id']} (exit {call['exit_code']})"
    limited = [c for c in summary["per_call"] if c["usage_limit"]]
    if limited:
        return "usage_limit", "Claude usage limit reached in a subagent call: " + detail(limited[0])
    failed = [c for c in summary["per_call"] if c["failed"]]
    if failed:
        return "claude_error", f"{len(failed)} Claude subagent call(s) failed: " + "; ".join(map(detail, failed[:3]))
    return None


def spawn_calls(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """claude-agent calls in the shape of transcript.summarize_behavior's subagent_spawn_calls."""
    return [{"source": "main", "subagent_type": c["agent"], "model_arg": None, "observed_models": [],
             "timestamp": c["started_epoch"], "harness": "claude"} for c in summary["per_call"]]


def validate(claude_dir: Path, summary: Mapping[str, Any], agent_models: Mapping[str, str], effort: str,
             forbidden_tools: Sequence[str]) -> list[dict[str, Any]]:
    """Violations: a call on another model or effort or using a forbidden tool, a failed or unfinished
    call, or a finished call without a transcript (reported, never acted on)."""
    assert agent_models
    found: Counter[tuple[str, str, str]] = Counter()
    calls = {c["call_id"]: c for c in call_records(claude_dir)}
    for entry in summary["per_call"]:
        call = calls[entry["call_id"]]
        agent = str(call.get("agent"))
        if entry["failed"]:
            found[("claude_call_failed", "exit 0 with a report", f"{entry['call_id']} exit {entry['exit_code']}")] += 1
        elif entry["unfinished"]:
            found[("claude_call_unfinished", "an end record", entry["call_id"])] += 1
        elif not entry["transcript"] and not entry["stopped_by_signal"]:
            found[("claude_transcript_missing", "a session transcript", entry["call_id"])] += 1
        path = call_transcript(claude_dir, call)
        if path is None or agent not in agent_models:
            continue
        for violation in transcript.validate_models([path], [], agent_models[agent], (), {agent_models[agent]: effort},
                                                    forbidden_tools):
            found[(str(violation["kind"]), str(violation["expected"]), str(violation["observed"]))] += int(violation["count"])
    return [{"kind": k[0], "source": "subagent", "file": "-", "expected": k[1], "observed": k[2], "count": n}
            for k, n in sorted(found.items())]


def leak_scan_files(claude_dir: Path) -> list[Path]:
    """Files whose new lines the leak scan reads: the calls' transcripts and the tasks handed to them."""
    tasks = sorted((claude_dir / CALLS_DIR).glob("*/task.txt")) if (claude_dir / CALLS_DIR).is_dir() else []
    return [*transcript_paths(claude_dir), *tasks]


def untranscribed(claude_dir: Path) -> dict[str, dict[str, Any]]:
    """Per model, the tokens and USD that the calls' own `modelUsage` totals hold beyond their
    transcripts (requests Claude Code makes without transcribing them, such as compaction summaries
    and small helper calls), priced as cost.billed_from_model_usage does."""
    total: dict[str, dict[str, Any]] = {}
    for call in call_records(claude_dir):
        path = call_transcript(claude_dir, call)
        usage = call.get("model_usage")
        if path is None or not isinstance(usage, dict) or not usage:
            continue
        report = cost.compute_cost(cost.load_subagent_files([path]))
        gap = cost.billed_from_model_usage(report, usage, str(call.get("model")))["untranscribed"]
        for model, found in gap.items():
            acc = total.setdefault(model, {"tokens": {}, "usd": 0.0})
            acc["usd"] += float(found["usd"])
            for key, value in found["tokens"].items():
                acc["tokens"][key] = acc["tokens"].get(key, 0) + int(value)
    return total
