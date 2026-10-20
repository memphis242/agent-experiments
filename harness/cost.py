#!/usr/bin/env python3
"""Offline cost computation from session transcripts under three cache scenarios.

Scenarios (see docs/experiment-design.md):
  billed: every token at the price it was recorded under (the primary figure).
  The remaining scenarios reprice the MAIN agent only; subagent requests stay
  as recorded.
  none: cache read and cache creation tokens are billed at the input price.
  5m:   cache creation is billed at the 5m write price; at each simulated
        pause longer than 300 s the request following the pause has its cache
        read tokens rebilled as 5m writes.
  1h:   cache creation is billed at the 1h write price; the same rebilling
        applies only to pauses longer than 3600 s.
  uniform_1h: subagent cache writes repriced at the 1h write price.

Pauses are positioned by main-transcript request index (the position of a
deduplicated request in the main transcript, starting at 0). Subagent
requests are never rebilled.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from transcript import TranscriptError, iter_entries, parse_timestamp, subagent_files

# "billed" prices every token as it was recorded and is the primary figure.
# "none", "5m", and "1h" are counterfactuals for the MAIN agent only; subagent
# requests keep their recorded pricing there. "uniform_1h" reprices subagent
# writes at the 1h write price and leaves the main agent as recorded.
SCENARIOS: tuple[str, ...] = ("billed", "none", "5m", "1h", "uniform_1h")
PRIMARY_SCENARIO = "billed"
PAUSE_THRESHOLD_S: Mapping[str, float] = MappingProxyType({"5m": 300.0, "1h": 3600.0})
SOURCES: tuple[str, ...] = ("main", "subagent")
_PER_MILLION = 1_000_000.0


class CostError(Exception):
    """Raised for unknown models or inconsistent cost inputs."""


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""
    input: float
    cache_read: float
    write_5m: float
    write_1h: float
    output: float


@dataclass(frozen=True)
class ModelPricing:
    """A model's price card, plus an optional card for prompts longer than a threshold.

    A request is priced on its own prompt length (see context_tokens): above
    long_threshold tokens the whole request, output included, uses long_card.
    """
    base: ModelPrice
    long_threshold: int = 0
    long_card: ModelPrice | None = None

    def __post_init__(self) -> None:
        assert (self.long_card is None) == (self.long_threshold == 0)
        if self.long_card is not None:
            assert self.long_threshold > 0
            assert all(getattr(self.long_card, f.name) >= getattr(self.base, f.name) for f in fields(ModelPrice))

    def at(self, prompt_tokens: int) -> ModelPrice:
        """Card for a request whose prompt has prompt_tokens tokens."""
        assert prompt_tokens >= 0
        if self.long_card is not None and prompt_tokens > self.long_threshold:
            return self.long_card
        return self.base

    @property
    def longest(self) -> ModelPrice:
        """Card for the longest prompts; the base card when the model has no tier."""
        return self.long_card or self.base


PRICING: Mapping[str, ModelPricing] = MappingProxyType({
    "claude-opus-5-5": ModelPricing(ModelPrice(4.00, 0.20, 5.00, 8.00, 20.00)),
    "claude-sonnet-5-5": ModelPricing(ModelPrice(2.00, 0.10, 2.50, 4.00, 10.00)),
    "claude-sonnet-5": ModelPricing(ModelPrice(2.00, 0.20, 2.50, 4.00, 10.00)),
    "claude-sonnet-4-6": ModelPricing(ModelPrice(3.00, 0.30, 3.75, 6.00, 15.00)),
    "claude-haiku-5-5": ModelPricing(ModelPrice(0.10, 0.01, 0.125, 0.20, 0.50),
                                     100_000, ModelPrice(0.50, 0.05, 0.625, 1.00, 2.50)),
    # OpenAI standard tier (developers.openai.com/api/docs/pricing, 2026-10-19): input, cached
    # input, cache write (one price, so both write slots carry it), output; above 272K input
    # tokens the request is priced at 2x for input and 1.5x for output. Reasoning tokens are output.
    "gpt-6.1-sol": ModelPricing(ModelPrice(2.00, 0.10, 2.50, 2.50, 10.00),
                                272_000, ModelPrice(4.00, 0.20, 5.00, 5.00, 15.00)),
    # Same tier and conventions (developers.openai.com/api/docs/pricing, 2026-10-20); above 272K
    # input tokens input is 2x and output 1.5x.
    "gpt-6-luna": ModelPricing(ModelPrice(0.10, 0.01, 0.125, 0.125, 0.50),
                               272_000, ModelPrice(0.20, 0.02, 0.25, 0.25, 0.75)),
})


def pricing_for(model: str) -> ModelPricing:
    """Pricing lookup; a context-window suffix such as '[1m]' is ignored."""
    key = model.split("[", 1)[0]
    try:
        return PRICING[key]
    except KeyError:
        raise CostError(f"no price for model {model!r}; known models: {', '.join(PRICING)}") from None


def price_for(model: str, prompt_tokens: int) -> ModelPrice:
    """Price card that applies to a request with the given prompt length."""
    return pricing_for(model).at(prompt_tokens)


@dataclass(frozen=True)
class Request:
    message_id: str
    model: str
    source: str                 # "main" or "subagent"
    main_index: int | None      # position in the main transcript; None for subagents
    timestamp: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    thread: str = ""            # transcript file stem for subagent requests
    ttl_split_known: bool = True  # False when the usage carried no 1h/5m split (writes then count as 5m)

    def __post_init__(self) -> None:
        assert self.source in SOURCES
        assert (self.source == "main") == (self.main_index is not None)


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _request_from_usage(message: dict, usage: dict, entry: dict, source: str, main_index: int | None,
                        thread: str = "") -> Request:
    created = _int(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        w1h = _int(split.get("ephemeral_1h_input_tokens"))
        w5m = _int(split.get("ephemeral_5m_input_tokens"))
        if w1h + w5m != created:  # remainder of an incomplete split counts as 5m
            w5m = created - w1h
    else:
        w1h, w5m = 0, created
    split_known = isinstance(split, dict)
    assert w1h >= 0 and w5m >= 0 and w1h + w5m == created
    stamp = entry.get("timestamp")
    return Request(
        message_id=str(message.get("id") or entry.get("uuid") or ""),
        model=str(message.get("model") or ""),
        source=source,
        main_index=main_index,
        timestamp=parse_timestamp(stamp) if isinstance(stamp, str) else 0.0,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_write_5m_tokens=w5m,
        cache_write_1h_tokens=w1h,
        thread=thread,
        ttl_split_known=split_known,
    )


def _load_file(path: Path, source: str, seen: set[str], start_index: int = 0) -> list[Request]:
    """Requests from one transcript, deduplicated by message id.

    Repeated entries for one message id (parallel tool calls, streaming
    snapshots) collapse to the first position with the largest output count.
    """
    ordered: list[str] = []
    best: dict[str, tuple[dict, dict, dict]] = {}
    for entry in iter_entries(path):
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict) or message.get("model") == "<synthetic>":
            continue
        key = str(message.get("id") or entry.get("uuid") or "")
        if not key or key in seen:
            continue
        if key not in best:
            ordered.append(key)
            best[key] = (message, usage, entry)
        elif _int(usage.get("output_tokens")) >= _int(best[key][1].get("output_tokens")):
            first_entry = best[key][2]
            best[key] = (message, usage, first_entry)
    seen.update(ordered)
    requests: list[Request] = []
    for position, key in enumerate(ordered):
        message, usage, entry = best[key]
        requests.append(_request_from_usage(message, usage, entry, source, start_index + position if source == "main" else None, path.stem))
    return requests


def load_requests(main_paths: Path | Sequence[Path],
                  subagent_dirs: Path | Sequence[Path] | None = None) -> tuple[Request, ...]:
    """Deduplicated requests: main transcripts in order, then subagent transcripts.

    Several main transcripts (a resumed session that wrote a new file) share
    one message-id namespace and one continuing main request index.
    """
    mains = [main_paths] if isinstance(main_paths, Path) else list(main_paths)
    dirs = [subagent_dirs] if isinstance(subagent_dirs, Path) else list(subagent_dirs or [])
    seen: set[str] = set()
    requests: list[Request] = []
    main_count = 0
    for path in mains:
        loaded = _load_file(path, "main", seen, main_count)
        main_count += len(loaded)
        requests.extend(loaded)
    for directory in dirs:
        for path in subagent_files(directory):
            requests.extend(_load_file(path, "subagent", seen))
    return tuple(requests)


def main_request_count(main_paths: Path | Sequence[Path]) -> int:
    return sum(1 for r in load_requests(main_paths) if r.source == "main")


def _recorded_cost(req: Request) -> float:
    p = price_for(req.model, context_tokens(req))
    return (req.input_tokens * p.input + req.output_tokens * p.output + req.cache_read_tokens * p.cache_read
            + req.cache_write_5m_tokens * p.write_5m + req.cache_write_1h_tokens * p.write_1h) / _PER_MILLION


def context_tokens(req: Request) -> int:
    """Prompt size of one request: uncached input plus cache reads and writes."""
    return req.input_tokens + req.cache_read_tokens + req.cache_write_5m_tokens + req.cache_write_1h_tokens


def context_peaks(requests: Sequence[Request]) -> dict[str, object]:
    """Peak context per thread over deduplicated requests: main, all subagents (None if
    there are none), and each subagent transcript by file stem."""
    main = [context_tokens(r) for r in requests if r.source == "main"]
    by_thread: dict[str, int] = {}
    for r in requests:
        if r.source == "subagent":
            by_thread[r.thread] = max(by_thread.get(r.thread, 0), context_tokens(r))
    return {"main": max(main) if main else None,
            "subagent": max(by_thread.values()) if by_thread else None,
            "by_thread": by_thread}


def request_cost(req: Request, scenario: str, pause_s: float = 0.0) -> float:
    """USD for one request under a scenario; pause_s is the pause just before it."""
    assert scenario in SCENARIOS and pause_s >= 0.0
    p = price_for(req.model, context_tokens(req))  # the scenarios reprice cache treatment, not prompt size
    writes = req.cache_write_5m_tokens + req.cache_write_1h_tokens
    read = req.cache_read_tokens
    if scenario == "billed":
        return _recorded_cost(req)
    if scenario == "uniform_1h":
        if req.source == "main":
            return _recorded_cost(req)
        return (req.input_tokens * p.input + req.output_tokens * p.output + read * p.cache_read
                + writes * p.write_1h) / _PER_MILLION
    if req.source != "main":
        return _recorded_cost(req)
    usd = req.output_tokens * p.output
    if scenario == "none":
        usd += (req.input_tokens + writes + read) * p.input
    else:
        write_price = p.write_5m if scenario == "5m" else p.write_1h
        usd += req.input_tokens * p.input + writes * write_price
        if pause_s > PAUSE_THRESHOLD_S[scenario]:
            usd += read * write_price
        else:
            usd += read * p.cache_read
    return usd / _PER_MILLION


def billed_by_round(requests: Sequence[Request], round_starts: Sequence[float]) -> tuple[float, ...]:
    """Billed USD per round; a request belongs to the last round that started at or before it."""
    assert round_starts, "at least one round start is required"
    totals = [0.0] * len(round_starts)
    for req in requests:
        index = 0
        for i, start in enumerate(round_starts):
            if req.timestamp + 1.0 >= start:
                index = i
        totals[index] += _recorded_cost(req)
    return tuple(totals)


@dataclass(frozen=True)
class ScenarioCost:
    total_usd: float
    by_model: Mapping[str, float]
    by_source: Mapping[str, float]


@dataclass(frozen=True)
class CostReport:
    scenarios: Mapping[str, ScenarioCost]
    tokens: Mapping[str, Mapping[str, int]]  # "<model>|<source>" -> token counts
    requests: int
    pauses: Mapping[int, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "pauses_s": {str(k): v for k, v in sorted(self.pauses.items())},
            "tokens": {k: dict(v) for k, v in sorted(self.tokens.items())},
            "scenarios": {
                name: {
                    "total_usd": round(s.total_usd, 6),
                    "by_model": {k: round(v, 6) for k, v in sorted(s.by_model.items())},
                    "by_source": {k: round(v, 6) for k, v in sorted(s.by_source.items())},
                }
                for name, s in self.scenarios.items()
            },
        }


def compute_cost(requests: Sequence[Request], pauses: Mapping[int, float] | None = None) -> CostReport:
    """pauses maps a main request index to the simulated pause (seconds) before it."""
    pauses = dict(pauses or {})
    main_count = sum(1 for r in requests if r.source == "main")
    for index, gap in pauses.items():
        if not 0 <= index < main_count:
            raise CostError(f"pause index {index} is outside the main transcript's {main_count} requests")
        if gap < 0:
            raise CostError(f"negative pause {gap} at request index {index}")
    scenarios: dict[str, ScenarioCost] = {}
    for scenario in SCENARIOS:
        by_model: dict[str, float] = {}
        by_source: dict[str, float] = {s: 0.0 for s in SOURCES}
        total = 0.0
        for req in requests:
            gap = pauses.get(req.main_index, 0.0) if req.main_index is not None else 0.0
            usd = request_cost(req, scenario, gap)
            total += usd
            by_model[req.model] = by_model.get(req.model, 0.0) + usd
            by_source[req.source] += usd
        scenarios[scenario] = ScenarioCost(total, MappingProxyType(by_model), MappingProxyType(by_source))
    tokens: dict[str, dict[str, int]] = {}
    for req in requests:
        t = tokens.setdefault(f"{req.model}|{req.source}", {
            "requests": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write_5m": 0, "cache_write_1h": 0})
        t["requests"] += 1
        t["input"] += req.input_tokens
        t["output"] += req.output_tokens
        t["cache_read"] += req.cache_read_tokens
        t["cache_write_5m"] += req.cache_write_5m_tokens
        t["cache_write_1h"] += req.cache_write_1h_tokens
    return CostReport(MappingProxyType(scenarios), MappingProxyType(tokens), len(requests), MappingProxyType(pauses))


_MU_FIELDS = (("input", "inputTokens"), ("output", "outputTokens"), ("cache_read", "cacheReadInputTokens"),
              ("cache_creation", "cacheCreationInputTokens"))


def _price_tokens(p: ModelPrice, t: Mapping[str, float], share_1h: float) -> float:
    creation_price = share_1h * p.write_1h + (1.0 - share_1h) * p.write_5m
    return (t["input"] * p.input + t["output"] * p.output + t["cache_read"] * p.cache_read
            + t["cache_creation"] * creation_price) / _PER_MILLION


def billed_from_model_usage(report: CostReport, model_usage: Mapping[str, Mapping[str, object]],
                            main_model: str) -> dict[str, object]:
    """Billed cost from the CLI's session-total modelUsage token counts.

    The CLI counts include requests the transcripts omit (compaction summaries).
    Transcript requests are priced one by one, each at the card for its own
    prompt length. The untranscribed remainder (CLI minus transcript) is priced
    in aggregate at the model's longest-prompt card, since summary prompts are
    large. The CLI's 5m/1h creation split is unknown, so each model's split is
    taken from the transcript's writes; a model with no transcript writes uses
    1h for the main model and 5m otherwise. Returns total, per-model figures, and
    the untranscribed remainder (CLI minus transcript) per model.
    """
    transcript_tokens: dict[str, dict[str, int]] = {}
    writes: dict[str, list[int]] = {}
    for key, t in report.tokens.items():
        model = key.split("|", 1)[0].split("[", 1)[0]
        acc = transcript_tokens.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0})
        acc["input"] += t["input"]
        acc["output"] += t["output"]
        acc["cache_read"] += t["cache_read"]
        acc["cache_creation"] += t["cache_write_5m"] + t["cache_write_1h"]
        w = writes.setdefault(model, [0, 0])
        w[0] += t["cache_write_5m"]
        w[1] += t["cache_write_1h"]
    transcript_usd: dict[str, float] = {}
    for model_key, usd in report.scenarios[PRIMARY_SCENARIO].by_model.items():
        model = model_key.split("[", 1)[0]
        transcript_usd[model] = transcript_usd.get(model, 0.0) + usd
    cli_tokens: dict[str, dict[str, float]] = {}
    for raw_model, usage in model_usage.items():
        acc = cli_tokens.setdefault(raw_model.split("[", 1)[0], {name: 0.0 for name, _ in _MU_FIELDS})
        for name, field in _MU_FIELDS:
            acc[name] += float(usage.get(field) or 0)
    by_model: dict[str, float] = {}
    untranscribed: dict[str, dict[str, object]] = {}
    for model, cli in cli_tokens.items():
        w5m, w1h = writes.get(model, [0, 0])
        share_1h = w1h / (w5m + w1h) if w5m + w1h else (1.0 if model == main_model else 0.0)
        have = transcript_tokens.get(model, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0})
        gap = {name: cli[name] - have[name] for name in cli}
        gap_usd = _price_tokens(pricing_for(model).longest, gap, share_1h)
        by_model[model] = transcript_usd.get(model, 0.0) + gap_usd
        untranscribed[model] = {"tokens": {k: int(v) for k, v in gap.items()}, "usd": round(gap_usd, 6)}
    for model, usd in transcript_usd.items():  # models seen only in transcripts still count
        by_model.setdefault(model, usd)
    return {"total_usd": sum(by_model.values()), "by_model": by_model, "untranscribed": untranscribed}


def load_gaps(path: Path) -> tuple[float, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CostError(f"cannot read pause samples {path}: {exc}") from exc
    if not isinstance(data, list) or not data or not all(isinstance(g, (int, float)) and g >= 0 for g in data):
        raise CostError(f"{path} must hold a non-empty JSON list of non-negative seconds")
    return tuple(float(g) for g in data)


def sample_pauses(indices: Sequence[int], gaps: Sequence[float], seed: int) -> dict[int, float]:
    """One gap per resume index, drawn reproducibly from the sample list."""
    if not gaps:
        raise CostError("cannot sample pauses from an empty gap list")
    rng = random.Random(seed)
    return {index: rng.choice(gaps) for index in indices}


def cost_for_transcripts(main_path: Path | Sequence[Path], subagents_dir: Path | Sequence[Path] | None,
                         resume_indices: Sequence[int], gaps: Sequence[float] | None, seed: int) -> CostReport:
    requests = load_requests(main_path, subagents_dir)
    pauses = sample_pauses(resume_indices, gaps, seed) if resume_indices and gaps else {}
    return compute_cost(requests, pauses)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute session cost under none/5m/1h cache scenarios.")
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--subagents-dir", type=Path, help="default: <transcript stem>/subagents next to the transcript")
    parser.add_argument("--pause-samples", type=Path, help="gaps.json from gaps.py")
    parser.add_argument("--resume-indices", type=int, nargs="*", default=[],
                        help="main request indices where a resume round began")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    sub_dir = args.subagents_dir or args.transcript.with_suffix("") / "subagents"
    try:
        gaps = load_gaps(args.pause_samples) if args.pause_samples else None
        if args.resume_indices and gaps is None:
            raise CostError("--resume-indices needs --pause-samples")
        report = cost_for_transcripts(args.transcript, sub_dir, args.resume_indices, gaps, args.seed)
    except (CostError, TranscriptError) as exc:
        print(f"cost: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
