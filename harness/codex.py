"""Headless Codex sessions: per-run CODEX_HOME setup and check, and session-log analysis.

A Codex-subagent arm's Claude Code main agent starts subagents with the codex-agent
command (codex_agent.py); a Codex-main arm's main agent is itself a `codex exec` session
that run.py starts and resumes (main_command, main_round_result).
Every run gets its own CODEX_HOME under the run dir, built from the arm's codex/
config, so no Codex state (sessions, history, memories, caches, skills, login
refreshes) is shared with other runs or with the user's ~/.codex, which the run's
mount namespace hides. Usage, model, and effort are read from Codex's own session
logs (sessions/**/rollout-*.jsonl), one token_usage_record per model response.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import cost
from codex_agent import USAGE_LIMIT_RE, toml_string
from transcript import TranscriptError, parse_timestamp

HOME_DIR = "codex-home"          # under the run dir; CODEX_HOME while the agent runs
CALLS_DIR = "agent-calls"        # written by codex-agent
AUTH_FILE = "auth.json"
USER_AUTH = Path("~/.codex/auth.json").expanduser()
# The run's namespace hides ~/.codex; only the installed Codex releases (binaries, no
# user data) are bound back, read-only, since ~/.local/bin/codex links into them.
HOST_VISIBLE: tuple[Path, ...] = (Path("~/.codex/packages").expanduser(),)
RUNTIME_FILE = "agent-runtime.json"  # read by codex-agent: the Codex binary this run is pinned to
PROMPT_INPUT_FILE = "codex-prompt-input.json"
SUBAGENT_HOME_DIR = "subagents"  # legacy shell-launched children, for offline reanalysis
# Restricted prompt sections; built-in delegation arms allow the collaboration sections.
FORBIDDEN_PROMPT_MARKERS: tuple[str, ...] = ("<skills_instructions>", "<multi_agent_role>", "<multi_agent_mode>",
                                             "<apps_instructions>", "<plugins_instructions>")
SETUP_TIMEOUT_S = 120
OWNER_FILE = "owner.json"        # in CODEX_HOME: the run.py process holding this home's login copy
AUTH_LOCK = USER_AUTH.parent / ".experiment-auth.lock"
# Codex refreshes a ChatGPT login once it is this old (or once its access token expires). Two
# runs refreshing the same login at once would race on its single-use refresh token, so no run
# starts when the refresh point falls within RUN_MARGIN (well above the longest run so far, 2.3 h).
LOGIN_REFRESH_AGE = timedelta(days=8)
RUN_MARGIN = timedelta(hours=12)
# What `codex exec - </dev/null` prints once its configuration has loaded (no network involved).
NO_PROMPT_MARKER = "No prompt provided"


class CodexError(Exception):
    """A Codex home that cannot be prepared or fails its isolation check."""


def home_for(run_dir: Path) -> Path:
    return run_dir / HOME_DIR


def subagent_home_for(home: Path) -> Path:
    """The separate CODEX_HOME of archived shell-launched Codex children."""
    return home / SUBAGENT_HOME_DIR


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


def _proc_start(pid: int) -> str | None:
    """A process's start time in clock ticks since boot, or None when it no longer exists."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def _auth_fields(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _last_refresh(path: Path) -> str:
    return str(_auth_fields(path).get("last_refresh") or "")


def _jwt_exp(token: object) -> datetime | None:
    """The expiry of a JWT (decoded locally, never logged)."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    body = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return datetime.fromtimestamp(exp, timezone.utc) if isinstance(exp, (int, float)) else None


def login_refresh_point(path: Path | None = None) -> datetime | None:
    """When Codex will next refresh this login: LOGIN_REFRESH_AGE after its last refresh or
    its access token's expiry, whichever comes first (None when neither is readable)."""
    data = _auth_fields(USER_AUTH if path is None else path)
    points = []
    stamp = data.get("last_refresh")
    if isinstance(stamp, str) and stamp:
        try:
            points.append(datetime.fromisoformat(stamp[:26].rstrip("Z") + "+00:00") + LOGIN_REFRESH_AGE)
        except ValueError:
            pass
    exp = _jwt_exp((data.get("tokens") or {}).get("access_token") if isinstance(data.get("tokens"), dict) else None)
    if exp is not None:
        points.append(exp)
    return min(points) if points else None


def _write_back(copy: Path) -> bool:
    """Replace ~/.codex/auth.json with a run's copy when the copy was refreshed later. Caller holds AUTH_LOCK."""
    if _last_refresh(copy) <= _last_refresh(USER_AUTH):
        return False
    tmp = USER_AUTH.with_name(USER_AUTH.name + ".experiment-tmp")
    shutil.copyfile(copy, tmp)
    tmp.chmod(0o600)
    os.replace(tmp, USER_AUTH)
    return True


def sweep_stale_logins(runs_dir: Path) -> list[str]:
    """Login copies left by runs that died without cleanup (SIGKILL, power loss): write back
    a refreshed one and remove them all. Caller holds AUTH_LOCK. Returns the run dirs swept."""
    swept = []
    for copy in sorted(runs_dir.glob(f"*/{HOME_DIR}/{AUTH_FILE}")):
        owner = _auth_fields(copy.parent / OWNER_FILE)
        pid = owner.get("pid")
        if isinstance(pid, int) and _proc_start(pid) == owner.get("start"):
            continue  # a live run
        try:
            _write_back(copy)
        finally:
            copy.unlink(missing_ok=True)
        swept.append(str(copy.parent.parent))
    return swept


def _build_home(home: Path, source: Path, run_dir: Path, repo: Path, base_env: dict[str, str], codex_bin: str,
                prompt_file: str) -> dict[str, Any]:
    """Fill an existing CODEX_HOME from `source` (a dir with config.toml and AGENTS.md): pin the
    Codex binary, check the feature states, disable the bundled skills, copy role files, check
    the model-visible prompt (saved as `prompt_file` in the run dir), and validate the final config
    strictly. Returns the feature states and the prompt's provenance."""
    (home / RUNTIME_FILE).write_text(json.dumps({"codex_bin": codex_bin}, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(source / "config.toml", home / "config.toml")
    shutil.copyfile(source / "AGENTS.md", home / "AGENTS.md")
    if any((source / "roles").glob("*.toml")):
        shutil.copytree(source / "roles", home / "roles")
    env = {**base_env, "CODEX_HOME": str(home)}
    states = _feature_states(_capture([codex_bin, "features", "list"], repo, env))
    wanted = tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("features", {})
    expected = {name: value.get("enabled", True) if isinstance(value, dict) else value
                for name, value in wanted.items()}
    mismatched = sorted(name for name, on in expected.items() if states.get(name) is not on)
    if mismatched:  # a misspelled or retired feature key leaves the feature at its default
        raise CodexError(f"Codex features not in their configured state: {mismatched}")
    render = [codex_bin, "debug", "prompt-input"]
    _capture(render, repo, env)  # the first start installs the bundled skills under skills/.system
    skills = sorted((home / "skills").rglob("SKILL.md"))
    with (home / "config.toml").open("a", encoding="utf-8") as config:
        for path in skills:
            config.write(f'\n[[skills.config]]\npath = {json.dumps(str(path))}\nenabled = false\n')
    rendered = _capture(render, repo, env)
    (run_dir / prompt_file).write_text(rendered, encoding="utf-8")
    joined = "\n".join(t for _, t in _prompt_texts(rendered))
    allowed = {"<multi_agent_role>", "<multi_agent_mode>"} if expected.get("multi_agent") else set()
    present = [m for m in FORBIDDEN_PROMPT_MARKERS if m not in allowed and m in joined]
    if present:
        raise CodexError(f"the Codex prompt still carries {present}; see {run_dir / prompt_file}")
    agreement = (source / "AGENTS.md").read_text(encoding="utf-8").strip()
    if agreement not in joined:
        raise CodexError(f"the Codex prompt lacks the working agreement; see {run_dir / prompt_file}")
    if f"<cwd>{repo.resolve()}</cwd>" not in joined:
        raise CodexError(f"the Codex prompt does not place the agent in {repo}; see {run_dir / prompt_file}")
    check_strict_config(codex_bin, repo, env)
    return {"states": states, "skills": len(skills), "prompt_sha256": hashlib.sha256(rendered.encode()).hexdigest()}


def prepare_home(run_dir: Path, codex_config: Path, repo: Path, base_env: dict[str, str]) -> dict[str, Any]:
    """Create <run>/codex-home from the arm's codex/ dir, pin the Codex binary, check the
    feature states, disable Codex's bundled skills, check the model-visible prompt, validate
    the final config strictly, and install a copy of the user's Codex login.

    The prompt check renders exactly what a Codex agent's first request carries apart from
    its role (a subagent), run id (the main agent), and task, saves it as codex-prompt-input.json
    in the run dir, and fails unless the arm's AGENTS.md is present and no skills, app or plugin
    section is. Collaboration sections are allowed only for built-in arms. The login is copied
    last, and not at all when Codex would refresh it within RUN_MARGIN.

    Built-in role files are copied into the shared home. Returns provenance for run.json.
    """
    home = home_for(run_dir)
    home.mkdir()
    codex_bin = str(resolve_codex_bin())
    main = _build_home(home, codex_config, run_dir, repo, base_env, codex_bin, PROMPT_INPUT_FILE)
    states = main["states"]
    version = _capture([codex_bin, "--version"], repo, base_env).strip()
    if not USER_AUTH.is_file():
        raise CodexError(f"no Codex login at {USER_AUTH}; run `codex login`")
    target = home / AUTH_FILE
    with AUTH_LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        swept = sweep_stale_logins(run_dir.parent)
        point = login_refresh_point()
        if point is None or point <= datetime.now(timezone.utc) + RUN_MARGIN:
            raise CodexError(
                f"the Codex login in {USER_AUTH} is due for a refresh at {point.isoformat() if point else 'an unknown time'}, "
                f"within the {RUN_MARGIN} a run may take; parallel runs would race on its single-use refresh "
                "token. Let one Codex session refresh it while no Codex run is live, then relaunch")
        (home / OWNER_FILE).write_text(json.dumps({"pid": os.getpid(), "start": _proc_start(os.getpid())}) + "\n",
                                       encoding="utf-8")
        shutil.copyfile(USER_AUTH, target)
        target.chmod(0o600)
        installed = hashlib.sha256(target.read_bytes()).hexdigest()
    enabled = sorted(name for name, on in states.items() if on)
    info = {"codex_version": version, "codex_bin": codex_bin, "features_enabled": enabled,
            "skills_disabled": main["skills"], "prompt_input_sha256": main["prompt_sha256"],
            "auth_sha256": installed, "login_refresh_point": point.isoformat(), "stale_logins_swept": swept}
    return info


def check_strict_config(codex_bin: str, repo: Path, env: dict[str, str]) -> None:
    """Load the final config the way every codex-agent call does (--strict-config), with an
    empty prompt so that Codex stops right after loading it, before any network use."""
    try:
        done = subprocess.run([codex_bin, "exec", "--strict-config", "-C", str(repo), "-"], cwd=repo, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=SETUP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexError(f"codex exec --strict-config failed: {exc}") from exc
    output = done.stdout + done.stderr
    if NO_PROMPT_MARKER not in output:
        raise CodexError(f"codex exec --strict-config rejected the run's config: {output.strip()[-600:]}")


def release_home(run_dir: Path, installed_sha256: str | None) -> dict[str, Any]:
    """Remove the run's copy of the Codex login. When Codex refreshed it during the run,
    write it back to ~/.codex/auth.json (under a lock, and only when its last_refresh is
    newer) so that the user's login stays valid after a token rotation. The copy is removed
    even when the write-back fails."""
    target = home_for(run_dir) / AUTH_FILE
    if not target.is_file():
        return {"auth_present": False, "auth_refreshed": False, "auth_written_back": False}
    written_back = False
    try:
        refreshed = hashlib.sha256(target.read_bytes()).hexdigest() != installed_sha256
        if refreshed:
            with AUTH_LOCK.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                written_back = _write_back(target)
    finally:
        target.unlink(missing_ok=True)
    return {"auth_present": True, "auth_refreshed": refreshed, "auth_written_back": written_back}


# -- analysis -----------------------------------------------------------------------------------
def rollout_files(home: Path) -> tuple[Path, ...]:
    return tuple(sorted((home / "sessions").rglob("rollout-*.jsonl"))) if (home / "sessions").is_dir() else ()


def _rollout_entries(path: Path) -> Iterator[tuple[dict, bool]]:
    """(entry, False) per JSON line of a rollout, then (_, True) once if its last line is cut off
    (a Codex process killed mid-write). Invalid JSON anywhere else is an error."""
    try:
        # JSON Lines end at "\n" only: splitlines() would also break inside strings at U+2028 and the like.
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise TranscriptError(f"cannot read Codex session log {path}: {exc}") from exc
    while lines and not lines[-1].strip():
        lines.pop()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            if number == len(lines):
                yield {}, True
                return
            raise TranscriptError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if isinstance(entry, dict):
            yield entry, False


@dataclass
class Rollout:
    """What one Codex session log records."""
    path: Path
    thread: str = ""
    parent_thread: str | None = None
    role: str | None = None
    started_epoch: float = 0.0
    completed: bool = False
    errors: list[str] = field(default_factory=list)
    usage_limit: bool = False
    usage: list[tuple[str, str, float, dict]] = field(default_factory=list)  # response id, model, epoch, usage
    unkeyed_usage: int = 0           # token_usage_record entries without a response id (not priced)
    turn_contexts: list[tuple[str, str]] = field(default_factory=list)  # model, effort
    tools: Counter = field(default_factory=Counter)
    compacted_items: int = 0
    compacted_events: int = 0
    rate_limit_used: list[float] = field(default_factory=list)
    truncated: bool = False


def read_rollout(path: Path) -> Rollout:
    out = Rollout(path)
    model = ""
    for entry, cut in _rollout_entries(path):
        if cut:
            out.truncated = True
            break
        kind = entry.get("type")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if kind == "session_meta" and not out.thread and payload.get("id"):
            out.thread = str(payload["id"])
            source = payload.get("source")
            subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
            spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
            if not isinstance(spawn, dict):
                spawn = {}
            out.parent_thread = payload.get("parent_thread_id") or spawn.get("parent_thread_id")
            out.role = payload.get("agent_role") or spawn.get("agent_role")
            stamp = entry.get("timestamp") or payload.get("timestamp")
            out.started_epoch = parse_timestamp(stamp) if isinstance(stamp, str) else 0.0
        elif kind == "turn_context":
            out.completed = False
            model = str(payload.get("model") or model)
            out.turn_contexts.append((str(payload.get("model")), str(payload.get("effort"))))
        elif kind == "compacted":
            out.compacted_items += 1
        elif kind == "event_msg" and payload.get("type") == "context_compacted":
            out.compacted_events += 1
        elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            out.tools[str(payload.get("name"))] += 1
        elif kind == "response_item" and payload.get("type") == "web_search_call":
            out.tools["web_search"] += 1
        elif kind == "token_usage_record" and isinstance(payload.get("usage"), dict):
            key = str(payload.get("response_id") or "")
            if not key:
                out.unkeyed_usage += 1
                continue
            stamp = entry.get("timestamp")
            out.usage.append((key, model, parse_timestamp(stamp) if isinstance(stamp, str) else 0.0,
                              payload["usage"]))
        elif kind == "event_msg" and payload.get("type") == "token_count":
            primary = ((payload.get("rate_limits") or {}).get("primary") or {})
            if isinstance(primary.get("used_percent"), (int, float)):
                out.rate_limit_used.append(float(primary["used_percent"]))
        elif kind == "event_msg" and payload.get("type") in ("task_started", "turn_started"):
            out.completed = False
        elif kind == "event_msg" and payload.get("type") in ("task_complete", "turn_complete", "turn_completed"):
            out.completed = True
        elif kind in ("error", "turn.failed") or (kind == "event_msg" and payload.get("type") in
                                                   ("error", "turn_failed", "turn_aborted")):
            out.errors.append(str(payload.get("message") or payload.get("error") or payload or entry))
            detail = json.dumps(entry)
            out.usage_limit |= bool(USAGE_LIMIT_RE.search(detail)) or "usage_limit_reached" in detail
    if not out.thread:  # else the UUID ending the file name
        out.thread = "-".join(path.stem.rsplit("-", 5)[-5:])
    return out


def read_rollouts(home: Path) -> tuple[Rollout, ...]:
    """Every session log, each (thread, response) counted once in the first log recording it."""
    seen: set[tuple[str, str]] = set()
    rollouts = []
    for path in rollout_files(home):
        rollout = read_rollout(path)
        kept = []
        for usage in rollout.usage:
            key = (rollout.thread, usage[0])
            if key not in seen:
                seen.add(key)
                kept.append(usage)
        rollout.usage = kept
        rollouts.append(rollout)
    return tuple(rollouts)


def split_threads(rollouts: Sequence[Rollout], root_threads: Sequence[str]) -> tuple[tuple[Rollout, ...],
                                                                                 tuple[Rollout, ...],
                                                                                 tuple[Rollout, ...]]:
    """Main threads, descendants, and unrelated/orphan/cyclic threads, using recorded parent links."""
    roots = set(root_threads)
    parents = {r.thread: r.parent_thread for r in rollouts}
    descendants: set[str] = set()
    for thread in parents.keys() - roots:
        seen: set[str] = set()
        current: str | None = thread
        while current in parents and current not in seen and current not in roots:
            seen.add(current)
            current = parents[current]
        if current in roots:
            descendants.add(thread)
    return (tuple(r for r in rollouts if r.thread in roots),
            tuple(r for r in rollouts if r.thread in descendants),
            tuple(r for r in rollouts if r.thread not in roots | descendants))


def thread_id(path: Path) -> str:
    """The session id of a rollout file: its session_meta id, else the UUID ending its name."""
    return read_rollout(path).thread


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


def load_requests(home: Path, source: str = "subagent", *,
                  root_threads: Sequence[str] | None = None) -> tuple[cost.Request, ...]:
    """One request per model response in the Codex session logs: a subagent's by default,
    or, for a Codex-main arm (source "main"), the main agent's, numbered in log order.
    With root_threads, main selects those roots and subagent selects their descendants.

    Codex reports input_tokens inclusive of cached and cache-write tokens; the
    uncached remainder is billed at the input price, cached tokens at the cache-read
    price, and cache writes at the cache-write price (both TTL slots carry it)."""
    assert source in cost.SOURCES
    requests: list[cost.Request] = []
    rollouts = read_rollouts(home)
    if root_threads is not None:
        main, children, _ = split_threads(rollouts, root_threads)
        rollouts = main if source == "main" else children
    for rollout in rollouts:
        for key, model, stamp, usage in rollout.usage:
            total = _usage_int(usage, "input_tokens")
            cached = _usage_int(usage, "cached_input_tokens")
            written = _usage_int(usage, "cache_write_input_tokens")
            if cached + written > total:
                raise TranscriptError(f"{rollout.path}: response {key} reports more cached and written tokens than input")
            requests.append(cost.Request(
                message_id=key, model=model, source=source,
                main_index=len(requests) if source == "main" else None, timestamp=stamp,
                input_tokens=total - cached - written, output_tokens=_usage_int(usage, "output_tokens"),
                cache_read_tokens=cached, cache_write_5m_tokens=written, cache_write_1h_tokens=0,
                thread=f"codex-{rollout.thread}", ttl_split_known=False))
    return tuple(requests)


def call_failed(call: dict[str, Any]) -> bool:
    """A call that ended without a report, other than one stopped by a signal (the main agent
    or the CLI stopping it). Calls with no end record are counted as unfinished instead."""
    return call.get("ended_epoch") is not None and bool(call.get("failed")) and not call.get("stopped_by_signal")


def summarize(home: Path, *, rollouts: Sequence[Rollout] | None = None) -> dict[str, Any]:
    """Calls by agent, tool use, compactions, models and efforts, and per-call timing."""
    calls = call_records(home)
    rollouts = read_rollouts(home) if rollouts is None else rollouts
    tools: Counter[str] = Counter()
    models: Counter[str] = Counter()
    efforts: Counter[str] = Counter()
    threads: dict[str, dict[str, Any]] = {}
    for rollout in rollouts:
        tools.update(rollout.tools)
        models.update(m for m, _ in rollout.turn_contexts)
        efforts.update(e for _, e in rollout.turn_contexts)
        inputs = [_usage_int(u, "input_tokens") for _, _, _, u in rollout.usage]
        threads[rollout.thread] = {"requests": len(inputs), "first_input_tokens": inputs[0] if inputs else None,
                                   "max_input_tokens": max(inputs, default=0)}
    rate_limit_used = [v for r in rollouts for v in r.rate_limit_used]
    per_call = []
    for call in calls:
        start, end = call.get("started_epoch"), call.get("ended_epoch")
        thread = threads.get(str(call.get("thread_id")), {})
        per_call.append({"call_id": call["call_id"], "agent": call.get("agent"), "thread_id": call.get("thread_id"),
                         "started_epoch": start, "wall_s": round(end - start, 3) if start and end else None,
                         "exit_code": call.get("exit_code"), "failed": call_failed(call),
                         "unfinished": end is None, "stopped_by_signal": call.get("stopped_by_signal"),
                         "usage_limit": bool(call.get("usage_limit")), "errors": call.get("errors") or [],
                         "requests": thread.get("requests"), "first_input_tokens": thread.get("first_input_tokens"),
                         "max_input_tokens": thread.get("max_input_tokens")})
    return {"calls": len(calls), "calls_by_agent": dict(sorted(Counter(str(c.get("agent")) for c in calls).items())),
            "tool_calls": dict(sorted(tools.items())),
            # Codex may log one compaction both as an item and as an event; count the larger.
            "compaction_events": sum(max(r.compacted_items, r.compacted_events) for r in rollouts),
            "models": dict(models), "efforts": dict(efforts),
            "reasoning_output_tokens": sum(_usage_int(u, "reasoning_output_tokens")
                                           for r in rollouts for _, _, _, u in r.usage),
            "rate_limit_used_percent_max": max(rate_limit_used) if rate_limit_used else None,
            "usage_limit": any(c["usage_limit"] for c in per_call),
            "unpriced_usage_records": sum(r.unkeyed_usage for r in rollouts),
            "truncated_logs": [str(r.path.name) for r in rollouts if r.truncated], "per_call": per_call}


def run_outcome(summary: dict[str, Any]) -> tuple[str, str] | None:
    """(status, error) when the run's Codex subagents make it invalid: a Codex usage limit
    ("usage_limit"), or a call that failed ("codex_error"); None otherwise. Such a run measured
    the main agent working around a missing subagent, so it is rerun rather than reported."""
    def detail(call: dict[str, Any]) -> str:
        errors = "; ".join(str(e) for e in call["errors"][-2:])
        return f"{call['call_id']} (exit {call['exit_code']}){': ' + errors if errors else ''}"
    limited = [c for c in summary["per_call"] if c["usage_limit"]]
    if limited:
        return "usage_limit", "Codex usage limit reached in a subagent call: " + detail(limited[0])
    failed = [c for c in summary["per_call"] if c["failed"]]
    if failed:
        return "codex_error", f"{len(failed)} Codex subagent call(s) failed: " + "; ".join(map(detail, failed[:3]))
    return None


def summarize_children(home: Path, children: Sequence[Rollout]) -> dict[str, Any]:
    """Built-in spawns and failures from each descendant's own persisted rollout."""
    summary = summarize(home, rollouts=children)
    by_thread: dict[str, list[Rollout]] = {}
    for child in children:
        by_thread.setdefault(child.thread, []).append(child)
    calls = []
    for thread, logs in by_thread.items():
        errors = [error for log in logs for error in log.errors]
        unfinished = not logs[-1].completed or logs[-1].truncated
        calls.append({"call_id": thread, "thread_id": thread, "agent": logs[0].role,
                      "parent_thread_id": logs[0].parent_thread, "started_epoch": logs[0].started_epoch,
                      "exit_code": None, "failed": bool(errors) or unfinished, "unfinished": unfinished,
                      "errors": errors, "usage_limit": any(log.usage_limit for log in logs),
                      "observed_models": sorted({model for log in logs for model, _ in log.turn_contexts})})
    summary.update(calls=len(calls), per_call=calls,
                   calls_by_agent=dict(sorted(Counter(str(c["agent"]) for c in calls).items())),
                   usage_limit=any(c["usage_limit"] for c in calls))
    return summary


def validate_children(children: Sequence[Rollout], agent_models: Mapping[str, str],
                      effort: str) -> list[dict[str, Any]]:
    """Check every built-in child's role, model, effort, tools, and completed rollout."""
    violations = []
    for child in children:
        found: Counter[tuple[str, str, str]] = Counter()
        expected = agent_models.get(child.role or "")
        if expected is None:
            found[("codex_unknown_role", " or ".join(sorted(agent_models)), str(child.role))] += 1
        for model, got_effort in child.turn_contexts:
            if expected is None or model != expected:
                found[("model", expected or "a configured role's model", model)] += 1
            if got_effort != effort:
                found[("effort", effort, got_effort)] += 1
        for tool, count in child.tools.items():
            if tool.rsplit(".", 1)[-1] in FORBIDDEN_CHILD_TOOLS:
                found[("forbidden_tool", "not used", tool)] += count
        if child.errors:
            found[("codex_call_failed", "no error events", "; ".join(child.errors))] += 1
        if not child.completed or child.truncated:
            found[("codex_call_unfinished", "a completed rollout", child.thread)] += 1
        violations.extend({"kind": k[0], "source": "subagent", "file": child.path.name,
                           "expected": k[1], "observed": k[2], "count": n} for k, n in sorted(found.items()))
    return violations


def spawn_calls(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Codex spawns in the shape of transcript.summarize_behavior's subagent_spawn_calls."""
    return [{"source": "main", "subagent_type": c["agent"], "model_arg": None,
             "observed_models": c.get("observed_models", []),
             "timestamp": c["started_epoch"], "harness": "codex"} for c in summary["per_call"]]


def validate(home: Path, summary: dict[str, Any], agent_models: Mapping[str, str],
             effort: str) -> list[dict[str, Any]]:
    """Violations: a Codex turn on another model or effort, a failed or unfinished call, a call
    whose session log is missing, or a session no codex-agent call started.
    A turn must run on the model of the agent whose call started its session; a session
    no call claimed may run on any of the arm's agent models."""
    assert agent_models
    found: Counter[tuple[str, str, str]] = Counter()
    rollouts = read_rollouts(home)
    agent_of_thread = {str(c["thread_id"]): str(c["agent"]) for c in summary["per_call"]}
    arm_models = sorted(set(agent_models.values()))
    for rollout in rollouts:
        agent = agent_of_thread.get(rollout.thread)
        allowed = [agent_models[agent]] if agent in agent_models else arm_models
        expected = " or ".join(allowed)
        for got_model, got_effort in rollout.turn_contexts:
            if got_model not in allowed:
                found[("model", expected, got_model)] += 1
            if got_effort != effort:
                found[("effort", effort, got_effort)] += 1
    logged = {r.thread for r in rollouts}
    for call in summary["per_call"]:
        if call["failed"]:
            found[("codex_call_failed", "exit 0 with a report", f"{call['call_id']} exit {call['exit_code']}")] += 1
        elif call["unfinished"]:
            found[("codex_call_unfinished", "an end record", str(call["call_id"]))] += 1
        elif call["thread_id"] not in logged:
            found[("codex_session_log_missing", "rollout file", str(call["call_id"]))] += 1
    claimed = {str(c["thread_id"]) for c in summary["per_call"]}
    for thread in sorted(logged - claimed):
        found[("codex_session_without_call", "a codex-agent call", thread)] += 1
    return [{"kind": k[0], "source": "codex", "file": "-", "expected": k[1], "observed": k[2], "count": n}
            for k, n in sorted(found.items())]


def leak_scan_files(home: Path) -> list[Path]:
    """Files whose new lines the leak scan reads: session logs and the tasks handed to subagents."""
    tasks = sorted((home / CALLS_DIR).glob("*/task.txt")) if (home / CALLS_DIR).is_dir() else []
    return [*rollout_files(home), *tasks]


# -- Codex as the main agent -----------------------------------------------------------------------
# Solo main agents and children cannot use collaboration tools or web search.
FORBIDDEN_MAIN_TOOLS: frozenset[str] = frozenset({
    "spawn_agent", "send_input", "send_message", "followup_task", "wait_agent", "close_agent", "resume_agent",
    "interrupt_agent", "list_agents", "web_search"})
# Built-in children keep the collaboration tools for reporting and waiting, but cannot start or steer agents.
FORBIDDEN_CHILD_TOOLS: frozenset[str] = FORBIDDEN_MAIN_TOOLS - {"send_message", "list_agents", "wait_agent"}
USAGE_KEYS: tuple[str, ...] = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def main_command(codex_bin: str, model: str, effort: str, developer_instructions: str, last_message: Path,
                 repo: Path, thread_id: str | None) -> list[str]:
    """argv of one round of a Codex main agent, the prompt or feedback arriving on standard input.

    The first round is `codex exec`; later rounds are `codex exec resume <thread id>`, which has no
    -C and so runs in the working directory the caller sets (the repo)."""
    assert last_message.is_absolute() and repo.is_absolute()
    cmd = [codex_bin, "exec"]
    if thread_id is not None:
        cmd.append("resume")
    cmd += ["--json", "--strict-config"]
    if thread_id is None:
        cmd += ["-C", str(repo)]
    cmd += ["-o", str(last_message), "-c", f"model={toml_string(model)}",
            "-c", f"model_reasoning_effort={toml_string(effort)}",
            "-c", f"developer_instructions={toml_string(developer_instructions)}"]
    if thread_id is not None:
        cmd.append(thread_id)
    cmd.append("-")
    return cmd


def parse_exec_events(stdout: str) -> dict[str, Any]:
    """From a `codex exec --json` stream: the thread id, the token usage of the last
    turn.completed event (which Codex reports cumulatively for the whole thread, resumed
    rounds included), how many turns completed or failed, and the error messages.
    Lines that are not JSON objects are ignored."""
    thread: str | None = None
    usage: dict[str, int] | None = None
    completed = failed = 0
    errors: list[str] = []
    for line in stdout.split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            thread = str(event["thread_id"])
        elif kind == "turn.completed":
            completed += 1
            if isinstance(event.get("usage"), dict):
                usage = {key: _usage_int(event["usage"], key) for key in USAGE_KEYS}
        elif kind in ("error", "turn.failed"):
            if kind == "turn.failed":
                failed += 1
            detail = event.get("message") or (event.get("error") or {}).get("message") or json.dumps(event)
            errors.append(str(detail))
    return {"thread_id": thread, "usage": usage, "turns_completed": completed, "turns_failed": failed,
            "errors": errors}


def main_round_result(stdout: str, stderr: str, exit_code: int, last_message: Path) -> dict[str, Any]:
    """The record of one Codex main-agent round, shaped like the fields run.py reads from a
    Claude result: session_id (the thread id, None when the stream never started one), is_error,
    result (the final message, or the failure detail of a failed round), _exit_code, and
    _usage_limit, plus usage, turns_completed, and errors.

    Success requires a completed turn and a readable, nonempty final message. Error events
    alone are warnings. A failed round hit the usage limit when its errors or stderr say so."""
    events = parse_exec_events(stdout)
    detail = "; ".join(events["errors"][-3:]) or stderr.strip()[-600:]
    output_error = ""
    try:
        final = last_message.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        final = ""
        output_error = f"cannot read Codex final message {last_message}: {exc}"
    if not events["turns_completed"]:
        detail = detail or "Codex stream ended without a completed turn"
    if not final.strip():
        detail = detail or output_error or f"Codex final message {last_message} is empty"
    failed = exit_code != 0 or events["turns_failed"] > 0 or not events["turns_completed"] or not final.strip()
    if failed and not detail:
        detail = f"Codex failed (exit {exit_code})"
    limited = failed and bool(USAGE_LIMIT_RE.search("\n".join(events["errors"]) + "\n" + stderr))
    return {"session_id": events["thread_id"], "usage": events["usage"],
            "turns_completed": events["turns_completed"], "errors": events["errors"],
            "is_error": failed, "result": detail if failed else final, "_exit_code": exit_code,
            "_usage_limit": limited}


def validate_main(summary: dict[str, Any], rollouts: Sequence[Rollout], model: str, effort: str,
                  thread_ids: Sequence[str], *, builtin_subagents: bool = False) -> list[dict[str, Any]]:
    """Violations for a Codex main agent: a turn on another model or
    effort, a forbidden tool, a recorded thread without a session log, or a session log of a thread
    the rounds did not report."""
    found: Counter[tuple[str, str, str]] = Counter()
    for rollout in rollouts:
        for got_model, got_effort in rollout.turn_contexts:
            if got_model != model:
                found[("model", model, got_model)] += 1
            if got_effort != effort:
                found[("effort", effort, got_effort)] += 1
    forbidden = {"web_search"} if builtin_subagents else FORBIDDEN_MAIN_TOOLS
    for tool, count in summary["tool_calls"].items():
        if tool.rsplit(".", 1)[-1] in forbidden:
            found[("forbidden_tool", "not used", tool)] += count
    logged = {r.thread for r in rollouts}
    for thread in sorted(set(thread_ids) - logged):
        found[("codex_session_log_missing", "rollout file", thread)] += 1
    for thread in sorted(logged - set(thread_ids)):
        found[("codex_unexpected_session", "a thread a round reported", thread)] += 1
    return [{"kind": k[0], "source": "main", "file": "-", "expected": k[1], "observed": k[2], "count": n}
            for k, n in sorted(found.items())]


# -- login refresh ---------------------------------------------------------------------------------
REFRESH_PROMPT = "Reply with the single word OK."
FORCED_LAST_REFRESH = "2000-01-01T00:00:00Z"  # an age past LOGIN_REFRESH_AGE, so Codex refreshes on start


def live_login_copies(runs_dir: Path) -> list[str]:
    """Run dirs whose live run.py holds a copy of the login. Caller holds AUTH_LOCK."""
    sweep_stale_logins(runs_dir)
    return [str(copy.parent.parent) for copy in sorted(runs_dir.glob(f"*/{HOME_DIR}/{AUTH_FILE}"))]


def refresh_login(runs_dir: Path, codex_config: Path) -> dict[str, Any]:
    """Refresh the user's Codex login once, in a scratch CODEX_HOME, while no run holds a copy.

    Before its refresh point the scratch copy's last_refresh is backdated so that Codex
    refreshes it on start; one minimal request follows. The refreshed login is written back
    to ~/.codex/auth.json only when Codex really refreshed it. AUTH_LOCK is held throughout,
    so no run copies the login meanwhile. Returns the outcome (never token contents)."""
    with AUTH_LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        live = live_login_copies(runs_dir)
        if live:
            raise CodexError(f"{len(live)} live Codex run(s) hold a copy of the login: {live}")
        before = login_refresh_point()
        original = _last_refresh(USER_AUTH)
        scratch = Path(tempfile.mkdtemp(prefix=".codex-refresh-", dir=runs_dir))
        try:
            shutil.copyfile(codex_config / "config.toml", scratch / "config.toml")
            data = _auth_fields(USER_AUTH)
            if not data:
                raise CodexError(f"no readable Codex login at {USER_AUTH}; run `codex login`")
            forced = before is not None and before > datetime.now(timezone.utc)
            if forced:
                data = {**data, "last_refresh": FORCED_LAST_REFRESH}
            copy = scratch / AUTH_FILE
            copy.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            copy.chmod(0o600)
            (scratch / "work").mkdir()
            try:
                done = subprocess.run(
                    [str(resolve_codex_bin()), "exec", "--skip-git-repo-check", "-C", str(scratch / "work"),
                     "-c", 'model_reasoning_effort="low"', "-"], input=REFRESH_PROMPT, text=True,
                    capture_output=True, env={**os.environ, "CODEX_HOME": str(scratch)}, timeout=SETUP_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CodexError(f"the refresh request failed: {exc}") from exc
            refreshed = _last_refresh(copy) > max(original, FORCED_LAST_REFRESH if forced else "")
            written_back = refreshed and _write_back(copy)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return {"forced": forced, "request_exit": done.returncode, "refreshed": refreshed,
                "written_back": written_back, "refresh_point_before": before.isoformat() if before else None,
                "refresh_point_after": (lambda p: p.isoformat() if p else None)(login_refresh_point()),
                "detail": "" if refreshed else (done.stderr or done.stdout).strip()[-400:]}


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Codex login maintenance for the experiment runs.")
    parser.add_argument("command", choices=["refresh-point", "refresh-login"])
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--codex-config", type=Path, help="an arm's codex/ dir (refresh-login)")
    args = parser.parse_args()
    if args.command == "refresh-point":
        point = login_refresh_point()
        print(json.dumps({"refresh_point": point.isoformat() if point else None,
                          "starts_blocked_from": (point - RUN_MARGIN).isoformat() if point else None}))
        sys.exit(0)
    if args.codex_config is None:
        parser.error("refresh-login needs --codex-config")
    try:
        outcome = refresh_login(args.runs_dir, args.codex_config)
    except CodexError as exc:
        print(json.dumps({"refreshed": False, "error": str(exc)}))
        sys.exit(1)
    print(json.dumps(outcome))
    sys.exit(0 if outcome["refreshed"] else 1)
