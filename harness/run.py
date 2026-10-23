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
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import functools
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import acceptance
import analysis
import arms
import claude_calls
import codex
import cost
import proc
import transcript

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = Path("/workspace/agent-experiments-fixture/mission-template")
DEFAULT_RUNS_DIR = Path("/workspace/agent-experiments-runs")
DEFAULT_TASKS_DIR = REPO_ROOT / "tasks"
DEFAULT_GAPS = Path(__file__).resolve().parent / "gaps.json"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# The child environment is built only from these variables (when set), plus
# the token, the config dir, and fixed values below. Nothing else is inherited,
# so launching-session variables cannot override an arm's settings.
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL")
# In print mode the CLI stops background subagents still running ten minutes after the
# main agent's last turn, ending the round with their work unfinished. The ceiling is raised
# to two hours so a round ends only once its subagents report; --cli-timeout still bounds
# the whole invocation.
BG_WAIT_CEILING_MS = 2 * 60 * 60 * 1000
ENV_FIXED = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": str(BG_WAIT_CEILING_MS)}
# Loopback port forwarding is disabled in all four directions so that the
# namespace's traffic can never reach another session's cFS on the host.
PASTA_PREFIX = ("pasta", "--config-net", "--quiet", "-t", "none", "-u", "none", "-T", "none", "-U", "none", "--")
# A statically linked resolver (musl, as in the Codex CLI) sends plain DNS to the host's
# stub resolver on 127.0.0.53, which the namespace cannot reach since loopback forwarding is
# off; glibc programs reach systemd-resolved over its unix socket instead. With dns_forward,
# pasta forwards queries sent to this address to the host's resolver, and the namespace's
# /etc/resolv.conf names it.
DNS_FORWARD_ADDR = "169.254.1.1"
CODEX_CONTEXT_WINDOW = 475_000
MQUEUE_MSG_MAX = 512
STRAY_PROCESS_NAMES = frozenset({"core-cpu1"})
# The Claude login of a Codex main agent's claude-agent subagents. wrap_netns reads it from this
# variable of its own environment into a file in the namespace's private /dev/shm, and removes the
# variable before the agent starts; claude-agent reads the file named by CREDENTIALS_FILE_ENV.
CREDENTIALS_SOURCE_ENV = "HARNESS_CLAUDE_AGENT_LOGIN"
CREDENTIALS_FILE_ENV = "CLAUDE_AGENT_CREDENTIALS_FILE"
CREDENTIALS_FILE = "/dev/shm/.claude-agent-credentials"

# Paths covered by an empty tmpfs while claude runs (not during acceptance runs).
# "~" is the harness user's home. Only this run's repo/ and config/ (and, for a
# Codex arm, codex-home/) are bound back afterwards, so nothing else under these
# paths is visible to the agent.
HIDDEN_PATHS: tuple[tuple[str, str], ...] = (
    ("/workspace", "the runs dir holds other runs' solved repos; agent-experiments-scout holds reference "
                   "solutions; the worktree holds the acceptance packs; the arms dir holds every arm's config"),
    ("~/.claude", "the user's own Claude Code transcripts and settings"),
    ("~/.codex", "the user's own Codex sessions, settings, and login (a Codex arm gets its own CODEX_HOME)"),
    ("~/.secrets", "the owner's secrets; the harness loads credentials before entering the namespace"),
    ("~/projects/agent-experiments", "the primary checkout holds the acceptance packs"),
    ("/tmp", "scratch space of other sessions, which may hold packs or solutions"),
)
EXIT_USAGE_LIMIT = 3
USAGE_LIMIT_RE = re.compile(r"usage limit|rate limit|exceeded|\b429\b|hit your\b[^.\n]*\blimit", re.IGNORECASE)
SCHEMA_VERSION = 2
STATUSES = ("running", "completed", "usage_limit", "codex_error", "claude_error", "infra_error", "cli_error",
            "interrupted", "error", "accounting_error", "validation_error")


class RunError(Exception):
    """A fatal, user-actionable problem with the run setup or the claude invocation."""


class UsageLimit(RunError):
    """The subscription usage or rate limit was hit; the attempt is recorded with its spend."""


class InfraError(RunError):
    """A fault in the run environment (for example the CTF setup script), not in the agent."""


class Interrupted(RunError):
    """run.py received SIGTERM or SIGHUP; raised so that cleanup (the Codex login copy) still runs."""


def raise_interrupted(signum: int, _frame: object) -> None:
    raise Interrupted(f"interrupted by {signal.Signals(signum).name}")


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


ARM_ROOT_ENTRIES = ("settings.json", "CLAUDE.md", "agents", arms.CODEX_DIR, arms.CLAUDE_DIR)
VISIBLE_RUN_DIRS = ("repo", "config")


def hash_config_root(root: Path) -> str:
    """SHA-256 over the relative paths and bytes of settings.json, CLAUDE.md, agents/, codex/, and claude/."""
    digest = hashlib.sha256()
    paths = [root / "settings.json", root / "CLAUDE.md", *sorted((root / "agents").glob("*")),
             *sorted((root / arms.CODEX_DIR).rglob("*")), *sorted((root / arms.CLAUDE_DIR).rglob("*"))]
    for path in paths:
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def hash_tree(root: Path) -> str:
    """SHA-256 over every file's relative path and bytes under root."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def copy_arm_root(source: Path, dest: Path, claude_files: bool = True) -> None:
    """Copy exactly settings.json, CLAUDE.md, agents/, codex/, and claude/; refuse any other content.
    An arm whose main agent is Codex (claude_files False) has only codex/, which is required."""
    extras = sorted(p.name for p in source.iterdir() if p.name not in ARM_ROOT_ENTRIES)
    if not claude_files:  # a Claude Code configuration would be inert here, and so is a sign of a stale root
        extras += sorted(n for n in ("settings.json", "CLAUDE.md", "agents") if (source / n).exists())
    if extras:
        raise RunError(f"arm root {source} holds unexpected entries {extras}; remove them or regenerate the arm")
    required = ("settings.json", "CLAUDE.md") if claude_files else (arms.CODEX_DIR,)
    for name in required:
        if not (source / name).exists():
            raise RunError(f"arm root {source} lacks {name}; run harness/arms.py")
    dest.mkdir(parents=True)
    if claude_files:
        shutil.copyfile(source / "settings.json", dest / "settings.json")
        shutil.copyfile(source / "CLAUDE.md", dest / "CLAUDE.md")
    if (source / "agents").exists():
        shutil.copytree(source / "agents", dest / "agents")
    for name in (arms.CODEX_DIR, arms.CLAUDE_DIR):
        if (source / name).exists():
            shutil.copytree(source / name, dest / name)


def _capture(cmd: Sequence[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    try:
        done = subprocess.run(list(cmd), cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    if done.returncode != 0:
        return f"unavailable: exit {done.returncode}: {done.stderr.strip()[:200]}"
    return done.stdout.strip()


@functools.cache
def harness_git() -> tuple[str, bool]:
    """(HEAD sha, dirty) of the worktree for files under harness/."""
    sha = _capture(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    status = _capture(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", "harness"])
    return sha, status.startswith("unavailable") or bool(status)


def new_run_id(runs_dir: Path) -> str:
    """Opaque 8-hex nonce; the arm/task/rep mapping lives in run.json and runs.tsv."""
    while True:
        nonce = secrets.token_hex(4)
        if not (runs_dir / nonce).exists():
            return nonce


def append_index(runs_dir: Path, fields: Sequence[object]) -> None:
    """Append one tab-separated line to runs.tsv (a single O_APPEND write)."""
    line = "\t".join(str(f) for f in fields) + "\n"
    fd = os.open(runs_dir / "runs.tsv", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


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
    acceptance_timeout_s: float
    cli_timeout_s: float | None
    seed: int
    claude_bin: str
    task_dir: str = ""
    netns: bool = True
    compact_pct: int | None = None

    def __post_init__(self) -> None:
        assert self.rep >= 1 and self.max_rounds >= 1
        assert self.compact_pct is None or 1 <= self.compact_pct <= 100
        arms.get_arm(self.arm)


def hiding_steps(run_dir: Path, visible: Sequence[str] = VISIBLE_RUN_DIRS,
                 host_visible: Sequence[Path] = ()) -> list[str]:
    """Shell steps (run as root in the mount namespace) that hide HIDDEN_PATHS and
    bind this run's `visible` subdirectories back at their original paths, plus the
    host_visible directories (absolute paths under hidden ones) read-only."""
    assert visible and all(re.fullmatch(r"[a-z][a-z-]*", name) for name in visible)
    assert all(p.is_absolute() for p in host_visible)
    run = run_dir.resolve()
    stage = "/dev/shm/.stage"
    # (staging dir, original path, read-only) for every directory bound back after hiding
    binds = [(f"{stage}/{name}", shlex.quote(str(run / name)), False) for name in visible]
    binds += [(f"{stage}/host{i}", shlex.quote(str(p)), True) for i, p in enumerate(host_visible)]
    staged = " ".join(s for s, _, _ in binds)
    steps = [f"mkdir -p {staged}"]
    steps += [f"mount --bind {original} {staging}" for staging, original, _ in binds]
    for template, _reason in HIDDEN_PATHS:
        path = shlex.quote(os.path.expanduser(template))
        steps.append(f"{{ [ ! -d {path} ] || mount -t tmpfs tmpfs {path}; }}")
    steps.append(f"mkdir -p {' '.join(original for _, original, _ in binds)}")
    for staging, original, read_only in binds:
        steps.append(f"mount --bind {staging} {original}")
        if read_only:
            steps.append(f"mount -o remount,bind,ro {original}")
    steps += [f"umount {staged}", f"rmdir {staged} {stage}"]
    return steps


def wrap_netns(cmd: Sequence[str], hide_run_dir: Path | None = None,
               visible: Sequence[str] = VISIBLE_RUN_DIRS, host_visible: Sequence[Path] = (),
               dns_forward: bool = False, credentials: bool = False) -> list[str]:
    """Run cmd isolated in network, mount, IPC, PID, and user namespaces.

    Layers, outermost first: pasta (user + network namespace, root inside);
    `unshare -m -i -p -f --mount-proc --kill-child` (mount, IPC, and PID
    namespaces, so CTF's pgrep/pkill see only this run and everything dies with
    the namespace), whose script mounts a private tmpfs on /dev/shm and, when
    hide_run_dir is given, hides HIDDEN_PATHS while still root; then
    `unshare -U` mapping the harness uid/gid back so the command is not fake
    root (OSAL skips its message-queue depth adaptation for uid 0 and cFS then
    fails to boot). pasta cannot exec the program directly, hence `sh -c` at
    each layer. With hide_run_dir the command starts in <hide_run_dir>/repo, and of
    the run dir only the `visible` subdirectories, and of the hidden host paths only
    host_visible (read-only), can be seen. With dns_forward, DNS sent to DNS_FORWARD_ADDR
    reaches the host's resolver and a private /etc/resolv.conf names that address. With credentials,
    the value of CREDENTIALS_SOURCE_ENV in this command's environment is written (mode 600) to
    CREDENTIALS_FILE on the private /dev/shm, and the final command is exec'd without that variable, so
    that no process of the namespace holds the value in its environment.
    """
    mapped = (f"unshare -U --map-user={os.getuid()} --map-group={os.getgid()} "
              f"sh -c {shlex.quote(shlex.join(cmd))}")
    steps = [f"printf '%s\\n' {MQUEUE_MSG_MAX} > /proc/sys/fs/mqueue/msg_max || "
             "{ printf 'failed to raise POSIX mqueue msg_max to 512\\n' >&2; exit 1; }; "
             f'test "$(cat /proc/sys/fs/mqueue/msg_max)" -ge {MQUEUE_MSG_MAX} || '
             "{ printf 'POSIX mqueue msg_max is below 512\\n' >&2; exit 1; }",
             "mount -t tmpfs tmpfs /dev/shm"]
    if credentials:
        steps.append(f'( umask 077 && printf %s "${CREDENTIALS_SOURCE_ENV}" > {CREDENTIALS_FILE} )')
        mapped = f"exec env -u {CREDENTIALS_SOURCE_ENV} {mapped}"
    if dns_forward:
        steps += [f"printf 'nameserver {DNS_FORWARD_ADDR}\\n' > /dev/shm/.resolv.conf",
                  "mount --bind /dev/shm/.resolv.conf /etc/resolv.conf", "rm /dev/shm/.resolv.conf"]
    if hide_run_dir is not None:
        steps += hiding_steps(hide_run_dir, visible, host_visible)
        steps.append(f"cd {shlex.quote(str(hide_run_dir.resolve() / 'repo'))}")
    steps.append(mapped)
    namespaced = ("unshare -m -i -p -f --mount-proc --kill-child sh -c "
                  + shlex.quote(" && ".join(steps)))
    prefix = [*PASTA_PREFIX[:-1], "--dns-forward", DNS_FORWARD_ADDR, "--"] if dns_forward else list(PASTA_PREFIX)
    return [*prefix, "sh", "-c", namespaced]


# A raw UDP DNS query for api.openai.com's A record to the forwarded resolver; exits 0 on any answer.
DNS_PROBE = ("import socket,struct,sys; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(5); "
             "q=struct.pack('>HHHHHH',0x1234,0x0100,1,0,0,0)+b''.join(bytes([len(p)])+p.encode() "
             "for p in 'api.openai.com'.split('.'))+b'\\0'+struct.pack('>HH',1,1); "
             f"s.sendto(q,('{DNS_FORWARD_ADDR}',53)); d=s.recv(512); sys.exit(0 if d[:2]==q[:2] else 1)")


def check_netns(runs_dir: Path, claude_bin: str = "claude", extra_bins: Sequence[str] = (),
                host_visible: Sequence[Path] = (), dns_forward: bool = False) -> None:
    """Fail clearly unless the full wrapper (with hiding) isolates as intended: harness uid,
    empty private /dev/shm, own PID namespace, only this run's repo/config visible,
    and the claude binary (and any extra_bins) still reachable."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    probe_dir = Path(tempfile.mkdtemp(prefix=".selfcheck-", dir=runs_dir))
    try:
        (probe_dir / "repo").mkdir()
        (probe_dir / "config").mkdir()
        (probe_dir / "repo" / "marker").write_text("x")
        (probe_dir / "hidden-sibling").write_text("x")
        run = shlex.quote(str(probe_dir.resolve()))
        script = (f'test "$(id -u)" = {os.getuid()} && test -z "$(ls -A /dev/shm)" '
                  f'&& case "$(cat /proc/1/comm)" in sh|bash|dash) ;; *) exit 1;; esac '
                  f'&& test -f {run}/repo/marker && test -w {run}/repo && test -w {run}/config '
                  f'&& test ! -e {run}/hidden-sibling && command -v {shlex.quote(claude_bin)} >/dev/null'
                  + "".join(f" && command -v {shlex.quote(b)} >/dev/null" for b in extra_bins))
        script += f" && test ! -e {shlex.quote(str(codex.USER_AUTH))}"  # ~/.codex is hidden
        script += "".join(f" && test -d {shlex.quote(str(p))} && test ! -w {shlex.quote(str(p))}"
                          for p in host_visible)  # bound back read-only
        if dns_forward:  # the forwarded resolver must answer a plain DNS query
            script += (f" && grep -qx 'nameserver {DNS_FORWARD_ADDR}' /etc/resolv.conf"
                       f" && python3 -c {shlex.quote(DNS_PROBE)}")
        cmd = wrap_netns(["sh", "-c", script], hide_run_dir=probe_dir, host_visible=host_visible,
                         dns_forward=dns_forward)
        try:
            done = proc.run_group(cmd, cwd=None, env=None, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunError(f"run isolation self-check failed ({shlex.join(cmd)}): {exc}; "
                           "install pasta and unshare or pass --no-netns") from exc
        if done.returncode != 0:
            raise RunError(f"run isolation self-check failed ({shlex.join(cmd)} exited "
                           f"{done.returncode}): {done.stderr.strip()}; fix pasta or pass --no-netns")
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def child_env(config_dir: Path, base: dict[str, str] | None = None, include_token: bool = True,
              codex_home: Path | None = None, compact_pct: int | None = None) -> dict[str, str]:
    """Allowlisted environment for claude (token included) and acceptance packs (token excluded).
    With codex_home (a Codex arm's claude), CODEX_HOME is set and the codex-agent command
    is put first on PATH."""
    source = os.environ if base is None else base
    env = {k: source[k] for k in ENV_ALLOWLIST if k in source}
    if include_token and TOKEN_ENV in source:
        env[TOKEN_ENV] = source[TOKEN_ENV]
    env.update(ENV_FIXED)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if compact_pct is not None:
        assert 1 <= compact_pct <= 100
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(compact_pct)
    if codex_home is not None:
        assert include_token, "acceptance packs never get Codex"
        env["CODEX_HOME"] = str(codex_home)
        bin_dir = config_dir / arms.CODEX_DIR / "bin"
        env["PATH"] = f"{bin_dir}:{env['PATH']}" if env.get("PATH") else str(bin_dir)
    return env


def codex_main_env(codex_home: Path, base: dict[str, str] | None = None, bin_dirs: Sequence[Path] = (),
                   credentials_file: str | None = None) -> dict[str, str]:
    """Allowlisted environment for a Codex main agent: no Claude token or configuration, and
    CODEX_HOME naming the run's own Codex home. The subagent launchers' bin dirs come first on
    PATH. With credentials_file, CREDENTIALS_FILE_ENV names the file claude-agent reads the
    Claude login from (the login itself is never in this environment)."""
    source = os.environ if base is None else base
    env = {k: source[k] for k in ENV_ALLOWLIST if k in source}
    env["CODEX_HOME"] = str(codex_home)
    if bin_dirs:
        env["PATH"] = ":".join([*map(str, bin_dirs), *([env["PATH"]] if env.get("PATH") else [])])
    if credentials_file is not None:
        env[CREDENTIALS_FILE_ENV] = credentials_file
    return env


def compact_token_limit(compact_pct: int) -> int:
    """Codex token threshold for a percentage of the Sol context window."""
    assert 1 <= compact_pct <= 100
    return CODEX_CONTEXT_WINDOW * compact_pct // 100


def kill_stray_processes(run_dir: Path, names: frozenset[str] = STRAY_PROCESS_NAMES) -> list[int]:
    """SIGKILL processes named in `names` whose cwd is under run_dir (found via /proc)."""
    root = run_dir.resolve()
    killed: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if (proc / "comm").read_text().strip() not in names:
                continue
            cwd = Path(os.readlink(proc / "cwd"))
            if cwd != root and root not in cwd.parents:
                continue
            os.kill(int(proc.name), signal.SIGKILL)
            killed.append(int(proc.name))
        except OSError:
            continue  # exited meanwhile or not ours
    return killed


def build_claude_command(cfg: RunConfig, run_id: str, message: str, session_id: str | None) -> list[str]:
    """argv for the initial call (session_id None) or a resume. The variadic
    --disallowedTools flag goes last so it cannot swallow the message."""
    arm = arms.get_arm(cfg.arm)
    cmd = [cfg.claude_bin, "-p"]
    if session_id is not None:
        cmd += ["--resume", session_id]
    cmd += [message,
            "--model", arm.main_model,
            "--permission-mode", "bypassPermissions",
            "--permission-prompts", "none",
            "--output-format", "json",
            "--append-system-prompt", f"Run ID: {run_id}"]
    if cfg.autocompact is not None:
        cmd += ["--autocompact", str(cfg.autocompact)]
    if arm.subagent_model is None or arm.codex:
        cmd += ["--disallowedTools", "Agent"]
    return cmd


def codex_developer_instructions(run_id: str) -> str:
    """The Codex main agent's counterpart of the Claude main agent's `--append-system-prompt`."""
    return f"Run ID: {run_id}"


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


def provenance(cfg: RunConfig, spec: Path, pack: Path) -> dict[str, object]:
    sha, dirty = harness_git()
    version_env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    return {
        "claude_version": (None if arms.get_arm(cfg.arm).codex_main
                           else _capture([cfg.claude_bin, "--version"], env=version_env)),
        "harness_sha": sha, "harness_dirty": dirty,
        "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
        "pack_sha256": hash_tree(pack),
        "template_head": _capture(["git", "-C", str(cfg.template), "rev-parse", "HEAD"]),
    }


class Runner:
    def __init__(self, cfg: RunConfig, run_id: str, run_dir: Path) -> None:
        self.cfg, self.run_id, self.run_dir = cfg, run_id, run_dir
        self.source_root = arms.arm_root(cfg.arm, cfg.arms_dir)
        self.arm_root = run_dir / "config"  # this run's private CLAUDE_CONFIG_DIR
        self.repo = run_dir / "repo"
        self.session_ids: list[str] = []
        self.resume_indices: list[int] = []
        self.leak_hits: list[dict[str, object]] = []
        self.leak_offsets: dict[str, int] = {}
        self.record: dict[str, Any] = {}
        arm = arms.get_arm(cfg.arm)
        self.codex_main = arm.codex_main
        self.codex_home: Path | None = codex.home_for(run_dir) if arm.uses_codex else None
        # Built-in children share CODEX_HOME and are identified by rollout ancestry.
        self.subagent_home: Path | None = None
        self.claude_dir: Path | None = self.arm_root / arms.CLAUDE_DIR if arm.claude_agents else None
        self.codex_bin: str | None = None  # the pinned Codex binary of a Codex main agent, set by prepare_codex

    def init_record(self, started: datetime) -> None:
        arm = arms.get_arm(self.cfg.arm)
        # The configuration the agent runs under: Claude Code's settings.json, or a Codex main agent's config.toml.
        settings_path = (self.source_root / arms.CODEX_DIR / "config.toml" if arm.codex_main
                         else self.source_root / "settings.json")
        try:
            text = settings_path.read_text(encoding="utf-8")
            settings = tomllib.loads(text) if arm.codex_main else json.loads(text)
        except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise RunError(f"arm root not generated or unreadable ({settings_path}): {exc}; run harness/arms.py") from exc
        config_hash = hash_config_root(self.source_root)
        try:
            copy_arm_root(self.source_root, self.arm_root, claude_files=not arm.codex_main)
        except OSError as exc:
            raise RunError(f"cannot copy arm root {self.source_root} to {self.arm_root}: {exc}") from exc
        assert hash_config_root(self.arm_root) == config_hash
        task_root = self.cfg.tasks_dir / self.cfg.task_dir
        self.record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id, "arm": arms.arm_label(self.cfg.arm, self.cfg.compact_pct),
            "task": self.cfg.task, "task_dir": self.cfg.task_dir,
            "rep": self.cfg.rep,
            "status": "running", "started_at": _iso(started), "finished_at": None,
            "main_harness": arm.main_harness,
            "models": {"main": arm.main_model,
                       "subagent": ({n: arm.model_for(n) for n in arm.agents} if arm.multi_model
                                    else arm.subagent_model)},
            "delegation": arm.delegation,
            "settings": settings, "config_sha256": config_hash,
            "provenance": provenance(self.cfg, task_root / "spec.md", task_root / "acceptance"),
            "subagent_harness": arm.subagent_harness,
            "autocompact": self.cfg.autocompact,
            "compact_pct": self.cfg.compact_pct,
            "max_rounds": self.cfg.max_rounds,
            "session_ids": [], "resume_request_indices": [],
            "rounds": [], "passed": False, "rounds_used": 0, "cap_hit": False,
            "error": None, "cost": None, "behavior": None, "totals": None,
            "validation": None, "leak_check": [],
            "layer2": {"rounds": None, "tokens": None, "cost_usd": None, "rating": None},
        }
        self.save()

    def prepare_codex(self) -> None:
        """For a Codex arm, build and check this run's CODEX_HOME (after the repo exists)."""
        if self.codex_home is None:
            return
        try:
            info = codex.prepare_home(self.run_dir, self.arm_root / arms.CODEX_DIR, self.repo,
                                      child_env(self.arm_root, include_token=False))
        except (codex.CodexError, OSError) as exc:
            raise InfraError(f"cannot prepare the Codex home: {exc}") from exc
        self.codex_bin = str(info["codex_bin"])
        self.record["codex"] = info
        if self.claude_dir is not None:
            self.prepare_claude_agents()
        self.save()

    def prepare_claude_agents(self) -> None:
        """Pin the Claude Code binary that claude-agent runs (it must be visible in the namespaces)."""
        assert self.claude_dir is not None
        found = shutil.which(self.cfg.claude_bin)
        if found is None:
            raise InfraError(f"{self.cfg.claude_bin!r} is not on PATH; claude-agent needs the Claude Code binary")
        claude_bin = str(Path(found).resolve())
        try:
            (self.claude_dir / "agent-runtime.json").write_text(json.dumps({"claude_bin": claude_bin}, indent=2) + "\n",
                                                                 encoding="utf-8")
        except OSError as exc:
            raise InfraError(f"cannot write the Claude agent runtime file: {exc}") from exc
        version_env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
        self.record["claude_agents"] = {"claude_bin": claude_bin,
                                        "claude_version": _capture([claude_bin, "--version"], env=version_env)}

    def release_codex(self) -> None:
        """Remove this run's copy of the Codex login (writing back a refreshed one)."""
        if self.codex_home is None or not self.record:
            return
        info = self.record.setdefault("codex", {})
        try:
            info.update(codex.release_home(self.run_dir, info.get("auth_sha256")))
        except OSError as exc:
            info["release_error"] = str(exc)

    def save(self) -> None:
        self.record["session_ids"] = list(self.session_ids)
        self.record["resume_request_indices"] = list(self.resume_indices)
        write_json_atomic(self.run_dir / "run.json", self.record)

    # -- agent invocation --------------------------------------------------
    def execute(self, cmd: list[str], env: dict[str, str], round_no: int, name: str, binary: str,
                stdin: str | None = None, credentials: bool = False) -> subprocess.CompletedProcess[str]:
        """Run the agent's command for one round (wrapped in namespaces unless --no-netns) in the
        repo; its leftover cFS processes are killed afterwards."""
        if self.cfg.netns:
            codex_arm = self.codex_home is not None
            cmd = wrap_netns(cmd, hide_run_dir=self.run_dir,
                             visible=VISIBLE_RUN_DIRS + ((codex.HOME_DIR,) if codex_arm else ()),
                             host_visible=codex.HOST_VISIBLE if codex_arm else (), dns_forward=codex_arm,
                             credentials=credentials)
        try:
            return proc.run_group(cmd, cwd=self.repo, env=env, timeout=self.cfg.cli_timeout_s, stdin=stdin)
        except subprocess.TimeoutExpired:
            raise RunError(f"{name} exceeded --cli-timeout of {self.cfg.cli_timeout_s} s in round {round_no}") from None
        except OSError as exc:
            raise RunError(f"cannot execute {binary!r}: {exc}") from exc
        finally:
            kill_stray_processes(self.run_dir)

    def invoke_codex(self, message: str, thread_id: str | None, round_no: int) -> dict[str, Any]:
        """One round of a Codex main agent: `codex exec` (or `codex exec resume <thread>`) with the
        message on standard input. Raises UsageLimit or RunError when the round started no thread."""
        assert self.codex_main and self.codex_bin is not None and self.codex_home is not None
        arm = arms.get_arm(self.cfg.arm)
        last_message = self.codex_home / f"codex-main-round{round_no}.last.txt"
        cmd = codex.main_command(self.codex_bin, arm.main_model, arm.main_effort,
                                 codex_developer_instructions(self.run_id), last_message.resolve(),
                                 self.repo.resolve(), thread_id)
        if self.cfg.compact_pct is not None:
            option = ["-c", f"model_auto_compact_token_limit={compact_token_limit(self.cfg.compact_pct)}"]
            insert_at = cmd.index(thread_id) if thread_id is not None else len(cmd) - 1
            cmd[insert_at:insert_at] = option
        bin_dirs = [self.arm_root / arms.CLAUDE_DIR / "bin"] if arm.claude_agents else []
        login_file: Path | None = None
        login_owned = False
        try:
            if self.claude_dir is None:
                env, credentials = codex_main_env(self.codex_home, bin_dirs=bin_dirs), False
            elif self.cfg.netns:  # wrap_netns hands the login to the namespace's private /dev/shm
                env = codex_main_env(self.codex_home, bin_dirs=bin_dirs, credentials_file=CREDENTIALS_FILE)
                env[CREDENTIALS_SOURCE_ENV] = os.environ[TOKEN_ENV]
                credentials = True
            else:  # debugging without namespaces: a private file on the host's /dev/shm for this round
                login_file = Path(f"/dev/shm/.claude-agent-credentials-{self.run_id}")
                env, credentials = codex_main_env(self.codex_home, bin_dirs=bin_dirs, credentials_file=str(login_file)), False
                fd = os.open(login_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                login_owned = True
                try:
                    handle = os.fdopen(fd, "w")
                except BaseException:
                    os.close(fd)
                    raise
                with handle:
                    handle.write(os.environ[TOKEN_ENV])
            done = self.execute(cmd, env, round_no, "codex", self.codex_bin, stdin=message, credentials=credentials)
        finally:
            if login_owned:
                assert login_file is not None
                login_file.unlink(missing_ok=True)
        (self.run_dir / f"codex-main-round{round_no}.events.jsonl").write_text(done.stdout, encoding="utf-8")
        (self.run_dir / f"codex-main-round{round_no}.stderr.txt").write_text(done.stderr, encoding="utf-8")
        result = codex.main_round_result(done.stdout, done.stderr, done.returncode, last_message)
        if last_message.is_file():
            try:
                shutil.copy2(last_message, self.run_dir / last_message.name)
            except OSError as exc:
                result.update(is_error=True, result=f"cannot archive Codex final message {last_message}: {exc}")
        if result["session_id"] is None:
            text = f"codex started no thread in round {round_no} (exit {done.returncode}): {result['result'] or 'no output'}"
            raise (UsageLimit if result["_usage_limit"] else RunError)(text)
        if thread_id is not None and result["session_id"] != thread_id:
            raise RunError(f"codex resumed thread {thread_id} but reported thread {result['session_id']} "
                           f"in round {round_no}")
        return result

    def invoke(self, message: str, session_id: str | None, round_no: int) -> dict[str, Any]:
        if self.codex_main:
            return self.invoke_codex(message, session_id, round_no)
        cmd = build_claude_command(self.cfg, self.run_id, message, session_id)
        done = self.execute(cmd, child_env(self.arm_root, codex_home=self.codex_home,
                                           compact_pct=self.cfg.compact_pct), round_no, "claude",
                            self.cfg.claude_bin)
        (self.run_dir / f"cli-round{round_no}.stdout.json").write_text(done.stdout, encoding="utf-8")
        (self.run_dir / f"cli-round{round_no}.stderr.txt").write_text(done.stderr, encoding="utf-8")
        try:
            result = parse_cli_result(done.stdout)
        except RunError as exc:
            if USAGE_LIMIT_RE.search(done.stdout + "\n" + done.stderr):
                raise UsageLimit(f"usage limit suspected in round {round_no}: {exc}") from exc
            raise
        result["_exit_code"] = done.returncode
        failed = bool(result.get("is_error")) or done.returncode != 0
        if failed and USAGE_LIMIT_RE.search(str(result.get("result") or "") + "\n" + done.stderr):
            result["_usage_limit"] = True
        return result

    # -- analysis ----------------------------------------------------------
    def transcripts(self) -> tuple[list[Path], list[Path]]:
        """Claude main transcripts and their subagent dirs; none for a Codex main agent, whose
        session logs live in the run's Codex home."""
        if self.codex_main:
            return [], []
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

    def scan_leaks(self, mains: list[Path], sub_dirs: list[Path], round_no: int) -> None:
        """Report-only: strings that point at the answer key in transcript entries new this round."""
        files = list(mains) + [p for d in sub_dirs for p in transcript.subagent_files(d)]
        if self.codex_home is not None:
            files += codex.leak_scan_files(self.codex_home)
        if self.subagent_home is not None:
            files += codex.leak_scan_files(self.subagent_home)
        if self.claude_dir is not None:
            files += claude_calls.leak_scan_files(self.claude_dir)
        for path in files:
            hits, total = transcript.scan_new_lines(path, self.leak_offsets.get(str(path), 0))
            self.leak_offsets[str(path)] = total
            self.leak_hits.extend({**h, "round": round_no} for h in hits)
        self.record["leak_check"] = list(self.leak_hits)

    def analyze(self, round_no: int) -> None:
        """Refresh cost, behavior, validation, and totals; record failures for the completion gate."""
        try:
            mains, sub_dirs = self.transcripts()
            if not self.codex_main:
                self.archive_transcripts(mains)
            self.scan_leaks(mains, sub_dirs, round_no)
        except (transcript.TranscriptError, OSError) as exc:
            self.record["analysis_error"] = str(exc)
            if self.codex_main:
                self.record.update(accounting_incomplete=True, accounting_issues=[str(exc)], validation=None)
                if self.record.get("cost") is not None:
                    self.record["cost"]["accounting_incomplete"] = True
                if self.record.get("totals") is not None:
                    self.record["totals"]["billed_usd"] = None
            return
        analysis.update_record(self.record, mains, sub_dirs, self.resume_indices, self.cfg.gaps_path,
                               self.cfg.seed, arms.agent_models(self.arm_root), self.codex_home,
                               self.subagent_home, self.claude_dir)

    # -- main loop ---------------------------------------------------------
    def run(self, prompt: str) -> None:
        message, session_id = prompt, None
        pack_src = self.cfg.tasks_dir / self.cfg.task_dir / "acceptance"
        if not pack_src.is_dir():
            raise RunError(f"acceptance pack not found: {pack_src}")
        for round_no in range(1, self.cfg.max_rounds + 1):
            entry: dict[str, Any] = {"round": round_no, "kind": "initial" if session_id is None else "resume",
                                     "started_at": _iso(_utc_now()), "started_epoch": time.time(),
                                     "message": message}
            t0 = time.monotonic()
            try:
                result = self.invoke(message, session_id, round_no)
            except RunError as exc:
                status = "usage_limit" if isinstance(exc, UsageLimit) else "cli_error"
                entry.update(wall_time_s=round(time.monotonic() - t0, 3), cli={}, acceptance=None, error=str(exc))
                self.record["rounds"].append(entry)
                self.record.update(status=status, error=str(exc))
                self.analyze_if_possible(round_no)
                return
            entry["wall_time_s"] = round(time.monotonic() - t0, 3)
            entry["ended_epoch"] = time.time()
            if self.codex_main:
                entry["codex_cli"] = {k: result.get(k) for k in (
                    "session_id", "usage", "turns_completed", "errors", "is_error", "result", "_exit_code")}
            else:
                entry["cli"] = {k: result.get(k) for k in (
                    "session_id", "total_cost_usd", "num_turns", "duration_ms", "duration_api_ms",
                    "usage", "modelUsage", "is_error", "result", "subtype", "_exit_code")}
            session_id = str(result["session_id"])
            if not self.session_ids or self.session_ids[-1] != session_id:
                self.session_ids.append(session_id)
            self.record["rounds"].append(entry)
            self.record["rounds_used"] = round_no
            if self.codex_main:
                self.record["session_ids"] = list(self.session_ids)
            self.analyze(round_no)
            self.save()
            if result.get("is_error") or result["_exit_code"] != 0:
                agent = "codex" if self.codex_main else "claude"
                text = f"{agent} reported an error in round {round_no} (exit {result['_exit_code']}): {result.get('result')}"
                self.record.update(status="usage_limit" if result.get("_usage_limit") else "cli_error", error=text)
                return
            if self.codex_invalidated(round_no):
                return
            if self.codex_main:
                if self.record.get("analysis_error") or self.record.get("accounting_incomplete"):
                    self.record.update(status="accounting_error", error=self.record.get("analysis_error") or
                                       "; ".join(self.record["accounting_issues"]))
                    return
                validation = self.record.get("validation")
                if not validation or not validation["ok"]:
                    self.record.update(status="validation_error", error=f"round {round_no}: model/tool validation failed")
                    return
            # The next resume's first request is the one after those seen so far.
            next_index = self.main_request_count()
            feedback: str | None = None
            try:
                try:
                    outcome = acceptance.run_pack(
                        pack_src, self.run_dir, self.repo, round_no, self.cfg.acceptance_timeout_s,
                        wrap=wrap_netns if self.cfg.netns else None,
                        env=child_env(self.arm_root, include_token=False))
                finally:
                    kill_stray_processes(self.run_dir)
            except acceptance.AcceptanceTimeout as exc:
                # The agent's code was being exercised: a failed round, not an infrastructure error.
                entry["acceptance"] = {"passed": False, "timed_out": True, "error": str(exc), "checks": []}
                feedback = acceptance.GENERIC_TIMEOUT_FEEDBACK
            except acceptance.ContractError as exc:
                entry["acceptance"] = None
                entry["acceptance_error"] = str(exc)
                self.record.update(status="infra_error", error=str(exc))
                return
            else:
                entry["acceptance"] = outcome.to_dict()
                if outcome.passed:
                    self.record.update(status="completed", passed=True)
                    return
                feedback = acceptance.compose_feedback(outcome.failures())
            if round_no == self.cfg.max_rounds:
                self.record.update(status="completed", cap_hit=True)
                return
            message = feedback
            self.resume_indices.append(next_index)
            self.save()
        raise AssertionError("loop exits through a return")

    def main_request_count(self) -> int:
        """The main agent's requests so far, which is the index of the next resume's first request."""
        if self.codex_main:
            assert self.codex_home is not None
            return len(codex.load_requests(self.codex_home, "main", root_threads=self.session_ids))
        mains, _ = self.transcripts()
        return cost.main_request_count(mains)

    def codex_invalidated(self, round_no: int) -> bool:
        """Stop the run (before acceptance) when a Codex or Claude subagent hit its usage limit or failed."""
        behavior = self.record.get("behavior") or {}
        if self.codex_home is None:
            return False
        if not self.codex_main:
            summary, judge = behavior.get("codex"), codex.run_outcome
        elif arms.get_arm(self.cfg.arm).builtin_subagents:
            summary, judge = behavior.get("codex_subagents"), codex.run_outcome
        else:
            summary, judge = behavior.get("claude_agents"), claude_calls.run_outcome
        outcome = None if summary is None else judge(summary)
        if outcome is None:
            return False
        self.record.update(status=outcome[0], error=f"round {round_no}: {outcome[1]}")
        return True

    def analyze_if_possible(self, round_no: int) -> None:
        # A Codex main agent that was stopped mid-round has session logs (and spend) without a reported thread id.
        if self.session_ids or (self.codex_main and self.codex_home is not None
                                and codex.rollout_files(self.codex_home)):
            self.analyze(round_no)


def dry_run(cfg: RunConfig, run_id: str, run_dir: Path, prompt: str) -> None:
    arm = arms.get_arm(cfg.arm)
    repo = run_dir / "repo"
    sample_feedback = "<deterministic feedback composed from failing checks>"
    steps = [
        ["git", "clone", "--quiet", str(cfg.template), str(repo)],
        ["cp", str(cfg.tasks_dir / cfg.task_dir / "spec.md"), str(repo / "TASK.md")],
        ["git", "-C", str(repo), "add", "TASK.md"],
        ["git", "-C", str(repo), "commit", "-m", "Add TASK.md"],
        ["bash", str(repo / "ctf" / "setup_ctf.sh")],
        ["cp", ("codex/" + (", claude/" if arm.claude_agents else "") + " from") if arm.codex_main
         else "settings.json, CLAUDE.md, agents/, codex/ from",
         str(arms.arm_root(cfg.arm, cfg.arms_dir)), "to", str(run_dir / "config")],
    ]
    if arm.uses_codex:
        steps.append(["# build", str(codex.home_for(run_dir)), "from config/codex/, disable bundled skills, check "
                      "the Codex prompt, copy", str(codex.USER_AUTH), "(removed when the run ends)"])
    print(f"# run id: {run_id}\n# run dir: {run_dir}")
    for step in steps:
        print(shlex.join(step))
    allowlist = f"env -i {' '.join(f'{k}=<{k}>' for k in ENV_ALLOWLIST)} "
    if arm.codex_main:
        env = allowlist + f"CODEX_HOME={shlex.quote(str(codex.home_for(run_dir)))}"
        bins = [run_dir / "config" / arms.CLAUDE_DIR / "bin"] if arm.claude_agents else []
        if bins:
            env += f" PATH={shlex.quote(':'.join(map(str, bins)))}:<PATH>"
        if arm.claude_agents:
            env += f" {CREDENTIALS_FILE_ENV}={CREDENTIALS_FILE} {CREDENTIALS_SOURCE_ENV}=<token>"
    else:
        env = (allowlist + " ".join(f"{k}={v}" for k, v in ENV_FIXED.items())
               + f" CLAUDE_CONFIG_DIR={shlex.quote(str(run_dir / 'config'))} {TOKEN_ENV}=<token>")
    if cfg.compact_pct is not None and not arm.codex_main:
        env += f" CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={cfg.compact_pct}"
    codex_arm = arm.uses_codex
    if arm.codex and not arm.codex_main:
        env += (f" CODEX_HOME={shlex.quote(str(codex.home_for(run_dir)))}"
                f" PATH={shlex.quote(str(run_dir / 'config' / arms.CODEX_DIR / 'bin'))}:<PATH>")

    def claude_line(message: str, session: str | None) -> str:
        if arm.codex_main:
            cmd = codex.main_command("<pinned codex>", arm.main_model, arm.main_effort,
                                     codex_developer_instructions(run_id),
                                     run_dir.resolve() / "codex-main-round<N>.last.txt", repo.resolve(), session)
            if cfg.compact_pct is not None:
                option = ["-c", f"model_auto_compact_token_limit={compact_token_limit(cfg.compact_pct)}"]
                insert_at = cmd.index(session) if session is not None else len(cmd) - 1
                cmd[insert_at:insert_at] = option
        else:
            cmd = build_claude_command(cfg, run_id, message, session)
        if cfg.netns:
            cmd = wrap_netns(cmd, hide_run_dir=run_dir,
                             visible=VISIBLE_RUN_DIRS + ((codex.HOME_DIR,) if codex_arm else ()),
                             host_visible=codex.HOST_VISIBLE if codex_arm else (), dns_forward=codex_arm,
                             credentials=arm.claude_agents)
        # A Codex main agent reads the message from standard input.
        feed = f"printf %s {shlex.quote(message)} | " if arm.codex_main else ""
        return f"cd {shlex.quote(str(repo))} && {feed}{env} " + shlex.join(cmd)

    print(claude_line(prompt, None))
    print(f"# then, up to {cfg.max_rounds - 1} times while acceptance fails (pack copied to {run_dir}/acceptance "
          f"without selftest/, results under {run_dir}/results/, copy removed afterwards):")
    pack_cmd = ["bash", f"{run_dir}/acceptance/run.sh", str(repo), f"{run_dir}/results/acceptance-result-roundN.json"]
    print(shlex.join(wrap_netns(pack_cmd) if cfg.netns else pack_cmd))
    print(claude_line(sample_feedback, "<session_id>"))


DEFAULT_ACCEPTANCE_TIMEOUT_S = 2400.0
PACK_TIMEOUT_FILE = "timeout_s"


def pack_timeout_s(pack: Path) -> float | None:
    """The minimum acceptance timeout a pack declares in `timeout_s` (one positive integer,
    optional trailing newline), or None when the pack has no such file."""
    path = pack / PACK_TIMEOUT_FILE
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise RunError(f"cannot read {path}: {exc}") from exc
    if not re.fullmatch(r"[0-9]+\n?", text) or int(text) < 1:
        raise RunError(f"{path} must hold a single positive integer number of seconds, got {text!r}")
    return float(int(text))


def resolve_acceptance_timeout(explicit: float | None, pack: Path) -> float:
    """The explicit --acceptance-timeout, else the pack's declared timeout, else the default.
    An explicit value below the pack's declared minimum is an error."""
    declared = pack_timeout_s(pack)
    if explicit is None:
        return DEFAULT_ACCEPTANCE_TIMEOUT_S if declared is None else declared
    if declared is not None and explicit < declared:
        raise RunError(f"--acceptance-timeout {explicit:g} is below the {declared:g} s that "
                       f"{pack / PACK_TIMEOUT_FILE} declares as this pack's minimum")
    return explicit


def parse_args(argv: Sequence[str] | None) -> tuple[argparse.Namespace, RunConfig]:
    p = argparse.ArgumentParser(description="Run one arm x task x repetition.")
    p.add_argument("--arm", required=True, choices=list(arms.ARMS))
    p.add_argument("--task", required=True, help="task1..task4 or the full directory name under tasks/")
    p.add_argument("--rep", required=True, type=int)
    p.add_argument("--autocompact", type=int, default=None, help="auto-compaction window in tokens (min 100000)")
    p.add_argument("--compact-pct", type=int, default=None,
                   help="auto-compaction threshold percentage (1..100), with arm variant label")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    p.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    p.add_argument("--arms-dir", type=Path, default=arms.DEFAULT_ARMS_DIR)
    p.add_argument("--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR)
    p.add_argument("--gaps", type=Path, default=DEFAULT_GAPS, help="gaps.json from gaps.py")
    p.add_argument("--max-rounds", type=int, default=5,
                   help="agent invocations in total: the initial one plus up to N-1 repair resumes (default 5)")
    p.add_argument("--acceptance-timeout", type=float, default=None,
                   help="seconds per acceptance run (default: the pack's acceptance/timeout_s, else 2400; "
                        "may not be lower than the pack's)")
    p.add_argument("--cli-timeout", type=float, default=21600.0, help="seconds per claude invocation (default 6 hours)")
    p.add_argument("--seed", type=int, default=0, help="seed for simulated pause sampling")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--no-netns", action="store_true",
                   help="do not isolate claude and the acceptance pack in namespaces (debugging)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if a.rep < 1 or a.max_rounds < 1:
        p.error("--rep and --max-rounds must be >= 1")
    if a.autocompact is not None and a.autocompact < 100_000:
        p.error("--autocompact must be at least 100000")
    if a.autocompact is not None and arms.get_arm(a.arm).codex_main:
        p.error("--autocompact configures Claude Code and does not apply to a Codex main agent")
    if a.compact_pct is not None and not 1 <= a.compact_pct <= 100:
        p.error("--compact-pct must be between 1 and 100")
    try:
        short, directory = resolve_task(a.tasks_dir, a.task)
    except RunError as exc:
        p.error(str(exc))
    try:
        acceptance_timeout = resolve_acceptance_timeout(a.acceptance_timeout, a.tasks_dir / directory / "acceptance")
    except RunError as exc:
        p.error(str(exc))
    assert acceptance_timeout > 0
    cfg = RunConfig(a.arm, short, a.rep, a.autocompact, a.template, a.runs_dir, a.arms_dir, a.tasks_dir,
                    a.gaps, a.max_rounds, acceptance_timeout, a.cli_timeout, a.seed,
                    a.claude_bin, directory, not a.no_netns, a.compact_pct)
    return a, cfg


def main(argv: Sequence[str] | None = None) -> int:
    args, cfg = parse_args(argv)
    now = _utc_now()
    prompt_path = cfg.tasks_dir / cfg.task_dir / "prompt.md"
    runner: Runner | None = None
    run_id = "<run-id>"
    try:
        if not prompt_path.is_file():
            raise RunError(f"task prompt not found: {prompt_path}")
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if args.dry_run:
            run_id = "<8-hex-nonce>"
            dry_run(cfg, run_id, cfg.runs_dir / run_id, prompt)
            return 0
        arm = arms.get_arm(cfg.arm)
        if (not arm.codex_main or arm.claude_agents) and not os.environ.get(TOKEN_ENV):
            raise RunError(f"{TOKEN_ENV} is not set; create one with `claude setup-token`")
        if cfg.netns:
            if arm.codex_main:
                check_netns(cfg.runs_dir, "codex", ("python3", *((cfg.claude_bin,) if arm.claude_agents else ())),
                            codex.HOST_VISIBLE, dns_forward=True)
            elif arm.codex:
                check_netns(cfg.runs_dir, cfg.claude_bin, ("codex", "python3"), codex.HOST_VISIBLE, dns_forward=True)
            else:
                check_netns(cfg.runs_dir, cfg.claude_bin)
        if not cfg.template.is_dir():
            raise RunError(f"mission template not found: {cfg.template}; run fixture/build-template.sh")
        cfg.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id(cfg.runs_dir)
        run_dir = cfg.runs_dir / run_id
        run_dir.mkdir()
        append_index(cfg.runs_dir, [run_id, cfg.arm, cfg.task, cfg.rep, "start", _iso(now), "running"])
        runner = Runner(cfg, run_id, run_dir)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, raise_interrupted)
        try:
            runner.init_record(now)
            prepare_repo(cfg, run_dir)
            runner.prepare_codex()
            runner.run(prompt)
        except RunError as exc:
            status = "infra_error" if isinstance(exc, InfraError) else "interrupted" if isinstance(exc, Interrupted) \
                else "error"
            runner.record.update(status=status, error=str(exc))
            raise
        finally:
            runner.release_codex()
            if runner.record:
                runner.record["finished_at"] = _iso(_utc_now())
                runner.save()
            append_index(cfg.runs_dir, [run_id, cfg.arm, cfg.task, cfg.rep, "end", _iso(_utc_now()),
                                        runner.record.get("status", "error")])
    except (RunError, arms.ArmError, OSError) as exc:
        print(f"run: error: {exc}", file=sys.stderr)
        return 1
    rec = runner.record
    print(f"{run_id}: arm={cfg.arm} task={cfg.task} rep={cfg.rep} status={rec['status']} "
          f"passed={rec['passed']} rounds={rec['rounds_used']} cap_hit={rec['cap_hit']}")
    if rec["status"] == "usage_limit":
        return EXIT_USAGE_LIMIT
    return 0 if rec["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
