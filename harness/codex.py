"""Headless Codex subagents: per-run CODEX_HOME setup and check, and session-log analysis.

A Codex arm's main agent starts subagents with the codex-agent command (codex_agent.py).
Every run gets its own CODEX_HOME under the run dir, built from the arm's codex/
config, so no Codex state (sessions, history, memories, caches, skills, login
refreshes) is shared with other runs or with the user's ~/.codex, which the run's
mount namespace hides. Usage, model, and effort are read from Codex's own session
logs (sessions/**/rollout-*.jsonl), one token_usage_record per model response.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import cost
from transcript import TranscriptError, iter_entries, parse_timestamp

HOME_DIR = "codex-home"          # under the run dir; CODEX_HOME while the agent runs
CALLS_DIR = "agent-calls"        # written by codex-agent
AUTH_FILE = "auth.json"
USER_AUTH = Path("~/.codex/auth.json").expanduser()
# The run's namespace hides ~/.codex; only the installed Codex releases (binaries, no
# user data) are bound back, read-only, since ~/.local/bin/codex links into them.
HOST_VISIBLE: tuple[Path, ...] = (Path("~/.codex/packages").expanduser(),)
RUNTIME_FILE = "agent-runtime.json"  # read by codex-agent: the Codex binary this run is pinned to
PROMPT_INPUT_FILE = "codex-prompt-input.json"
# Prompt sections that must not reach a subagent: bundled skills, multi-agent
# collaboration, connected apps and plugins.
FORBIDDEN_PROMPT_MARKERS: tuple[str, ...] = ("<skills_instructions>", "<multi_agent_role>", "<multi_agent_mode>",
                                             "<apps_instructions>", "<plugins_instructions>")
SETUP_TIMEOUT_S = 120


class CodexError(Exception):
    """A Codex home that cannot be prepared or fails its isolation check."""


def home_for(run_dir: Path) -> Path:
    return run_dir / HOME_DIR


def _capture(cmd: Sequence[str], cwd: Path, env: dict[str, str]) -> str:
    try:
        done = subprocess.run(list(cmd), cwd=cwd, env=env, capture_output=True, text=True, check=False,
                              timeout=SETUP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexError(f"{' '.join(cmd[:3])} failed: {exc}") from exc
    if done.returncode != 0:
        raise CodexError(f"{' '.join(cmd[:3])} exited {done.returncode}: {done.stderr.strip()[-600:]}")
    return done.stdout


def _feature_states(listing: str) -> dict[str, bool]:
    """Feature name to enabled state from `codex features list` (name, stage, true/false)."""
    states: dict[str, bool] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] in ("true", "false"):
            states[parts[0]] = parts[-1] == "true"
    return states


def _prompt_texts(rendered: str) -> list[tuple[str, str]]:
    """(role, text) for each message of `codex debug prompt-input` output."""
    try:
        items = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise CodexError(f"codex debug prompt-input printed no JSON: {exc}") from exc
    texts: list[tuple[str, str]] = []
    for item in items if isinstance(items, list) else []:
        for block in item.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append((str(item.get("role")), block["text"]))
    return texts


def resolve_codex_bin(name: str = "codex") -> Path:
    """The Codex binary `name` resolves to, which must lie under a HOST_VISIBLE directory."""
    found = shutil.which(name)
    if found is None:
        raise CodexError(f"{name!r} is not on PATH; install the Codex CLI")
    resolved = Path(found).resolve()
    if not any(root.resolve() in resolved.parents for root in HOST_VISIBLE):
        raise CodexError(f"{found} resolves to {resolved}, outside {[str(p) for p in HOST_VISIBLE]}, "
                         "so a run's namespace would not see it")
    return resolved


def prepare_home(run_dir: Path, codex_config: Path, repo: Path, base_env: dict[str, str]) -> dict[str, Any]:
    """Create <run>/codex-home from the arm's codex/ dir, pin the Codex binary, validate the
    config strictly, disable Codex's bundled skills, check the model-visible prompt, and
    install a copy of the user's Codex login.

    The prompt check renders exactly what a subagent's first request carries apart from
    its role and task, saves it as codex-prompt-input.json in the run dir, and fails
    unless the working agreement is present and no skills, multi-agent, app, or plugin
    section is. Returns provenance for run.json."""
    home = home_for(run_dir)
    home.mkdir()
    codex_bin = str(resolve_codex_bin())
    (home / RUNTIME_FILE).write_text(json.dumps({"codex_bin": codex_bin}, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(codex_config / "config.toml", home / "config.toml")
    shutil.copyfile(codex_config / "AGENTS.md", home / "AGENTS.md")
    env = {**base_env, "CODEX_HOME": str(home)}
    states = _feature_states(_capture([codex_bin, "features", "list"], repo, env))
    wanted = tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("features", {})
    mismatched = sorted(name for name, on in wanted.items() if states.get(name) is not on)
    if mismatched:  # a misspelled or retired feature key leaves the feature at its default
        raise CodexError(f"Codex features not in their configured state: {mismatched}")
    render = [codex_bin, "debug", "prompt-input"]
    _capture(render, repo, env)  # the first start installs the bundled skills under skills/.system
    skills = sorted((home / "skills").rglob("SKILL.md"))
    with (home / "config.toml").open("a", encoding="utf-8") as config:
        for path in skills:
            config.write(f'\n[[skills.config]]\npath = {json.dumps(str(path))}\nenabled = false\n')
    rendered = _capture(render, repo, env)
    (run_dir / PROMPT_INPUT_FILE).write_text(rendered, encoding="utf-8")
    texts = _prompt_texts(rendered)
    joined = "\n".join(t for _, t in texts)
    present = [m for m in FORBIDDEN_PROMPT_MARKERS if m in joined]
    if present:
        raise CodexError(f"the Codex subagent prompt still carries {present}; see {run_dir / PROMPT_INPUT_FILE}")
    agreement = (codex_config / "AGENTS.md").read_text(encoding="utf-8").strip()
    if agreement not in joined:
        raise CodexError(f"the Codex subagent prompt lacks the working agreement; see {run_dir / PROMPT_INPUT_FILE}")
    if f"<cwd>{repo.resolve()}</cwd>" not in joined:
        raise CodexError(f"the Codex subagent prompt does not place it in {repo}; see {run_dir / PROMPT_INPUT_FILE}")
    if not USER_AUTH.is_file():
        raise CodexError(f"no Codex login at {USER_AUTH}; run `codex login`")
    target = home / AUTH_FILE
    shutil.copyfile(USER_AUTH, target)
    target.chmod(0o600)
    version = _capture([codex_bin, "--version"], repo, base_env).strip()
    enabled = sorted(name for name, on in states.items() if on)
    return {"codex_version": version, "codex_bin": codex_bin, "features_enabled": enabled,
            "skills_disabled": len(skills),
            "prompt_input_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "auth_sha256": hashlib.sha256(USER_AUTH.read_bytes()).hexdigest()}


def _last_refresh(path: Path) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("last_refresh") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def release_home(run_dir: Path, installed_sha256: str | None) -> dict[str, Any]:
    """Remove the run's copy of the Codex login. When Codex refreshed it during the run,
    write it back to ~/.codex/auth.json (under a lock, and only when its last_refresh is
    newer) so that the user's login stays valid after a token rotation."""
    target = home_for(run_dir) / AUTH_FILE
    if not target.is_file():
        return {"auth_present": False, "auth_refreshed": False, "auth_written_back": False}
    refreshed = hashlib.sha256(target.read_bytes()).hexdigest() != installed_sha256
    written_back = False
    if refreshed:
        with (USER_AUTH.parent / ".experiment-auth.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _last_refresh(target) > _last_refresh(USER_AUTH):
                tmp = USER_AUTH.with_name(USER_AUTH.name + ".experiment-tmp")
                shutil.copyfile(target, tmp)
                tmp.chmod(0o600)
                os.replace(tmp, USER_AUTH)
                written_back = True
    target.unlink()
    return {"auth_present": True, "auth_refreshed": refreshed, "auth_written_back": written_back}


# -- analysis -----------------------------------------------------------------------------------
def rollout_files(home: Path) -> tuple[Path, ...]:
    return tuple(sorted((home / "sessions").rglob("rollout-*.jsonl"))) if (home / "sessions").is_dir() else ()


def thread_id(path: Path) -> str:
    """The session id of a rollout file: its session_meta id, else the UUID ending its name."""
    for entry in iter_entries(path):
        payload = entry.get("payload")
        if entry.get("type") == "session_meta" and isinstance(payload, dict) and payload.get("id"):
            return str(payload["id"])
        break
    return "-".join(path.stem.rsplit("-", 5)[-5:])


def call_records(home: Path) -> list[dict[str, Any]]:
    """call.json of every codex-agent call, oldest first; a call without one is reported as unreadable."""
    calls: list[dict[str, Any]] = []
    root = home / CALLS_DIR
    for directory in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            record = json.loads((directory / "call.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            record = {"agent": None, "unreadable": str(exc)}
        record["call_id"] = directory.name
        calls.append(record)
    return calls


def _usage_int(usage: dict, key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


def load_requests(home: Path) -> tuple[cost.Request, ...]:
    """One subagent request per model response in the Codex session logs.

    Codex reports input_tokens inclusive of cached and cache-write tokens; the
    uncached remainder is billed at the input price, cached tokens at the cache-read
    price, and cache writes at the cache-write price (both TTL slots carry it)."""
    requests: list[cost.Request] = []
    seen: set[str] = set()
    for path in rollout_files(home):
        model = ""
        thread = thread_id(path)
        for entry in iter_entries(path):
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            if entry.get("type") == "turn_context":
                model = str(payload.get("model") or model)
                continue
            if entry.get("type") != "token_usage_record":
                continue
            usage = payload.get("usage")
            key = str(payload.get("response_id") or "")
            if not isinstance(usage, dict) or not key or key in seen:
                continue
            seen.add(key)
            total = _usage_int(usage, "input_tokens")
            cached = _usage_int(usage, "cached_input_tokens")
            written = _usage_int(usage, "cache_write_input_tokens")
            if cached + written > total:
                raise TranscriptError(f"{path}: response {key} reports more cached and written tokens than input")
            stamp = entry.get("timestamp")
            requests.append(cost.Request(
                message_id=key, model=model, source="subagent", main_index=None,
                timestamp=parse_timestamp(stamp) if isinstance(stamp, str) else 0.0,
                input_tokens=total - cached - written, output_tokens=_usage_int(usage, "output_tokens"),
                cache_read_tokens=cached, cache_write_5m_tokens=written, cache_write_1h_tokens=0,
                thread=f"codex-{thread}", ttl_split_known=False))
    return tuple(requests)


def summarize(home: Path) -> dict[str, Any]:
    """Calls by agent, tool use, compactions, models and efforts, and per-call timing."""
    calls = call_records(home)
    tools: Counter[str] = Counter()
    models: Counter[str] = Counter()
    efforts: Counter[str] = Counter()
    compactions = 0
    reasoning_tokens = 0
    rate_limit_used: list[float] = []
    threads: dict[str, dict[str, Any]] = {}
    for path in rollout_files(home):
        info = threads.setdefault(thread_id(path), {"requests": 0, "first_input_tokens": None, "max_input_tokens": 0})
        for entry in iter_entries(path):
            kind = entry.get("type")
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            if kind == "turn_context":
                models[str(payload.get("model"))] += 1
                efforts[str(payload.get("effort"))] += 1
            elif kind == "compacted" or (kind == "event_msg" and payload.get("type") == "context_compacted"):
                compactions += 1
            elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
                tools[str(payload.get("name"))] += 1
            elif kind == "token_usage_record" and isinstance(payload.get("usage"), dict):
                usage = payload["usage"]
                info["requests"] += 1
                tokens = _usage_int(usage, "input_tokens")
                if info["first_input_tokens"] is None:
                    info["first_input_tokens"] = tokens
                info["max_input_tokens"] = max(info["max_input_tokens"], tokens)
                reasoning_tokens += _usage_int(usage, "reasoning_output_tokens")
            elif kind == "event_msg" and payload.get("type") == "token_count":
                primary = ((payload.get("rate_limits") or {}).get("primary") or {})
                if isinstance(primary.get("used_percent"), (int, float)):
                    rate_limit_used.append(float(primary["used_percent"]))
    per_call = []
    for call in calls:
        start, end = call.get("started_epoch"), call.get("ended_epoch")
        thread = threads.get(str(call.get("thread_id")), {})
        per_call.append({"call_id": call["call_id"], "agent": call.get("agent"), "thread_id": call.get("thread_id"),
                         "started_epoch": start, "wall_s": round(end - start, 3) if start and end else None,
                         "exit_code": call.get("exit_code"), "failed": call.get("failed", end is None),
                         "usage_limit": bool(call.get("usage_limit")), "requests": thread.get("requests"),
                         "first_input_tokens": thread.get("first_input_tokens"),
                         "max_input_tokens": thread.get("max_input_tokens")})
    return {"calls": len(calls), "calls_by_agent": dict(sorted(Counter(str(c.get("agent")) for c in calls).items())),
            "tool_calls": dict(sorted(tools.items())), "compaction_events": compactions,
            "models": dict(models), "efforts": dict(efforts), "reasoning_output_tokens": reasoning_tokens,
            "rate_limit_used_percent_max": max(rate_limit_used) if rate_limit_used else None,
            "usage_limit": any(c["usage_limit"] for c in per_call), "per_call": per_call}


def spawn_calls(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """codex-agent calls in the shape of transcript.summarize_behavior's subagent_spawn_calls."""
    return [{"source": "main", "subagent_type": c["agent"], "model_arg": None, "observed_models": [],
             "timestamp": c["started_epoch"], "harness": "codex"} for c in summary["per_call"]]


def validate(home: Path, summary: dict[str, Any], model: str, effort: str) -> list[dict[str, Any]]:
    """Violations: a Codex turn on another model or effort, a failed or unfinished call, or a
    call whose session log is missing (reported, never acted on)."""
    found: Counter[tuple[str, str, str]] = Counter()
    for path in rollout_files(home):
        for entry in iter_entries(path):
            if entry.get("type") != "turn_context" or not isinstance(entry.get("payload"), dict):
                continue
            payload = entry["payload"]
            if payload.get("model") != model:
                found[("model", model, str(payload.get("model")))] += 1
            if payload.get("effort") != effort:
                found[("effort", effort, str(payload.get("effort")))] += 1
    logged = {thread_id(p) for p in rollout_files(home)}
    for call in summary["per_call"]:
        if call["failed"]:
            found[("codex_call_failed", "exit 0 with a report", f"{call['call_id']} exit {call['exit_code']}")] += 1
        elif call["thread_id"] not in logged:
            found[("codex_session_log_missing", "rollout file", str(call["call_id"]))] += 1
    return [{"kind": k[0], "source": "codex", "file": "-", "expected": k[1], "observed": k[2], "count": n}
            for k, n in sorted(found.items())]


def leak_scan_files(home: Path) -> list[Path]:
    """Files whose new lines the leak scan reads: session logs and the tasks handed to subagents."""
    tasks = sorted((home / CALLS_DIR).glob("*/task.txt")) if (home / CALLS_DIR).is_dir() else []
    return [*rollout_files(home), *tasks]
