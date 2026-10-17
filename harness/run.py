#!/usr/bin/env python3
"""Run one experiment cell: arm x task x repetition, with the acceptance loop.

Protocol (docs/experiment-design.md, "Completion protocol"): fresh clone of the
mission template, initial headless session, then up to --max-rounds rounds of
acceptance check and deterministic feedback via `claude -p --resume`.
run.json is rewritten atomically after every step so a crash leaves a record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import acceptance
import arms
import cost
import transcript

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = Path("/workspace/agent-experiments-fixture/mission-template")
DEFAULT_RUNS_DIR = Path("/workspace/agent-experiments-runs")
DEFAULT_TASKS_DIR = REPO_ROOT / "tasks"
DEFAULT_GAPS = Path(__file__).resolve().parent / "gaps.json"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
SCHEMA_VERSION = 1
STATUSES = ("running", "completed", "infra_error", "cli_error", "error")


class RunError(Exception):
    """A fatal, user-actionable problem with the run setup or the claude invocation."""


class InfraError(RunError):
    """A fault in the run environment (for example the CTF setup script), not in the agent."""


def resolve_task(tasks_dir: Path, name: str) -> tuple[str, str]:
    """Map a full task directory name or its short form (task1) to (short, directory name)."""
    if (tasks_dir / name).is_dir():
        directory = name
    else:
        matches = sorted(p.name for p in tasks_dir.glob(f"{name}-*") if p.is_dir())
        if len(matches) != 1:
            raise RunError(f"task {name!r} does not match exactly one directory in {tasks_dir} "
                           f"({len(matches)} matches)")
        directory = matches[0]
    found = re.match(r"task\d+", directory)
    return (found.group(0) if found else directory), directory


def hash_config_root(root: Path) -> str:
    """SHA-256 over the relative paths and bytes of settings.json, CLAUDE.md, and agents/."""
    digest = hashlib.sha256()
    paths = [root / "settings.json", root / "CLAUDE.md", *sorted((root / "agents").glob("*"))]
    for path in paths:
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class RunConfig:
    arm: str
    task: str
    rep: int
    autocompact: int | None
    template: Path
    runs_dir: Path
    arms_dir: Path
    tasks_dir: Path
    gaps_path: Path
    max_rounds: int
    max_budget_usd: float
    acceptance_timeout_s: float
    cli_timeout_s: float | None
    seed: int
    claude_bin: str
    task_dir: str = ""

    def __post_init__(self) -> None:
        assert self.rep >= 1 and self.max_rounds >= 1 and self.max_budget_usd > 0
        arms.get_arm(self.arm)


def make_run_id(arm: str, task: str, rep: int, now: datetime) -> str:
    return f"{arm}-{task}-r{rep}-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def build_claude_command(cfg: RunConfig, run_id: str, message: str, session_id: str | None,
                         remaining_budget_usd: float) -> list[str]:
    """argv for the initial call (session_id None) or a resume. The variadic
    --disallowedTools flag goes last so it cannot swallow the message."""
    arm = arms.get_arm(cfg.arm)
    cmd = [cfg.claude_bin, "-p"]
    if session_id is not None:
        cmd += ["--resume", session_id]
    cmd += [message,
            "--model", arms.MAIN_MODEL,
            "--permission-mode", "bypassPermissions",
            "--permission-prompts", "none",
            "--output-format", "json",
            "--append-system-prompt", f"Run ID: {run_id}",
            "--max-budget-usd", f"{remaining_budget_usd:.2f}"]
    if cfg.autocompact is not None:
        cmd += ["--autocompact", str(cfg.autocompact)]
    if arm.subagent_model is None:
        cmd += ["--disallowedTools", "Agent"]
    return cmd


def parse_cli_result(stdout: str) -> dict[str, Any]:
    """The JSON result object; tolerates a JSON array by taking its last result entry."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RunError(f"claude printed no valid JSON ({exc}); stdout starts: {stdout[:200]!r}") from exc
    if isinstance(data, list):
        results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
        if not results:
            raise RunError("claude JSON array holds no result entry")
        data = results[-1]
    if not isinstance(data, dict) or "session_id" not in data:
        raise RunError("claude JSON result lacks session_id")
    return data


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _git(args: Sequence[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(["git", "-c", "user.name=experiment-harness", "-c", "user.email=harness@localhost",
                           *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RunError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def prepare_repo(cfg: RunConfig, run_dir: Path) -> Path:
    spec = cfg.tasks_dir / cfg.task_dir / "spec.md"
    if not spec.is_file():
        raise RunError(f"task spec not found: {spec}")
    repo = run_dir / "repo"
    _git(["clone", "--quiet", str(cfg.template), str(repo)])
    shutil.copyfile(spec, repo / "TASK.md")
    _git(["add", "TASK.md"], cwd=repo)
    _git(["commit", "--quiet", "-m", "Add TASK.md"], cwd=repo)
    setup = repo / "ctf" / "setup_ctf.sh"
    if setup.is_file():
        proc = subprocess.run(["bash", str(setup)], cwd=repo, capture_output=True, text=True, check=False)
        (run_dir / "setup_ctf.log").write_text(
            f"exit {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}", encoding="utf-8")
        if proc.returncode != 0:
            raise InfraError(f"ctf/setup_ctf.sh exited {proc.returncode}; see {run_dir / 'setup_ctf.log'}")
    return repo


class Runner:
    def __init__(self, cfg: RunConfig, run_id: str, run_dir: Path) -> None:
        self.cfg, self.run_id, self.run_dir = cfg, run_id, run_dir
        self.source_root = arms.arm_root(cfg.arm, cfg.arms_dir)
        self.arm_root = run_dir / "config"  # this run's private CLAUDE_CONFIG_DIR
        self.repo = run_dir / "repo"
        self.session_ids: list[str] = []
        self.resume_indices: list[int] = []
        self.spent_usd = 0.0
        self.record: dict[str, Any] = {}

    def init_record(self, started: datetime) -> None:
        arm = arms.get_arm(self.cfg.arm)
        settings_path = self.source_root / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunError(f"arm root not generated or unreadable ({settings_path}): {exc}; run harness/arms.py") from exc
        config_hash = hash_config_root(self.source_root)
        try:
            shutil.copytree(self.source_root, self.arm_root, ignore=shutil.ignore_patterns("projects"))
        except OSError as exc:
            raise RunError(f"cannot copy arm root {self.source_root} to {self.arm_root}: {exc}") from exc
        assert hash_config_root(self.arm_root) == config_hash
        self.record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id, "arm": self.cfg.arm, "task": self.cfg.task, "task_dir": self.cfg.task_dir, "rep": self.cfg.rep,
            "status": "running", "started_at": _iso(started), "finished_at": None,
            "models": {"main": arms.MAIN_MODEL, "subagent": arm.subagent_model},
            "delegation": arm.delegation,
            "settings": settings, "config_sha256": config_hash,
            "autocompact": self.cfg.autocompact,
            "max_rounds": self.cfg.max_rounds, "max_budget_usd": self.cfg.max_budget_usd,
            "session_ids": [], "resume_request_indices": [],
            "rounds": [], "passed": False, "rounds_used": 0, "cap_hit": False,
            "error": None, "cost": None, "behavior": None, "totals": None,
            "layer2": {"rounds": None, "tokens": None, "cost_usd": None, "rating": None},
        }
        self.save()

    def save(self) -> None:
        self.record["session_ids"] = list(self.session_ids)
        self.record["resume_request_indices"] = list(self.resume_indices)
        write_json_atomic(self.run_dir / "run.json", self.record)

    # -- claude invocation -------------------------------------------------
    def invoke(self, message: str, session_id: str | None, round_no: int) -> dict[str, Any]:
        remaining = self.cfg.max_budget_usd - self.spent_usd
        cmd = build_claude_command(self.cfg, self.run_id, message, session_id, remaining)
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.arm_root)}
        try:
            proc = subprocess.run(cmd, cwd=self.repo, env=env, capture_output=True, text=True,
                                  timeout=self.cfg.cli_timeout_s, check=False)
        except subprocess.TimeoutExpired:
            raise RunError(f"claude exceeded --cli-timeout of {self.cfg.cli_timeout_s} s in round {round_no}") from None
        except OSError as exc:
            raise RunError(f"cannot execute {self.cfg.claude_bin!r}: {exc}") from exc
        (self.run_dir / f"cli-round{round_no}.stdout.json").write_text(proc.stdout, encoding="utf-8")
        (self.run_dir / f"cli-round{round_no}.stderr.txt").write_text(proc.stderr, encoding="utf-8")
        result = parse_cli_result(proc.stdout)
        result["_exit_code"] = proc.returncode
        return result

    # -- analysis ----------------------------------------------------------
    def transcripts(self) -> tuple[list[Path], list[Path]]:
        mains: list[Path] = []
        for sid in dict.fromkeys(self.session_ids):
            mains.append(transcript.find_main_transcript(self.arm_root, self.repo.resolve(), sid))
        return mains, [transcript.subagents_dir_for(m) for m in mains]

    def archive_transcripts(self, mains: list[Path]) -> None:
        dest = self.run_dir / "transcripts"
        dest.mkdir(exist_ok=True)
        for main in mains:
            shutil.copy2(main, dest / main.name)
            sub = transcript.subagents_dir_for(main)
            if sub.is_dir():
                shutil.copytree(sub, dest / main.stem / "subagents", dirs_exist_ok=True)

    def analyze(self) -> None:
        """Refresh cost, behavior, and totals; failures are recorded, not fatal."""
        try:
            mains, sub_dirs = self.transcripts()
            self.archive_transcripts(mains)
            gaps: tuple[float, ...] | None = None
            warnings: list[str] = []
            if self.resume_indices:
                if self.cfg.gaps_path.is_file():
                    gaps = cost.load_gaps(self.cfg.gaps_path)
                else:
                    warnings.append(f"pause samples {self.cfg.gaps_path} missing; scenarios computed without pauses")
            report = cost.cost_for_transcripts(mains, sub_dirs, self.resume_indices, gaps, self.cfg.seed)
            self.record["cost"] = {**report.to_dict(), "seed": self.cfg.seed, "warnings": warnings}
            behavior = transcript.summarize_behavior(mains, sub_dirs)
            behavior["agent_definition_models"] = arms.agent_models(self.arm_root)
            self.record["behavior"] = behavior
            self.record["analysis_error"] = None
        except (cost.CostError, transcript.TranscriptError, OSError) as exc:
            self.record["analysis_error"] = str(exc)
        rounds = self.record["rounds"]
        self.record["totals"] = {
            "cli_cost_usd": round(sum(r["cli"].get("total_cost_usd") or 0.0 for r in rounds), 6),
            "turns": sum(r["cli"].get("num_turns") or 0 for r in rounds),
            "api_time_ms": sum(r["cli"].get("duration_api_ms") or 0 for r in rounds),
            "cli_duration_ms": sum(r["cli"].get("duration_ms") or 0 for r in rounds),
            "wall_time_s": round(sum(r["wall_time_s"] for r in rounds), 3),
        }

    # -- main loop ---------------------------------------------------------
    def run(self, prompt: str) -> None:
        message, session_id = prompt, None
        pack_src = self.cfg.tasks_dir / self.cfg.task_dir / "acceptance"
        if not pack_src.is_dir():
            raise RunError(f"acceptance pack not found: {pack_src}")
        for round_no in range(1, self.cfg.max_rounds + 1):
            if self.cfg.max_budget_usd - self.spent_usd <= 0:
                self.record.update(status="completed", error="budget exhausted", cap_hit=True)
                return
            entry: dict[str, Any] = {"round": round_no, "kind": "initial" if session_id is None else "resume",
                                     "started_at": _iso(_utc_now()), "message": message}
            t0 = time.monotonic()
            try:
                result = self.invoke(message, session_id, round_no)
            except RunError as exc:
                entry.update(wall_time_s=round(time.monotonic() - t0, 3), cli={}, acceptance=None, error=str(exc))
                self.record["rounds"].append(entry)
                self.record.update(status="cli_error", error=str(exc))
                self.analyze_if_possible()
                return
            entry["wall_time_s"] = round(time.monotonic() - t0, 3)
            entry["cli"] = {k: result.get(k) for k in (
                "session_id", "total_cost_usd", "num_turns", "duration_ms", "duration_api_ms",
                "usage", "modelUsage", "is_error", "result", "subtype", "_exit_code")}
            session_id = str(result["session_id"])
            if not self.session_ids or self.session_ids[-1] != session_id:
                self.session_ids.append(session_id)
            self.spent_usd += float(result.get("total_cost_usd") or 0.0)
            self.record["rounds"].append(entry)
            self.record["rounds_used"] = round_no
            self.analyze()
            self.save()
            if result.get("is_error") or result["_exit_code"] != 0:
                message_text = f"claude reported an error in round {round_no} (exit {result['_exit_code']}): {result.get('result')}"
                self.record.update(status="cli_error", error=message_text)
                return
            # The next resume's first request is the one after those seen so far.
            mains, _ = self.transcripts()
            next_index = cost.main_request_count(mains)
            try:
                outcome = acceptance.run_pack(pack_src, self.run_dir, self.repo, round_no, self.cfg.acceptance_timeout_s)
            except acceptance.ContractError as exc:
                entry["acceptance"] = None
                entry["acceptance_error"] = str(exc)
                self.record.update(status="infra_error", error=str(exc))
                return
            entry["acceptance"] = outcome.to_dict()
            if outcome.passed:
                self.record.update(status="completed", passed=True)
                return
            if round_no == self.cfg.max_rounds:
                self.record.update(status="completed", cap_hit=True)
                return
            message = acceptance.compose_feedback(outcome.failures())
            self.resume_indices.append(next_index)
            self.save()
        raise AssertionError("loop exits through a return")

    def analyze_if_possible(self) -> None:
        if self.session_ids:
            self.analyze()


def dry_run(cfg: RunConfig, run_id: str, run_dir: Path, prompt: str) -> None:
    repo = run_dir / "repo"
    sample_feedback = "<deterministic feedback composed from failing checks>"
    steps = [
        ["git", "clone", "--quiet", str(cfg.template), str(repo)],
        ["cp", str(cfg.tasks_dir / cfg.task_dir / "spec.md"), str(repo / "TASK.md")],
        ["git", "-C", str(repo), "add", "TASK.md"],
        ["git", "-C", str(repo), "commit", "-m", "Add TASK.md"],
        ["bash", str(repo / "ctf" / "setup_ctf.sh")],
        ["cp", "-r", str(arms.arm_root(cfg.arm, cfg.arms_dir)), str(run_dir / "config")],
    ]
    print(f"# run id: {run_id}\n# run dir: {run_dir}")
    for step in steps:
        print(shlex.join(step))
    env = f"CLAUDE_CONFIG_DIR={shlex.quote(str(run_dir / 'config'))} {TOKEN_ENV}=<token>"
    print(f"cd {shlex.quote(str(repo))} && {env} " + shlex.join(
        build_claude_command(cfg, run_id, prompt, None, cfg.max_budget_usd)))
    print(f"# then, up to {cfg.max_rounds - 1} times while acceptance fails:")
    print(f"bash {run_dir}/acceptance/run.sh {repo} {run_dir}/acceptance-result-roundN.json")
    print(f"cd {shlex.quote(str(repo))} && {env} " + shlex.join(
        build_claude_command(cfg, run_id, sample_feedback, "<session_id>", cfg.max_budget_usd)))


def parse_args(argv: Sequence[str] | None) -> tuple[argparse.Namespace, RunConfig]:
    p = argparse.ArgumentParser(description="Run one arm x task x repetition.")
    p.add_argument("--arm", required=True, choices=list(arms.ARMS))
    p.add_argument("--task", required=True, help="task1..task4 or the full directory name under tasks/")
    p.add_argument("--rep", required=True, type=int)
    p.add_argument("--autocompact", type=int, default=None, help="auto-compaction window in tokens (min 100000)")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    p.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    p.add_argument("--arms-dir", type=Path, default=arms.DEFAULT_ARMS_DIR)
    p.add_argument("--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR)
    p.add_argument("--gaps", type=Path, default=DEFAULT_GAPS, help="gaps.json from gaps.py")
    p.add_argument("--max-rounds", type=int, default=5,
                   help="agent invocations in total: the initial one plus up to N-1 repair resumes (default 5)")
    p.add_argument("--max-budget-usd", type=float, default=150.0)
    p.add_argument("--acceptance-timeout", type=float, default=1800.0)
    p.add_argument("--cli-timeout", type=float, default=None, help="seconds per claude invocation (default: none)")
    p.add_argument("--seed", type=int, default=0, help="seed for simulated pause sampling")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if a.rep < 1 or a.max_rounds < 1 or a.max_budget_usd <= 0:
        p.error("--rep and --max-rounds must be >= 1 and --max-budget-usd > 0")
    if a.autocompact is not None and a.autocompact < 100_000:
        p.error("--autocompact must be at least 100000")
    try:
        short, directory = resolve_task(a.tasks_dir, a.task)
    except RunError as exc:
        p.error(str(exc))
    cfg = RunConfig(a.arm, short, a.rep, a.autocompact, a.template, a.runs_dir, a.arms_dir, a.tasks_dir,
                    a.gaps, a.max_rounds, a.max_budget_usd, a.acceptance_timeout, a.cli_timeout, a.seed,
                    a.claude_bin, directory)
    return a, cfg


def main(argv: Sequence[str] | None = None) -> int:
    args, cfg = parse_args(argv)
    now = _utc_now()
    run_id = make_run_id(cfg.arm, cfg.task, cfg.rep, now)
    run_dir = cfg.runs_dir / run_id
    prompt_path = cfg.tasks_dir / cfg.task_dir / "prompt.md"
    try:
        if not prompt_path.is_file():
            raise RunError(f"task prompt not found: {prompt_path}")
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if args.dry_run:
            dry_run(cfg, run_id, run_dir, prompt)
            return 0
        if not os.environ.get(TOKEN_ENV):
            raise RunError(f"{TOKEN_ENV} is not set; create one with `claude setup-token`")
        if not cfg.template.is_dir():
            raise RunError(f"mission template not found: {cfg.template}; run fixture/build-template.sh")
        run_dir.mkdir(parents=True)
        runner = Runner(cfg, run_id, run_dir)
        runner.init_record(now)
        try:
            prepare_repo(cfg, run_dir)
            runner.run(prompt)
        except RunError as exc:
            runner.record.update(status="infra_error" if isinstance(exc, InfraError) else "error", error=str(exc))
            raise
        finally:
            runner.record["finished_at"] = _iso(_utc_now())
            runner.save()
    except (RunError, arms.ArmError, OSError) as exc:
        print(f"run: error: {exc}", file=sys.stderr)
        return 1
    rec = runner.record
    print(f"{run_id}: status={rec['status']} passed={rec['passed']} rounds={rec['rounds_used']} "
          f"cap_hit={rec['cap_hit']}")
    return 0 if rec["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
