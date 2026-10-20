#!/usr/bin/env python3
"""codex-agent: run one headless Codex subagent and print its final report.

Usage: codex-agent AGENT [TASK]   (the task is read from standard input when omitted or "-")

Installed by harness/arms.py as <config>/codex/bin/codex-agent of a Codex arm.
The roster and role prompts sit next to it (../agent.json, which gives each agent's model and effort, and ../roles/<agent>.md);
Codex state lives in $CODEX_HOME, which the harness prepares for each run. Each
call is recorded under $CODEX_HOME/agent-calls/<call id>/ (task, Codex event
stream, stderr, final message, and call.json) for the harness's analysis.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RUNTIME_FILE = "agent-runtime.json"  # in $CODEX_HOME, written by the harness
NESTING_ENV = "CODEX_AGENT_DEPTH"
# Variables of the Claude Code main agent that a Codex subagent has no use for.
DROPPED_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC")
USAGE_LIMIT_RE = re.compile(r"usage limit|rate limit|hit your\b[^.\n]*\blimit|\b429\b", re.IGNORECASE)
PR_SET_PDEATHSIG = 1
EXIT_USAGE = 2
EXIT_FAILED = 1


def fail(message: str, code: int = EXIT_FAILED) -> int:
    print(f"codex-agent: {message}", file=sys.stderr)
    return code


def toml_string(text: str) -> str:
    """A TOML basic string for a -c override."""
    return json.dumps(text)  # JSON string escapes are valid TOML basic-string escapes


def die_with_parent() -> None:
    """Child-side: receive SIGTERM if codex-agent itself is killed."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def scan_events(path: Path) -> tuple[str | None, list[str]]:
    """(thread id, error messages) from a Codex --json event stream."""
    thread_id: str | None = None
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, ["event stream missing"]
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = str(event.get("thread_id"))
        elif kind in ("error", "turn.failed"):
            detail = event.get("message") or (event.get("error") or {}).get("message") or json.dumps(event)
            errors.append(str(detail))
    return thread_id, errors


def main(argv: list[str]) -> int:
    if os.environ.get(NESTING_ENV):
        return fail("a subagent cannot start subagents", EXIT_USAGE)
    here = Path(__file__).resolve().parent.parent  # <config>/codex
    try:
        roster = json.loads((here / "agent.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"cannot read the agent roster {here / 'agent.json'}: {exc}")
    agents = roster["agents"]
    if not argv or argv[0] in ("-h", "--help") or len(argv) > 2:
        names = ", ".join(agents)
        return fail(f"usage: codex-agent AGENT [TASK]  (task on standard input when omitted); agents: {names}",
                    EXIT_USAGE)
    name = argv[0]
    if name not in agents:
        return fail(f"unknown agent {name!r}; agents: {', '.join(agents)}", EXIT_USAGE)
    task = sys.stdin.read() if len(argv) == 1 or argv[1] == "-" else argv[1]
    if not task.strip():
        return fail("the task is empty; pass it on standard input or as the second argument", EXIT_USAGE)
    home = os.environ.get("CODEX_HOME")
    if not home or not Path(home, "config.toml").is_file():
        return fail("CODEX_HOME is unset or holds no config.toml; this command runs only inside an experiment run")
    role = (here / "roles" / f"{name}.md").read_text(encoding="utf-8")
    try:
        codex_bin = json.loads(Path(home, RUNTIME_FILE).read_text(encoding="utf-8"))["codex_bin"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return fail(f"cannot read the pinned Codex binary from {Path(home, RUNTIME_FILE)}: {exc}")

    started = time.time()
    call_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}-{name}"
    call_dir = Path(home) / "agent-calls" / call_id
    call_dir.mkdir(parents=True)
    (call_dir / "task.txt").write_text(task, encoding="utf-8")
    last_message = call_dir / "last-message.txt"
    read_only = bool(agents[name]["read_only"])
    model, effort = str(agents[name]["model"]), str(agents[name]["effort"])
    cmd = [codex_bin, "exec", "--json", "--strict-config", "-C", os.getcwd(), "-o", str(last_message),
           "-c", f"model={toml_string(model)}", "-c", f"model_reasoning_effort={toml_string(effort)}",
           "-c", f"developer_instructions={toml_string(role)}"]
    if read_only:
        cmd += ["--sandbox", "read-only"]
    cmd.append("-")
    env = {k: v for k, v in os.environ.items() if not k.startswith(DROPPED_ENV_PREFIXES)}
    env[NESTING_ENV] = "1"
    record = {"agent": name, "model": model, "effort": effort, "read_only": read_only, "cwd": os.getcwd(),
              "started_epoch": started, "argv": cmd[:-1]}
    (call_dir / "call.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    with (call_dir / "events.jsonl").open("wb") as events, (call_dir / "stderr.txt").open("wb") as errors:
        try:
            child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=events, stderr=errors, env=env,
                                     preexec_fn=die_with_parent)
        except OSError as exc:
            return fail(f"cannot start {codex_bin}: {exc}")

        stopped: list[int] = []

        def forward(signum: int, _frame: object) -> None:
            stopped.append(signum)
            child.send_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, forward)
        assert child.stdin is not None
        try:
            child.stdin.write(task.encode("utf-8"))
            child.stdin.close()
        except BrokenPipeError:  # codex exited before reading its task; its stderr says why
            pass
        code = child.wait()

    thread_id, messages = scan_events(call_dir / "events.jsonl")
    report = last_message.read_text(encoding="utf-8") if last_message.is_file() else ""
    usage_limit = any(USAGE_LIMIT_RE.search(m) for m in messages)
    # Warnings about configuration arrive as error items; only a failed run counts as a failure.
    failed = code != 0 or not report.strip()
    record.update(ended_epoch=time.time(), exit_code=code, thread_id=thread_id, errors=messages,
                  usage_limit=usage_limit and failed, failed=failed,
                  stopped_by_signal=signal.Signals(stopped[0]).name if stopped else None)
    (call_dir / "call.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if failed:
        detail = "; ".join(messages[-3:]) or (call_dir / "stderr.txt").read_text(errors="replace")[-600:]
        prefix = "Codex usage limit reached" if usage_limit else f"the subagent failed (exit {code})"
        return fail(f"{prefix}: {detail.strip()}")
    sys.stdout.write(report if report.endswith("\n") else report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
