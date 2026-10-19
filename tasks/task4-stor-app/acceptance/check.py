#!/usr/bin/env python3
"""Acceptance checks for the STOR storage management application.

Usage: check.py <repo-path> <out.json>

The repository is copied to a scratch directory and built there; the original
is never modified. Exit status: 0 every check passed, 1 at least one failed,
2 infrastructure error found before the agent's code was used (bad arguments,
tooling missing, scratch space unavailable). Every fault after that point,
including running out of time, is reported as failed checks.

Feedback names each failing behaviour in one sentence; the detail keeps every
deviation found, for the experimenter.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PACK = Path(__file__).resolve().parent
sys.path.insert(0, str(PACK))

import stor_live as sl  # noqa: E402
from stor_live import Fail, Problems, SharedFail  # noqa: E402

# A coverage figure: a percentage ("97.5%", "100 percent") or covered/total lines ("44 / 44", "176 of 176").
COVERAGE_FIGURE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|percent\b)|\b\d+\s*(?:/|of)\s*\d+\b", re.IGNORECASE)

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# The harness allows 2400 s; run.sh stops this script a little after the budget, and the budget leaves
# room to write the result file.
TOTAL_BUDGET_S = 36 * 60
# STOR_PACK_JOBS lets pack self-tests on a shared machine build with fewer jobs; the harness never sets it.
JOBS = f"-j{os.environ.get('STOR_PACK_JOBS') or os.cpu_count() or 16}"
SOURCE_PCT_MIN = 95.0
# A check is started only with at least this much budget left; otherwise it is reported as not verified.
MIN_CHECK_BUDGET_S = 45

# -- table geometry for the injected-packet scenarios -------------------------------
# Packets are 700 bytes so that a 4096-byte packet file holds five of them
# (64-byte header + 5 * 700 = 3564) and the sixth forces a roll-over.
PKT = 700
FILE_FULL = 64 + 5 * PKT

NOT_VERIFIED_TIME = ("This requirement could not be verified because the review ran out of time: the build, the unit "
                     "tests, the cFS startups with STOR or the CTF test took far longer than expected, so check that "
                     "none of them hangs.")


class Terminated(BaseException):
    """SIGTERM arrived (the outer time limit); unwinds through every check so the result file is still written."""


def quiet_table(**kw: object) -> sl.TableSpec:
    base = dict(telemetry_quota=65536, file_quota=1048576, max_packet_file=1024, threshold=100,
                entries=((sl.INJ_A, 10, 1),))
    base.update(kw)
    return sl.TableSpec(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    feedback: str
    fn: Callable[["Ctx"], None]


@dataclass
class UnitTests:
    """Outcome of the mission's unit-test run (see run_unit_tests)."""
    ran: bool = False
    log: str = ""
    timed_out: bool = False
    names: list[str] = field(default_factory=list)
    component: dict[str, str] = field(default_factory=dict)
    failed_first: list[str] = field(default_factory=list)
    failed_final: list[str] = field(default_factory=list)
    touched: set[str] | None = None


class Ctx:
    def __init__(self, repo: Path, work: Path, deadline: float) -> None:
        self.repo = repo
        self.work = work
        self.tree = work / "tree"
        self.deadline = deadline
        self.stor = self.tree / "apps" / "stor"
        self.build_ok: bool | None = None
        self.build_log = ""
        self.warnings: list[str] = []
        self.coverage: dict[str, tuple[int, int]] = {}
        self.unit: UnitTests | None = None
        self.cfs: sl.Cfs | None = None
        self.session_key: str | None = None
        self.pristine_table: bytes | None = None
        self.start_feedback_seen: set[str] = set()
        self.unit_note = ""

    # -- time budget -------------------------------------------------------------
    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def timeout_for(self, want: float) -> float:
        left = self.remaining() - 20
        if left < 30:
            raise Fail("the time budget ran out during this check", NOT_VERIFIED_TIME)
        return min(want, left)

    # -- repository copy and build ---------------------------------------------------
    def copy_tree(self) -> None:
        if self.tree.exists():
            return
        skip = shutil.ignore_patterns(".git", "build-native_std", "results", "__pycache__", "*.pyc")
        shutil.copytree(self.repo, self.tree, ignore=skip, symlinks=True)

    def run(self, cmd: list[str], timeout: float, env: dict[str, str] | None = None,
            cwd: Path | None = None) -> tuple[int, str, bool]:
        """(exit status, combined output, timed out); never raises for a slow command."""
        try:
            proc = subprocess.run(cmd, cwd=cwd or self.tree, text=True, errors="replace", capture_output=True,
                                  timeout=self.timeout_for(timeout), env=env)
            return proc.returncode, proc.stdout + proc.stderr, False
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            out = out.decode(errors="replace") if isinstance(out, bytes) else out
            err = exc.stderr or ""
            err = err.decode(errors="replace") if isinstance(err, bytes) else err
            return -1, out + err, True

    def sh(self, cmd: list[str], timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Run a command in the tree; a command that exceeds its timeout fails the current check."""
        try:
            return subprocess.run(cmd, cwd=self.tree, text=True, errors="replace", capture_output=True,
                                  timeout=self.timeout_for(timeout), env=env)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            out = out.decode(errors="replace") if isinstance(out, bytes) else out
            raise Fail(f"`{' '.join(cmd)}` did not finish within {timeout:.0f} s; output tail: {out[-1500:]}",
                       f"`{' '.join(cmd)}` must finish; it did not complete within {timeout:.0f} s in this tree.") from None

    def build(self) -> None:
        """Configure and build once; later calls only report the stored outcome."""
        if self.build_ok is not None:
            if not self.build_ok:
                raise SharedFail("the mission does not configure and build: " + self.build_log[-3000:], BUILD_FEEDBACK)
            return
        self.copy_tree()
        rc, log, slow = self.run(["make", "native_std.prep"], 300)
        ok = rc == 0
        if ok:
            rc, out, slow = self.run(["make", JOBS, "native_std.install"], 900)
            log += out
            ok = rc == 0
        self.build_ok = ok
        self.build_log = log
        self.warnings = [ln for ln in log.splitlines() if "warning:" in ln]
        if not ok:
            lines = [ln for ln in log.splitlines() if "error" in ln.lower()]
            self.build_log = ("(timed out) " if slow else "") + ("\n".join(lines[-40:]) or log[-3000:])
            raise SharedFail("the mission does not configure and build: " + self.build_log[-3000:], BUILD_FEEDBACK)

    # -- cFS sessions -----------------------------------------------------------------
    def table_file(self) -> bytes:
        if self.pristine_table is None:
            self.build()
            p = self.tree / "build-native_std" / "exe" / "cpu1" / "cf" / "stor_tbl.tbl"
            if not p.is_file():
                raise SharedFail("the build does not install stor_tbl.tbl",
                           "The build must install the StorageTable image as /cf/stor_tbl.tbl "
                           "(build-native_std/exe/cpu1/cf/stor_tbl.tbl).")
            self.pristine_table = p.read_bytes()
        return self.pristine_table

    def session(self, key: str, table: sl.TableSpec | None = None, seed: dict[str, bytes] | None = None,
                reuse: bool = False) -> sl.Cfs:
        self.build()
        image = self.table_file()
        if len(image) != sl.TABLE_FILE_HDR + sl.TABLE_NAME_SIZE:
            raise SharedFail(f"the installed StorageTable image is {len(image)} bytes",
                       f"The installed StorageTable image must hold the {sl.TABLE_NAME_SIZE}-byte "
                       f"STOR_StorageTable_t defined in the specification; it holds "
                       f"{len(image) - sl.TABLE_FILE_HDR} bytes of table data.")
        self.timeout_for(60)
        if self.cfs is None:
            self.cfs = sl.Cfs(self.tree, image)
        if reuse and self.session_key == key and self.cfs.running and self.cfs.alive():
            return self.cfs
        self.session_key = None
        self.cfs.start(table, seed=seed)
        self.session_key = key
        return self.cfs

    def shutdown(self) -> None:
        if self.cfs is not None:
            self.cfs.stop()
            self.cfs = None
        self.session_key = None


BUILD_FEEDBACK = ("The mission must configure with make native_std.prep and build with make native_std.install; the "
                  "build currently stops with errors.")


def wait_event(cfs: sl.Cfs, mark: int, eid: int, timeout: float = 4.0, count: int = 1) -> list[tuple[int, str]]:
    end = time.monotonic() + timeout
    got = cfs.events(mark, eid)
    while len(got) < count and time.monotonic() < end:
        time.sleep(0.2)
        got = cfs.events(mark, eid)
    return got


def disk_sum(cfs: sl.Cfs) -> int:
    return sum(f.size for f in cfs.packet_files())


def used_matches_disk(cfs: sl.Cfs) -> tuple[sl.Hk, int, int]:
    """Housekeeping plus the packet-file bytes on disk before and after it was taken."""
    before = disk_sum(cfs)
    hk = cfs.hk()
    after = disk_sum(cfs)
    return hk, before, after


# ============================================================================
# static checks
# ============================================================================

def strip_c_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def c_app_registered(ctx: Ctx) -> None:
    ctx.copy_tree()
    t = ctx.tree
    p = Problems()
    stor = t / "apps" / "stor"
    if not p.check(stor.is_dir(), "apps/stor does not exist", "STOR must exist as a new application in apps/stor."):
        p.raise_if()
    for sub in ("fsw/src", "fsw/inc", "fsw/tables", "config", "unit-test"):
        p.check((stor / sub).is_dir(), f"apps/stor/{sub} is missing", f"apps/stor must contain the directory {sub}.")
    srcs = list((stor / "fsw" / "src").glob("*.c")) if (stor / "fsw" / "src").is_dir() else []
    p.check(bool(srcs), "apps/stor/fsw/src has no C sources", "apps/stor/fsw/src must hold STOR's C sources.")
    text = "\n".join(s.read_text(errors="replace") for s in srcs)
    p.check("STOR_AppMain" in text, "no STOR_AppMain entry point", "STOR's entry point must be STOR_AppMain.")
    for hdr in ("stor_topicids.h", "stor_eventids.h", "stor_fcncodes.h"):
        p.check((stor / "fsw" / "inc" / hdr).is_file(), f"fsw/inc/{hdr} is missing", f"apps/stor/fsw/inc/{hdr} must exist.")
    for cfg in ("default_stor_tbldefs.h", "default_stor_fcncode_values.h"):
        p.check((stor / "config" / cfg).is_file(), f"config/{cfg} is missing", f"apps/stor/config/{cfg} must exist.")
    p.check((stor / "fsw" / "tables" / "stor_tbl.c").is_file(), "fsw/tables/stor_tbl.c is missing",
            "The default table source apps/stor/fsw/tables/stor_tbl.c must exist.")
    targets = t / "sample_defs" / "targets.cmake"
    listed = False
    if targets.is_file():
        for line in targets.read_text(errors="replace").splitlines():
            code = line.split("#", 1)[0]
            if "MISSION_GLOBAL_APPLIST" in code and "STATIC" not in code and re.search(r"(?<![\w-])stor(?![\w-])", code):
                listed = True
    p.check(listed, "stor is not in MISSION_GLOBAL_APPLIST in sample_defs/targets.cmake",
            "stor must be listed in MISSION_GLOBAL_APPLIST in sample_defs/targets.cmake.")
    osc = t / "sample_defs" / "default_osconfig.cmake"
    m = re.search(r"^\s*set\(\s*OSAL_CONFIG_MAX_FILE_NAME\s+(\d+)\s*\)", osc.read_text(errors="replace") if osc.is_file() else "", re.M)
    p.check(m is not None and int(m.group(1)) >= 32,
            "OSAL_CONFIG_MAX_FILE_NAME is not set to 32 in sample_defs/default_osconfig.cmake",
            "OSAL_CONFIG_MAX_FILE_NAME must be set to 32 in sample_defs/default_osconfig.cmake (packet file names "
            "are 21 characters).")
    sch = t / "sample_defs" / "tables" / "sch_lab_table.c"
    if p.check(sch.is_file(), "sample_defs/tables/sch_lab_table.c missing",
               "sample_defs/tables/sch_lab_table.c must schedule STOR_SEND_HK_MID and STOR_WAKEUP_MID."):
        s = strip_c_comments(sch.read_text(errors="replace"))
        for name, period, what in (("STOR_SEND_HK_MID", 49, "every 4.9 s"), ("STOR_WAKEUP_MID", 10, "every 1.0 s")):
            # The entry's offset (third field) is free.
            m = re.search(r"\{\s*CFE_SB_MSGID_WRAP_VALUE\(\s*" + name + r"\s*\)\s*,\s*(\d+)\s*,\s*(\d+)\s*\}", s)
            behaviour = f"sample_defs/tables/sch_lab_table.c must send {name} {what} (period {period})."
            if p.check(m is not None, f"sch_lab_table.c has no {name} entry", behaviour):
                p.check(int(m.group(1)) == period, f"sch_lab_table.c {name} period {m.group(1)}, expected {period}",
                        behaviour)
    to = t / "sample_defs" / "tables" / "to_lab_sub.c"
    if p.check(to.is_file(), "sample_defs/tables/to_lab_sub.c missing",
               "sample_defs/tables/to_lab_sub.c must forward STOR_HK_TLM_MID, STOR_LIST_TLM_MID and STOR_STATS_TLM_MID."):
        s = strip_c_comments(to.read_text(errors="replace"))
        for name, need in (("STOR_HK_TLM_MID", 1), ("STOR_LIST_TLM_MID", 4), ("STOR_STATS_TLM_MID", 1)):
            m = re.search(r"\{\s*CFE_SB_MSGID_WRAP_VALUE\(\s*" + name + r"\s*\)\s*,\s*\{[^}]*\}\s*,\s*(\d+)\s*\}", s)
            loose = re.search(r"\{\s*CFE_SB_MSGID_WRAP_VALUE\(\s*" + name + r"\s*\)", s)
            behaviour = (f"sample_defs/tables/to_lab_sub.c must forward {name} with buffer limit {need}"
                         + (" so that a whole listing reaches the ground." if need > 1 else "."))
            if p.check(loose is not None, f"to_lab_sub.c has no {name} entry", behaviour) and m is not None:
                p.check(int(m.group(1)) >= need, f"to_lab_sub.c {name} buffer limit {m.group(1)}, expected {need}",
                        behaviour)
    p.raise_if()


def c_build_no_warnings(ctx: Ctx) -> None:
    ctx.build()
    if ctx.warnings:
        p = Problems()
        for w in ctx.warnings:
            p.check(False, w, "The mission build must produce no compiler warnings; it reports " +
                    f"{len(ctx.warnings)}, the first being: {ctx.warnings[0].strip()[:300]}")
        p.raise_if()


# ============================================================================
# unit tests and coverage
# ============================================================================

CFE_MODULE_DIRS = {"config", "es", "evs", "fs", "msg", "resourceid", "sb", "sbr", "tbl", "time", "core_api",
                   "core_private", "cfe_testcase", "cfe_assert"}


def test_component(command: str, build: Path) -> str:
    """The source component a unit test belongs to: apps/<name>, osal, psp or cfe."""
    try:
        rel = Path(command).resolve().relative_to((build / "native").resolve()).parts
    except ValueError:
        return "unknown"
    parts = rel[1:] if rel else ()
    if len(parts) >= 2 and parts[0] == "apps":
        return f"apps/{parts[1]}"
    if parts and parts[0] in ("osal", "psp"):
        return parts[0]
    return "cfe"


def agent_changed_paths(repo: Path) -> set[str] | None:
    """Paths the agent added or changed relative to the repository's first commit (the mission template)."""
    git = ["git", "--no-optional-locks", "-C", str(repo)]
    try:
        roots = subprocess.run(git + ["rev-list", "--max-parents=0", "HEAD"], text=True, capture_output=True,
                               timeout=60).stdout.split()
        if not roots:
            return None
        diff = subprocess.run(git + ["diff", "--name-only", roots[-1]], text=True, capture_output=True, timeout=120)
        new = subprocess.run(git + ["ls-files", "--others", "--exclude-standard"], text=True, capture_output=True,
                             timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if diff.returncode != 0 or new.returncode != 0:
        return None
    return {ln.strip() for ln in (diff.stdout + new.stdout).splitlines() if ln.strip()}


def component_touched(component: str, changed: set[str] | None) -> bool | None:
    if changed is None:
        return None
    prefix = {"cfe": "cfe/", "osal": "osal/", "psp": "psp/"}.get(component, component + "/")
    return any(path.startswith(prefix) for path in changed)


def run_unit_tests(ctx: Ctx) -> UnitTests:
    """Run every unit test once in parallel (keep going past failures), then each failed test once more serially.

    OSAL's functional tests share a working directory and can collide on its mount point when run in parallel, so
    a failure that does not repeat in the serial pass is not counted. Later callers reuse the outcome.
    """
    if ctx.unit is not None:
        return ctx.unit
    ctx.build()
    ut = UnitTests()
    ctx.unit = ut
    rc, log, slow = ctx.run(["make", JOBS, "-k", "native_std.runtest"], 900)
    ut.log, ut.timed_out = log, slow
    build = ctx.tree / "build-native_std"
    listing = build / "test-list.json"
    if not listing.is_file():
        return ut
    try:
        tests = json.loads(listing.read_text())
    except json.JSONDecodeError:
        return ut
    ut.ran = True
    for t in tests:
        name = str(t.get("name", ""))
        if name:
            ut.names.append(name)
            ut.component[name] = test_component(str(t.get("command") or ""), build)
    results = build / "test-results"
    ut.failed_first = [n for n in ut.names if not (results / f"{n}.log").is_file()]
    if ut.failed_first and not slow:
        rc2, log2, slow2 = ctx.run(["make", "-j1", "-k", "native_std.runtest"], 600)
        ut.log += "\n--- serial retry of failed tests ---\n" + log2
        ut.timed_out = slow2
    ut.failed_final = [n for n in ut.names if not (results / f"{n}.log").is_file()]
    if ut.failed_final:
        ut.touched = agent_changed_paths(ctx.repo)
    return ut


def stor_test(name: str) -> bool:
    return name.startswith("coverage-stor-")


def test_output(ctx: Ctx, name: str) -> str:
    results = ctx.tree / "build-native_std" / "test-results"
    for cand in (results / f"{name}.log", results / f"{name}.log.tmp"):
        if cand.is_file():
            return cand.read_text(errors="replace")
    return ""


def c_unit_tests_pass(ctx: Ctx) -> None:
    ut = run_unit_tests(ctx)
    p = Problems()
    if not ut.ran:
        tail = "\n".join(ln for ln in ut.log.splitlines() if "rror" in ln)[-2000:] or ut.log[-2000:]
        p.check(False, f"make native_std.runtest did not get to running tests{' (timed out)' if ut.timed_out else ''}: {tail}",
                "make native_std.runtest must build the unit tests and run them; it stops before running any test.")
        p.raise_if()
    flaky = [n for n in ut.failed_first if n not in ut.failed_final]
    excused: list[str] = []
    for name in ut.failed_final:
        comp = ut.component.get(name, "unknown")
        out = test_output(ctx, name)
        fails = re.findall(r"^\[ ?FAIL\].*$", out, flags=re.M)
        what = f"{name} ({comp}): " + (" | ".join(fails) if fails else "exited with an error or did not finish; output tail: "
                                       + out[-400:].replace("\n", " | "))
        if stor_test(name):
            p.check(False, what, f"STOR's unit test {name} must pass under make native_std.runtest; it "
                    + ("reports failing assertions." if fails else "exits with an error or does not finish."))
            continue
        touched = component_touched(comp, ut.touched)
        if touched:
            p.check(False, what, f"The unit test {name} of {comp}, which your changes touch, must still pass under "
                    f"make native_std.runtest; it fails.")
        else:
            excused.append(what)
    if ut.timed_out:
        p.check(False, "make native_std.runtest did not finish within its time limit; tests without a result: "
                + ", ".join(ut.failed_final),
                "make native_std.runtest must finish; a unit test does not terminate"
                + (f" (no result for {', '.join(n for n in ut.failed_final if stor_test(n))})."
                   if any(stor_test(n) for n in ut.failed_final) else "."))
    notes: list[str] = []
    if flaky:
        notes.append("failed in the parallel run, passed serially: " + ", ".join(flaky))
    if excused:
        notes.append(("failing tests of components the agent did not change (not counted): " if ut.touched is not None
                      else "failing non-STOR tests, not counted because the agent's changes could not be determined: ")
                     + " || ".join(excused))
    ctx.unit_note = "; ".join(notes)
    if notes:
        print(f"[unit-tests-pass] note: {ctx.unit_note}", flush=True)
    if p.items:
        if notes:
            p.items.append((f"[{ctx.unit_note}]", ""))  # detail only
        p.raise_if()


def c_stor_coverage_tests(ctx: Ctx) -> None:
    ctx.build()
    srcs = sorted((ctx.stor / "fsw" / "src").glob("*.c"))
    if not srcs:
        raise Fail("apps/stor has no sources", "apps/stor/fsw/src must hold STOR's C sources.")
    ut = run_unit_tests(ctx)
    p = Problems()
    stor_names = sorted(n for n in ut.names if stor_test(n) and n != "coverage-stor-hidden")
    p.check(bool(stor_names), "make native_std.runtest runs no coverage-stor-* test",
            "make native_std.runtest must build and run STOR's coverage tests (apps/stor/unit-test, registered "
            "with add_cfe_coverage_test as sample_app does).")
    for s in srcs:
        if "eds" in s.stem:
            continue
        p.check(f"coverage-stor-{s.stem}" in stor_names, f"no coverage test for module {s.stem}",
                f"STOR's source module {s.stem}.c must have its own coverage test "
                f"(apps/stor/unit-test/coveragetest/coveragetest_{s.stem}.c) run by make native_std.runtest.")
    for name in stor_names:
        text = test_output(ctx, name)
        fails = sum(int(f) for f in re.findall(r"FAIL::(\d+)", text))
        passes = sum(int(x) for x in re.findall(r"PASS::(\d+)", text))
        fail_lines = re.findall(r"^\[ ?FAIL\].*$", text, flags=re.M)
        p.check(fails == 0 and not fail_lines, f"{name}: {fails} failing assertions: " + " | ".join(fail_lines),
                f"STOR's coverage test {name} must pass; it reports failing assertions.")
        p.check(passes > 0 or name in ut.failed_final, f"{name}: no passing assertions",
                f"STOR's coverage test {name} must exercise its module with passing assertions; it reports none.")
        if name in ut.failed_final and fails == 0 and not fail_lines:
            p.check(False, f"{name}: exited with an error or did not finish; tail: {text[-400:]}",
                    f"STOR's coverage test {name} must run to completion; it exits with an error or does not finish.")
    p.raise_if()


def gcov_line_coverage(tree: Path) -> dict[str, tuple[int, int]]:
    """Per STOR source file: (executed lines, instrumented lines), the union over every STOR coverage test object."""
    base = tree / "build-native_std" / "native"
    gcdas = sorted(g for g in base.rglob("coverage-stor-*-object.dir/**/*.gcda")
                   if "coverage-stor-hidden-object.dir" not in str(g))
    if not gcdas:
        raise Fail("no coverage data (.gcda) found for the STOR sources after running the unit tests",
                   "Running STOR's coverage tests under make native_std.runtest must produce gcov coverage data for "
                   "the STOR sources; none was found.")
    src_dir = (tree / "apps" / "stor" / "fsw" / "src").resolve()
    lines: dict[str, dict[int, int]] = {}
    gcov_errors: list[str] = []
    for gcda in gcdas:
        try:
            proc = subprocess.run(["gcov", "--json-format", "--stdout", gcda.name], cwd=gcda.parent, text=True,
                                  errors="replace", capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            gcov_errors.append(f"{gcda}: gcov timed out")
            continue
        if proc.returncode != 0:
            gcov_errors.append(f"{gcda}: {(proc.stdout + proc.stderr)[-300:]}")
            continue
        out = proc.stdout
        dec = json.JSONDecoder()
        pos = 0
        while pos < len(out):  # gcov may print several JSON documents
            while pos < len(out) and out[pos].isspace():
                pos += 1
            if pos >= len(out):
                break
            try:
                doc, pos = dec.raw_decode(out, pos)
            except json.JSONDecodeError as exc:
                gcov_errors.append(f"{gcda}: unreadable gcov output ({exc})")
                break
            for f in doc.get("files", []):
                path = Path(f["file"])
                if not path.is_absolute():
                    path = gcda.parent / path
                try:
                    path = path.resolve()
                except OSError:
                    continue
                if path.parent != src_dir or path.suffix != ".c":
                    continue
                counts = lines.setdefault(path.name, {})
                for ln in f.get("lines", []):
                    n = int(ln["line_number"])
                    counts[n] = max(counts.get(n, 0), int(ln["count"]))
    if gcov_errors and not lines:
        raise Fail("gcov could not read the STOR coverage data: " + " || ".join(gcov_errors),
                   "gcov must be able to read the coverage data of STOR's unit-test build (gcov --json-format on the "
                   "coverage objects); it fails on them.")
    if gcov_errors:
        print("gcov problems: " + " || ".join(gcov_errors), flush=True)
    return {name: (sum(1 for c in counts.values() if c > 0), len(counts)) for name, counts in lines.items()}


def c_coverage_threshold(ctx: Ctx) -> None:
    ctx.build()
    srcs = sorted((ctx.stor / "fsw" / "src").glob("*.c"))
    if not srcs:
        raise Fail("apps/stor has no sources", "apps/stor/fsw/src must hold STOR's C sources.")
    run_unit_tests(ctx)
    cov = gcov_line_coverage(ctx.tree)
    ctx.coverage = cov
    p = Problems()
    for s in srcs:
        if "eds" in s.stem:
            continue
        got = cov.get(s.name)
        if not p.check(got is not None, f"apps/stor/fsw/src/{s.name}: no coverage data",
                       f"STOR's unit tests must execute apps/stor/fsw/src/{s.name}; gcov has no coverage data for it."):
            continue
        hit, total = got
        pct = 100.0 if total == 0 else 100.0 * hit / total
        p.check(pct >= SOURCE_PCT_MIN, f"{s.name}: {hit}/{total} lines = {pct:.1f}% (< {SOURCE_PCT_MIN}%)",
                f"Line coverage of apps/stor/fsw/src/{s.name} must be at least 95 percent; STOR's unit tests execute "
                f"{hit} of its {total} lines ({pct:.1f} percent).")
    print("coverage: " + ", ".join(f"{n} {h}/{t}" for n, (h, t) in sorted(cov.items())), flush=True)
    p.raise_if()


def c_default_table_contents(ctx: Ctx) -> None:
    image = ctx.table_file()
    if len(image) != sl.TABLE_FILE_HDR + sl.TABLE_NAME_SIZE:
        raise Fail(f"table image is {len(image)} bytes, expected {sl.TABLE_FILE_HDR + sl.TABLE_NAME_SIZE}",
                   f"The installed StorageTable image must hold the {sl.TABLE_NAME_SIZE}-byte STOR_StorageTable_t "
                   f"defined in the specification; it holds {len(image) - sl.TABLE_FILE_HDR} bytes of table data.")
    name = image[sl.CFE_FS_HDR + 12:sl.CFE_FS_HDR + 52].split(b"\0", 1)[0]
    spec = sl.unpack_table(image[sl.TABLE_FILE_HDR:])
    p = Problems()
    p.check(name.endswith(b"StorageTable"), f"table name {name!r} is not StorageTable",
            "The installed table must be named StorageTable (STOR.StorageTable).")
    for got, want, label in ((spec.telemetry_root, "/cf/stor/tlm", "TelemetryRoot"),
                             (spec.file_root, "/cf/stor/files", "FileRoot"),
                             (spec.telemetry_quota, 262144, "TelemetryQuotaBytes"),
                             (spec.file_quota, 1048576, "FileQuotaBytes"),
                             (spec.max_packet_file, 16384, "MaxPacketFileBytes"),
                             (spec.threshold, 80, "PurgeThresholdPercent")):
        p.check(got == want, f"{label} {got!r}", f"The default table must set {label} to {want}; it has {got!r}.")
    want = {(sl.EVS_HK_MID, 10, 1), (sl.ES_HK_MID, 20, 1), (sl.SAMPLE_HK_MID, 30, 2), (sl.TIME_HK_MID, 40, 1)}
    p.check(set(spec.entries) == want and len(spec.entries) == 4,
            f"entries {sorted(spec.entries)} != {sorted(want)}",
            "The default table must have exactly the four used entries CFE_EVS_HK_TLM_MID (priority 10, decimation 1), "
            "CFE_ES_HK_TLM_MID (20, 1), SAMPLE_APP_HK_TLM_MID (30, 2) and CFE_TIME_HK_TLM_MID (40, 1).")
    p.raise_if()


# ============================================================================
# live checks
# ============================================================================

def c_scheduled_housekeeping(ctx: Ctx) -> None:
    cfs = ctx.session("default-a")
    assert cfs.rx is not None
    got = cfs.unsolicited(sl.HK_MID, 7.5)
    if not got:
        raise Fail("no STOR housekeeping packet within 7.5 s of enabling telemetry output without a request",
                   "With STOR_SEND_HK_MID scheduled every 4.9 s by SCH_LAB and STOR_HK_TLM_MID forwarded by TO_LAB, "
                   "STOR housekeeping (0x08C4) must reach the ground without any request; none arrived within 7.5 s.")
    sl.parse_hk(got[-1])


def c_startup_and_hk(ctx: Ctx) -> None:
    cfs = ctx.session("default-a", reuse=True)
    p = Problems()
    n_init = len(cfs.events(0, 1))
    p.check(n_init == 1, f"initialization event 1 emitted {n_init} times",
            "STOR must send its initialization event (ID 1) exactly once at startup.")
    got = cfs._request(sl.SEND_HK_MID, 0, b"", sl.HK_MID)
    if not got:
        raise Fail("no housekeeping packet (message ID 0x08C4) followed SEND_HK",
                   "STOR must answer SEND_HK with a STOR_HkTlm_t on STOR_HK_TLM_MID (0x08C4); none reached the ground.")
    raw = got[-1]
    p.check(sl.tlm_len(raw) == sl.TLM_HDR + sl.HK_SIZE, f"housekeeping packet is {sl.tlm_len(raw)} bytes",
            f"The housekeeping packet must be the {sl.TLM_HDR + sl.HK_SIZE}-byte STOR_HkTlm_t with the fields in the "
            f"specified order; it is {sl.tlm_len(raw)} bytes.")
    hk = sl.parse_hk(raw)
    for got_v, want, label in ((hk.telemetry_quota, 262144, "TelemetryQuotaBytes"),
                               (hk.file_quota, 1048576, "FileQuotaBytes"),
                               (hk.purge_threshold, 80, "PurgeThresholdPercent")):
        p.check(got_v == want, f"{label} {got_v}",
                f"With the default table, housekeeping must report {label} {want}; it reports {got_v}.")
    p.check(hk.active_entries == 4, f"ActiveEntries {hk.active_entries}",
            "ActiveEntries must count the used entries of the active table (4 with the default table); it reports "
            f"{hk.active_entries}.")
    p.check(hk.command_counter == 0 and hk.command_error_counter == 0,
            f"counters {hk.command_counter}/{hk.command_error_counter} before any command",
            "Before any ground command, CommandCounter and CommandErrorCounter must be 0: the scheduled SEND_HK and "
            f"WAKEUP messages never count; they read {hk.command_counter}/{hk.command_error_counter}.")
    p.check(hk.file_count == 0 and hk.file_used == 0, f"FileCount {hk.file_count}, FileBytesUsed {hk.file_used}",
            "With an empty file area, FileCount and FileBytesUsed must be 0 at startup.")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_NOOP)
    p.check(bool(wait_event(cfs, mark, 3)), "NOOP did not produce event 3", "NOOP must send event 3 (NOOP_INF).")
    hk2 = cfs.hk()
    p.check(hk2.command_counter == 1, f"CommandCounter {hk2.command_counter} after one NOOP",
            f"NOOP must increment CommandCounter (0 -> 1); it reads {hk2.command_counter}.")
    # The default table's four streams are live: their files appear without any command.
    time.sleep(1.0)
    p.check(len(cfs.packet_files()) >= 1, "no packet file exists after housekeeping traffic was stored",
            "With the default table, STOR must store the subscribed housekeeping streams in packet files under "
            "/cf/stor/tlm; no packet file appeared.")
    p.raise_if()


LIVE_CHECKS = {"scheduled-housekeeping", "startup-and-hk", "decimation-and-stats", "packet-file-naming-and-header",
               "rollover", "idle-file-close", "purge-order", "quota-drop", "restart-scan", "set-param-errors",
               "file-add-list-delete", "file-path-rejection", "file-exists-rejection", "file-quota-rejection",
               "reset-counters"}


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def stats_entry(p: Problems, st: dict[int, sl.EntryStats], mid: int, context: str) -> sl.EntryStats | None:
    e = st.get(mid)
    p.check(e is not None, f"no statistics entry for {mid:#06x} ({context})",
            f"SEND_STATS must report an element for every used table entry; there is none for MsgId {mid:#06x}.")
    return e


def c_decimation_and_stats(ctx: Ctx) -> None:
    p = Problems()
    # Part 1: default table, the SAMPLE_APP housekeeping stream (decimation 2).
    cfs = ctx.session("decim-default")
    mark = cfs.log_mark()
    base = stats_entry(p, cfs.stats(), sl.SAMPLE_HK_MID, "default table")
    p.check(bool(wait_event(cfs, mark, 28, timeout=1.0)), "SEND_STATS did not produce event 28",
            "SEND_STATS must send event 28 (STATS_INF) after transmitting the statistics.")
    if base is None:
        p.raise_if()
    cfs.inject(sl.SAMPLE_HK_MID, 10, 120, first_index=1000)
    cfs.cmd(sl.CC_CLOSE_FILES)
    st = cfs.stats()
    e = stats_entry(p, st, sl.SAMPLE_HK_MID, "after injection")
    dec_rule = ("STOR must store a packet of an entry only when (Received - 1) % Decimation == 0 and count the others "
                "as Decimated; with decimation 2, Stored must equal ceil(Received / 2).")
    if e is not None:
        p.check(e.received - base.received >= 10, f"0x0883 Received grew by {e.received - base.received} after 10 packets",
                "Received must count every packet that arrives for a used entry; 10 injected 0x0883 packets raised it by "
                f"{e.received - base.received}.")
        p.check(e.stored == ceil_div(e.received, 2), f"0x0883 Stored {e.stored} != ceil(Received {e.received} / 2)", dec_rule)
        p.check(e.decimated == e.received - e.stored, f"0x0883 Decimated {e.decimated}", dec_rule)
        p.check((e.priority, e.decimation) == (30, 2), f"0x0883 priority/decimation {e.priority}/{e.decimation}",
                "The statistics must report each entry's current Priority and Decimation (0x0883: 30 and 2).")
    for mid, pri in ((sl.EVS_HK_MID, 10), (sl.ES_HK_MID, 20), (sl.TIME_HK_MID, 40)):
        o = stats_entry(p, st, mid, "default table")
        if o is None:
            continue
        p.check((o.priority, o.decimation) == (pri, 1), f"{mid:#06x} priority/decimation {o.priority}/{o.decimation}",
                f"The statistics must report each entry's current Priority and Decimation ({mid:#06x}: {pri} and 1).")
        p.check(o.stored == o.received and o.decimated == 0, f"{mid:#06x} stored {o.stored} of {o.received}", dec_rule)
    hk = cfs.hk()
    if e is not None:
        p.check(hk.packets_decimated >= e.decimated, "PacketsDecimated below the per-entry total",
                "PacketsDecimated must count every decimated packet of all entries.")

    # Part 2: exact rule on a quiet table (no other stream), including decimation 255.
    table = quiet_table(entries=((sl.INJ_A, 10, 1), (sl.INJ_B, 20, 3), (sl.INJ_C, 30, 255)))
    cfs = ctx.session("decim-quiet", table)
    cfs.inject(sl.INJ_A, 4, 300)
    cfs.inject(sl.INJ_B, 10, 300)
    cfs.inject(sl.INJ_C, 10, 300)
    st = cfs.stats()
    for mid, (rx, stored, dec) in ((sl.INJ_A, (4, 4, 0)), (sl.INJ_B, (10, 4, 6)), (sl.INJ_C, (10, 1, 9))):
        o = stats_entry(p, st, mid, "quiet table")
        if o is None:
            continue
        p.check((o.received, o.stored, o.decimated, o.dropped) == (rx, stored, dec, 0),
                f"{mid:#06x} received/stored/decimated/dropped {o.received}/{o.stored}/{o.decimated}/{o.dropped} "
                f"!= {rx}/{stored}/{dec}/0", dec_rule)
    cfs.cmd(sl.CC_CLOSE_FILES)
    indices: dict[int, list[int]] = {sl.INJ_A: [], sl.INJ_B: [], sl.INJ_C: []}
    for f in cfs.packet_files():
        try:
            for pkt in sl.read_packets(f.path.read_bytes()):
                indices.setdefault(f.mid, []).append(int.from_bytes(pkt[sl.TLM_HDR:sl.TLM_HDR + 4], "little"))
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}",
                    "A packet file must hold the 64-byte CFE_FS_Header_t followed only by whole packets as received.")
    which = ("The stored packets must be exactly the 1st, (1 + Decimation)th, ... packets received for the entry "
             "(decimation 3 keeps packets 0, 3, 6 and 9 of 10).")
    p.check(indices[sl.INJ_B] == [0, 3, 6, 9], f"stored B packets {indices[sl.INJ_B]} != [0, 3, 6, 9]", which)
    p.check(indices[sl.INJ_C] == [0], f"stored C packets {indices[sl.INJ_C]} != [0]", which)
    p.check(indices[sl.INJ_A] == [0, 1, 2, 3], f"stored A packets {indices[sl.INJ_A]}", which)
    hk = cfs.hk()
    p.check(hk.packets_stored == 9 and hk.packets_decimated == 15,
            f"PacketsStored/PacketsDecimated {hk.packets_stored}/{hk.packets_decimated} != 9/15",
            "PacketsStored and PacketsDecimated must total the per-entry Stored and Decimated counts.")
    # Runtime change: decimation 1 stores every following packet; the priority change shows in the statistics.
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_DECIMATION, sl.pstr_id(sl.INJ_B, 1))
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.INJ_B, 77))
    p.check(len(wait_event(cfs, mark, 16, count=2)) == 2, "SET_DECIMATION/SET_PRIORITY did not each produce event 16",
            "An accepted SET_DECIMATION or SET_PRIORITY must send event 16 (SET_PARAM_INF).")
    cfs.inject(sl.INJ_B, 2, 300, first_index=10)
    o = stats_entry(p, cfs.stats(), sl.INJ_B, "after runtime change")
    if o is not None:
        p.check((o.priority, o.decimation, o.received, o.stored) == (77, 1, 12, 6),
                f"B after runtime change priority/decimation/received/stored "
                f"{o.priority}/{o.decimation}/{o.received}/{o.stored} != 77/1/12/6",
                "SET_DECIMATION and SET_PRIORITY must take effect at run time: after decimation 1 every following "
                "packet is stored and the statistics report the new values.")
    p.raise_if()


HEADER_RULE = ("Each packet file must start with a CFE_FS_Header_t written with CFE_FS_WriteHeader (ContentType 'cFE1'), "
               "sub type 0x5354 and description \"STOR packet file\".")


def c_packet_file_naming_and_header(ctx: Ctx) -> None:
    cfs = ctx.session("naming")
    p = Problems()
    cfs.inject(sl.SAMPLE_HK_MID, 3, 200, first_index=2000)
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_CLOSE_FILES)
    p.check(bool(wait_event(cfs, mark, 27)), "CLOSE_FILES did not produce event 27",
            "CLOSE_FILES must send event 27 (CLOSE_INF) with the number of files closed.")
    time.sleep(0.5)
    files = cfs.packet_files()
    names = sorted(x.name for x in cfs.tlm_dir.iterdir()) if cfs.tlm_dir.is_dir() else []
    for n in names:
        p.check(sl.PKT_FILE_RE.match(n) is not None, f"file {n!r} does not match tlm_<MMMM>_<NNNNNNNN>.bin",
                "STOR must create nothing in TelemetryRoot except packet files named tlm_<MMMM>_<NNNNNNNN>.bin "
                "(four upper-case hex digits, eight decimal digits).")
    mine = [f for f in files if f.mid == sl.SAMPLE_HK_MID]
    if not p.check(bool(mine), "no tlm_0883_NNNNNNNN.bin file exists under /cf/stor/tlm",
                   "Packets of a used entry must be stored in <TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin; no file for "
                   "MID 0x0883 (tlm_0883_...) appeared under /cf/stor/tlm."):
        p.raise_if()
    seen_marked = 0
    for f in files:
        data = f.path.read_bytes()
        for msg in sl.check_fs_header(data):
            p.check(False, f"{f.path.name}: {msg}", HEADER_RULE)
        try:
            pkts = sl.read_packets(data)
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}",
                    "A packet file must hold the 64-byte CFE_FS_Header_t followed only by whole packets as received.")
            continue
        p.check(len(pkts) >= 1, f"{f.path.name}: no packets after the header",
                "A closed packet file must hold at least one packet after its header.")
        for pkt in pkts:
            p.check(sl.tlm_mid(pkt) == f.mid, f"{f.path.name}: packet MID {sl.tlm_mid(pkt):#06x} differs from the name",
                    "A packet file must hold only packets of the MID in its name.")
            if f.mid == sl.SAMPLE_HK_MID and int.from_bytes(pkt[16:20], "little") >= 2000:
                seen_marked += 1
                p.check(len(pkt) == 200 and pkt == sl.build_telemetry(sl.SAMPLE_HK_MID, 200, int.from_bytes(pkt[16:20], "little")),
                        f"{f.path.name}: stored packet differs from the packet received",
                        "STOR must append each stored packet in full, headers and payload exactly as received.")
    p.check(seen_marked >= 1, "none of the injected 0x0883 packets was found in the files",
            "Stored packets of MID 0x0883 must appear in its packet files; none of the injected ones did.")
    p.check(len({(f.mid, f.seq) for f in files}) == len(files), "duplicate file names",
            "Packet file names must be unique per MID and sequence.")
    hk, before, after = used_matches_disk(cfs)
    p.check(before <= hk.telemetry_used <= after, f"TelemetryBytesUsed {hk.telemetry_used} not in [{before}, {after}] (disk)",
            "TelemetryBytesUsed must equal the total size on disk of the packet files, headers included.")
    p.check(hk.open_files <= 4, f"OpenPacketFiles {hk.open_files}",
            "OpenPacketFiles must count the packet files currently open (at most one per used entry).")
    p.raise_if()


ROLLOVER_RULE = ("When appending a packet would make a packet file exceed MaxPacketFileBytes, STOR must close it and "
                 "write the packet to a new file with the next sequence number; a packet that makes the file exactly "
                 "MaxPacketFileBytes still fits.")


def c_rollover(ctx: Ctx) -> None:
    cfs = ctx.session("rollover", quiet_table(entries=((sl.INJ_A, 10, 1), (sl.INJ_B, 20, 1))))
    p = Problems()
    cfs.inject(sl.INJ_A, 10, 300)
    files = [f for f in cfs.packet_files() if f.mid == sl.INJ_A]
    p.check([f.seq for f in files] == [0, 1, 2, 3], f"sequence numbers {[f.seq for f in files]} != [0, 1, 2, 3]",
            "Packet file sequence numbers must start at 0 on a clean directory and increase by one per new file.")
    p.check([f.size for f in files] == [964, 964, 964, 364], f"file sizes {[f.size for f in files]} != [964, 964, 964, 364]",
            ROLLOVER_RULE)
    got: list[int] = []
    for f in files:
        data = f.path.read_bytes()
        for msg in sl.check_fs_header(data):
            p.check(False, f"{f.path.name}: {msg}", HEADER_RULE)
        try:
            got += [int.from_bytes(x[16:20], "little") for x in sl.read_packets(data)]
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}",
                    "A packet file must hold the 64-byte CFE_FS_Header_t followed only by whole packets as received.")
    p.check(got == list(range(10)), f"packet order across files {got}",
            "Packets must be stored in arrival order across consecutive packet files, none lost or repeated.")
    hk = cfs.hk()
    p.check(hk.telemetry_used == disk_sum(cfs) == 3256,
            f"TelemetryBytesUsed {hk.telemetry_used}, disk {disk_sum(cfs)}, expected 3256",
            "TelemetryBytesUsed must equal the total size on disk of the packet files, headers included.")
    p.check(hk.open_files == 1, f"OpenPacketFiles {hk.open_files} != 1",
            "After a roll-over only the newest file of the entry stays open (OpenPacketFiles 1).")
    # A packet that makes the file exactly MaxPacketFileBytes still fits: 64 + 4 * 240 = 1024.
    cfs.inject(sl.INJ_B, 9, 240)
    bf = [f for f in cfs.packet_files() if f.mid == sl.INJ_B]
    p.check([f.size for f in bf] == [1024, 1024, 304], f"exact-fit file sizes {[f.size for f in bf]} != [1024, 1024, 304]",
            ROLLOVER_RULE)
    p.check([f.seq for f in bf] == [0, 1, 2], f"second MID has sequences {[f.seq for f in bf]}, expected [0, 1, 2]",
            "Packet file sequence numbers are counted per MID, starting at 0 for each MID.")
    p.check(all(f.size <= 1024 for f in cfs.packet_files()), "a packet file exceeds MaxPacketFileBytes", ROLLOVER_RULE)
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_CLOSE_FILES)
    hk = cfs.hk()
    p.check(hk.open_files == 0, f"OpenPacketFiles {hk.open_files} after CLOSE_FILES",
            "CLOSE_FILES must close every open packet file (OpenPacketFiles 0 afterwards).")
    p.check(bool(cfs.events(mark, 27)), "no event 27 for CLOSE_FILES",
            "CLOSE_FILES must send event 27 (CLOSE_INF) with the number of files closed.")
    p.raise_if()


def c_idle_file_close(ctx: Ctx) -> None:
    cfs = ctx.session("idle", quiet_table())
    p = Problems()
    cfs.inject(sl.INJ_A, 1, 300)
    t_write = time.monotonic()
    hk = cfs.hk()
    p.check(hk.open_files == 1, f"OpenPacketFiles {hk.open_files} after one packet",
            "After a packet is stored its packet file stays open (OpenPacketFiles 1) until it rolls over, is closed by "
            "command, or has not been written for 10 seconds.")
    time.sleep(max(0.0, 8.0 - (time.monotonic() - t_write)))
    hk = cfs.hk()
    p.check(hk.open_files == 1, f"file closed after only {time.monotonic() - t_write:.1f} s idle",
            "WAKEUP must not close a packet file that was written less than 10 seconds ago (measured with cFE time).")
    time.sleep(max(0.0, 13.0 - (time.monotonic() - t_write)))
    hk = cfs.hk()
    p.check(hk.open_files == 0, f"OpenPacketFiles {hk.open_files} after {time.monotonic() - t_write:.1f} s idle",
            "WAKEUP (1 Hz) must close every packet file whose last write is 10 seconds or more ago.")
    cfs.inject(sl.INJ_A, 1, 300, first_index=1)
    files = [f for f in cfs.packet_files() if f.mid == sl.INJ_A]
    p.check(len(files) == 2, f"{len(files)} files after a packet arrived following the idle close; expected 2",
            "A closed packet file is never reopened: the next packet after an idle close goes to a new file with the "
            "next sequence number.")
    p.raise_if()


PURGE_RULE = ("While TelemetryBytesUsed exceeds PurgeThresholdPercent of TelemetryQuotaBytes, STOR must delete closed "
              "packet files one at a time in ascending order of (current priority of the file's MID, sequence number, "
              "MID value), never an open file.")


def prefix_violations(universe: list[tuple[int, int]], remaining: set[tuple[int, int]],
                      prio: dict[int, int]) -> list[str]:
    """Missing files must be a lowest-first prefix of the (priority, sequence, MID) order: one line per purged
    file that has lower-ranked survivors."""
    rank = {k: (prio[k[0]], k[1], k[0]) for k in universe}
    bad: list[str] = []
    for x in universe:
        if x in remaining:
            continue
        lower = sorted(f"{y[0]:04X}_{y[1]}" for y in universe if y in remaining and rank[y] < rank[x])
        if lower:
            bad.append(f"{x[0]:04X}_{x[1]} (rank {rank[x]}) was purged while lower-ranked files survived: {', '.join(lower)}")
    return bad


def c_purge_order(ctx: Ctx) -> None:
    quota, thr = 40960, 50
    limit = quota * thr // 100
    table = sl.TableSpec(telemetry_quota=quota, max_packet_file=4096, threshold=thr,
                         entries=((sl.INJ_A, 5, 1), (sl.INJ_B, 200, 1), (sl.INJ_C, 100, 1)))
    cfs = ctx.session("purge", table)
    p = Problems()
    seen: set[tuple[int, int]] = set()

    def snap() -> set[tuple[int, int]]:
        cur = {(f.mid, f.seq) for f in cfs.packet_files()}
        seen.update(cur)
        return cur

    def inject_tracking(mid: int, n: int) -> None:
        for i in range(0, n, 3):
            cfs.inject(mid, min(3, n - i), PKT, first_index=i)
            snap()

    inject_tracking(sl.INJ_B, 10)
    cfs.cmd(sl.CC_CLOSE_FILES)
    inject_tracking(sl.INJ_C, 5)
    cfs.cmd(sl.CC_CLOSE_FILES)
    snap()
    hk = cfs.hk()
    p.check(hk.files_purged == 0 and hk.purge_cycles == 0,
            f"purged {hk.files_purged} files in {hk.purge_cycles} cycles while below the threshold",
            "STOR must not purge while TelemetryBytesUsed is at or below the purge threshold.")
    p.check(hk.telemetry_used == 3 * FILE_FULL == disk_sum(cfs),
            f"TelemetryBytesUsed {hk.telemetry_used}, disk {disk_sum(cfs)}, expected {3 * FILE_FULL}",
            "TelemetryBytesUsed must equal the total size on disk of the packet files, headers included.")
    high = {k for k in snap() if k[0] in (sl.INJ_B, sl.INJ_C)}
    inject_tracking(sl.INJ_A, 40)
    cfs.cmd(sl.CC_CLOSE_FILES)
    cfs.send(sl.build_command(sl.WAKEUP_MID, 0))
    time.sleep(2.0)
    rem1 = snap()
    hk = cfs.hk()
    phase1_prio = {sl.INJ_A: 5, sl.INJ_B: 200, sl.INJ_C: 100}
    p.check(hk.files_purged > 0, "FilesPurged is 0 after the threshold was exceeded",
            "FilesPurged must count every packet file deleted by the purge.")
    p.check(hk.purge_cycles > 0, "PurgeCycles is 0 after the threshold was exceeded",
            "PurgeCycles must count every purge pass that deletes at least one file.")
    p.check(hk.telemetry_used <= limit, f"TelemetryBytesUsed {hk.telemetry_used} above the threshold {limit}", PURGE_RULE)
    p.check(hk.telemetry_used == disk_sum(cfs), f"TelemetryBytesUsed {hk.telemetry_used} != bytes on disk {disk_sum(cfs)}",
            "TelemetryBytesUsed must equal the total size on disk of the packet files after purging.")
    p.check(high <= rem1, f"higher-priority files were purged: {sorted(high - rem1)}", PURGE_RULE)
    gone1 = sorted(k for k in seen if k not in rem1)
    p.check(bool(gone1) and all(k[0] == sl.INJ_A for k in gone1), f"purged files {gone1} are not all lowest-priority",
            PURGE_RULE)
    p.check(hk.files_purged >= len(gone1), f"FilesPurged {hk.files_purged} < {len(gone1)} files gone from disk",
            "FilesPurged must count every packet file deleted by the purge.")
    p.check(hk.packets_dropped == 0, f"PacketsDropped {hk.packets_dropped} although closed files were purgeable",
            "Packets must not be dropped while the purge keeps TelemetryBytesUsed well below the quota.")
    for msg in prefix_violations(sorted(seen), rem1, phase1_prio):
        p.check(False, msg, PURGE_RULE)
    # Phase 2: runtime priorities are reversed; the previously safest files are now the first to go.
    universe2 = set(rem1)
    seen_before = set(seen)
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.INJ_B, 1))
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.INJ_A, 250))
    inject_tracking(sl.INJ_A, 20)
    cfs.cmd(sl.CC_CLOSE_FILES)
    cfs.send(sl.build_command(sl.WAKEUP_MID, 0))
    time.sleep(2.0)
    rem2 = snap()
    universe2 |= (seen - seen_before)
    hk2 = cfs.hk()
    runtime_rule = "Run-time SET_PRIORITY changes must change which closed files the purge deletes first."
    p.check(hk2.files_purged > hk.files_purged, "no further purge after the priorities were changed and more data stored",
            PURGE_RULE)
    p.check(hk2.telemetry_used <= limit, f"TelemetryBytesUsed {hk2.telemetry_used} above the threshold {limit} (phase 2)",
            PURGE_RULE)
    p.check(not (high & rem2) or len(high & rem2) < len(high),
            "after B was made the lowest priority, none of its files was purged first", runtime_rule)
    phase2_prio = {sl.INJ_A: 250, sl.INJ_B: 1, sl.INJ_C: 100}
    for msg in prefix_violations(sorted(universe2), rem2 & universe2, phase2_prio):
        p.check(False, "phase 2: " + msg, runtime_rule)
    p.raise_if()


QUOTA_RULE = ("A packet due to be stored must be dropped when TelemetryBytesUsed as it stands, plus the packet, plus the "
              "64-byte file header if it starts a new file, would exceed TelemetryQuotaBytes; the quota check itself "
              "never deletes files, so with PurgeThresholdPercent 100 nothing is purged and storing stops once the "
              "quota is full.")


def c_quota_drop(ctx: Ctx) -> None:
    # With PurgeThresholdPercent 100 nothing is purged (usage can never exceed 100 % of the quota), so injecting
    # 30 packets of 700 bytes into a 16384-byte quota must fill it: four closed files of 3564 bytes (14256) plus a
    # fifth file that takes two packets (14256 + 64 + 2 * 700 = 15720) and drops the rest. Lowering the threshold
    # to 50 purges the closed files and storing resumes; refilling drops again, and event 13 is sent once per drop
    # episode (twice in total), not once per dropped packet.
    quota = 16384
    cfs = ctx.session("quota", sl.TableSpec(telemetry_quota=quota, max_packet_file=4096, threshold=100,
                                            entries=((sl.INJ_A, 10, 1),)))
    p = Problems()
    mark = cfs.log_mark()
    cfs.inject(sl.INJ_A, 30, PKT)
    hk = cfs.hk()
    st = cfs.stats()
    p.check(hk.packets_dropped > 0, "PacketsDropped is 0 after more packets than the quota can hold", QUOTA_RULE)
    p.check(hk.telemetry_used <= quota, f"TelemetryBytesUsed {hk.telemetry_used} exceeds the quota {quota}", QUOTA_RULE)
    p.check(quota - hk.telemetry_used < 64 + PKT, f"only {hk.telemetry_used} of {quota} bytes used when dropping began",
            QUOTA_RULE)
    p.check(hk.packets_stored + hk.packets_dropped == 30, f"stored {hk.packets_stored} + dropped {hk.packets_dropped} != 30",
            "Every packet due to be stored must be counted as either stored or dropped.")
    p.check(hk.packets_stored == 22, f"{hk.packets_stored} stored, expected 22 (the new file's header counts against the quota)",
            QUOTA_RULE)
    p.check(hk.files_purged == 0 and hk.purge_cycles == 0,
            f"files were purged although the threshold is 100 percent (FilesPurged {hk.files_purged})", QUOTA_RULE)
    p.check(hk.telemetry_used == disk_sum(cfs), f"TelemetryBytesUsed {hk.telemetry_used} != bytes on disk {disk_sum(cfs)}",
            "TelemetryBytesUsed must equal the total size on disk of the packet files; a dropped packet never creates "
            "a file.")
    e = stats_entry(p, st, sl.INJ_A, "quota")
    if e is not None:
        p.check(e.dropped == hk.packets_dropped, f"per-entry Dropped {e.dropped} != PacketsDropped {hk.packets_dropped}",
                "Each drop must increment both the entry's Dropped and PacketsDropped.")
    ev = cfs.events(mark, 13)
    once_rule = ("Event 13 (TLM_QUOTA_ERR) must be sent on an entry's first drop and not again until a packet of that "
                 "entry is stored.")
    p.check(len(ev) == 1, f"event 13 occurred {len(ev)} times during one drop episode; expected exactly once", once_rule)
    # Free space by lowering the threshold, then store again.
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([50, 0, 0, 0]))
    cfs.settle()
    hk_p = cfs.hk()
    p.check(hk_p.files_purged > 0 and hk_p.telemetry_used <= quota // 2,
            f"SET_PURGE_THRESHOLD 50 left {hk_p.telemetry_used} bytes (FilesPurged {hk_p.files_purged})",
            "An accepted SET_PURGE_THRESHOLD must run a purge pass with the new threshold.")
    dropped_before = hk_p.packets_dropped
    cfs.inject(sl.INJ_A, 3, PKT, first_index=100)
    hk_s = cfs.hk()
    p.check(hk_s.packets_dropped == dropped_before and hk_s.packets_stored == hk_p.packets_stored + 3,
            f"packets were not stored again after space was freed (stored {hk_s.packets_stored}, dropped {hk_s.packets_dropped})",
            "Once purging has freed space below the quota, packets must be stored again.")
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([100, 0, 0, 0]))
    cfs.inject(sl.INJ_A, 30, PKT, first_index=200)
    hk_f = cfs.hk()
    p.check(hk_f.packets_dropped > dropped_before, "no drops when refilling the quota", QUOTA_RULE)
    ev2 = cfs.events(mark, 13)
    p.check(len(ev2) == 2, f"event 13 occurred {len(ev2)} times over two drop episodes; expected exactly 2", once_rule)
    p.raise_if()


def c_restart_scan(ctx: Ctx) -> None:
    seed = {"stor/tlm/tlm_08E0_00000007.bin": bytes(1000), "stor/tlm/tlm_08E1_00000003.bin": bytes(500),
            "stor/tlm/notes.txt": bytes(50), "stor/tlm/sub/tlm_08E0_00000009.bin": bytes(80),
            "stor/files/x/y.bin": bytes(700), "stor/files/z.bin": bytes(300)}
    cfs = ctx.session("restart", quiet_table(entries=((sl.INJ_A, 10, 1), (sl.INJ_B, 10, 1))), seed=seed)
    p = Problems()
    hk = cfs.hk()
    p.check(hk.telemetry_used == 1500, f"TelemetryBytesUsed {hk.telemetry_used} != 1500 (sizes of the existing packet files)",
            "At startup TelemetryBytesUsed must be the total size of the packet files directly in TelemetryRoot "
            "(other files and subdirectories are ignored, contents are not inspected).")
    p.check(hk.file_used == 1000, f"FileBytesUsed {hk.file_used} != 1000 (sizes of the existing files)",
            "At startup FileBytesUsed must be the total size of all regular files under FileRoot, recursively.")
    p.check(hk.file_count == 2, f"FileCount {hk.file_count} != 2 at startup",
            "At startup FileCount must be the number of regular files under FileRoot, recursively.")
    p.check((cfs.tlm_dir / "notes.txt").is_file(), "notes.txt in TelemetryRoot was removed",
            "Files in TelemetryRoot that are not packet files must be left alone.")
    cfs.inject(sl.INJ_A, 1, 300)
    names = sorted(f.path.name for f in cfs.packet_files())
    seq_rule = ("The packet file sequence of each MID must continue from the highest sequence found for that MID at "
                "startup plus one.")
    p.check("tlm_08E0_00000008.bin" in names, f"no tlm_08E0_00000008.bin after restart; files {names}", seq_rule)
    cfs.inject(sl.INJ_B, 1, 300)
    names = sorted(f.path.name for f in cfs.packet_files())
    p.check("tlm_08E1_00000004.bin" in names, f"no tlm_08E1_00000004.bin; files {names}", seq_rule)
    hk = cfs.hk()
    p.check(hk.telemetry_used == 1500 + 2 * (64 + 300),
            f"TelemetryBytesUsed {hk.telemetry_used} != {1500 + 2 * (64 + 300)} after two packets",
            "TelemetryBytesUsed must grow by each new file's header and each stored packet.")
    p.raise_if()


def c_set_param_errors(ctx: Ctx) -> None:
    cfs = ctx.session("params")
    p = Problems()
    reject_rule = ("SET_PRIORITY and SET_DECIMATION must reject value 0 or an unknown MsgId with event 17 "
                   "(SET_PARAM_ERR), CommandErrorCounter incremented and CommandCounter unchanged.")
    mark = cfs.log_mark()
    a = cfs.hk()
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(0x0FFF, 5))
    cfs.cmd(sl.CC_SET_DECIMATION, sl.pstr_id(sl.SAMPLE_HK_MID, 0))
    cfs.settle()
    b = cfs.hk()
    p.check(b.command_error_counter == a.command_error_counter + 2,
            f"CommandErrorCounter {a.command_error_counter} -> {b.command_error_counter} after two invalid SET_* commands",
            reject_rule)
    p.check(len(cfs.events(mark, 17)) == 2, f"event 17 seen {len(cfs.events(mark, 17))} times, expected 2", reject_rule)
    p.check(b.command_counter == a.command_counter, "CommandCounter changed for rejected commands", reject_rule)
    # Priority 0 is rejected too; a valid change is accepted with event 16.
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.EVS_HK_MID, 0))
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.EVS_HK_MID, 11))
    cfs.cmd(sl.CC_SET_DECIMATION, sl.pstr_id(sl.EVS_HK_MID, 4))
    cfs.settle()
    p.check(len(cfs.events(mark, 17)) == 1, "SET_PRIORITY 0 did not produce exactly one event 17", reject_rule)
    p.check(len(cfs.events(mark, 16)) == 2, "valid SET_PRIORITY/SET_DECIMATION did not each produce event 16",
            "An accepted SET_PRIORITY or SET_DECIMATION must send event 16 (SET_PARAM_INF).")
    e = stats_entry(p, cfs.stats(), sl.EVS_HK_MID, "params")
    if e is not None:
        p.check((e.priority, e.decimation) == (11, 4), f"EVS entry priority/decimation {e.priority}/{e.decimation} != 11/4",
                "Accepted SET_PRIORITY and SET_DECIMATION values must show in the statistics.")
    # Purge threshold: 0 and 101 rejected, 50 and 100 accepted.
    thr_rule = ("SET_PURGE_THRESHOLD must reject values outside 1..100 with event 17 and CommandErrorCounter incremented, "
                "leaving the threshold unchanged.")
    c = cfs.hk()
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([0, 0, 0, 0]))
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([101, 0, 0, 0]))
    cfs.settle()
    d = cfs.hk()
    p.check(len(cfs.events(mark, 17)) == 2, "thresholds 0 and 101 did not each produce event 17", thr_rule)
    p.check(d.purge_threshold == c.purge_threshold == 80,
            f"PurgeThresholdPercent {c.purge_threshold}->{d.purge_threshold} after rejected values", thr_rule)
    p.check(d.command_error_counter == c.command_error_counter + 2, "rejected thresholds did not each count as command errors",
            thr_rule)
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([50, 0, 0, 0]))
    cfs.settle()
    e2 = cfs.hk()
    p.check(len(cfs.events(mark, 16)) == 1, "threshold 50 did not produce event 16",
            "An accepted SET_PURGE_THRESHOLD must send event 16 (SET_PARAM_INF).")
    p.check(e2.purge_threshold == 50, f"PurgeThresholdPercent {e2.purge_threshold} != 50",
            "An accepted SET_PURGE_THRESHOLD must set the threshold that housekeeping reports.")
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([100, 0, 0, 0]))
    cfs.settle()
    t100 = cfs.hk().purge_threshold
    p.check(t100 == 100, f"threshold 100 was not accepted ({t100})", "SET_PURGE_THRESHOLD must accept 100.")
    # Bad function code and bad length.
    mark = cfs.log_mark()
    f0 = cfs.hk()
    cfs.cmd(99)
    cfs.cmd(sl.CC_NOOP, b"\0\0\0\0")
    cfs.settle()
    f1 = cfs.hk()
    p.check(len(cfs.events(mark, 2)) == 1, "invalid function code did not produce event 2",
            "An invalid function code must send event 2 (CC_ERR).")
    p.check(len(cfs.events(mark, 6)) == 1, "wrong command length did not produce event 6",
            "A STOR_CMD_MID command with the wrong length must send event 6 (CMD_LEN_ERR) and not execute.")
    p.check(f1.command_error_counter == f0.command_error_counter + 2,
            f"CommandErrorCounter {f0.command_error_counter} -> {f1.command_error_counter} after a bad code and a bad length",
            "An invalid function code and a wrong command length must each increment CommandErrorCounter (unlike "
            "sample_app, a bad function code counts).")
    p.raise_if()


def small_file_source(cfs: sl.Cfs) -> int:
    return (cfs.cf / "stor_tbl.tbl").stat().st_size


def add_payload(src: str, dest: str) -> bytes:
    return sl.pstr(src) + sl.pstr(dest)


LISTING_LOST = ("Listing packets sent back to back can be lost before they reach the ground when the telemetry output "
                "path cannot queue a whole listing, so also check the STOR_LIST_TLM_MID buffer limit in "
                "sample_defs/tables/to_lab_sub.c.")
LIST_RULE = ("LIST_DIR and LIST_ALL must send full-size STOR_ListTlm_t packets of at most four entries each, with "
             "TotalEntries, a 0-based PacketIndex and EntriesInPacket; an empty listing is one packet with "
             "TotalEntries 0.")


def c_file_add_list_delete(ctx: Ctx) -> None:
    cfs = ctx.session("files-a")
    p = Problems()
    size = small_file_source(cfs)
    hk_start = cfs.hk()
    # Empty file area.
    pk = cfs.listing(sl.CC_LIST_ALL)
    p.check(len(pk) == 1 and pk[0].total == 0 and pk[0].count == 0, f"LIST_ALL of an empty file area: {pk}", LIST_RULE)
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr(""))
    p.check(len(pk) == 1 and pk[0].total == 0, f"LIST_DIR of the empty root: {pk}",
            LIST_RULE + " The . and .. entries are skipped.")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "a/b/copy.tbl"))
    add_rule = ("ADD_FILE must copy the source to <FileRoot>/<DestPath>, creating intermediate directories, and send "
                "event 22 (FILE_ADD_INF).")
    p.check(len(wait_event(cfs, mark, 22)) == 1, "ADD_FILE did not produce event 22", add_rule)
    dest = cfs.files_dir / "a" / "b" / "copy.tbl"
    p.check(dest.is_file() and dest.read_bytes() == (cfs.cf / "stor_tbl.tbl").read_bytes(),
            "the added file is missing or differs from its source", add_rule)
    hk = cfs.hk()
    p.check(hk.file_count == 1, f"FileCount {hk.file_count} != 1", "A successful ADD_FILE must increment FileCount.")
    p.check(hk.file_used == size, f"FileBytesUsed {hk.file_used} != {size}",
            "A successful ADD_FILE must add the file's size to FileBytesUsed.")
    mark = cfs.log_mark()
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr("a"))
    p.check(len(pk) == 1 and pk[0].total == 1 and pk[0].entries == (("b/", 0),), f"LIST_DIR a: {pk}",
            "LIST_DIR must list the bare names of the immediate entries (no directory prefix), directories with a "
            "trailing / and size 0.")
    p.check(len(cfs.events(mark, 26)) == 1, f"LIST_DIR produced {len(cfs.events(mark, 26))} events 26",
            "Each successful listing command must send exactly one event 26 (LIST_INF).")
    pk = cfs.listing(sl.CC_LIST_ALL)
    p.check(len(pk) == 1 and pk[0].total == 1 and pk[0].entries == (("a/b/copy.tbl", size),), f"LIST_ALL: {pk}",
            "LIST_ALL must list every regular file under FileRoot with its path relative to FileRoot and its size.")
    # Multi-packet listing: five more files in one directory.
    for i in range(5):
        cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", f"m/f{i}.tbl"))
    mark = cfs.log_mark()
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr("m"))
    shape = [(x.index, x.count, x.total) for x in pk]
    ok_m = [x.index for x in pk] == [0, 1] and all(x.total == 5 for x in pk) and [x.count for x in pk] == [4, 1]
    lost = len(pk) < 2 and all(x.total == 5 for x in pk)
    p.check(ok_m, f"LIST_DIR m packets {shape} != indices [0, 1], counts [4, 1], total 5", LIST_RULE)
    p.check(not lost, "LIST_DIR m: only part of a multi-packet listing arrived", LISTING_LOST)
    p.check(len(cfs.events(mark, 26)) == 1, f"a two-packet LIST_DIR produced {len(cfs.events(mark, 26))} events 26",
            "Each successful listing command must send exactly one event 26 (LIST_INF), not one per packet.")
    names = sorted(n for x in pk for n, _ in x.entries)
    p.check(names == [f"f{i}.tbl" for i in range(5)] or lost, f"LIST_DIR m names {names}",
            "LIST_DIR must list the bare names of every immediate entry of the directory.")
    pk = cfs.listing(sl.CC_LIST_ALL)
    allnames = [n for x in pk for n, _ in x.entries]
    lost = len(pk) < 2 and all(x.total == 6 for x in pk)
    p.check(len(pk) == 2 and all(x.total == 6 for x in pk) and sorted(allnames) ==
            sorted(["a/b/copy.tbl"] + [f"m/f{i}.tbl" for i in range(5)]),
            f"LIST_ALL after six adds: packets {[(x.index, x.count, x.total) for x in pk]}, names {allnames}",
            "LIST_ALL must list every regular file under FileRoot, relative to FileRoot, four per packet.")
    p.check(not lost, "LIST_ALL: only part of a multi-packet listing arrived", LISTING_LOST)
    m_idx = [i for i, n in enumerate(allnames) if n.startswith("m/")]
    p.check(m_idx == list(range(m_idx[0], m_idx[0] + len(m_idx))) if m_idx else lost,
            "LIST_ALL entries of one directory are not contiguous",
            "LIST_ALL must walk the tree depth-first in pre-order, so one directory's files are listed together.")
    # Missing directory and a regular file given to LIST_DIR: event 25 and no packet.
    a = cfs.hk()
    list_err = ("LIST_DIR of a missing path or of a path that is not a directory must send event 25 (LIST_ERR), count "
                "as a command error and send no listing packet.")
    mark = cfs.log_mark()
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr("nosuchdir"))
    p.check(bool(wait_event(cfs, mark, 25)), "LIST_DIR of a missing directory did not produce event 25", list_err)
    p.check(not pk, f"LIST_DIR of a missing directory sent {len(pk)} packets", list_err)
    mark = cfs.log_mark()
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr("m/f0.tbl"))
    p.check(bool(wait_event(cfs, mark, 25)), "LIST_DIR of a regular file did not produce event 25", list_err)
    p.check(not pk, f"LIST_DIR of a regular file sent {len(pk)} packets", list_err)
    # Delete.
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("a/b/copy.tbl"))
    del_rule = ("DELETE_FILE must remove a regular file, send event 24 (FILE_DEL_INF), decrement FileCount and subtract "
                "its size from FileBytesUsed.")
    p.check(len(wait_event(cfs, mark, 24)) == 1, "DELETE_FILE did not produce event 24", del_rule)
    p.check(not dest.exists(), "the deleted file still exists", del_rule)
    b = cfs.hk()
    p.check(b.file_count == a.file_count - 1, f"FileCount {a.file_count} -> {b.file_count} after DELETE_FILE", del_rule)
    p.check(b.file_used == a.file_used - size, f"FileBytesUsed {a.file_used} -> {b.file_used} after DELETE_FILE", del_rule)
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("a/b/copy.tbl"))
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("a"))
    cfs.settle()
    c = cfs.hk()
    del_err = ("DELETE_FILE of a missing file or of a directory must send event 23 (FILE_DEL_ERR), count as a command "
               "error and remove nothing.")
    p.check(len(cfs.events(mark, 23)) == 2, "deleting a missing file and a directory did not each produce event 23", del_err)
    p.check(c.command_error_counter == b.command_error_counter + 2, "failed deletes did not each count as command errors",
            del_err)
    p.check((cfs.files_dir / "a").is_dir(), "DELETE_FILE removed a directory", del_err)
    # accepted: 2 empty listings, 6 adds, 4 listings, 1 delete
    rise = (c.command_counter - hk_start.command_counter) % 256
    p.check(rise == 13, f"CommandCounter rose by {rise}, expected 13 accepted commands",
            "Every accepted ADD_FILE, DELETE_FILE, LIST_DIR and LIST_ALL must increment CommandCounter.")
    p.check(c.command_error_counter - hk_start.command_error_counter == 4,
            f"CommandErrorCounter rose by {c.command_error_counter - hk_start.command_error_counter}, expected 4",
            "Every rejected listing or file command must increment CommandErrorCounter.")
    p.raise_if()


def c_file_path_rejection(ctx: Ctx) -> None:
    cfs = ctx.session("files-path")
    p = Problems()
    rule = ("A file area path that contains .. or starts with / must be rejected with event 18 (PATH_ERR) and counted as "
            "a command error, for ADD_FILE (destination), DELETE_FILE and LIST_DIR, without touching anything.")
    a = cfs.hk()
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "../x"))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "/abs"))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "a/../b"))
    cfs.settle()
    b = cfs.hk()
    p.check(len(cfs.events(mark, 18)) == 3, f"event 18 seen {len(cfs.events(mark, 18))} times for three bad destinations", rule)
    p.check(b.command_error_counter == a.command_error_counter + 3, "rejected destinations did not each count as command errors",
            rule)
    p.check(b.file_count == 0, f"FileCount {b.file_count} after rejections", rule)
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("../x"))
    cfs.cmd(sl.CC_LIST_DIR, sl.pstr(".."))
    cfs.cmd(sl.CC_LIST_DIR, sl.pstr("/etc"))
    cfs.settle()
    p.check(len(cfs.events(mark, 18)) == 3, f"event 18 seen {len(cfs.events(mark, 18))} times for bad DELETE_FILE/LIST_DIR paths",
            rule)
    stray = [q for q in (cfs.cf / "x", cfs.cf / "stor" / "x", cfs.cf / "b", cfs.cf / "abs", cfs.files_dir / "abs",
                         cfs.files_dir / "b", Path("/abs")) if q.exists()]
    p.check(not stray, f"files were written outside the file area: {stray}", rule)
    p.raise_if()


def c_file_exists_rejection(ctx: Ctx) -> None:
    cfs = ctx.session("files-exists")
    p = Problems()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "dup.tbl"))
    a = cfs.hk()
    p.check(a.file_count == 1, f"FileCount {a.file_count} after the first add",
            "ADD_FILE to a new destination must succeed and increment FileCount.")
    first = (cfs.files_dir / "dup.tbl").read_bytes() if (cfs.files_dir / "dup.tbl").is_file() else b""
    rule = ("ADD_FILE must reject an existing destination with event 20 (FILE_EXISTS_ERR) and CommandErrorCounter "
            "incremented, leaving the existing file, FileCount, FileBytesUsed and FileAddsRejected unchanged.")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "dup.tbl"))
    cfs.settle()
    b = cfs.hk()
    p.check(len(cfs.events(mark, 20)) == 1, "adding to an existing destination did not produce event 20", rule)
    p.check(b.command_error_counter == a.command_error_counter + 1, "the rejected add did not count as a command error", rule)
    p.check(b.file_count == 1 and b.file_used == a.file_used, "FileCount/FileBytesUsed changed by the rejected add", rule)
    p.check((cfs.files_dir / "dup.tbl").is_file() and (cfs.files_dir / "dup.tbl").read_bytes() == first,
            "the existing file was modified", rule)
    src_rule = ("ADD_FILE must reject a source that cannot be opened with event 19 (FILE_SRC_ERR), leaving no destination "
                "file and FileAddsRejected unchanged.")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/nosuchsource.bin", "other.tbl"))
    p.check(bool(wait_event(cfs, mark, 19)), "a missing source file did not produce event 19", src_rule)
    p.check(not (cfs.files_dir / "other.tbl").exists(), "a destination exists for a missing source", src_rule)
    c = cfs.hk()
    p.check(c.file_adds_rejected == 0, f"FileAddsRejected {c.file_adds_rejected} after an exists and a source rejection",
            "FileAddsRejected must count only ADD_FILE commands rejected for the file quota.")
    p.raise_if()


def c_file_quota_rejection(ctx: Ctx) -> None:
    cfs = ctx.session("files-quota")
    p = Problems()
    quota = 1048576
    rule = ("ADD_FILE must reject a file whose size plus FileBytesUsed exceeds FileQuotaBytes with event 21 "
            "(FILE_QUOTA_ERR), FileAddsRejected and CommandErrorCounter incremented and no file or directory left behind.")
    big = cfs.cf / "big.bin"
    big.write_bytes(bytes(quota + 1))
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "big/dest.bin"))
    p.check(len(wait_event(cfs, mark, 21)) == 1, "an add beyond FileQuotaBytes did not produce event 21", rule)
    hk = cfs.hk()
    p.check(hk.file_adds_rejected == 1, f"FileAddsRejected {hk.file_adds_rejected} != 1", rule)
    p.check(hk.command_error_counter == 1, f"CommandErrorCounter {hk.command_error_counter} != 1", rule)
    leftovers = [str(x.relative_to(cfs.files_dir)) for x in cfs.files_dir.rglob("*")] if cfs.files_dir.is_dir() else []
    p.check(not leftovers, f"partial files or directories left in the file area: {leftovers}", rule)
    p.check(hk.file_used == 0 and hk.file_count == 0, f"FileBytesUsed/FileCount {hk.file_used}/{hk.file_count} after rejection",
            rule)
    # Boundary: a file that exactly fills the quota is accepted, one byte more is not.
    small = small_file_source(cfs)
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "small.tbl"))
    p.check(len(wait_event(cfs, mark, 22)) == 1, "a small file within the quota was not accepted",
            "ADD_FILE of a file that fits in the file quota must be accepted with event 22.")
    fit = cfs.cf / "big.bin"
    fit.write_bytes(bytes(quota - small))
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "fill.bin"))
    fill_rule = "A file that makes FileBytesUsed exactly FileQuotaBytes must be accepted."
    p.check(len(wait_event(cfs, mark, 22)) == 1, "a file that exactly fills FileQuotaBytes was not accepted", fill_rule)
    hk = cfs.hk()
    p.check(hk.file_used == quota, f"FileBytesUsed {hk.file_used} != {quota} after filling the quota", fill_rule)
    fit.write_bytes(bytes(1))
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "one-more.bin"))
    p.check(len(wait_event(cfs, mark, 21)) == 1, "one byte beyond the quota was not rejected with event 21", rule)
    hk = cfs.hk()
    p.check(hk.file_adds_rejected == 2, f"FileAddsRejected {hk.file_adds_rejected} != 2", rule)
    p.check(not (cfs.files_dir / "one-more.bin").exists(), "a file exists for the rejected add", rule)
    big.unlink(missing_ok=True)
    p.raise_if()


def c_reset_counters(ctx: Ctx) -> None:
    # Setup that makes every counter non-zero before the reset: threshold 100 lets the quota fill and drop packets
    # (12 of 14 packets of A stored, then both stored-due packets of B dropped, 2 of B decimated), SET_PURGE_THRESHOLD
    # 50 then purges two closed files, and an ADD_FILE beyond the file quota is rejected.
    table = quiet_table(telemetry_quota=4096, max_packet_file=1024, threshold=100, file_quota=2048,
                        entries=((sl.INJ_A, 10, 1), (sl.INJ_B, 20, 2)))
    cfs = ctx.session("reset", table)
    p = Problems()
    cfs.inject(sl.INJ_A, 14, 300)
    cfs.inject(sl.INJ_B, 4, 300)
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([50, 0, 0, 0]))
    cfs.cmd(sl.CC_NOOP)
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.INJ_A, 42))
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(0x0FFE, 2))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "keep.tbl"))
    big = cfs.cf / "big.bin"
    big.write_bytes(bytes(3000))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "toobig.bin"))
    cfs.settle()
    pre = cfs.hk()
    st_pre = cfs.stats()
    unset = [label for label, v in (("PacketsStored", pre.packets_stored), ("PacketsDecimated", pre.packets_decimated),
                                    ("PacketsDropped", pre.packets_dropped), ("FilesPurged", pre.files_purged),
                                    ("PurgeCycles", pre.purge_cycles), ("CommandCounter", pre.command_counter),
                                    ("CommandErrorCounter", pre.command_error_counter),
                                    ("FileAddsRejected", pre.file_adds_rejected)) if v == 0]
    if unset:
        # Without the setup state the reset cannot be judged; the behaviours behind it are checked elsewhere too.
        raise Fail(f"setup state not reached before RESET_COUNTERS: {pre}",
                   "RESET_COUNTERS could not be checked: the setup before it (storing until the telemetry quota drops "
                   "packets, SET_PURGE_THRESHOLD 50 purging closed files, commands accepted and rejected, and an ADD_FILE "
                   f"beyond the file quota) left {', '.join(unset)} at 0.")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_RESET)
    p.check(bool(wait_event(cfs, mark, 4)), "RESET_COUNTERS did not produce event 4", "RESET_COUNTERS must send event 4 (RESET_INF).")
    post = cfs.hk()
    zero_rule = ("RESET_COUNTERS must zero CommandCounter, CommandErrorCounter, PacketsStored, PacketsDecimated, "
                 "PacketsDropped, FilesPurged, PurgeCycles, FileAddsRejected and all per-entry counters, and must not "
                 "increment CommandCounter afterwards (both command counters read 0).")
    zero = {"command_counter": 0, "command_error_counter": 0, "packets_stored": 0, "packets_decimated": 0,
            "packets_dropped": 0, "files_purged": 0, "purge_cycles": 0, "file_adds_rejected": 0}
    for k, v in zero.items():
        p.check(getattr(post, k) == v, f"{k} is {getattr(post, k)} after RESET_COUNTERS", zero_rule)
    kept_rule = ("RESET_COUNTERS must not change byte accounting, FileCount, open packet files, the threshold, quotas or "
                 "ActiveEntries.")
    kept = {"telemetry_used": pre.telemetry_used, "file_used": pre.file_used, "file_count": pre.file_count,
            "open_files": pre.open_files, "purge_threshold": pre.purge_threshold,
            "telemetry_quota": pre.telemetry_quota, "active_entries": pre.active_entries}
    for k, v in kept.items():
        p.check(getattr(post, k) == v, f"{k} changed {v} -> {getattr(post, k)} by RESET_COUNTERS", kept_rule)
    st = cfs.stats()
    for mid in (sl.INJ_A, sl.INJ_B):
        o = stats_entry(p, st, mid, "after reset")
        if o is None:
            continue
        p.check((o.received, o.stored, o.decimated, o.dropped) == (0, 0, 0, 0),
                f"{mid:#06x} statistics not zero after RESET_COUNTERS: {o} (before: {st_pre.get(mid)})", zero_rule)
    a = st.get(sl.INJ_A)
    if a is not None:
        p.check(a.priority == 42, f"runtime priority {a.priority} changed by RESET_COUNTERS (was 42)",
                "RESET_COUNTERS must not change run-time parameters such as a priority set by SET_PRIORITY.")
    big.unlink(missing_ok=True)
    p.raise_if()


# ============================================================================
# CTF, CCDD, report, hidden test
# ============================================================================

REQUIRED_CTF_WORDS = ("NOOP", "SET_PRIORITY", "ADD_FILE", "LIST_ALL", "DELETE_FILE", "SEND_STATS")


def c_ccdd_definitions(ctx: Ctx) -> None:
    ctx.copy_tree()
    d = ctx.tree / "ctf" / "ccdd" / "json"
    p = Problems()
    texts: list[str] = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        raw = f.read_text(errors="replace")
        if "STOR" not in raw.upper():
            continue
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            p.check(False, f"{f.name} is not valid JSON: {exc}", f"ctf/ccdd/json/{f.name} must be valid JSON.")
            continue
        texts.append(raw)
    blob = "\n".join(texts)
    p.check(bool(texts), "no CCDD JSON file under ctf/ccdd/json mentions STOR",
            "CTF definitions for STOR's commands and telemetry must exist as JSON under ctf/ccdd/json.")
    for word in ("NOOP", "RESET_COUNTERS", "SET_PRIORITY", "SET_DECIMATION", "SET_PURGE_THRESHOLD", "ADD_FILE",
                 "DELETE_FILE", "LIST_DIR", "LIST_ALL", "SEND_STATS", "CLOSE_FILES"):
        p.check(re.search(r'"cc_name"\s*:\s*"[A-Za-z0-9_]*' + word + r'[A-Za-z0-9_]*"', blob) is not None,
                f"no command code definition for {word}",
                f"The CTF definitions under ctf/ccdd/json must define the STOR command code {word} (cc_name).")
    for mid in ("STOR_CMD_MID", "STOR_SEND_HK_MID", "STOR_WAKEUP_MID", "STOR_HK_TLM_MID", "STOR_LIST_TLM_MID",
                "STOR_STATS_TLM_MID"):
        p.check(mid in blob or mid in _mission_mids(d), f"{mid} is not defined",
                f"The CTF definitions under ctf/ccdd/json must define the message ID {mid}.")
    for fld in ("TelemetryBytesUsed", "PacketsDropped", "FilesPurged", "TotalEntries", "Received", "Decimated"):
        p.check(fld in blob, f"telemetry field {fld} is not defined",
                f"The CTF telemetry definitions must include the field {fld}.")
    for fld in ("MsgId", "Priority", "Decimation", "Percent", "SourcePath", "DestPath"):
        p.check(fld in blob, f"command field {fld} is not defined",
                f"The CTF command definitions must include the field {fld}.")
    p.raise_if()


def _mission_mids(d: Path) -> str:
    f = d / "mission_mids.json"
    return f.read_text(errors="replace") if f.is_file() else ""


def ctf_failure_detail(results: Path, output: str) -> str:
    """The failing CTF steps and the result summary, from the CTF results tree and the runner's output."""
    parts: list[str] = []
    for summary in sorted(results.rglob("results_summary.txt")):
        rows = [ln for ln in summary.read_text(errors="replace").splitlines() if ln.strip() and not set(ln) <= {"-"}]
        parts.append("summary: " + " | ".join(" ".join(r.split()) for r in rows[:8]))
    for log in sorted(results.rglob("logs/*/*.log")):
        for ln in log.read_text(errors="replace").splitlines():
            if "TEST_FAIL" in ln or "TEST_ERROR" in ln or "*** ERROR" in ln:
                parts.append(ln.strip()[:600])
    if not parts:
        lines = [ln for ln in output.splitlines() if re.search(r"fail|error|Traceback", ln, re.I)]
        parts.append("runner output: " + " | ".join(lines[-20:]) + " || tail: " + output[-800:])
    return " || ".join(parts)


def c_ctf_test_passes(ctx: Ctx) -> None:
    ctx.build()
    test = ctx.tree / "ctf" / "tests" / "stor_basic.json"
    if not test.is_file():
        raise Fail("ctf/tests/stor_basic.json does not exist", "The CTF test ctf/tests/stor_basic.json must exist.")
    p = Problems()
    try:
        doc = json.loads(test.read_text())
    except json.JSONDecodeError as exc:
        raise Fail(f"stor_basic.json is not valid JSON: {exc}", "ctf/tests/stor_basic.json must be valid JSON.") from None
    flat = json.dumps(doc)
    for w in REQUIRED_CTF_WORDS:
        p.check(w in flat, f"the CTF test does not exercise {w}", f"ctf/tests/stor_basic.json must exercise {w}.")
    p.check(("CFE_EVS_HK_TLM_MID" in flat or "0x0801" in flat.lower() or "2049" in flat) and "Received" in flat,
            "the CTF test does not check the Received count of the CFE_EVS_HK_TLM_MID statistics entry",
            "ctf/tests/stor_basic.json must check that SEND_STATS shows CFE_EVS_HK_TLM_MID with Received greater than 0.")
    p.raise_if()
    if ctx.cfs is not None:
        ctx.cfs.stop()
        ctx.cfs.restore_table()
        ctx.session_key = None
    shutil.rmtree(ctx.tree / "build-native_std" / "exe" / "cpu1" / "cf" / "stor", ignore_errors=True)
    results = ctx.work / "ctf-results"
    env = dict(os.environ, CFS_ROOT=str(ctx.tree), CTF_RESULTS_DIR=str(results))
    run = ctx.tree / "ctf" / "run_ctf.sh"
    if not run.is_file():
        raise Fail("ctf/run_ctf.sh does not exist", "ctf/run_ctf.sh must exist (it is part of the mission).")
    rc, out, slow = ctx.run(["bash", str(run), str(test)], 360, env=env)
    if rc != 0:
        detail = f"run_ctf.sh stor_basic.json exited {rc}{' (timed out)' if slow else ''}: " + ctf_failure_detail(results, out)
        feedback = "ctf/tests/stor_basic.json must pass when run with ctf/run_ctf.sh; it " + (
            "did not finish within 6 minutes." if slow else "fails.")
        smoke = ctx.tree / "ctf" / "tests" / "smoke_sample_app.json"
        if smoke.is_file() and not slow:
            src, sout, _ = ctx.run(["bash", str(run), str(smoke)], 240, env=dict(env, CTF_RESULTS_DIR=str(results) + "-smoke"))
            if src != 0:
                detail += f" || the mission's own smoke test fails too (exit {src}): " + ctf_failure_detail(
                    Path(str(results) + "-smoke"), sout)
                feedback += (" CTF cannot even run the mission's sample smoke test in this tree, so also check that "
                             "every CCDD JSON file you added under ctf/ccdd/json loads and that the CTF set-up is "
                             "unchanged.")
        raise Fail(detail, feedback)
    shutil.rmtree(ctx.tree / "build-native_std" / "exe" / "cpu1" / "cf" / "stor", ignore_errors=True)


def c_report_present(ctx: Ctx) -> None:
    ctx.copy_tree()
    f = ctx.tree / "docs" / "reports" / "stor.md"
    if not f.is_file():
        raise Fail("docs/reports/stor.md does not exist", "The report docs/reports/stor.md must exist.")
    text = f.read_text(errors="replace")
    low = text.lower()
    p = Problems()
    p.check(len(text.strip()) >= 800, f"report is only {len(text.strip())} characters",
            "docs/reports/stor.md must be a substantive report (it is very short).")
    p.check("design" in low, "no design content", "docs/reports/stor.md must contain a design summary.")
    p.check("unit test" in low or "unit-test" in low or "coverage test" in low, "no unit test content",
            "docs/reports/stor.md must report the unit test counts.")
    p.check("coverage" in low and COVERAGE_FIGURE_RE.search(text) is not None, "no coverage figures",
            "docs/reports/stor.md must report the coverage figures.")
    p.check("ctf" in low, "no CTF content", "docs/reports/stor.md must report the CTF results.")
    p.check("limitation" in low, "no limitations section", "docs/reports/stor.md must list limitations.")
    p.raise_if()


def c_hidden_coverage_test(ctx: Ctx) -> None:
    ctx.build()
    if not list((ctx.stor / "fsw" / "src").glob("*.c")):
        raise Fail("apps/stor has no sources", "apps/stor/fsw/src must hold STOR's C sources.")
    from hidden_glue import run_hidden  # type: ignore[import-not-found]
    run_hidden(ctx, PACK)


# ============================================================================
# registry
# ============================================================================

CHECKS: list[CheckSpec] = [
    CheckSpec("app-registered",
              "STOR must exist as a new application in apps/stor (fsw/src, fsw/inc, fsw/tables, config and unit-test, with "
              "STOR_AppMain as the entry point and stor_topicids.h, stor_eventids.h, stor_fcncodes.h, "
              "config/default_stor_tbldefs.h and fsw/tables/stor_tbl.c), listed in MISSION_GLOBAL_APPLIST in "
              "sample_defs/targets.cmake, with OSAL_CONFIG_MAX_FILE_NAME set to 32 in sample_defs/default_osconfig.cmake, "
              "scheduled in sample_defs/tables/sch_lab_table.c (STOR_SEND_HK_MID every 4.9 s and STOR_WAKEUP_MID every "
              "1.0 s) and forwarded by sample_defs/tables/to_lab_sub.c (STOR_HK_TLM_MID and STOR_STATS_TLM_MID with "
              "buffer limit 1, STOR_LIST_TLM_MID with buffer limit 4).", c_app_registered),
    CheckSpec("build-no-warnings",
              "The mission must configure with make native_std.prep and build with make native_std.install with no "
              "compiler warnings, including the STOR sources, tables and the registrations in sample_defs.",
              c_build_no_warnings),
    CheckSpec("unit-tests-pass",
              "make native_std.runtest must pass: STOR's unit tests build and succeed, and the mission's existing tests "
              "still pass.", c_unit_tests_pass),
    CheckSpec("stor-coverage-tests",
              "apps/stor/unit-test/coveragetest must hold one coverage test per STOR source module "
              "(coveragetest_<module>.c), built and run by make native_std.runtest, each with passing assertions and "
              "none failing.", c_stor_coverage_tests),
    CheckSpec("coverage-threshold",
              "Line coverage of every STOR source file in apps/stor/fsw/src, as measured with gcov from the unit-test "
              "build, must be at least 95 percent.", c_coverage_threshold),
    CheckSpec("hidden-coverage-test",
              "STOR must behave as specified when driven only through its entry point STOR_AppMain with messages on the "
              "software bus, using only the names, structures, message IDs, function codes and event IDs defined in the "
              "specification.", c_hidden_coverage_test),
    CheckSpec("default-table-contents",
              "The installed StorageTable (/cf/stor_tbl.tbl) must use the structure in the specification and default to "
              "roots /cf/stor/tlm and /cf/stor/files, TelemetryQuotaBytes 262144, FileQuotaBytes 1048576, "
              "MaxPacketFileBytes 16384, PurgeThresholdPercent 80 and the four entries CFE_EVS_HK_TLM_MID (priority 10, "
              "decimation 1), CFE_ES_HK_TLM_MID (20, 1), SAMPLE_APP_HK_TLM_MID (30, 2) and CFE_TIME_HK_TLM_MID (40, 1).",
              c_default_table_contents),
    CheckSpec("scheduled-housekeeping",
              "With STOR_SEND_HK_MID scheduled every 4.9 s and STOR_HK_TLM_MID forwarded by TO_LAB, STOR housekeeping "
              "(message ID 0x08C4) must reach the ground without any request.", c_scheduled_housekeeping),
    CheckSpec("startup-and-hk",
              "STOR must start with the mission on cpu1, send its initialization event (ID 1) once, and answer SEND_HK "
              "with a STOR_HkTlm_t on STOR_HK_TLM_MID in the specified field order.", c_startup_and_hk),
    CheckSpec("decimation-and-stats",
              "STOR must count every received packet whose MID is a used table entry and store it only when "
              "(Received - 1) % Decimation == 0; SEND_STATS must report every used entry.", c_decimation_and_stats),
    CheckSpec("packet-file-naming-and-header",
              "Stored packets must be appended, as received, to files named <TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin, "
              "each starting with the specified CFE_FS_Header_t.", c_packet_file_naming_and_header),
    CheckSpec("rollover", ROLLOVER_RULE, c_rollover),
    CheckSpec("idle-file-close",
              "On every WAKEUP STOR must close any packet file not written for 10 seconds or more, and not before.",
              c_idle_file_close),
    CheckSpec("purge-order", PURGE_RULE, c_purge_order),
    CheckSpec("quota-drop", QUOTA_RULE, c_quota_drop),
    CheckSpec("restart-scan",
              "At startup STOR must initialize TelemetryBytesUsed, FileBytesUsed and FileCount from the files already "
              "present and continue each MID's packet file sequence.", c_restart_scan),
    CheckSpec("set-param-errors",
              "SET_PRIORITY, SET_DECIMATION and SET_PURGE_THRESHOLD must validate their values as specified, and invalid "
              "function codes and lengths must be rejected and counted.", c_set_param_errors),
    CheckSpec("file-add-list-delete",
              "ADD_FILE, LIST_DIR, LIST_ALL and DELETE_FILE must behave as specified.", c_file_add_list_delete),
    CheckSpec("file-path-rejection",
              "A file area path that contains .. or starts with / must be rejected with event 18 and counted as a command "
              "error.", c_file_path_rejection),
    CheckSpec("file-exists-rejection",
              "ADD_FILE must reject an existing destination with event 20 and an unopenable source with event 19.",
              c_file_exists_rejection),
    CheckSpec("file-quota-rejection",
              "ADD_FILE must reject a file whose size plus FileBytesUsed exceeds FileQuotaBytes with event 21, while a "
              "file that exactly fills the quota is accepted.", c_file_quota_rejection),
    CheckSpec("reset-counters",
              "RESET_COUNTERS must zero the specified counters without changing byte accounting, FileCount, open packet "
              "files or run-time parameters.", c_reset_counters),
    CheckSpec("ccdd-definitions",
              "CTF definitions for all STOR commands and telemetry must exist as JSON under ctf/ccdd/json.",
              c_ccdd_definitions),
    CheckSpec("ctf-test-passes",
              "ctf/tests/stor_basic.json must exist, cover the specified steps, and pass when run with ctf/run_ctf.sh.",
              c_ctf_test_passes),
    CheckSpec("report-present",
              "docs/reports/stor.md must contain the design summary, file layout, unit test counts, coverage figures, CTF "
              "results and limitations.", c_report_present),
]

assert len({c.check_id for c in CHECKS}) == len(CHECKS)
assert all(ID_RE.match(c.check_id) for c in CHECKS)
assert LIVE_CHECKS <= {c.check_id for c in CHECKS}

# Execution order: static checks and the build first, then the live scenarios, then the hidden test.
RUN_ORDER = [
    "app-registered", "build-no-warnings", "unit-tests-pass", "stor-coverage-tests", "coverage-threshold",
    "default-table-contents",
    "scheduled-housekeeping", "startup-and-hk", "decimation-and-stats", "packet-file-naming-and-header", "rollover",
    "idle-file-close", "purge-order", "quota-drop", "restart-scan", "set-param-errors", "file-add-list-delete",
    "file-path-rejection", "file-exists-rejection", "file-quota-rejection", "reset-counters", "ccdd-definitions",
    "ctf-test-passes", "report-present", "hidden-coverage-test",
]
assert sorted(RUN_ORDER) == sorted(c.check_id for c in CHECKS)


def run_check(ctx: Ctx, spec: CheckSpec) -> str:
    """Run one check and return a note for the detail. A live scenario that fails is run once more from a fresh cFS
    before it counts as failed; a first-attempt failure is always reported (log and detail)."""
    try:
        spec.fn(ctx)
        return ""
    except Fail as first:
        if (spec.check_id not in LIVE_CHECKS or first.feedback == NOT_VERIFIED_TIME
                or (isinstance(first, SharedFail) and not first.retry)):
            raise
        print(f"[{spec.check_id}] FIRST ATTEMPT FAILED: {first}; retrying once from a fresh cFS", flush=True)
        if ctx.cfs is not None:
            ctx.cfs.stop()
        ctx.session_key = None
        try:
            spec.fn(ctx)
        except Fail as second:
            second.args = (f"{second} [first attempt: {first}]",)
            raise
        return f"passed on the second attempt; first attempt failed: {first}"


def feedback_for(ctx: Ctx, spec: CheckSpec, exc: Fail) -> str:
    fb = exc.feedback or spec.feedback
    if isinstance(exc, SharedFail):
        # A cause shared by several checks (build, table image, cFS startup) is described once in full; the table
        # a scenario used does not make a startup failure a different one.
        key = re.sub(r"^With (the default StorageTable|a StorageTable with [^\[]*\[[^\]]*\]), ", "", fb)
        if key in ctx.start_feedback_seen:
            return ("This requirement could not be checked because of a problem described in full in another item: "
                    + (key if len(key) <= 140 else key[:137].rsplit(" ", 1)[0] + " ..."))
        ctx.start_feedback_seen.add(key)
    return fb


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check.py <repo-path> <out.json>", file=sys.stderr)
        return 2
    repo = Path(argv[1]).resolve()
    out = Path(argv[2]).resolve()
    if not repo.is_dir():
        print(f"repository {repo} not found", file=sys.stderr)
        return 2
    if not (repo / "Makefile").is_file() or not (repo / "tools" / "run_cfs.sh").is_file():
        print(f"{repo} is not a cFS mission tree", file=sys.stderr)
        return 2
    for tool in ("make", "cmake", "gcc", "gcov", "git"):
        if shutil.which(tool) is None:
            print(f"required tool {tool} not found", file=sys.stderr)
            return 2
    deadline = time.monotonic() + TOTAL_BUDGET_S

    def on_term(*_: object) -> None:
        raise Terminated()

    signal.signal(signal.SIGTERM, on_term)
    base = os.environ.get("STOR_PACK_TMP") or "/workspace/tmp-acceptance"
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="stor-pack-", dir=base))
    except OSError as exc:
        print(f"infrastructure error: cannot create scratch space under {base}: {exc}", file=sys.stderr)
        return 2
    ctx = Ctx(repo, work, deadline)
    by_id = {c.check_id: c for c in CHECKS}
    results: dict[str, dict[str, object]] = {}
    only = [x for x in os.environ.get("STOR_PACK_ONLY", "").split(",") if x]
    current = ""
    try:
        for cid in RUN_ORDER:
            current = cid
            spec = by_id[cid]
            if only and cid not in only:
                results[cid] = {"id": cid, "passed": False, "feedback": spec.feedback, "detail": "not run (STOR_PACK_ONLY)"}
                continue
            if ctx.remaining() < MIN_CHECK_BUDGET_S:
                results[cid] = {"id": cid, "passed": False, "feedback": NOT_VERIFIED_TIME,
                                "detail": "not run: the time budget was exhausted"}
                print(f"[{cid}] FAILED: not run, time budget exhausted", flush=True)
                continue
            t0 = time.monotonic()
            try:
                note = run_check(ctx, spec)
                if cid == "unit-tests-pass":
                    note = ctx.unit_note or note
                results[cid] = {"id": cid, "passed": True, "feedback": "",
                                "detail": f"{time.monotonic() - t0:.0f} s" + (f"; {note}" if note else "")}
            except Fail as exc:
                results[cid] = {"id": cid, "passed": False, "feedback": feedback_for(ctx, spec, exc),
                                "detail": f"{exc} ({time.monotonic() - t0:.0f} s)"}
            except Exception as exc:  # noqa: BLE001 - a check must never take the whole run down
                tb = traceback.format_exc()
                print(tb, file=sys.stderr)
                results[cid] = {"id": cid, "passed": False, "feedback": spec.feedback,
                                "detail": f"unexpected {type(exc).__name__}: {exc}\n{tb[-1500:]}"}
            print(f"[{cid}] {'ok' if results[cid]['passed'] else 'FAILED'}: {results[cid]['detail']}", flush=True)
    except Terminated:
        print(f"terminated during {current}; remaining checks reported as not verified", file=sys.stderr)
        for cid in RUN_ORDER:
            if cid not in results:
                results[cid] = {"id": cid, "passed": False, "feedback": NOT_VERIFIED_TIME,
                                "detail": "not run or interrupted: the outer time limit stopped the review"
                                          + (" during this check" if cid == current else "")}
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ctx.shutdown()
        if os.environ.get("STOR_PACK_KEEP") != "1":
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"scratch kept at {work}", file=sys.stderr)
    checks = [results[c.check_id] for c in CHECKS]
    passed = all(c["passed"] for c in checks)
    out.write_text(json.dumps({"passed": passed, "checks": checks}, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
