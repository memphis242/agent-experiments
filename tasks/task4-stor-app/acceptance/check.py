#!/usr/bin/env python3
"""Acceptance checks for the STOR storage management application.

Usage: check.py <repo-path> <out.json>

The repository is copied to a scratch directory and built there; the original
is never modified. Exit status: 0 every check passed, 1 at least one failed,
2 infrastructure error (tooling missing, bad arguments, pack defect).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PACK = Path(__file__).resolve().parent
sys.path.insert(0, str(PACK))

import stor_live as sl  # noqa: E402
from stor_live import Fail, InfraError, Problems  # noqa: E402

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TOTAL_BUDGET_S = 29 * 60
JOBS = "-j16"
SOURCE_PCT_MIN = 95.0

# -- table geometry for the injected-packet scenarios -------------------------------
# Packets are 700 bytes so that a 4096-byte packet file holds five of them
# (64-byte header + 5 * 700 = 3564) and the sixth forces a roll-over.
PKT = 700
FILE_FULL = 64 + 5 * PKT


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
        self.prepared = False
        self.runtest_done = False
        self.cfs: sl.Cfs | None = None
        self.session_key: str | None = None
        self.pristine_table: bytes | None = None

    # -- time budget -------------------------------------------------------------
    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def timeout_for(self, want: float) -> float:
        left = self.remaining() - 20
        if left < 30:
            raise Fail("the time budget for the review ran out before this step could run")
        return min(want, left)

    # -- repository copy and build ---------------------------------------------------
    def copy_tree(self) -> None:
        if self.tree.exists():
            return
        skip = shutil.ignore_patterns(".git", "build-native_std", "results", "__pycache__", "*.pyc")
        shutil.copytree(self.repo, self.tree, ignore=skip, symlinks=True)

    def sh(self, cmd: list[str], timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(cmd, cwd=self.tree, text=True, errors="replace", capture_output=True,
                                  timeout=self.timeout_for(timeout), env=env)
        except subprocess.TimeoutExpired as exc:
            tail = (exc.stdout or b"")[-600:] if isinstance(exc.stdout, bytes) else str(exc.stdout or "")[-600:]
            raise Fail(f"`{' '.join(cmd)}` did not finish within {timeout:.0f} s; output tail: {tail}") from None

    def build(self) -> None:
        """Configure and build once; later calls only report the stored outcome."""
        if self.build_ok is not None:
            if not self.build_ok:
                raise Fail("the mission does not configure and build: " + self.build_log[-1200:])
            return
        self.copy_tree()
        prep = self.sh(["make", "native_std.prep"], 300)
        log = prep.stdout + prep.stderr
        ok = prep.returncode == 0
        if ok:
            inst = self.sh(["make", JOBS, "native_std.install"], 900)
            log += inst.stdout + inst.stderr
            ok = inst.returncode == 0
        self.build_ok = ok
        self.build_log = log
        self.warnings = [ln for ln in log.splitlines() if "warning:" in ln]
        if not ok:
            lines = [ln for ln in log.splitlines() if "error" in ln.lower() or "Error" in ln]
            self.build_log = "\n".join(lines[-25:]) or log[-1500:]
            raise Fail("the mission does not configure and build: " + self.build_log[-1200:])

    # -- cFS sessions -----------------------------------------------------------------
    def table_file(self) -> bytes:
        if self.pristine_table is None:
            self.build()
            p = self.tree / "build-native_std" / "exe" / "cpu1" / "cf" / "stor_tbl.tbl"
            if not p.is_file():
                raise Fail("the build does not install the storage table file stor_tbl.tbl")
            self.pristine_table = p.read_bytes()
        return self.pristine_table

    def session(self, key: str, table: sl.TableSpec | None = None, seed: dict[str, bytes] | None = None,
                reuse: bool = False) -> sl.Cfs:
        self.build()
        image = self.table_file()
        if len(image) != sl.TABLE_FILE_HDR + sl.TABLE_NAME_SIZE:
            raise Fail(f"the installed StorageTable image is {len(image)} bytes; the structure defined in the "
                       f"specification is {sl.TABLE_NAME_SIZE} bytes")
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
    p.check(stor.is_dir(), "apps/stor does not exist")
    if stor.is_dir():
        for sub in ("fsw/src", "fsw/inc", "fsw/tables", "config", "unit-test"):
            p.check((stor / sub).is_dir(), f"apps/stor/{sub} is missing")
        srcs = list((stor / "fsw" / "src").glob("*.c")) if (stor / "fsw" / "src").is_dir() else []
        p.check(bool(srcs), "apps/stor/fsw/src has no C sources")
        text = "\n".join(s.read_text(errors="replace") for s in srcs)
        p.check("STOR_AppMain" in text, "no STOR_AppMain entry point")
        for hdr in ("stor_topicids.h", "stor_eventids.h", "stor_fcncodes.h"):
            p.check((stor / "fsw" / "inc" / hdr).is_file(), f"fsw/inc/{hdr} is missing")
        for cfg in ("default_stor_tbldefs.h", "default_stor_fcncode_values.h"):
            p.check((stor / "config" / cfg).is_file(), f"config/{cfg} is missing")
        p.check((stor / "fsw" / "tables" / "stor_tbl.c").is_file(), "fsw/tables/stor_tbl.c is missing")
    targets = t / "sample_defs" / "targets.cmake"
    listed = False
    if targets.is_file():
        for line in targets.read_text(errors="replace").splitlines():
            code = line.split("#", 1)[0]
            if "MISSION_GLOBAL_APPLIST" in code and "STATIC" not in code and re.search(r"(?<![\w-])stor(?![\w-])", code):
                listed = True
    p.check(listed, "stor is not in MISSION_GLOBAL_APPLIST in sample_defs/targets.cmake")
    sch = t / "sample_defs" / "tables" / "sch_lab_table.c"
    if sch.is_file():
        s = strip_c_comments(sch.read_text(errors="replace"))
        for name, period in (("STOR_SEND_HK_MID", 49), ("STOR_WAKEUP_MID", 10)):
            m = re.search(r"\{\s*CFE_SB_MSGID_WRAP_VALUE\(\s*" + name + r"\s*\)\s*,\s*(\d+)\s*,\s*(\d+)\s*\}", s)
            if not m:
                p.check(False, f"sch_lab_table.c has no {name} entry")
            else:
                p.check(int(m.group(1)) == period and int(m.group(2)) == 0,
                        f"sch_lab_table.c {name} entry is ({m.group(1)}, {m.group(2)}), expected ({period}, 0)")
    else:
        p.check(False, "sample_defs/tables/sch_lab_table.c missing")
    to = t / "sample_defs" / "tables" / "to_lab_sub.c"
    if to.is_file():
        s = strip_c_comments(to.read_text(errors="replace"))
        for name in ("STOR_HK_TLM_MID", "STOR_LIST_TLM_MID", "STOR_STATS_TLM_MID"):
            p.check(re.search(r"\{\s*CFE_SB_MSGID_WRAP_VALUE\(\s*" + name + r"\s*\)", s) is not None,
                    f"to_lab_sub.c has no {name} entry")
    else:
        p.check(False, "sample_defs/tables/to_lab_sub.c missing")
    p.raise_if()


def c_build_no_warnings(ctx: Ctx) -> None:
    ctx.build()
    if ctx.warnings:
        raise Fail(f"{len(ctx.warnings)} compiler warnings, first: {ctx.warnings[0]}")


def c_unit_tests_pass(ctx: Ctx) -> None:
    ctx.build()
    srcs = sorted((ctx.stor / "fsw" / "src").glob("*.c"))
    if not srcs:
        raise Fail("apps/stor has no sources")
    proc = ctx.sh(["make", JOBS, "native_std.runtest"], 600)
    ctx.runtest_done = True
    p = Problems()
    p.check(proc.returncode == 0, f"runtest exit {proc.returncode}: " + (proc.stdout + proc.stderr)[-700:])
    logs = sorted((ctx.tree / "build-native_std" / "test-results").glob("coverage-stor-*.log"))
    p.check(bool(logs), "no coverage-stor-*.log was produced")
    names = {l.name for l in logs}
    for s in srcs:
        if "eds" in s.stem:
            continue
        p.check(f"coverage-stor-{s.stem}.log" in names, f"no test log for module {s.stem}")
    for lg in logs:
        text = lg.read_text(errors="replace")
        fails = re.findall(r"FAIL::(\d+)", text)
        passes = re.findall(r"PASS::(\d+)", text)
        p.check(all(int(f) == 0 for f in fails), f"{lg.name}: failing assertions")
        p.check(sum(int(x) for x in passes) > 0, f"{lg.name}: no passing assertions")
        p.check("[ FAIL]" not in text, f"{lg.name}: FAIL lines")
    p.raise_if()


def parse_lcov(text: str) -> dict[str, tuple[int, int]]:
    """Map source path -> (lines found, lines hit) from an lcov tracefile."""
    out: dict[str, tuple[int, int]] = {}
    sf = ""
    lf = lh = 0
    for line in text.splitlines():
        if line.startswith("SF:"):
            sf, lf, lh = line[3:].strip(), 0, 0
        elif line.startswith("LF:"):
            lf = int(line[3:])
        elif line.startswith("LH:"):
            lh = int(line[3:])
        elif line.startswith("end_of_record") and sf:
            f0, h0 = out.get(sf, (0, 0))
            out[sf] = (f0 + lf, h0 + lh)
            sf = ""
    return out


def c_coverage_threshold(ctx: Ctx) -> None:
    ctx.build()
    srcs = sorted((ctx.stor / "fsw" / "src").glob("*.c"))
    if not srcs:
        raise Fail("apps/stor has no sources")
    if not ctx.runtest_done:
        ctx.sh(["make", JOBS, "native_std.runtest"], 600)
        ctx.runtest_done = True
    proc = ctx.sh(["make", JOBS, "native_std.lcov"], 600)
    info = ctx.tree / "build-native_std" / "native" / "coverage_basic.info"
    if proc.returncode != 0 or not info.is_file():
        raise InfraError("`make native_std.lcov` failed or wrote no tracefile: " + (proc.stdout + proc.stderr)[-800:])
    cov = parse_lcov(info.read_text(errors="replace"))
    p = Problems()
    for s in srcs:
        rel = f"apps/stor/fsw/src/{s.name}"
        hit = [v for k, v in cov.items() if k.replace("\\", "/").endswith("/" + rel) or k == rel]
        if not hit:
            if "eds" in s.stem:
                continue
            p.check(False, f"{rel}: no coverage data")
            continue
        lf = sum(h[0] for h in hit)
        lh = sum(h[1] for h in hit)
        pct = 100.0 if lf == 0 else 100.0 * lh / lf
        p.check(pct >= SOURCE_PCT_MIN, f"{rel}: {pct:.1f}% of {lf} lines covered (< {SOURCE_PCT_MIN}%)")
    p.raise_if()


def c_default_table_contents(ctx: Ctx) -> None:
    image = ctx.table_file()
    if len(image) != sl.TABLE_FILE_HDR + sl.TABLE_NAME_SIZE:
        raise Fail(f"table image is {len(image)} bytes, expected {sl.TABLE_FILE_HDR + sl.TABLE_NAME_SIZE}")
    name = image[sl.CFE_FS_HDR + 12:sl.CFE_FS_HDR + 52].split(b"\0", 1)[0]
    spec = sl.unpack_table(image[sl.TABLE_FILE_HDR:])
    p = Problems()
    p.check(name.endswith(b"StorageTable"), f"table name {name!r} is not StorageTable")
    p.check(spec.telemetry_root == "/cf/stor/tlm", f"TelemetryRoot {spec.telemetry_root!r}")
    p.check(spec.file_root == "/cf/stor/files", f"FileRoot {spec.file_root!r}")
    p.check(spec.telemetry_quota == 262144, f"TelemetryQuotaBytes {spec.telemetry_quota}")
    p.check(spec.file_quota == 1048576, f"FileQuotaBytes {spec.file_quota}")
    p.check(spec.max_packet_file == 16384, f"MaxPacketFileBytes {spec.max_packet_file}")
    p.check(spec.threshold == 80, f"PurgeThresholdPercent {spec.threshold}")
    want = {(sl.EVS_HK_MID, 10, 1), (sl.ES_HK_MID, 20, 1), (sl.SAMPLE_HK_MID, 30, 2), (sl.TIME_HK_MID, 40, 1)}
    p.check(set(spec.entries) == want and len(spec.entries) == 4,
            f"entries {sorted(spec.entries)} != {sorted(want)}")
    p.raise_if()


# ============================================================================
# live checks
# ============================================================================

def c_scheduled_housekeeping(ctx: Ctx) -> None:
    cfs = ctx.session("default-a")
    assert cfs.rx is not None
    got = cfs.unsolicited(sl.HK_MID, 7.5)
    if not got:
        raise Fail("no STOR housekeeping packet within 7.5 s of enabling telemetry output without a request")
    sl.parse_hk(got[-1])


def c_startup_and_hk(ctx: Ctx) -> None:
    cfs = ctx.session("default-a", reuse=True)
    p = Problems()
    p.check(len(cfs.events(0, 1)) == 1, "initialization event 1 not emitted exactly once")
    got = cfs._request(sl.SEND_HK_MID, 0, b"", sl.HK_MID)
    if not got:
        raise Fail("no housekeeping packet (message ID 0x08C4) followed SEND_HK")
    raw = got[-1]
    p.check(sl.tlm_len(raw) == sl.TLM_HDR + sl.HK_SIZE, f"housekeeping packet is {sl.tlm_len(raw)} bytes")
    hk = sl.parse_hk(raw)
    p.check(hk.telemetry_quota == 262144, f"TelemetryQuotaBytes {hk.telemetry_quota}")
    p.check(hk.file_quota == 1048576, f"FileQuotaBytes {hk.file_quota}")
    p.check(hk.purge_threshold == 80, f"PurgeThresholdPercent {hk.purge_threshold}")
    p.check(hk.active_entries == 4, f"ActiveEntries {hk.active_entries}")
    p.check(hk.command_counter == 0 and hk.command_error_counter == 0,
            f"counters {hk.command_counter}/{hk.command_error_counter} before any command")
    p.check(hk.file_count == 0 and hk.file_used == 0, f"FileCount {hk.file_count}, FileBytesUsed {hk.file_used}")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_NOOP)
    p.check(bool(wait_event(cfs, mark, 3)), "NOOP did not produce event 3")
    hk2 = cfs.hk()
    p.check(hk2.command_counter == 1, f"CommandCounter {hk2.command_counter} after one NOOP")
    # The default table's four streams are live: their files appear without any command.
    time.sleep(1.0)
    p.check(len(cfs.packet_files()) >= 1, "no packet file exists after housekeeping traffic was stored")
    p.raise_if()


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def c_decimation_and_stats(ctx: Ctx) -> None:
    p = Problems()
    # Part 1: default table, the SAMPLE_APP housekeeping stream (decimation 2).
    cfs = ctx.session("decim-default")
    base = cfs.stats().get(sl.SAMPLE_HK_MID)
    if base is None:
        raise Fail("the statistics packet has no entry for message ID 0x0883 (SAMPLE_APP housekeeping)")
    cfs.inject(sl.SAMPLE_HK_MID, 10, 120, first_index=1000)
    cfs.cmd(sl.CC_CLOSE_FILES)
    st = cfs.stats()
    e = st.get(sl.SAMPLE_HK_MID)
    if e is None:
        raise Fail("the statistics packet lost the entry for message ID 0x0883")
    p.check(e.received - base.received >= 10, f"0x0883 Received grew by {e.received - base.received} after 10 packets")
    p.check(e.stored == ceil_div(e.received, 2), f"0x0883 Stored {e.stored} != ceil(Received {e.received} / 2)")
    p.check(e.decimated == e.received - e.stored, f"0x0883 Decimated {e.decimated}")
    p.check((e.priority, e.decimation) == (30, 2), f"0x0883 priority/decimation {e.priority}/{e.decimation}")
    for mid, pri in ((sl.EVS_HK_MID, 10), (sl.ES_HK_MID, 20), (sl.TIME_HK_MID, 40)):
        o = st.get(mid)
        if o is None:
            p.check(False, f"no statistics entry for {mid:#06x}")
            continue
        p.check((o.priority, o.decimation) == (pri, 1), f"{mid:#06x} priority/decimation {o.priority}/{o.decimation}")
        p.check(o.stored == o.received and o.decimated == 0, f"{mid:#06x} stored {o.stored} of {o.received}")
    hk = cfs.hk()
    p.check(hk.packets_decimated >= e.decimated, "PacketsDecimated below the per-entry total")

    # Part 2: exact rule on a quiet table (no other stream), including decimation 255.
    table = quiet_table(entries=((sl.INJ_A, 10, 1), (sl.INJ_B, 20, 3), (sl.INJ_C, 30, 255)))
    cfs = ctx.session("decim-quiet", table)
    cfs.inject(sl.INJ_A, 4, 300)
    cfs.inject(sl.INJ_B, 10, 300)
    cfs.inject(sl.INJ_C, 10, 300)
    st = cfs.stats()
    for mid, (rx, stored, dec) in ((sl.INJ_A, (4, 4, 0)), (sl.INJ_B, (10, 4, 6)), (sl.INJ_C, (10, 1, 9))):
        o = st.get(mid)
        if o is None:
            p.check(False, f"no statistics entry for {mid:#06x}")
            continue
        p.check((o.received, o.stored, o.decimated, o.dropped) == (rx, stored, dec, 0),
                f"{mid:#06x} received/stored/decimated/dropped {o.received}/{o.stored}/{o.decimated}/{o.dropped} "
                f"!= {rx}/{stored}/{dec}/0")
    cfs.cmd(sl.CC_CLOSE_FILES)
    indices: dict[int, list[int]] = {sl.INJ_A: [], sl.INJ_B: [], sl.INJ_C: []}
    for f in cfs.packet_files():
        try:
            for pkt in sl.read_packets(f.path.read_bytes()):
                indices[f.mid].append(int.from_bytes(pkt[sl.TLM_HDR:sl.TLM_HDR + 4], "little"))
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}")
    p.check(indices[sl.INJ_B] == [0, 3, 6, 9], f"stored B packets {indices[sl.INJ_B]} != [0, 3, 6, 9]")
    p.check(indices[sl.INJ_C] == [0], f"stored C packets {indices[sl.INJ_C]} != [0]")
    p.check(indices[sl.INJ_A] == [0, 1, 2, 3], f"stored A packets {indices[sl.INJ_A]}")
    hk = cfs.hk()
    p.check(hk.packets_stored == 9 and hk.packets_decimated == 15,
            f"PacketsStored/PacketsDecimated {hk.packets_stored}/{hk.packets_decimated} != 9/15")
    # Runtime change: decimation 1 stores every following packet; the priority change shows in the statistics.
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_DECIMATION, sl.pstr_id(sl.INJ_B, 1))
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.INJ_B, 77))
    p.check(len(wait_event(cfs, mark, 16, count=2)) == 2, "SET_DECIMATION/SET_PRIORITY did not each produce event 16")
    cfs.inject(sl.INJ_B, 2, 300, first_index=10)
    o = cfs.stats().get(sl.INJ_B)
    if o is not None:
        p.check((o.priority, o.decimation, o.received, o.stored) == (77, 1, 12, 6),
                f"B after runtime change priority/decimation/received/stored "
                f"{o.priority}/{o.decimation}/{o.received}/{o.stored} != 77/1/12/6")
    p.raise_if()


def c_packet_file_naming_and_header(ctx: Ctx) -> None:
    cfs = ctx.session("naming")
    p = Problems()
    cfs.inject(sl.SAMPLE_HK_MID, 3, 200, first_index=2000)
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_CLOSE_FILES)
    p.check(bool(wait_event(cfs, mark, 27)), "CLOSE_FILES did not produce event 27")
    time.sleep(0.5)
    files = cfs.packet_files()
    names = sorted(x.name for x in cfs.tlm_dir.iterdir()) if cfs.tlm_dir.is_dir() else []
    for n in names:
        p.check(sl.PKT_FILE_RE.match(n) is not None, f"file {n!r} does not match tlm_<MMMM>_<NNNNNNNN>.bin")
    mine = [f for f in files if f.mid == sl.SAMPLE_HK_MID]
    if not p.check(bool(mine), "no tlm_0883_NNNNNNNN.bin file exists under /cf/stor/tlm"):
        p.raise_if()
    seen_marked = 0
    for f in files:
        data = f.path.read_bytes()
        for msg in sl.check_fs_header(data):
            p.check(False, f"{f.path.name}: {msg}")
        try:
            pkts = sl.read_packets(data)
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}")
            continue
        p.check(len(pkts) >= 1, f"{f.path.name}: no packets after the header")
        for pkt in pkts:
            p.check(sl.tlm_mid(pkt) == f.mid, f"{f.path.name}: packet MID {sl.tlm_mid(pkt):#06x} differs from the name")
            if f.mid == sl.SAMPLE_HK_MID and int.from_bytes(pkt[16:20], "little") >= 2000:
                seen_marked += 1
                p.check(len(pkt) == 200 and pkt == sl.build_telemetry(sl.SAMPLE_HK_MID, 200, int.from_bytes(pkt[16:20], "little")),
                        f"{f.path.name}: stored packet differs from the packet received")
    p.check(seen_marked >= 1, "none of the injected 0x0883 packets was found in the files")
    seqs = [f.seq for f in files]
    p.check(len(set(seqs)) == len(files) or len({(f.mid, f.seq) for f in files}) == len(files),
            "duplicate file names")
    hk, before, after = used_matches_disk(cfs)
    p.check(before <= hk.telemetry_used <= after, f"TelemetryBytesUsed {hk.telemetry_used} not in [{before}, {after}] (disk)")
    p.check(hk.open_files <= 4, f"OpenPacketFiles {hk.open_files}")
    p.raise_if()


def c_rollover(ctx: Ctx) -> None:
    cfs = ctx.session("rollover", quiet_table(entries=((sl.INJ_A, 10, 1), (sl.INJ_B, 20, 1))))
    p = Problems()
    cfs.inject(sl.INJ_A, 10, 300)
    files = [f for f in cfs.packet_files() if f.mid == sl.INJ_A]
    p.check([f.seq for f in files] == [0, 1, 2, 3], f"sequence numbers {[f.seq for f in files]} != [0, 1, 2, 3]")
    p.check([f.size for f in files] == [964, 964, 964, 364], f"file sizes {[f.size for f in files]} != [964, 964, 964, 364]")
    got: list[int] = []
    for f in files:
        data = f.path.read_bytes()
        for msg in sl.check_fs_header(data):
            p.check(False, f"{f.path.name}: {msg}")
        try:
            got += [int.from_bytes(x[16:20], "little") for x in sl.read_packets(data)]
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}")
    p.check(got == list(range(10)), f"packet order across files {got}")
    hk = cfs.hk()
    p.check(hk.telemetry_used == sum(f.size for f in cfs.packet_files()) == 3256,
            f"TelemetryBytesUsed {hk.telemetry_used}, disk {disk_sum(cfs)}, expected 3256")
    p.check(hk.open_files == 1, f"OpenPacketFiles {hk.open_files} != 1")
    # A packet that makes the file exactly MaxPacketFileBytes still fits: 64 + 4 * 240 = 1024.
    cfs.inject(sl.INJ_B, 9, 240)
    bf = [f for f in cfs.packet_files() if f.mid == sl.INJ_B]
    p.check([f.size for f in bf] == [1024, 1024, 304], f"exact-fit file sizes {[f.size for f in bf]} != [1024, 1024, 304]")
    p.check(all(f.size <= 1024 for f in cfs.packet_files()), "a packet file exceeds MaxPacketFileBytes")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_CLOSE_FILES)
    hk = cfs.hk()
    p.check(hk.open_files == 0, f"OpenPacketFiles {hk.open_files} after CLOSE_FILES")
    p.check(bool(cfs.events(mark, 27)), "no event 27 for CLOSE_FILES")
    p.raise_if()


def c_idle_file_close(ctx: Ctx) -> None:
    cfs = ctx.session("idle", quiet_table())
    p = Problems()
    cfs.inject(sl.INJ_A, 1, 300)
    t_write = time.monotonic()
    hk = cfs.hk()
    p.check(hk.open_files == 1, f"OpenPacketFiles {hk.open_files} after one packet")
    time.sleep(max(0.0, 5.0 - (time.monotonic() - t_write)))
    hk = cfs.hk()
    p.check(hk.open_files == 1, f"file closed after only {time.monotonic() - t_write:.1f} s idle")
    time.sleep(max(0.0, 13.0 - (time.monotonic() - t_write)))
    hk = cfs.hk()
    p.check(hk.open_files == 0, f"OpenPacketFiles {hk.open_files} after {time.monotonic() - t_write:.1f} s idle")
    cfs.inject(sl.INJ_A, 1, 300, first_index=1)
    files = [f for f in cfs.packet_files() if f.mid == sl.INJ_A]
    p.check(len(files) == 2, f"{len(files)} files after a packet arrived following the idle close; expected 2")
    p.raise_if()


def prefix_violations(universe: list[tuple[int, int]], remaining: set[tuple[int, int]],
                      prio: dict[int, int]) -> list[str]:
    """Missing files must be a lowest-first prefix of the (priority, sequence) order."""
    rank = {k: (prio[k[0]], k[1]) for k in universe}
    bad: list[str] = []
    for x in universe:
        if x in remaining:
            continue
        for y in universe:
            if y in remaining and rank[y] < rank[x]:
                bad.append(f"{y[0]:04X}_{y[1]} (rank {rank[y]}) survived while {x[0]:04X}_{x[1]} (rank {rank[x]}) was purged")
    return bad[:4]


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
            f"purged {hk.files_purged} files in {hk.purge_cycles} cycles while below the threshold")
    p.check(hk.telemetry_used == 3 * FILE_FULL == disk_sum(cfs),
            f"TelemetryBytesUsed {hk.telemetry_used}, disk {disk_sum(cfs)}, expected {3 * FILE_FULL}")
    high = {k for k in snap() if k[0] in (sl.INJ_B, sl.INJ_C)}
    inject_tracking(sl.INJ_A, 40)
    cfs.cmd(sl.CC_CLOSE_FILES)
    cfs.send(sl.build_command(sl.WAKEUP_MID, 0))
    time.sleep(2.0)
    rem1 = snap()
    hk = cfs.hk()
    phase1_prio = {sl.INJ_A: 5, sl.INJ_B: 200, sl.INJ_C: 100}
    p.check(hk.files_purged > 0, "FilesPurged is 0 after the threshold was exceeded")
    p.check(hk.purge_cycles > 0, "PurgeCycles is 0 after the threshold was exceeded")
    p.check(hk.telemetry_used <= limit, f"TelemetryBytesUsed {hk.telemetry_used} above the threshold {limit}")
    p.check(hk.telemetry_used == disk_sum(cfs), f"TelemetryBytesUsed {hk.telemetry_used} != bytes on disk {disk_sum(cfs)}")
    p.check(high <= rem1, f"higher-priority files were purged: {sorted(high - rem1)}")
    gone1 = sorted(k for k in seen if k not in rem1)
    p.check(bool(gone1) and all(k[0] == sl.INJ_A for k in gone1), f"purged files {gone1} are not all lowest-priority")
    p.check(hk.files_purged >= len(gone1), f"FilesPurged {hk.files_purged} < {len(gone1)} files gone from disk")
    p.check(hk.packets_dropped == 0, f"PacketsDropped {hk.packets_dropped} although closed files were purgeable")
    for msg in prefix_violations(sorted(seen), rem1, phase1_prio):
        p.check(False, msg)
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
    p.check(hk2.files_purged > hk.files_purged, "no further purge after the priorities were changed and more data stored")
    p.check(hk2.telemetry_used <= limit, f"TelemetryBytesUsed {hk2.telemetry_used} above the threshold {limit} (phase 2)")
    p.check(not (high & rem2) or len(high & rem2) < len(high),
            "after B was made the lowest priority, none of its files was purged first")
    phase2_prio = {sl.INJ_A: 250, sl.INJ_B: 1, sl.INJ_C: 100}
    for msg in prefix_violations(sorted(universe2), rem2 & universe2, phase2_prio):
        p.check(False, "phase 2: " + msg)
    p.raise_if()


def c_quota_drop(ctx: Ctx) -> None:
    # Design: with PurgeThresholdPercent 100 nothing is purged (usage can never exceed 100 % of the quota),
    # so injecting 30 packets of 700 bytes into a 16384-byte quota must fill it: four closed files of 3564
    # bytes (14256) plus a fifth file that takes two packets (14256 + 64 + 2 * 700 = 15720) and drops the rest.
    # Lowering the threshold to 50 purges the closed files and storing resumes; refilling drops again, and
    # event 13 is emitted once per drop episode (twice in total), not once per dropped packet.
    quota = 16384
    cfs = ctx.session("quota", sl.TableSpec(telemetry_quota=quota, max_packet_file=4096, threshold=100,
                                            entries=((sl.INJ_A, 10, 1),)))
    p = Problems()
    mark = cfs.log_mark()
    cfs.inject(sl.INJ_A, 30, PKT)
    hk = cfs.hk()
    st = cfs.stats().get(sl.INJ_A)
    p.check(hk.packets_dropped > 0, "PacketsDropped is 0 after more packets than the quota can hold")
    p.check(hk.telemetry_used <= quota, f"TelemetryBytesUsed {hk.telemetry_used} exceeds the quota {quota}")
    p.check(quota - hk.telemetry_used < 64 + PKT, f"only {hk.telemetry_used} of {quota} bytes used when dropping began")
    p.check(hk.packets_stored + hk.packets_dropped == 30, f"stored {hk.packets_stored} + dropped {hk.packets_dropped} != 30")
    p.check(hk.files_purged == 0 and hk.purge_cycles == 0, "files were purged although the threshold is 100 percent")
    p.check(hk.telemetry_used == disk_sum(cfs), f"TelemetryBytesUsed {hk.telemetry_used} != bytes on disk {disk_sum(cfs)}")
    if st is not None:
        p.check(st.dropped == hk.packets_dropped, f"per-entry Dropped {st.dropped} != PacketsDropped {hk.packets_dropped}")
    ev = cfs.events(mark, 13)
    p.check(len(ev) == 1, f"event 13 occurred {len(ev)} times during one drop episode; expected exactly once")
    # Free space by lowering the threshold, then store again.
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([50, 0, 0, 0]))
    cfs.settle()
    hk_p = cfs.hk()
    p.check(hk_p.files_purged > 0 and hk_p.telemetry_used <= quota // 2,
            f"SET_PURGE_THRESHOLD 50 left {hk_p.telemetry_used} bytes (FilesPurged {hk_p.files_purged})")
    dropped_before = hk_p.packets_dropped
    cfs.inject(sl.INJ_A, 3, PKT, first_index=100)
    hk_s = cfs.hk()
    p.check(hk_s.packets_dropped == dropped_before and hk_s.packets_stored == hk_p.packets_stored + 3,
            f"packets were not stored again after space was freed (stored {hk_s.packets_stored}, dropped {hk_s.packets_dropped})")
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([100, 0, 0, 0]))
    cfs.inject(sl.INJ_A, 30, PKT, first_index=200)
    hk_f = cfs.hk()
    p.check(hk_f.packets_dropped > dropped_before, "no drops when refilling the quota")
    ev2 = cfs.events(mark, 13)
    p.check(len(ev2) == 2, f"event 13 occurred {len(ev2)} times over two drop episodes; expected exactly 2")
    p.raise_if()


def c_restart_scan(ctx: Ctx) -> None:
    seed = {"stor/tlm/tlm_08E0_00000007.bin": bytes(1000), "stor/tlm/tlm_08E1_00000003.bin": bytes(500),
            "stor/files/x/y.bin": bytes(700), "stor/files/z.bin": bytes(300)}
    cfs = ctx.session("restart", quiet_table(), seed=seed)
    p = Problems()
    hk = cfs.hk()
    p.check(hk.telemetry_used == 1500, f"TelemetryBytesUsed {hk.telemetry_used} != 1500 (sizes of the existing packet files)")
    p.check(hk.file_used == 1000, f"FileBytesUsed {hk.file_used} != 1000 (sizes of the existing files)")
    cfs.inject(sl.INJ_A, 1, 300)
    names = sorted(f.path.name for f in cfs.packet_files())
    p.check("tlm_08E0_00000008.bin" in names, f"no tlm_08E0_00000008.bin after restart; files {names}")
    hk = cfs.hk()
    p.check(hk.telemetry_used == 1500 + 64 + 300, f"TelemetryBytesUsed {hk.telemetry_used} != {1500 + 64 + 300} after one packet")
    p.raise_if()


def err_delta(cfs: sl.Cfs, fn: Callable[[], None]) -> tuple[sl.Hk, sl.Hk]:
    a = cfs.hk()
    fn()
    cfs.settle()
    return a, cfs.hk()


def c_set_param_errors(ctx: Ctx) -> None:
    cfs = ctx.session("params")
    p = Problems()
    mark = cfs.log_mark()
    a = cfs.hk()
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(0x0FFF, 5))
    cfs.cmd(sl.CC_SET_DECIMATION, sl.pstr_id(sl.SAMPLE_HK_MID, 0))
    cfs.settle()
    b = cfs.hk()
    p.check(b.command_error_counter == a.command_error_counter + 2,
            f"CommandErrorCounter {a.command_error_counter} -> {b.command_error_counter} after two invalid SET_* commands")
    p.check(len(cfs.events(mark, 17)) == 2, f"event 17 seen {len(cfs.events(mark, 17))} times, expected 2")
    p.check(b.command_counter == a.command_counter, "CommandCounter changed for rejected commands")
    # Priority 0 is rejected too; a valid change is accepted with event 16.
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.EVS_HK_MID, 0))
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.EVS_HK_MID, 11))
    cfs.cmd(sl.CC_SET_DECIMATION, sl.pstr_id(sl.EVS_HK_MID, 4))
    cfs.settle()
    p.check(len(cfs.events(mark, 17)) == 1, "SET_PRIORITY 0 did not produce exactly one event 17")
    p.check(len(cfs.events(mark, 16)) == 2, "valid SET_PRIORITY/SET_DECIMATION did not each produce event 16")
    st = cfs.stats().get(sl.EVS_HK_MID)
    if st is not None:
        p.check((st.priority, st.decimation) == (11, 4), f"EVS entry priority/decimation {st.priority}/{st.decimation} != 11/4")
    # Purge threshold: 0 and 101 rejected, 50 and 100 accepted.
    c = cfs.hk()
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([0, 0, 0, 0]))
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([101, 0, 0, 0]))
    cfs.settle()
    d = cfs.hk()
    p.check(len(cfs.events(mark, 17)) == 2, "thresholds 0 and 101 did not each produce event 17")
    p.check(d.purge_threshold == c.purge_threshold == 80, f"PurgeThresholdPercent {c.purge_threshold}->{d.purge_threshold} after rejected values")
    p.check(d.command_error_counter == c.command_error_counter + 2, "rejected thresholds did not each count as command errors")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([50, 0, 0, 0]))
    cfs.settle()
    e = cfs.hk()
    p.check(len(cfs.events(mark, 16)) == 1, "threshold 50 did not produce event 16")
    p.check(e.purge_threshold == 50, f"PurgeThresholdPercent {e.purge_threshold} != 50")
    cfs.cmd(sl.CC_SET_PURGE_THRESHOLD, bytes([100, 0, 0, 0]))
    cfs.settle()
    p.check(cfs.hk().purge_threshold == 100, "threshold 100 was not accepted")
    # Bad function code and bad length.
    mark = cfs.log_mark()
    f0 = cfs.hk()
    cfs.cmd(99)
    cfs.cmd(sl.CC_NOOP, b"\0\0\0\0")
    cfs.settle()
    f1 = cfs.hk()
    p.check(len(cfs.events(mark, 2)) == 1, "invalid function code did not produce event 2")
    p.check(len(cfs.events(mark, 6)) == 1, "wrong command length did not produce event 6")
    p.check(f1.command_error_counter == f0.command_error_counter + 2, "invalid code/length did not each count as command errors")
    p.raise_if()


def small_file_source(ctx: Ctx, cfs: sl.Cfs) -> int:
    src = cfs.cf / "stor_tbl.tbl"
    return src.stat().st_size


def add_payload(src: str, dest: str) -> bytes:
    return sl.pstr(src) + sl.pstr(dest)


def c_file_add_list_delete(ctx: Ctx) -> None:
    cfs = ctx.session("files-a")
    p = Problems()
    size = small_file_source(ctx, cfs)
    # Empty file area.
    pk = cfs.listing(sl.CC_LIST_ALL)
    p.check(len(pk) == 1 and pk[0].total == 0 and pk[0].count == 0, f"LIST_ALL of an empty file area: {pk}")
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr(""))
    p.check(len(pk) == 1 and pk[0].total == 0, f"LIST_DIR of the empty root: {pk}")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "a/b/copy.tbl"))
    p.check(len(wait_event(cfs, mark, 22)) == 1, "ADD_FILE did not produce event 22")
    dest = cfs.files_dir / "a" / "b" / "copy.tbl"
    p.check(dest.is_file() and dest.read_bytes() == (cfs.cf / "stor_tbl.tbl").read_bytes(),
            "the added file is missing or differs from its source")
    hk = cfs.hk()
    p.check(hk.file_count == 1, f"FileCount {hk.file_count} != 1")
    p.check(hk.file_used == size, f"FileBytesUsed {hk.file_used} != {size}")
    mark = cfs.log_mark()
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr("a"))
    p.check(len(pk) == 1 and pk[0].total == 1 and pk[0].entries == (("b/", 0),), f"LIST_DIR a: {pk}")
    p.check(bool(cfs.events(mark, 26)), "LIST_DIR did not produce event 26")
    pk = cfs.listing(sl.CC_LIST_ALL)
    p.check(len(pk) == 1 and pk[0].total == 1 and pk[0].entries == (("a/b/copy.tbl", size),), f"LIST_ALL: {pk}")
    # Multi-packet listing: five more files in one directory.
    for i in range(5):
        cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", f"m/f{i}.tbl"))
    pk = cfs.listing(sl.CC_LIST_DIR, sl.pstr("m"))
    p.check([x.index for x in pk] == [0, 1] and all(x.total == 5 for x in pk) and [x.count for x in pk] == [4, 1],
            f"LIST_DIR m packets {[(x.index, x.count, x.total) for x in pk]} != indices [0, 1], counts [4, 1], total 5")
    names = sorted(n for x in pk for n, _ in x.entries)
    p.check(names == [f"f{i}.tbl" for i in range(5)], f"LIST_DIR m names {names}")
    pk = cfs.listing(sl.CC_LIST_ALL)
    allnames = [n for x in pk for n, _ in x.entries]
    p.check(len(pk) == 2 and all(x.total == 6 for x in pk) and sorted(allnames) ==
            sorted(["a/b/copy.tbl"] + [f"m/f{i}.tbl" for i in range(5)]), f"LIST_ALL after six adds: {allnames}")
    m_idx = [i for i, n in enumerate(allnames) if n.startswith("m/")]
    p.check(m_idx == list(range(m_idx[0], m_idx[0] + len(m_idx))) if m_idx else False,
            "LIST_ALL entries of one directory are not contiguous (depth-first order)")
    # Missing directory.
    mark = cfs.log_mark()
    a = cfs.hk()
    cfs.cmd(sl.CC_LIST_DIR, sl.pstr("nosuchdir"))
    p.check(bool(wait_event(cfs, mark, 25)), "LIST_DIR of a missing directory did not produce event 25")
    # Delete.
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("a/b/copy.tbl"))
    p.check(len(wait_event(cfs, mark, 24)) == 1, "DELETE_FILE did not produce event 24")
    p.check(not dest.exists(), "the deleted file still exists")
    b = cfs.hk()
    p.check(b.file_count == a.file_count - 1, f"FileCount {a.file_count} -> {b.file_count} after DELETE_FILE")
    p.check(b.file_used == a.file_used - size, f"FileBytesUsed {a.file_used} -> {b.file_used} after DELETE_FILE")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("a/b/copy.tbl"))
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("a"))
    cfs.settle()
    c = cfs.hk()
    p.check(len(cfs.events(mark, 23)) == 2, "deleting a missing file and a directory did not each produce event 23")
    p.check(c.command_error_counter == b.command_error_counter + 2, "failed deletes did not each count as command errors")
    p.check((cfs.files_dir / "a").is_dir(), "DELETE_FILE removed a directory")
    p.raise_if()


def c_file_path_rejection(ctx: Ctx) -> None:
    cfs = ctx.session("files-path")
    p = Problems()
    a = cfs.hk()
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "../x"))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "/abs"))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "a/../b"))
    cfs.settle()
    b = cfs.hk()
    p.check(len(cfs.events(mark, 18)) == 3, f"event 18 seen {len(cfs.events(mark, 18))} times for three bad destinations")
    p.check(b.command_error_counter == a.command_error_counter + 3, "rejected destinations did not each count as command errors")
    p.check(b.file_count == 0, f"FileCount {b.file_count} after rejections")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_DELETE_FILE, sl.pstr("../x"))
    cfs.cmd(sl.CC_LIST_DIR, sl.pstr(".."))
    cfs.cmd(sl.CC_LIST_DIR, sl.pstr("/etc"))
    cfs.settle()
    p.check(len(cfs.events(mark, 18)) == 3, f"event 18 seen {len(cfs.events(mark, 18))} times for bad DELETE_FILE/LIST_DIR paths")
    stray = [q for q in (cfs.cf / "x", cfs.cf / "stor" / "x", cfs.cf / "b", cfs.cf / "abs", cfs.files_dir / "abs",
                         Path("/abs")) if q.exists()]
    p.check(not stray, f"files were written outside the file area: {stray}")
    p.raise_if()


def c_file_exists_rejection(ctx: Ctx) -> None:
    cfs = ctx.session("files-exists")
    p = Problems()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "dup.tbl"))
    a = cfs.hk()
    p.check(a.file_count == 1, f"FileCount {a.file_count} after the first add")
    first = (cfs.files_dir / "dup.tbl").read_bytes() if (cfs.files_dir / "dup.tbl").is_file() else b""
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "dup.tbl"))
    cfs.settle()
    b = cfs.hk()
    p.check(len(cfs.events(mark, 20)) == 1, "adding to an existing destination did not produce event 20")
    p.check(b.command_error_counter == a.command_error_counter + 1, "the rejected add did not count as a command error")
    p.check(b.file_count == 1 and b.file_used == a.file_used, "FileCount/FileBytesUsed changed by the rejected add")
    p.check((cfs.files_dir / "dup.tbl").is_file() and (cfs.files_dir / "dup.tbl").read_bytes() == first,
            "the existing file was modified")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/nosuchsource.bin", "other.tbl"))
    p.check(bool(wait_event(cfs, mark, 19)), "a missing source file did not produce event 19")
    p.check(not (cfs.files_dir / "other.tbl").exists(), "a destination exists for a missing source")
    p.raise_if()


def c_file_quota_rejection(ctx: Ctx) -> None:
    cfs = ctx.session("files-quota")
    p = Problems()
    quota = 1048576
    big = cfs.cf / "big.bin"
    big.write_bytes(bytes(quota + 1))
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "big/dest.bin"))
    p.check(len(wait_event(cfs, mark, 21)) == 1, "an add beyond FileQuotaBytes did not produce event 21")
    hk = cfs.hk()
    p.check(hk.file_adds_rejected == 1, f"FileAddsRejected {hk.file_adds_rejected} != 1")
    p.check(hk.command_error_counter == 1, f"CommandErrorCounter {hk.command_error_counter} != 1")
    leftovers = [str(x.relative_to(cfs.files_dir)) for x in cfs.files_dir.rglob("*") if x.is_file()]
    p.check(not leftovers, f"partial files left in the file area: {leftovers}")
    p.check(hk.file_used == 0 and hk.file_count == 0, f"FileBytesUsed/FileCount {hk.file_used}/{hk.file_count} after rejection")
    # Boundary: a file that exactly fills the quota is accepted, one byte more is not.
    small = small_file_source(ctx, cfs)
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "small.tbl"))
    fit = cfs.cf / "big.bin"
    fit.write_bytes(bytes(quota - small))
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "fill.bin"))
    p.check(len(wait_event(cfs, mark, 22)) == 1, "a file that exactly fills FileQuotaBytes was not accepted")
    hk = cfs.hk()
    p.check(hk.file_used == quota, f"FileBytesUsed {hk.file_used} != {quota} after filling the quota")
    fit.write_bytes(bytes(1))
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "one-more.bin"))
    p.check(len(wait_event(cfs, mark, 21)) == 1, "one byte beyond the quota was not rejected with event 21")
    hk = cfs.hk()
    p.check(hk.file_adds_rejected == 2, f"FileAddsRejected {hk.file_adds_rejected} != 2")
    p.check(not (cfs.files_dir / "one-more.bin").exists(), "a file exists for the rejected add")
    big.unlink(missing_ok=True)
    p.raise_if()


def c_reset_counters(ctx: Ctx) -> None:
    table = quiet_table(file_quota=2048, entries=((sl.INJ_A, 10, 1), (sl.INJ_B, 20, 2)))
    cfs = ctx.session("reset", table)
    p = Problems()
    cfs.inject(sl.INJ_A, 3, 300)
    cfs.inject(sl.INJ_B, 4, 300)
    cfs.cmd(sl.CC_NOOP)
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(sl.INJ_A, 42))
    cfs.cmd(sl.CC_SET_PRIORITY, sl.pstr_id(0x0FFE, 2))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/stor_tbl.tbl", "keep.tbl"))
    big = cfs.cf / "big.bin"
    big.write_bytes(bytes(3000))
    cfs.cmd(sl.CC_ADD_FILE, add_payload("/cf/big.bin", "toobig.bin"))
    pre = cfs.hk()
    p.check(pre.packets_stored == 5 and pre.packets_decimated == 2, f"setup: stored/decimated {pre.packets_stored}/{pre.packets_decimated}")
    p.check(pre.command_counter >= 1 and pre.command_error_counter >= 1 and pre.file_adds_rejected == 1,
            f"setup: counters {pre.command_counter}/{pre.command_error_counter}/{pre.file_adds_rejected}")
    mark = cfs.log_mark()
    cfs.cmd(sl.CC_RESET)
    p.check(bool(wait_event(cfs, mark, 4)), "RESET_COUNTERS did not produce event 4")
    post = cfs.hk()
    zero = {"command_counter": 0, "command_error_counter": 0, "packets_stored": 0, "packets_decimated": 0,
            "packets_dropped": 0, "files_purged": 0, "purge_cycles": 0, "file_adds_rejected": 0}
    for k, v in zero.items():
        p.check(getattr(post, k) == v, f"{k} is {getattr(post, k)} after RESET_COUNTERS")
    kept = {"telemetry_used": pre.telemetry_used, "file_used": pre.file_used, "file_count": pre.file_count,
            "open_files": pre.open_files, "purge_threshold": pre.purge_threshold,
            "telemetry_quota": pre.telemetry_quota, "active_entries": pre.active_entries}
    for k, v in kept.items():
        p.check(getattr(post, k) == v, f"{k} changed {v} -> {getattr(post, k)} by RESET_COUNTERS")
    st = cfs.stats()
    for mid in (sl.INJ_A, sl.INJ_B):
        o = st.get(mid)
        if o is None:
            p.check(False, f"no statistics entry for {mid:#06x}")
            continue
        p.check((o.received, o.stored, o.decimated, o.dropped) == (0, 0, 0, 0),
                f"{mid:#06x} statistics not zero after RESET_COUNTERS: {o}")
    a = st.get(sl.INJ_A)
    if a is not None:
        p.check(a.priority == 42, f"runtime priority {a.priority} changed by RESET_COUNTERS (was 42)")
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
            p.check(False, f"{f.name} is not valid JSON: {exc}")
            continue
        texts.append(raw)
    blob = "\n".join(texts)
    p.check(bool(texts), "no CCDD JSON file under ctf/ccdd/json mentions STOR")
    for word in ("NOOP", "RESET_COUNTERS", "SET_PRIORITY", "SET_DECIMATION", "SET_PURGE_THRESHOLD", "ADD_FILE",
                 "DELETE_FILE", "LIST_DIR", "LIST_ALL", "SEND_STATS", "CLOSE_FILES"):
        p.check(re.search(r'"cc_name"\s*:\s*"[A-Za-z0-9_]*' + word + r'[A-Za-z0-9_]*"', blob) is not None,
                f"no command code definition for {word}")
    for mid in ("STOR_CMD_MID", "STOR_SEND_HK_MID", "STOR_WAKEUP_MID", "STOR_HK_TLM_MID", "STOR_LIST_TLM_MID",
                "STOR_STATS_TLM_MID"):
        p.check(mid in blob or mid in _mission_mids(d), f"{mid} is not defined")
    for field in ("TelemetryBytesUsed", "PacketsDropped", "FilesPurged", "TotalEntries", "Received", "Decimated"):
        p.check(field in blob, f"telemetry field {field} is not defined")
    for field in ("MsgId", "Priority", "Decimation", "Percent", "SourcePath", "DestPath"):
        p.check(field in blob, f"command field {field} is not defined")
    p.raise_if()


def _mission_mids(d: Path) -> str:
    f = d / "mission_mids.json"
    return f.read_text(errors="replace") if f.is_file() else ""


def c_ctf_test_passes(ctx: Ctx) -> None:
    ctx.build()
    test = ctx.tree / "ctf" / "tests" / "stor_basic.json"
    if not test.is_file():
        raise Fail("ctf/tests/stor_basic.json does not exist")
    p = Problems()
    try:
        doc = json.loads(test.read_text())
    except json.JSONDecodeError as exc:
        raise Fail(f"stor_basic.json is not valid JSON: {exc}") from None
    flat = json.dumps(doc)
    for w in REQUIRED_CTF_WORDS:
        p.check(w in flat, f"the CTF test does not exercise {w}")
    p.check("CFE_EVS_HK_TLM_MID" in flat or "0x0801" in flat, "the CTF test does not check the CFE_EVS_HK_TLM_MID statistics entry")
    p.raise_if()
    if ctx.cfs is not None:
        ctx.cfs.stop()
        ctx.session_key = None
    shutil.rmtree(ctx.tree / "build-native_std" / "exe" / "cpu1" / "cf" / "stor", ignore_errors=True)
    env = dict(os.environ, CFS_ROOT=str(ctx.tree), CTF_RESULTS_DIR=str(ctx.work / "ctf-results"))
    run = ctx.tree / "ctf" / "run_ctf.sh"
    if not run.is_file():
        raise Fail("ctf/run_ctf.sh does not exist")
    proc = ctx.sh(["bash", str(run), str(test)], 360, env=env)
    if proc.returncode != 0:
        smoke = ctx.tree / "ctf" / "tests" / "smoke_sample_app.json"
        if smoke.is_file():
            sm = ctx.sh(["bash", str(run), str(smoke)], 240, env=env)
            if sm.returncode != 0:
                raise InfraError("the CTF framework cannot run its own smoke test: " + (sm.stdout + sm.stderr)[-600:])
        raise Fail(f"ctf/run_ctf.sh ctf/tests/stor_basic.json exited {proc.returncode}: " + (proc.stdout + proc.stderr)[-900:])
    shutil.rmtree(ctx.tree / "build-native_std" / "exe" / "cpu1" / "cf" / "stor", ignore_errors=True)


def c_report_present(ctx: Ctx) -> None:
    ctx.copy_tree()
    f = ctx.tree / "docs" / "reports" / "stor.md"
    if not f.is_file():
        raise Fail("docs/reports/stor.md does not exist")
    text = f.read_text(errors="replace")
    low = text.lower()
    p = Problems()
    p.check(len(text.strip()) >= 800, f"report is only {len(text.strip())} characters")
    p.check("design" in low, "no design content")
    p.check("unit test" in low or "unit-test" in low or "coverage test" in low, "no unit test content")
    p.check("coverage" in low and re.search(r"\d+(\.\d+)?\s*%", text) is not None, "no coverage figures")
    p.check("ctf" in low, "no CTF content")
    p.check("limitation" in low, "no limitations section")
    p.raise_if()


def c_hidden_coverage_test(ctx: Ctx) -> None:
    ctx.build()
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
              "sample_defs/targets.cmake, scheduled in sample_defs/tables/sch_lab_table.c (STOR_SEND_HK_MID every 4.9 s "
              "and STOR_WAKEUP_MID every 1.0 s) and forwarded by sample_defs/tables/to_lab_sub.c (STOR_HK_TLM_MID, "
              "STOR_LIST_TLM_MID, STOR_STATS_TLM_MID).", c_app_registered),
    CheckSpec("build-no-warnings",
              "The mission must configure with make native_std.prep and build with make native_std.install with no "
              "compiler warnings, including the STOR sources, tables and the registrations in sample_defs.",
              c_build_no_warnings),
    CheckSpec("unit-tests-pass",
              "make native_std.runtest must pass, with one coverage test per STOR source module in "
              "apps/stor/unit-test/coveragetest and no failing assertion in any STOR coverage test.", c_unit_tests_pass),
    CheckSpec("coverage-threshold",
              "Line coverage of every STOR source file in apps/stor/fsw/src, as reported by make native_std.lcov, must be "
              "at least 95 percent.", c_coverage_threshold),
    CheckSpec("hidden-coverage-test",
              "STOR must behave as specified when driven only through its entry point STOR_AppMain with messages on the "
              "software bus: table validation rejections, decimation, packet file roll-over, purge order, telemetry "
              "quota drops, path rejection, the file area commands and the listing, statistics and housekeeping "
              "packets, using only the names, structures, message IDs, function codes and event IDs defined in the "
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
              "STOR must start with the mission on cpu1, emit its initialization event (ID 1), and answer SEND_HK with a "
              "STOR_HkTlm_t on STOR_HK_TLM_MID in the specified field order; with the default table it reports "
              "TelemetryQuotaBytes 262144, FileQuotaBytes 1048576, PurgeThresholdPercent 80 and ActiveEntries 4, and NOOP "
              "increments CommandCounter and emits event 3.", c_startup_and_hk),
    CheckSpec("decimation-and-stats",
              "STOR must count every received packet whose MID is a used table entry and store it only when "
              "(Received - 1) % Decimation == 0, counting the others as Decimated; SEND_STATS must report per entry the "
              "MsgId, Priority, Decimation, Received, Stored, Decimated and Dropped (with decimation 2, Stored equals "
              "ceil(Received / 2)), and SET_DECIMATION and SET_PRIORITY take effect at run time.", c_decimation_and_stats),
    CheckSpec("packet-file-naming-and-header",
              "Stored packets must be appended, headers and payload as received, to files named "
              "<TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin (four-digit upper-case hexadecimal MID, eight-digit zero-padded "
              "sequence), each starting with a CFE_FS_Header_t written by CFE_FS_WriteHeader with sub type 0x5354 and "
              "description \"STOR packet file\"; CLOSE_FILES closes them and emits event 27, and TelemetryBytesUsed "
              "equals the bytes of the packet files.", c_packet_file_naming_and_header),
    CheckSpec("rollover",
              "When appending a packet would make a packet file exceed MaxPacketFileBytes, STOR must close it and open the "
              "next sequence number before writing, so that no file exceeds MaxPacketFileBytes (a packet that makes the "
              "file exactly MaxPacketFileBytes still fits), sequence numbers start at 0 and increase by one, and "
              "TelemetryBytesUsed tracks the bytes written including headers.", c_rollover),
    CheckSpec("idle-file-close",
              "On every WAKEUP (scheduled at 1 Hz), STOR must close any packet file not written for 10 seconds or more "
              "(and not before), after which the next packet opens a new file.", c_idle_file_close),
    CheckSpec("purge-order",
              "While TelemetryBytesUsed exceeds PurgeThresholdPercent of TelemetryQuotaBytes, after every store and on "
              "every WAKEUP, STOR must delete one closed packet file at a time: the one whose MID has the lowest current "
              "priority, lowest sequence first, never an open file; FilesPurged and PurgeCycles must count the "
              "deletions, TelemetryBytesUsed must fall to the threshold, and run-time SET_PRIORITY changes must change "
              "the order.", c_purge_order),
    CheckSpec("quota-drop",
              "If storing a packet would make TelemetryBytesUsed exceed TelemetryQuotaBytes, STOR must drop it "
              "(Dropped and PacketsDropped incremented) and emit event 13 once per entry until that entry stores a "
              "packet successfully again; SET_PURGE_THRESHOLD triggers a purge check that frees space.", c_quota_drop),
    CheckSpec("restart-scan",
              "At startup STOR must create its roots, initialize TelemetryBytesUsed and FileBytesUsed from the sizes of "
              "the regular files already present, and continue the packet file sequence from the highest existing "
              "sequence plus one.", c_restart_scan),
    CheckSpec("set-param-errors",
              "SET_PRIORITY and SET_DECIMATION must reject value 0 or an unknown MsgId, and SET_PURGE_THRESHOLD "
              "must reject values outside 1..100, each with event 17 and CommandErrorCounter incremented; accepted "
              "changes emit event 16 and are reflected in housekeeping and statistics; an invalid function code gives "
              "event 2 and an invalid length event 6, each counted as a command error.", c_set_param_errors),
    CheckSpec("file-add-list-delete",
              "ADD_FILE must copy the source to <FileRoot>/<DestPath> creating intermediate directories (event 22, "
              "FileCount and FileBytesUsed updated); LIST_DIR and LIST_ALL must send STOR_ListTlm_t packets of at most "
              "four entries (TotalEntries, PacketIndex, EntriesInPacket; directories with a trailing / and size 0; an "
              "empty listing is one packet with TotalEntries 0; a missing directory gives event 25; success event 26); "
              "DELETE_FILE must remove a regular file (event 24) and report a missing file or a directory with event 23.",
              c_file_add_list_delete),
    CheckSpec("file-path-rejection",
              "A file area path that contains .. or starts with / must be rejected with event 18 and counted as a command "
              "error, for ADD_FILE (destination), DELETE_FILE and LIST_DIR, without touching anything outside "
              "<FileRoot>.", c_file_path_rejection),
    CheckSpec("file-exists-rejection",
              "ADD_FILE must reject an existing destination with event 20 (CommandErrorCounter incremented, the existing "
              "file untouched) and an unopenable source with event 19.", c_file_exists_rejection),
    CheckSpec("file-quota-rejection",
              "ADD_FILE must reject a file whose size plus FileBytesUsed exceeds FileQuotaBytes with event 21, "
              "FileAddsRejected incremented and no partial file left behind, while a file that exactly fills the quota "
              "is accepted.", c_file_quota_rejection),
    CheckSpec("reset-counters",
              "RESET_COUNTERS must zero CommandCounter, CommandErrorCounter, all per-entry counters, PacketsStored, "
              "PacketsDecimated, PacketsDropped, FilesPurged, PurgeCycles and FileAddsRejected (event 4) without "
              "changing byte accounting, FileCount, open packet files or run-time parameters.", c_reset_counters),
    CheckSpec("ccdd-definitions",
              "CTF definitions for all STOR commands and telemetry (command function codes STOR_<NAME>_CC, message IDs, "
              "and the housekeeping, listing and statistics fields) must exist as JSON under ctf/ccdd/json.",
              c_ccdd_definitions),
    CheckSpec("ctf-test-passes",
              "ctf/tests/stor_basic.json must exist, cover NOOP, a SET_PRIORITY error on an unknown MID, ADD_FILE of "
              "/cf/stor_tbl.tbl as t/copy.tbl followed by LIST_ALL, DELETE_FILE, and SEND_STATS showing "
              "CFE_EVS_HK_TLM_MID with Received greater than 0, and pass when run with ctf/run_ctf.sh.", c_ctf_test_passes),
    CheckSpec("report-present",
              "docs/reports/stor.md must contain the design summary, file layout, unit test counts, coverage figures, CTF "
              "results and limitations.", c_report_present),
]

assert len({c.check_id for c in CHECKS}) == len(CHECKS)
assert all(ID_RE.match(c.check_id) for c in CHECKS)

# Execution order: static checks and the build first, then the live scenarios, then the hidden test.
RUN_ORDER = [
    "app-registered", "build-no-warnings", "unit-tests-pass", "coverage-threshold", "default-table-contents",
    "scheduled-housekeeping", "startup-and-hk", "decimation-and-stats", "packet-file-naming-and-header", "rollover",
    "idle-file-close", "purge-order", "quota-drop", "restart-scan", "set-param-errors", "file-add-list-delete",
    "file-path-rejection", "file-exists-rejection", "file-quota-rejection", "reset-counters", "ccdd-definitions",
    "ctf-test-passes", "report-present", "hidden-coverage-test",
]
assert sorted(RUN_ORDER) == sorted(c.check_id for c in CHECKS)


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
    for tool in ("make", "cmake", "gcc"):
        if shutil.which(tool) is None:
            print(f"required tool {tool} not found", file=sys.stderr)
            return 2
    stor_sources = list((repo / "apps" / "stor" / "fsw" / "src").glob("*.c"))
    if stor_sources:
        missing = [t for t in ("lcov", "genhtml") if shutil.which(t) is None]
        if missing:
            print(f"infrastructure error: {', '.join(missing)} not installed; the coverage-threshold check needs "
                  f"lcov and genhtml on PATH (make native_std.lcov)", file=sys.stderr)
            return 2
    deadline = time.monotonic() + TOTAL_BUDGET_S
    base = os.environ.get("STOR_PACK_TMP") or tempfile.gettempdir()
    work = Path(tempfile.mkdtemp(prefix="stor-pack-", dir=base))
    ctx = Ctx(repo, work, deadline)
    by_id = {c.check_id: c for c in CHECKS}
    results: dict[str, dict[str, object]] = {}
    infra: str | None = None
    try:
        only = [x for x in os.environ.get("STOR_PACK_ONLY", "").split(",") if x]
        for cid in RUN_ORDER:
            if only and cid not in only:
                results[cid] = {"id": cid, "passed": False, "feedback": by_id[cid].feedback, "detail": "not run"}
                continue
            spec = by_id[cid]
            t0 = time.monotonic()
            try:
                spec.fn(ctx)
                results[cid] = {"id": cid, "passed": True, "feedback": "", "detail": f"{time.monotonic() - t0:.0f} s"}
            except Fail as exc:
                results[cid] = {"id": cid, "passed": False, "feedback": spec.feedback,
                                "detail": f"{exc} ({time.monotonic() - t0:.0f} s)"}
            except InfraError as exc:
                infra = f"{cid}: {exc}"
                break
            except Exception as exc:  # noqa: BLE001 - a check must never take the whole run down
                tb = traceback.format_exc()
                print(tb, file=sys.stderr)
                results[cid] = {"id": cid, "passed": False, "feedback": spec.feedback,
                                "detail": f"unexpected {type(exc).__name__}: {exc}\n{tb[-800:]}"}
            print(f"[{cid}] {'ok' if results[cid]['passed'] else 'FAILED'}: {str(results[cid]['detail'])[:300]}", flush=True)
    finally:
        ctx.shutdown()
        if os.environ.get("STOR_PACK_KEEP") != "1":
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"scratch kept at {work}", file=sys.stderr)
    if infra is not None:
        print(f"infrastructure error: {infra}", file=sys.stderr)
        return 2
    checks = [results[c.check_id] for c in CHECKS]
    passed = all(c["passed"] for c in checks)
    out.write_text(json.dumps({"passed": passed, "checks": checks}, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
