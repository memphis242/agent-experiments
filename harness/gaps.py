#!/usr/bin/env python3
"""Compute inter-prompt gaps from ~/.claude/history.jsonl and write gaps.json.

history.jsonl holds one JSON object per submitted prompt:
  {"display": str, "pastedContents": {...}, "project": str,
   "timestamp": <epoch milliseconds>, "sessionId": str}
A gap is the time between two consecutive prompts of the same session (and
therefore the same project). Slash commands (display starting with '/') are
not turns and are excluded; gaps longer than --max-gap-s (default one day)
are treated as separate working sessions and excluded.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_HISTORY = Path.home() / ".claude" / "history.jsonl"
DEFAULT_OUT = Path(__file__).resolve().parent / "gaps.json"


class GapsError(Exception):
    pass


def compute_gaps(lines: list[str], max_gap_s: float) -> list[float]:
    stamps: dict[tuple[str, str], list[float]] = defaultdict(list)
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GapsError(f"history line {number}: invalid JSON: {exc}") from exc
        display, session = entry.get("display"), entry.get("sessionId")
        ts = entry.get("timestamp")
        if not isinstance(ts, (int, float)) or not isinstance(session, str):
            continue
        if isinstance(display, str) and display.lstrip().startswith("/"):
            continue
        stamps[(str(entry.get("project", "")), session)].append(ts / 1000.0)
    gaps: list[float] = []
    for times in stamps.values():
        times.sort()
        gaps.extend(b - a for a, b in zip(times, times[1:]) if 0 < b - a <= max_gap_s)
    return sorted(round(g, 3) for g in gaps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write sorted inter-prompt gaps (seconds) to gaps.json.")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-gap-s", type=float, default=86400.0)
    args = parser.parse_args(argv)
    try:
        gaps = compute_gaps(args.history.read_text(encoding="utf-8").splitlines(), args.max_gap_s)
        if not gaps:
            raise GapsError(f"no usable gaps found in {args.history}")
        args.out.write_text(json.dumps(gaps) + "\n", encoding="utf-8")
    except (OSError, GapsError) as exc:
        print(f"gaps: error: {exc}", file=sys.stderr)
        return 1
    over5m = sum(g > 300 for g in gaps)
    over1h = sum(g > 3600 for g in gaps)
    print(f"wrote {len(gaps)} gaps to {args.out} ({over5m} over 5 min, {over1h} over 1 h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
