"""Helpers for reading Claude Code session transcripts (JSONL)."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Collection, Iterator, Mapping, Sequence

SPAWN_TOOL_NAMES: frozenset[str] = frozenset({"Agent", "Task"})


class TranscriptError(Exception):
    """Raised when a transcript is missing or malformed."""


def sanitize_cwd(cwd: Path) -> str:
    """Claude Code names the project directory after the cwd with non-alphanumerics as dashes."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def iter_entries(path: Path) -> Iterator[dict]:
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise TranscriptError(f"cannot open transcript {path}: {exc}") from exc
    with handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TranscriptError(f"{path}:{number}: invalid JSON: {exc}") from exc
            if isinstance(entry, dict):
                yield entry


def parse_timestamp(text: str) -> float:
    """ISO 8601 (with Z) to epoch seconds."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def find_main_transcript(config_dir: Path, cwd: Path, session_id: str) -> Path:
    expected = config_dir / "projects" / sanitize_cwd(cwd) / f"{session_id}.jsonl"
    if expected.is_file():
        return expected
    matches = sorted((config_dir / "projects").glob(f"*/{session_id}.jsonl"))
    if len(matches) == 1:
        return matches[0]
    raise TranscriptError(
        f"transcript for session {session_id} not found at {expected} "
        f"({len(matches)} fallback matches under {config_dir / 'projects'})")


def subagents_dir_for(main_path: Path) -> Path:
    """Subagent transcripts live in <project>/<session_id>/subagents/agent-<id>.jsonl."""
    return main_path.with_suffix("") / "subagents"


def subagent_files(directory: Path | None) -> tuple[Path, ...]:
    if directory is None or not directory.is_dir():
        return ()
    return tuple(sorted(directory.glob("*.jsonl")))


def _tool_uses(entry: dict) -> Iterator[dict]:
    message = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _assistant_messages(path: Path) -> Iterator[tuple[dict, dict]]:
    """(entry, message) for assistant entries, one per message id."""
    seen: set[str] = set()
    for entry in iter_entries(path):
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        if message.get("model") == "<synthetic>":
            continue
        key = str(message.get("id") or entry.get("uuid") or "")
        if key in seen:
            continue
        seen.add(key)
        yield entry, message


def _read_meta(path: Path) -> dict:
    """The subagent transcript's .meta.json content; empty when absent or unreadable."""
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.is_file():
        return {}
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def subagent_inventory(subagent_dirs: Sequence[Path]) -> list[dict[str, object]]:
    """One record per subagent transcript: type and tool_use id from its .meta.json,
    and the models and efforts actually observed in the transcript."""
    inventory: list[dict[str, object]] = []
    for directory in subagent_dirs:
        for path in subagent_files(directory):
            meta = _read_meta(path)
            models: Counter[str] = Counter()
            efforts: Counter[str] = Counter()
            for entry, message in _assistant_messages(path):
                models[str(message.get("model"))] += 1
                if isinstance(entry.get("effort"), str):
                    efforts[entry["effort"]] += 1
            inventory.append({
                "path": path, "agent_id": path.stem.removeprefix("agent-"),
                "tool_use_id": meta.get("toolUseId"), "agent_type": meta.get("agentType"),
                "models": dict(models), "efforts": dict(efforts)})
    return inventory


def validate_models(main_paths: Sequence[Path], subagent_dirs: Sequence[Path], main_model: str,
                    subagent_models: Collection[str], expected_effort: Mapping[str, str],
                    forbidden_tools: Sequence[str],
                    agent_type_models: Mapping[str, str] | None = None) -> list[dict[str, object]]:
    """Violations of the arm's model, effort, and tool policy (reported, never acted on).

    A subagent request may use any model in subagent_models, each at its expected effort.
    With agent_type_models, each subagent transcript must instead use the model mapped to
    its recorded agent type, and a type outside the mapping is flagged."""
    found: Counter[tuple[str, str, str, str, str]] = Counter()

    def check(path: Path, source: str, expected_models: frozenset[str]) -> None:
        for entry, message in _assistant_messages(path):
            model = str(message.get("model"))
            if not expected_models:
                found[("unexpected_subagent", source, path.name, "none", model)] += 1
                continue
            if model not in expected_models:
                found[("model", source, path.name, "|".join(sorted(expected_models)), model)] += 1
            effort = entry.get("effort")
            wanted = expected_effort.get(model) if model in expected_models else None
            if isinstance(effort, str) and wanted and effort != wanted:
                found[("effort", source, path.name, wanted, effort)] += 1
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in forbidden_tools:
                    found[("forbidden_tool", source, path.name, "not used", str(block.get("name")))] += 1

    for path in main_paths:
        check(path, "main", frozenset({main_model}))
    for directory in subagent_dirs:
        for path in subagent_files(directory):
            if agent_type_models is None:
                check(path, "subagent", frozenset(subagent_models))
                continue
            agent_type = _read_meta(path).get("agentType")
            if agent_type not in agent_type_models:
                found[("agent_type", "subagent", path.name, "|".join(sorted(agent_type_models)), str(agent_type))] += 1
                check(path, "subagent", frozenset(subagent_models))
                continue
            check(path, "subagent", frozenset({agent_type_models[agent_type]}))
    return [{"kind": k[0], "source": k[1], "file": k[2], "expected": k[3], "observed": k[4], "count": n}
            for k, n in sorted(found.items())]


LEAK_PATTERNS: tuple[str, ...] = ("acceptance/", "selftest", "reference/", "agent-experiments-runs")


def scan_new_lines(path: Path, start_line: int, patterns: Sequence[str] = LEAK_PATTERNS
                   ) -> tuple[list[dict[str, object]], int]:
    """Hits for patterns in lines after start_line; returns (hits, total line count)."""
    hits: list[dict[str, object]] = []
    total = 0
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TranscriptError(f"cannot open transcript {path}: {exc}") from exc
    with handle:
        for number, line in enumerate(handle, 1):
            total = number
            if number <= start_line:
                continue
            for pattern in patterns:
                position = line.find(pattern)
                if position >= 0:
                    hits.append({"file": path.name, "line": number, "pattern": pattern,
                                 "excerpt": line[max(0, position - 60):position + 80].strip()})
    return hits, total


def summarize_behavior(main_paths: Sequence[Path], subagent_dirs: Sequence[Path]) -> dict[str, object]:
    """Counts of tool calls, subagent spawns, and compaction events.

    Tool-use blocks are deduplicated by block id because parallel tool calls
    can repeat the same message across several transcript entries. Each spawn
    records the subagent type and the models observed in the matching
    subagent transcript (joined by tool_use id).
    """
    tool_calls: dict[str, Counter[str]] = {"main": Counter(), "subagent": Counter()}
    spawns_by_type: Counter[str] = Counter()
    spawns_by_model: Counter[str] = Counter()
    spawn_calls: list[dict[str, object]] = []
    seen_blocks: set[str] = set()
    compactions = 0
    inventory = subagent_inventory(subagent_dirs)
    by_tool_use = {str(i["tool_use_id"]): i for i in inventory if i["tool_use_id"]}

    sources = [("main", p) for p in main_paths]
    sources += [("subagent", p) for d in subagent_dirs for p in subagent_files(d)]
    seen_boundaries: set[str] = set()
    for source, path in sources:
        for entry in iter_entries(path):
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                uuid = str(entry.get("uuid") or "")
                if not uuid or uuid not in seen_boundaries:
                    seen_boundaries.add(uuid)
                    compactions += 1
            for block in _tool_uses(entry):
                block_id = block.get("id")
                if isinstance(block_id, str):
                    if block_id in seen_blocks:
                        continue
                    seen_blocks.add(block_id)
                name = str(block.get("name"))
                tool_calls[source][name] += 1
                if name in SPAWN_TOOL_NAMES:
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    subagent_type = str(tool_input.get("subagent_type") or "general-purpose")
                    match = by_tool_use.get(str(block_id))
                    observed = sorted(match["models"]) if match else []
                    spawns_by_type[subagent_type] += 1
                    for model in observed or ["(no transcript)"]:
                        spawns_by_model[model] += 1
                    spawn_calls.append({"source": source, "subagent_type": subagent_type,
                                        "model_arg": tool_input.get("model"), "observed_models": observed,
                                        "timestamp": parse_timestamp(entry["timestamp"])
                                        if isinstance(entry.get("timestamp"), str) else None})
    return {
        "tool_calls_main": dict(sorted(tool_calls["main"].items())),
        "tool_calls_subagent": dict(sorted(tool_calls["subagent"].items())),
        "tool_calls_total": sum(tool_calls["main"].values()) + sum(tool_calls["subagent"].values()),
        "subagent_spawns": sum(spawns_by_type.values()),
        "subagent_spawns_by_type": dict(sorted(spawns_by_type.items())),
        "subagent_spawns_by_observed_model": dict(sorted(spawns_by_model.items())),
        "subagent_spawn_calls": spawn_calls,
        "compaction_events": compactions,
        "subagent_transcripts": len(inventory),
    }
