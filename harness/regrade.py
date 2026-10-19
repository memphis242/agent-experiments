#!/usr/bin/env python3
"""Re-grade the rounds of completed runs with a (fixed) acceptance pack and compare verdicts.

For every run of the task in a runs dir with at least one round that has an acceptance result, each such
round's graded commit is checked out into a scratch clone and the given pack runs on it the way the
harness runs it (acceptance.run_pack inside wrap_netns, allowlisted environment without the token). The
run repositories are only read. The output directory gets regrade.json (everything) and regrade.tsv (one
line per round and check, old and new verdict) and a summary of every verdict change is printed.

The graded commit of a round is HEAD at the end of the round's agent session (rounds[].ended_epoch),
taken from the HEAD reflog, else the last commit made by then. The pack graded the working tree, which
may have held uncommitted changes; the final round's are visible and are flagged as `dirty_final`.
A round in which HEAD did not move (`no_commit_in_round`) may have been graded on uncommitted work the
repository no longer holds; a verdict difference in such a round is reported as `unreproducible`, not as
a change. Every finished round is also appended to regrade.jsonl as it completes, and rounds already in
that file are skipped, so an interrupted re-grade resumes where it stopped.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import acceptance
import run

# A commit made within this many seconds after ended_epoch still belongs to the round (clock granularity).
COMMIT_SLACK_S = 2.0


@dataclass(frozen=True)
class Job:
    run_id: str
    run_dir: Path
    arm: str
    rep: int
    round_no: int
    started_epoch: float
    ended_epoch: float
    old: dict[str, Any]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo} failed: {done.stderr.strip()}")
    return done.stdout


def graded_commit(repo: Path, ended_epoch: float) -> tuple[str, str]:
    """(sha, source) of HEAD at ended_epoch: the HEAD reflog when it covers that time, else commit times.
    Both listings are newest first, so the first entry not after ended_epoch wins (ties keep log order)."""
    for line in git(repo, "reflog", "show", "--date=unix", "--format=%H %gd", "HEAD").splitlines():
        sha, _, selector = line.partition(" ")
        if selector.startswith("HEAD@{") and selector.endswith("}") \
                and float(selector[6:-1]) <= ended_epoch + COMMIT_SLACK_S:
            return sha, "reflog"
    for line in git(repo, "log", "--format=%H %ct", "HEAD").splitlines():
        sha, t = line.split()
        if float(t) <= ended_epoch + COMMIT_SLACK_S:
            return sha, "commit time"
    raise RuntimeError(f"no commit in {repo} at or before {ended_epoch}")


def collect_jobs(runs_dir: Path, task: str, only: Sequence[str]) -> list[Job]:
    """Rounds with an acceptance result; `only` holds run IDs ("ID", every round) or "ID:ROUND"."""
    wanted: dict[str, set[int] | None] = {}
    for item in only:
        rid, _, rnd = item.partition(":")
        if rnd:
            prev = wanted.get(rid, set())
            wanted[rid] = None if prev is None else prev | {int(rnd)}
        else:
            wanted[rid] = None
    jobs: list[Job] = []
    for path in sorted(runs_dir.glob("*/run.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue
        rid = path.parent.name
        if record.get("task") != task or (wanted and rid not in wanted):
            continue
        rounds = wanted.get(rid)
        for rnd in record.get("rounds", []):
            if rounds is not None and rnd.get("round") not in rounds:
                continue
            if isinstance(rnd.get("acceptance"), dict) and "ended_epoch" in rnd:
                jobs.append(Job(path.parent.name, path.parent, str(record.get("arm")), int(record.get("rep", 0)),
                                int(rnd["round"]), float(rnd["started_epoch"]), float(rnd["ended_epoch"]),
                                rnd["acceptance"]))
    return jobs


def prepare_checkout(job: Job, dest: Path) -> tuple[str, str, bool, bool]:
    """Clone the run's repo into dest at the round's graded commit, with the run's CTF set-up beside it.
    Returns (sha, source, dirty_final, no_commit_in_round)."""
    repo = job.run_dir / "repo"
    sha, source = graded_commit(repo, job.ended_epoch)
    assert job.started_epoch <= job.ended_epoch, job
    no_commit = graded_commit(repo, job.started_epoch)[0] == sha
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(repo), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "--quiet", sha],
                   check=True)
    ctf = repo / "ctf"
    if (ctf / "CTF" / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", "--shared", str(ctf / "CTF"), str(dest / "ctf" / "CTF")],
                       check=True)
        head = git(ctf / "CTF", "rev-parse", "HEAD").strip()
        subprocess.run(["git", "-C", str(dest / "ctf" / "CTF"), "-c", "advice.detachedHead=false", "checkout",
                        "--quiet", head], check=True)
    if (ctf / "venv").is_dir():
        (dest / "ctf" / "venv").symlink_to(ctf / "venv")
    dirty = bool(git(repo, "status", "--porcelain").strip()) and sha == git(repo, "rev-parse", "HEAD").strip()
    return sha, source, dirty, no_commit


def verdicts(result: dict[str, Any] | None) -> dict[str, bool]:
    return {c["id"]: bool(c["passed"]) for c in (result or {}).get("checks", [])}


def regrade(job: Job, pack: Path, scratch: Path, timeout_s: float) -> dict[str, Any]:
    work = scratch / f"{job.run_id}-r{job.round_no}"
    entry: dict[str, Any] = {"run": job.run_id, "arm": job.arm, "rep": job.rep, "round": job.round_no,
                             "old_passed": job.old.get("passed"), "old_timed_out": bool(job.old.get("timed_out")),
                             "old_checks": verdicts(job.old)}
    try:
        work.mkdir(parents=True, exist_ok=True)
        sha, source, dirty, no_commit = prepare_checkout(job, work / "repo")
        entry.update(commit=sha, commit_source=source, dirty_final=dirty, no_commit_in_round=no_commit)
        env = run.child_env(work / "config", include_token=False)
        assert run.TOKEN_ENV not in env
        result = acceptance.run_pack(pack, work, work / "repo", job.round_no, timeout_s, wrap=run.wrap_netns,
                                     env=env)
        new = result.to_dict()
        entry.update(new_passed=new["passed"], new_checks=verdicts(new),
                     new_feedback={c["id"]: c["feedback"] for c in new["checks"] if not c["passed"]},
                     new_detail={c["id"]: c["detail"] for c in new["checks"] if c["detail"]})
    except acceptance.AcceptanceTimeout as exc:
        entry.update(new_passed=False, new_timed_out=True, error=str(exc))
    except (acceptance.ContractError, RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        entry.update(error=f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work / "repo", ignore_errors=True)
    old, new_checks = entry["old_checks"], entry.get("new_checks", {})
    entry["changes"] = [{"id": cid, "old": old.get(cid), "new": new_checks.get(cid)}
                        for cid in sorted(set(old) | set(new_checks)) if old.get(cid) != new_checks.get(cid)]
    entry["verdict_changed"] = "error" in entry or entry.get("new_passed") != entry["old_passed"]
    # Without a commit in the round the pack may have graded work the repository no longer holds.
    entry["unreproducible"] = bool(entry.get("no_commit_in_round")) and bool(entry["changes"]) and "error" not in entry
    return entry


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Re-grade completed runs with an acceptance pack; compare verdicts.")
    p.add_argument("--runs-dir", type=Path, required=True)
    p.add_argument("--task", required=True, help="the run.json task, for example task3")
    p.add_argument("--pack", type=Path, required=True, help="the acceptance pack directory to grade with")
    p.add_argument("--out", type=Path, required=True, help="output directory (regrade.json, regrade.tsv)")
    p.add_argument("--scratch", type=Path, default=Path("/workspace/tmp-regrade"),
                   help="scratch directory for the checkouts and pack results (default /workspace/tmp-regrade)")
    p.add_argument("--run", nargs="*", default=[], help="only these runs: ID (every round) or ID:ROUND")
    p.add_argument("--parallel", type=int, default=8)
    p.add_argument("--timeout", type=float, default=None,
                   help="seconds per pack run (default: the pack's timeout_s, else the harness default)")
    a = p.parse_args(argv)
    if a.parallel < 1:
        p.error("--parallel must be at least 1")
    if not (a.pack / "run.sh").is_file():
        p.error(f"{a.pack} has no run.sh")
    timeout_s = run.resolve_acceptance_timeout(a.timeout, a.pack)
    run.check_netns(a.scratch, claude_bin="git")
    jobs = collect_jobs(a.runs_dir, a.task, a.run)
    if not jobs:
        p.error(f"no {a.task} rounds with an acceptance result under {a.runs_dir}")
    a.out.mkdir(parents=True, exist_ok=True)
    a.scratch.mkdir(parents=True, exist_ok=True)
    journal = a.out / "regrade.jsonl"
    finished: dict[tuple[str, int], dict[str, Any]] = {}
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done_entry = json.loads(line)
                finished[(done_entry["run"], done_entry["round"])] = done_entry
    todo = [j for j in jobs if (j.run_id, j.round_no) not in finished]
    print(f"re-grading {len(todo)} rounds of {len({j.run_id for j in todo})} runs, {a.parallel} at a time "
          f"({len(jobs) - len(todo)} already in {journal})", flush=True)
    lock = threading.Lock()

    def one(job: Job) -> dict[str, Any]:
        entry = regrade(job, a.pack.resolve(), a.scratch.resolve(), timeout_s)
        with lock:
            mark = ("UNREPRODUCIBLE" if entry["unreproducible"] else
                    "CHANGED" if entry["verdict_changed"] or entry["changes"] else "same")
            print(f"{job.run_id} round {job.round_no}: {entry['old_passed']} -> {entry.get('new_passed')} {mark} "
                  f"{entry['changes'] or ''} {entry.get('error', '')}", flush=True)
            with journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        return entry

    with ThreadPoolExecutor(max_workers=a.parallel) as pool:
        new_entries = list(pool.map(one, todo))
    wanted = {(j.run_id, j.round_no) for j in jobs}
    entries = [e for k, e in finished.items() if k in wanted] + new_entries
    assert len(entries) == len(jobs)
    entries.sort(key=lambda e: (e["run"], e["round"]))
    (a.out / "regrade.json").write_text(json.dumps({"pack": str(a.pack.resolve()), "rounds": entries}, indent=2)
                                        + "\n", encoding="utf-8")
    with open(a.out / "regrade.tsv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["run", "arm", "rep", "round", "commit", "check", "old", "new", "changed"])
        for e in entries:
            old, new = e["old_checks"], e.get("new_checks", {})
            for cid in sorted(set(old) | set(new)) or ["(none)"]:
                w.writerow([e["run"], e["arm"], e["rep"], e["round"], e.get("commit", ""), cid, old.get(cid, ""),
                            new.get(cid, e.get("error", "")), int(old.get(cid) != new.get(cid))])
    unreproducible = [e for e in entries if e.get("unreproducible")]
    changed = [e for e in entries if (e["verdict_changed"] or e["changes"]) and not e.get("unreproducible")]
    print(f"{len(changed)} of {len(entries)} rounds changed a verdict; details in {a.out / 'regrade.json'}")
    print(f"{len(unreproducible)} rounds without a commit graded differently (likely uncommitted work, "
          f"unreproducible): " + ", ".join(f"{e['run']} round {e['round']}" for e in unreproducible))
    for e in changed:
        print(f"  {e['run']} round {e['round']}: passed {e['old_passed']} -> {e.get('new_passed')}; "
              + ", ".join(f"{c['id']} {c['old']}->{c['new']}" for c in e["changes"]) + f" {e.get('error', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
