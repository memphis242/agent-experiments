#!/usr/bin/env python3
"""claude-agent: run one headless Claude Code subagent and print its final report.

Usage: claude-agent AGENT [TASK]   (the task is read from standard input when omitted or "-")

Installed by harness/arms.py as <config>/claude/bin/claude-agent of an arm whose Codex main
agent has Claude Code subagents. Next to it sit the roster (../agent.json, which gives each
agent's model, effort, and tools), the role prompts (../roles/<agent>.md), and the Claude Code
configuration every call runs under (../settings.json and ../CLAUDE.md, the working agreement):
the claude/ directory itself is the calls' CLAUDE_CONFIG_DIR, so their transcripts land in
../projects/. Each call is recorded under ../agent-calls/<call id>/ (task, JSON result, stderr,
and call.json) for the harness's analysis.

The Claude login reaches the call through the file named by CLAUDE_AGENT_CREDENTIALS_FILE, read
here and handed only to the claude child process; this command's environment otherwise carries no
secret, and Codex's shell tool withholds variables named like secrets from the commands it runs.
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

RUNTIME_FILE = "agent-runtime.json"  # in the claude/ dir, written by the harness
CREDENTIALS_ENV = "CLAUDE_AGENT_CREDENTIALS_FILE"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# Shared with codex-agent: neither command starts inside a subagent of the other.
NESTING_ENV = "CODEX_AGENT_DEPTH"
# The child gets only these variables, plus its configuration directory and login.
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL")
USAGE_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|hit your\b[^.\n]*\blimit|\b429\b", re.IGNORECASE)
PR_SET_PDEATHSIG = 1
EXIT_USAGE = 2
EXIT_FAILED = 1


def fail(message: str, code: int = EXIT_FAILED) -> int:
    print(f"claude-agent: {message}", file=sys.stderr)
    return code


def die_with_parent() -> None:
    """Child-side: receive SIGTERM if claude-agent itself is killed."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def parse_result(path: Path) -> dict:
    """The JSON result object of `claude -p --output-format json`; empty when absent or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, list):  # tolerate the array form by taking its last result entry
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


def main(argv: list[str]) -> int:
    if os.environ.get(NESTING_ENV):
        return fail("a subagent cannot start subagents", EXIT_USAGE)
    here = Path(__file__).resolve().parent.parent  # <config>/claude, the calls' CLAUDE_CONFIG_DIR
    try:
        roster = json.loads((here / "agent.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"cannot read the agent roster {here / 'agent.json'}: {exc}")
    agents = roster["agents"]
    if not argv or argv[0] in ("-h", "--help") or len(argv) > 2:
        names = ", ".join(agents)
        return fail(f"usage: claude-agent AGENT [TASK]  (task on standard input when omitted); agents: {names}",
                    EXIT_USAGE)
    name = argv[0]
    if name not in agents:
        return fail(f"unknown agent {name!r}; agents: {', '.join(agents)}", EXIT_USAGE)
    task = sys.stdin.read() if len(argv) == 1 or argv[1] == "-" else argv[1]
    if not task.strip():
        return fail("the task is empty; pass it on standard input or as the second argument", EXIT_USAGE)
    credentials = os.environ.get(CREDENTIALS_ENV)
    if not credentials:
        return fail(f"{CREDENTIALS_ENV} is unset; this command runs only inside an experiment run")
    try:
        token = Path(credentials).read_text(encoding="utf-8").strip()
        claude_bin = json.loads((here / RUNTIME_FILE).read_text(encoding="utf-8"))["claude_bin"]
        role = (here / "roles" / f"{name}.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return fail(f"cannot read the login {credentials}, the pinned Claude binary {here / RUNTIME_FILE}, "
                    f"or the role prompt: {exc}")
    if not token:
        return fail(f"the login file {credentials} is empty")

    started = time.time()
    call_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}-{name}"
    call_dir = here / "agent-calls" / call_id
    call_dir.mkdir(parents=True)
    (call_dir / "task.txt").write_text(task, encoding="utf-8")
    model, effort, tools = str(agents[name]["model"]), str(agents[name]["effort"]), list(agents[name]["tools"])
    cmd = [claude_bin, "-p", "--model", model, "--permission-mode", "bypassPermissions",
           "--permission-prompts", "none", "--output-format", "json", "--append-system-prompt", role,
           "--disallowedTools", "Agent", "--tools", ",".join(tools)]
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env.update({"CLAUDE_CONFIG_DIR": str(here), TOKEN_ENV: token, NESTING_ENV: "1", "DISABLE_AUTOUPDATER": "1"})
    record = {"agent": name, "model": model, "effort": effort, "tools": tools, "cwd": os.getcwd(),
              "started_epoch": started, "argv": cmd}
    (call_dir / "call.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    with (call_dir / "stdout.json").open("wb") as out, (call_dir / "stderr.txt").open("wb") as errors:
        try:
            child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=errors, env=env,
                                     preexec_fn=die_with_parent)
        except OSError as exc:
            return fail(f"cannot start {claude_bin}: {exc}")

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
        except BrokenPipeError:  # claude exited before reading its task; its stderr says why
            pass
        code = child.wait()

    result = parse_result(call_dir / "stdout.json")
    report = str(result.get("result") or "")
    failed = code != 0 or bool(result.get("is_error")) or not report.strip()
    stderr_text = (call_dir / "stderr.txt").read_text(errors="replace")
    usage_limit = failed and bool(USAGE_LIMIT_RE.search(report + "\n" + stderr_text))
    record.update(ended_epoch=time.time(), exit_code=code, session_id=result.get("session_id"),
                  is_error=bool(result.get("is_error")), failed=failed, usage_limit=usage_limit,
                  total_cost_usd=result.get("total_cost_usd"), num_turns=result.get("num_turns"),
                  duration_ms=result.get("duration_ms"), model_usage=result.get("modelUsage"),
                  stopped_by_signal=signal.Signals(stopped[0]).name if stopped else None)
    (call_dir / "call.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if failed:
        detail = (report or stderr_text[-600:]).strip()
        prefix = "Claude usage limit reached" if usage_limit else f"the subagent failed (exit {code})"
        return fail(f"{prefix}: {detail}")
    sys.stdout.write(report if report.endswith("\n") else report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
