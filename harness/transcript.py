"""Helpers for reading Claude Code session transcripts (JSONL)."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

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


def summarize_behavior(main_paths: Sequence[Path], subagent_dirs: Sequence[Path]) -> dict[str, object]:
    """Counts of tool calls, subagent spawns, and compaction events.

    Tool-use blocks are deduplicated by block id because parallel tool calls
    can repeat the same message across several transcript entries.
    """
    tool_calls: dict[str, Counter[str]] = {"main": Counter(), "subagent": Counter()}
    spawns_by_type: Counter[str] = Counter()
    spawns_by_model: Counter[str] = Counter()
    spawn_inputs: list[dict[str, object]] = []
    seen_blocks: set[str] = set()
    compactions = 0

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
                    model = tool_input.get("model")
                    spawns_by_type[subagent_type] += 1
                    spawns_by_model[str(model) if model else "(from agent definition)"] += 1
                    spawn_inputs.append({"source": source, "subagent_type": subagent_type, "model": model})
    return {
        "tool_calls_main": dict(sorted(tool_calls["main"].items())),
        "tool_calls_subagent": dict(sorted(tool_calls["subagent"].items())),
        "tool_calls_total": sum(tool_calls["main"].values()) + sum(tool_calls["subagent"].values()),
        "subagent_spawns": sum(spawns_by_type.values()),
        "subagent_spawns_by_type": dict(sorted(spawns_by_type.items())),
        "subagent_spawns_by_model_arg": dict(sorted(spawns_by_model.items())),
        "subagent_spawn_calls": spawn_inputs,
        "compaction_events": compactions,
        "subagent_transcripts": sum(1 for src, _ in sources if src == "subagent"),
    }
