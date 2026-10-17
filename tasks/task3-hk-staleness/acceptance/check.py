#!/usr/bin/env python3
"""Acceptance checks for Task 3 (HK input staleness tracking).

Usage:
    check.py <repo-path> <out.json>
    check.py --measure-baseline <repo-path> <baseline.json>

Exit status: 0 every check passed, 1 at least one check failed, 2 infrastructure
error (the checks themselves could not run).

The agent's repository is never modified: it is copied into a scratch directory
(see HK_ACCEPT_TMPDIR) where it is built, unit tested, measured and run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PACK = Path(__file__).resolve().parent
BASELINE_FILE = PACK / "baseline_coverage.json"
HIDDEN_TEST = PACK / "hidden_tests" / "hk_hidden_tests.c"
EXPECTED_HIDDEN_TESTS = 18

TOTAL_BUDGET_S = 25 * 60
BUDGET_RESERVE_S = 60
PORT_WAIT_S = 300
JOBS = max(1, min(32, os.cpu_count() or 4))
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Wire facts for this mission (little-endian payloads, big-endian CCSDS headers).
HOST = "127.0.0.1"
CI_LAB_PORT = 1234
TO_LAB_PORT = 2234
TLM_HEADER_LEN = 16
MID_TO_LAB_CMD = 0x1880
MID_HK_CMD = 0x189A
MID_HK_SEND_HK = 0x189B
MID_HK_SEND_COMBINED = 0x189C
MID_HK_TLM = 0x089B
MID_HK_PKT2 = 0x089D
MID_HK_STALE_RPT = 0x08A2
MID_SAMPLE_APP_HK = 0x0883
MID_LONG_EVENT = 0x0808
EVT_INFORMATION = 2
EVT_ERROR = 3
CC_HK_RESET = 1
CC_HK_SET_STALE = 2
CC_HK_REPORT_STALE = 3
EID_STALE_SET = 38
EID_STALE_REJECT = 39
EID_STALE = 40
EID_FRESH = 41
INVALID_MID = 0
REPORT_SLOTS = 16
HK_FMT = "<BBHHHIHHB3s"  # existing fields, then the three appended fields
HK_LEN = TLM_HEADER_LEN + struct.calcsize(HK_FMT)
REPORT_FMT = "<HH" + "I" * REPORT_SLOTS + "I" * REPORT_SLOTS
REPORT_LEN = TLM_HEADER_LEN + struct.calcsize(REPORT_FMT)
EVENT_MSG_OFFSET = TLM_HEADER_LEN + 32
EVENT_MSG_LEN = 122


class Fail(Exception):
    """A check did not hold; the message is the experimenter detail."""


class InfraError(Exception):
    """The pack itself cannot run."""


# --------------------------------------------------------------------------
# Check bookkeeping
# --------------------------------------------------------------------------
@dataclass
class Result:
    check_id: str
    passed: bool
    feedback: str
    detail: str


@dataclass
class Ctx:
    repo: Path
    scratch: Path
    deadline: float
    cache: dict[str, object] = field(default_factory=dict)

    @property
    def work(self) -> Path:
        return self.scratch / "tree"

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def short(s: str, n: int = 700) -> str:
    return s if len(s) <= n else s[:n] + "..."


def run_cmd(args: list[str], cwd: Path, timeout: float, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a command in its own process group; returns (rc, combined output). rc -9 means timeout."""
    full_env = dict(os.environ)
    full_env.pop("MAKEFLAGS", None)
    full_env.pop("MFLAGS", None)
    for secret in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        full_env.pop(secret, None)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", start_new_session=True)
    except FileNotFoundError as exc:
        raise InfraError(f"cannot run {args[0]}: {exc}") from exc
    try:
        out, _ = proc.communicate(timeout=max(1.0, timeout))
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        return -9, (out or "") + "\n[timed out]"


# --------------------------------------------------------------------------
# Scratch tree, build, unit tests, coverage
# --------------------------------------------------------------------------
SKIP_ANYWHERE = {"build-native_std", ".git", "__pycache__"}
SKIP_IN_CTF = {"venv", "CTF", "results"}


def prepare_tree(ctx: Ctx) -> None:
    if "tree" in ctx.cache:
        return

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = {n for n in names if n in SKIP_ANYWHERE}
        if Path(directory) == ctx.repo / "ctf":
            skip |= {n for n in names if n in SKIP_IN_CTF}
        return skip

    try:
        shutil.copytree(ctx.repo, ctx.work, symlinks=True, ignore=ignore)
    except OSError as exc:
        raise InfraError(f"cannot copy the repository into the scratch directory: {exc}") from exc
    ctx.cache["tree"] = True


def build_step(ctx: Ctx) -> tuple[bool, str]:
    """Configure and install in the scratch tree. Returns (success, combined log)."""
    if "build" in ctx.cache:
        return ctx.cache["build"]  # type: ignore[return-value]
    prepare_tree(ctx)
    if not (ctx.work / "Makefile").is_file():
        ctx.cache["build"] = (False, "no top-level Makefile in the repository")
        return ctx.cache["build"]  # type: ignore[return-value]
    rc1, out1 = run_cmd(["make", "native_std.prep"], ctx.work, min(600, ctx.remaining()))
    log = out1
    ok = rc1 == 0
    if ok:
        rc2, out2 = run_cmd(["make", f"-j{JOBS}", "native_std.install"], ctx.work, min(1200, ctx.remaining()))
        log += out2
        ok = rc2 == 0
    ctx.cache["build"] = (ok, log)
    return ok, log


def runtest_step(ctx: Ctx) -> tuple[int, str]:
    if "runtest" in ctx.cache:
        return ctx.cache["runtest"]  # type: ignore[return-value]
    ok, _ = build_step(ctx)
    if not ok:
        raise Fail("build did not complete")
    rc, out = run_cmd(["make", f"-j{JOBS}", "native_std.runtest"], ctx.work, min(900, ctx.remaining()))
    ctx.cache["runtest"] = (rc, out)
    return rc, out


def hk_source_files(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "apps" / "hk" / "fsw" / "src").glob("*.c"))


def gcov_line_coverage(root: Path) -> dict[str, tuple[int, int]]:
    """Per HK source file: (executed lines, instrumented lines), from the coverage test objects."""
    base = root / "build-native_std" / "native"
    result: dict[str, tuple[int, int]] = {}
    gcdas = sorted(base.rglob("coverage-hk-*-object.dir/**/*.gcda"))
    if not gcdas:
        raise Fail("no coverage data (.gcda) found for the HK sources after running the unit tests")
    src_dir = (root / "apps" / "hk" / "fsw" / "src").resolve()
    for gcda in gcdas:
        rc, out = run_cmd(["gcov", "--json-format", "--stdout", gcda.name], gcda.parent, 120)
        if rc != 0:
            raise InfraError(f"gcov failed on {gcda}: {short(out)}")
        # gcov may print several JSON documents; decode them one by one.
        dec = json.JSONDecoder()
        pos = 0
        while pos < len(out):
            while pos < len(out) and out[pos].isspace():
                pos += 1
            if pos >= len(out):
                break
            doc, pos = dec.raw_decode(out, pos)
            for f in doc.get("files", []):
                path = Path(f["file"])
                if not path.is_absolute():
                    path = (gcda.parent / path)
                try:
                    path = path.resolve()
                except OSError:
                    continue
                if path.parent != src_dir or path.suffix != ".c":
                    continue
                counts: dict[int, int] = {}
                for ln in f.get("lines", []):
                    n = int(ln["line_number"])
                    counts[n] = max(counts.get(n, 0), int(ln["count"]))
                hit = sum(1 for c in counts.values() if c > 0)
                prev = result.get(path.name, (0, 0))
                # The same file may be reported by more than one object; keep the better covered one.
                if prev[1] == 0 or hit * prev[1] > prev[0] * len(counts):
                    result[path.name] = (hit, len(counts))
    return result


# --------------------------------------------------------------------------
# Static source helpers
# --------------------------------------------------------------------------
def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError as exc:
        raise Fail(f"cannot read {path}: {exc}") from exc


def strip_c_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


# --------------------------------------------------------------------------
# Ground link to a running cFS
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Pkt:
    t: float
    mid: int
    raw: bytes


@dataclass(frozen=True)
class Evt:
    t: float
    app: str
    eid: int
    etype: int
    text: str


def build_command(mid: int, cc: int, payload: bytes = b"") -> bytes:
    total = 6 + 2 + len(payload)
    pkt = bytearray(struct.pack(">HHH", mid, 0xC000, total - 7))
    pkt += bytes([cc, 0]) + payload
    assert len(pkt) == total
    checksum = 0xFF
    for b in pkt:
        checksum ^= b
    pkt[7] = checksum
    return bytes(pkt)


def hex_tokens(text: str) -> set[int]:
    return {int(tok, 16) for tok in re.findall(r"0[xX]([0-9A-Fa-f]+)", text)}


def await_free_ports(timeout: float) -> None:
    """Wait until nothing else holds the CI_LAB and TO_LAB UDP ports; another cFS may be running on the host."""
    end = time.monotonic() + timeout
    while True:
        busy: list[int] = []
        for port in (CI_LAB_PORT, TO_LAB_PORT):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind((HOST, port))
            except OSError:
                busy.append(port)
            finally:
                probe.close()
        if not busy:
            return
        if time.monotonic() >= end:
            raise InfraError(f"UDP ports {busy} stayed in use for {timeout:.0f} s (another cFS on this host?)")
        time.sleep(2.0)


class Ground:
    def __init__(self) -> None:
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.rx.bind((HOST, TO_LAB_PORT))
        except OSError as exc:
            self.rx.close()
            raise InfraError(f"cannot listen on {HOST}:{TO_LAB_PORT}: {exc}") from exc
        self.rx.settimeout(0.1)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.packets: list[Pkt] = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_flag.is_set():
            try:
                data, _ = self.rx.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            if len(data) < 6:
                continue
            mid = struct.unpack(">H", data[:2])[0]
            with self.lock:
                self.packets.append(Pkt(time.monotonic(), mid, data))

    def close(self) -> None:
        self.stop_flag.set()
        self.thread.join(timeout=2)
        self.rx.close()
        self.tx.close()

    def send(self, mid: int, cc: int, payload: bytes = b"") -> float:
        t = time.monotonic()
        self.tx.sendto(build_command(mid, cc, payload), (HOST, CI_LAB_PORT))
        return t

    def snapshot(self) -> list[Pkt]:
        with self.lock:
            return list(self.packets)

    def since(self, t: float, mid: int | None = None) -> list[Pkt]:
        return [p for p in self.snapshot() if p.t >= t and (mid is None or p.mid == mid)]

    def wait_for(self, mid: int, t: float, timeout: float) -> Pkt | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = self.since(t, mid)
            if got:
                return got[0]
            time.sleep(0.02)
        return None

    def events(self, t: float, app: str = "HK") -> list[Evt]:
        out: list[Evt] = []
        for p in self.since(t, MID_LONG_EVENT):
            if len(p.raw) < EVENT_MSG_OFFSET + 4:
                continue
            name = p.raw[TLM_HEADER_LEN:TLM_HEADER_LEN + 20].split(b"\0")[0].decode("ascii", "replace")
            eid, etype = struct.unpack("<HH", p.raw[TLM_HEADER_LEN + 20:TLM_HEADER_LEN + 24])
            msg = p.raw[EVENT_MSG_OFFSET:EVENT_MSG_OFFSET + EVENT_MSG_LEN].split(b"\0")[0].decode("ascii", "replace")
            if name == app:
                out.append(Evt(p.t, name, eid, etype, msg))
        return out


@dataclass(frozen=True)
class Hk:
    raw_len: int
    command_counter: int
    command_error_counter: int
    combined_sent: int
    missing_data: int
    stale_input_count: int | None
    stale_transition_ctr: int | None
    stale_cycles: int | None
    spare: bytes | None


OLD_FMT = "<BBHHHI"
OLD_LEN = TLM_HEADER_LEN + struct.calcsize(OLD_FMT)


def parse_hk(pkt: Pkt) -> Hk:
    """Parse the existing fields always, the appended fields only when the packet carries them."""
    if len(pkt.raw) < OLD_LEN:
        raise Fail(f"HK housekeeping packet is {len(pkt.raw)} bytes, shorter than the existing payload")
    cc, cec, _pad, sent, missing, _mp = struct.unpack(OLD_FMT, pkt.raw[TLM_HEADER_LEN:OLD_LEN])
    if len(pkt.raw) >= HK_LEN:
        sic, stc, cyc, spare = struct.unpack("<HHB3s", pkt.raw[OLD_LEN:HK_LEN])
        return Hk(len(pkt.raw), cc, cec, sent, missing, sic, stc, cyc, spare)
    return Hk(len(pkt.raw), cc, cec, sent, missing, None, None, None, None)


def have(value: int | None, name: str) -> int:
    """The appended field, or a Fail when the housekeeping packet is too short to hold it."""
    if value is None:
        raise Fail(f"{name} is not present: the housekeeping packet is shorter than {HK_LEN} bytes")
    return value


class CfsProcess:
    """Runs core-cpu1 from the scratch install directory."""

    def __init__(self, exe_dir: Path, log_path: Path) -> None:
        self.exe_dir = exe_dir
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self, timeout: float) -> None:
        core = self.exe_dir / "core-cpu1"
        if not core.is_file():
            raise Fail(f"{core} was not produced by the build")
        if not os.access(core, os.X_OK):
            raise Fail(f"{core} is not executable")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((HOST, CI_LAB_PORT))
        except OSError as exc:
            raise InfraError(f"UDP port {CI_LAB_PORT} is already in use: {exc}") from exc
        finally:
            probe.close()
        logf = open(self.log_path, "wb")
        self.proc = subprocess.Popen([str(core)], cwd=self.exe_dir, stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        logf.close()
        end = time.monotonic() + timeout
        marker = b"CFE_ES_Main entering OPERATIONAL state"
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise Fail(f"cFS exited before becoming operational (status {self.proc.returncode}); "
                           f"log tail: {self.log_tail()}")
            try:
                if marker in self.log_path.read_bytes():
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise Fail(f"cFS did not reach the operational state within {timeout:.0f} s; log tail: {self.log_tail()}")

    def log_tail(self, n: int = 1500) -> str:
        try:
            return self.log_path.read_bytes()[-n:].decode("utf-8", "replace")
        except OSError:
            return ""

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        self.proc = None
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(60):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=10)


# --------------------------------------------------------------------------
# The UDP scenario (one cFS boot serves several checks)
# --------------------------------------------------------------------------
class Scenario:
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.errors: dict[str, str] = {}
        self.notes: dict[str, str] = {}
        self.startup_error: str | None = None
        self.g: Ground | None = None
        self.cfs: CfsProcess | None = None
        self.c0 = 0
        self.e0 = 0
        self.hk0: Hk | None = None

    # -- helpers ------------------------------------------------------------
    def request_hk(self) -> Hk:
        assert self.g is not None
        t = self.g.send(MID_HK_SEND_HK, 0)
        first = self.g.wait_for(MID_HK_TLM, t, 4.0)
        if first is None:
            raise Fail(f"no HK housekeeping packet (MID 0x{MID_HK_TLM:04X}) within 4 s of the request")
        time.sleep(0.25)
        latest = self.g.since(t, MID_HK_TLM)[-1]
        return parse_hk(latest)

    def wait_sample_hk(self, timeout: float = 9.0) -> float:
        assert self.g is not None
        t = time.monotonic()
        pkt = self.g.wait_for(MID_SAMPLE_APP_HK, t, timeout)
        if pkt is None:
            raise Fail(f"no SAMPLE_APP housekeeping packet (MID 0x{MID_SAMPLE_APP_HK:04X}) within {timeout:.0f} s")
        return pkt.t

    def send_pkt2(self, count: int, gap: float = 0.05) -> None:
        assert self.g is not None
        for _ in range(count):
            self.g.send(MID_HK_SEND_COMBINED, 0, struct.pack("<I", MID_HK_PKT2))
            time.sleep(gap)

    def set_cycles(self, value: int) -> float:
        assert self.g is not None
        return self.g.send(MID_HK_CMD, CC_HK_SET_STALE, bytes([value, 0, 0, 0]))

    def record(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Fail as exc:
            self.errors[name] = str(exc)
        except InfraError:
            raise
        except Exception as exc:  # noqa: BLE001 - a malformed packet must not hide the other checks
            self.errors[name] = f"{type(exc).__name__}: {exc}"

    # -- steps ----------------------------------------------------------------
    def step_layout(self) -> None:
        hk = self.request_hk()
        self.hk0 = hk
        self.c0, self.e0 = hk.command_counter, hk.command_error_counter
        if hk.raw_len != HK_LEN:
            raise Fail(f"HK housekeeping packet is {hk.raw_len} bytes, wanted {HK_LEN}")
        if hk.stale_cycles != 3:
            raise Fail(f"StaleCycles at start is {hk.stale_cycles}, wanted 3")
        if hk.stale_input_count != 0 or hk.stale_transition_ctr != 0:
            raise Fail(f"StaleInputCount={hk.stale_input_count} StaleTransitionCtr={hk.stale_transition_ctr} "
                       "before any combined send, wanted 0 and 0")
        if hk.spare != b"\0\0\0":
            raise Fail(f"Spare2 bytes are {hk.spare!r}, wanted zeros")

    def step_set_cycles(self) -> None:
        assert self.g is not None
        base = self.request_hk() if self.hk0 is None else self.hk0
        c0, e0 = base.command_counter, base.command_error_counter
        problems: list[str] = []
        for value in (255, 2):
            t = self.set_cycles(value)
            time.sleep(0.5)
            evs = [e for e in self.g.events(t) if e.eid == EID_STALE_SET]
            if len(evs) != 1:
                problems.append(f"value {value}: {len(evs)} events with ID {EID_STALE_SET}")
            else:
                if evs[0].etype != EVT_INFORMATION:
                    problems.append(f"event {EID_STALE_SET} has type {evs[0].etype}, wanted INFORMATION")
                if not re.search(rf"(?<!\d){value}(?!\d)", evs[0].text):
                    problems.append(f"event {EID_STALE_SET} text does not contain {value}: {evs[0].text!r}")
        t = self.set_cycles(0)
        time.sleep(0.5)
        evs = [e for e in self.g.events(t) if e.eid == EID_STALE_REJECT]
        if len(evs) != 1:
            problems.append(f"value 0: {len(evs)} events with ID {EID_STALE_REJECT}")
        elif evs[0].etype != EVT_ERROR:
            problems.append(f"event {EID_STALE_REJECT} has type {evs[0].etype}, wanted ERROR")
        if [e for e in self.g.events(t) if e.eid == EID_STALE_SET]:
            problems.append("value 0 also produced event 38")
        hk = self.request_hk()
        if hk.command_counter != (c0 + 2) % 256:
            problems.append(f"CommandCounter {c0} -> {hk.command_counter}, wanted +2")
        if hk.command_error_counter != (e0 + 1) % 256:
            problems.append(f"CommandErrorCounter {e0} -> {hk.command_error_counter}, wanted +1")
        cycles = have(hk.stale_cycles, "StaleCycles")
        if cycles != 2:
            problems.append(f"StaleCycles is {cycles} after setting 255, 2 and rejected 0; wanted 2")
        if problems:
            raise Fail("; ".join(problems))
        self.c0, self.e0 = hk.command_counter, hk.command_error_counter

    def step_stale(self) -> None:
        assert self.g is not None
        t_arr = self.wait_sample_hk()
        self.notes["t_arr"] = f"{t_arr:.3f}"
        self.send_pkt2(4)
        time.sleep(1.0)
        evs = [e for e in self.g.events(t_arr) if e.eid == EID_STALE]
        problems: list[str] = []
        if len(evs) != 1:
            problems.append(f"{len(evs)} events with ID {EID_STALE} after four combined packet 2 requests, wanted exactly 1")
        else:
            toks = hex_tokens(evs[0].text)
            if MID_SAMPLE_APP_HK not in toks or MID_HK_PKT2 not in toks:
                problems.append(f"event {EID_STALE} text does not name 0x{MID_SAMPLE_APP_HK:04X} and "
                                f"0x{MID_HK_PKT2:04X} in hex: {evs[0].text!r}")
            if evs[0].etype != EVT_INFORMATION:
                problems.append(f"event {EID_STALE} has type {evs[0].etype}, wanted INFORMATION")
        hk = self.request_hk()
        sic = have(hk.stale_input_count, "StaleInputCount")
        stc = have(hk.stale_transition_ctr, "StaleTransitionCtr")
        if sic != 1:
            problems.append(f"StaleInputCount is {sic}, wanted 1")
        if stc != 1:
            problems.append(f"StaleTransitionCtr is {stc}, wanted 1")
        if problems:
            raise Fail("; ".join(problems))

    def step_report(self) -> None:
        assert self.g is not None
        before = self.request_hk()
        t = self.g.send(MID_HK_CMD, CC_HK_REPORT_STALE)
        first = self.g.wait_for(MID_HK_STALE_RPT, t, 3.0)
        if first is None:
            raise Fail(f"no packet on MID 0x{MID_HK_STALE_RPT:04X} within 3 s of REPORT_STALE")
        time.sleep(0.4)
        pkts = self.g.since(t, MID_HK_STALE_RPT)
        problems: list[str] = []
        if len(pkts) != 1:
            problems.append(f"{len(pkts)} report packets, wanted exactly 1")
        raw = pkts[0].raw
        if len(raw) != REPORT_LEN:
            raise Fail(f"report packet is {len(raw)} bytes, wanted {REPORT_LEN}")
        fields = struct.unpack(REPORT_FMT, raw[TLM_HEADER_LEN:])
        stale_count, reported = fields[0], fields[1]
        ins = fields[2:2 + REPORT_SLOTS]
        outs = fields[2 + REPORT_SLOTS:]
        if stale_count != 1:
            problems.append(f"StaleCount {stale_count}, wanted 1")
        if reported != 1:
            problems.append(f"ReportedCount {reported}, wanted 1")
        if ins[0] != MID_SAMPLE_APP_HK:
            problems.append(f"InputMid[0] 0x{ins[0]:X}, wanted 0x{MID_SAMPLE_APP_HK:04X}")
        if outs[0] != MID_HK_PKT2:
            problems.append(f"OutputMid[0] 0x{outs[0]:X}, wanted 0x{MID_HK_PKT2:04X}")
        if any(v != INVALID_MID for v in ins[1:]) or any(v != INVALID_MID for v in outs[1:]):
            problems.append("unused slots are not all the invalid message ID")
        after = self.request_hk()
        if after.command_counter != (before.command_counter + 1) % 256:
            problems.append(f"CommandCounter {before.command_counter} -> {after.command_counter}, wanted +1")
        if problems:
            raise Fail("; ".join(problems))

    def step_fresh(self) -> None:
        assert self.g is not None
        t0 = time.monotonic()
        t_arr = self.wait_sample_hk(9.0)
        self.notes["t_arr2"] = f"{t_arr:.3f}"
        time.sleep(0.5)
        evs = [e for e in self.g.events(t0) if e.eid == EID_FRESH]
        problems: list[str] = []
        if len(evs) != 1:
            problems.append(f"{len(evs)} events with ID {EID_FRESH} when the input arrived, wanted exactly 1")
        else:
            toks = hex_tokens(evs[0].text)
            if MID_SAMPLE_APP_HK not in toks or MID_HK_PKT2 not in toks:
                problems.append(f"event {EID_FRESH} text does not name 0x{MID_SAMPLE_APP_HK:04X} and "
                                f"0x{MID_HK_PKT2:04X} in hex: {evs[0].text!r}")
            if evs[0].etype != EVT_INFORMATION:
                problems.append(f"event {EID_FRESH} has type {evs[0].etype}, wanted INFORMATION")
        self.send_pkt2(1)
        time.sleep(0.3)
        hk = self.request_hk()
        sic = have(hk.stale_input_count, "StaleInputCount")
        if sic != 0:
            problems.append(f"StaleInputCount is {sic} after the input arrived, wanted 0")
        if problems:
            raise Fail("; ".join(problems))

    def step_reset(self) -> None:
        assert self.g is not None
        self.send_pkt2(3)
        time.sleep(0.4)
        pre = self.request_hk()
        pre_ctr = have(pre.stale_transition_ctr, "StaleTransitionCtr")
        pre_cnt = have(pre.stale_input_count, "StaleInputCount")
        pre_cyc = have(pre.stale_cycles, "StaleCycles")
        if pre_ctr == 0 or pre_cnt == 0:
            raise Fail(f"could not build a state with StaleTransitionCtr and StaleInputCount above 0 "
                       f"(got {pre_ctr} and {pre_cnt}) before RESET_COUNTERS")
        self.g.send(MID_HK_CMD, CC_HK_RESET)
        time.sleep(0.3)
        post = self.request_hk()
        problems: list[str] = []
        if post.stale_transition_ctr != 0:
            problems.append(f"StaleTransitionCtr is {post.stale_transition_ctr} after RESET_COUNTERS, wanted 0")
        if post.stale_input_count != pre_cnt:
            problems.append(f"StaleInputCount changed {pre_cnt} -> {post.stale_input_count}")
        if post.stale_cycles != pre_cyc:
            problems.append(f"StaleCycles changed {pre_cyc} -> {post.stale_cycles}")
        if post.command_counter != 0 or post.command_error_counter != 0:
            problems.append("command counters were not reset")
        if problems:
            raise Fail("; ".join(problems))

    # -- driver -----------------------------------------------------------------
    def run(self) -> None:
        exe_dir = self.ctx.work / "build-native_std" / "exe" / "cpu1"
        ok, _ = build_step(self.ctx)
        if not ok:
            self.startup_error = "the build did not complete"
            return
        run_dir = self.ctx.scratch / "cfs-run"
        run_dir.mkdir(exist_ok=True)
        await_free_ports(PORT_WAIT_S)
        self.g = Ground()
        self.cfs = CfsProcess(exe_dir, run_dir / "cfs.log")
        try:
            try:
                self.cfs.start(40.0)
            except Fail as exc:
                self.startup_error = str(exc)
                return
            time.sleep(3.0)
            payload = b"127.0.0.1".ljust(16, b"\0")
            end = time.monotonic() + 20
            while time.monotonic() < end and not self.g.snapshot():
                self.g.send(MID_TO_LAB_CMD, 6, payload)
                time.sleep(0.5)
            if not self.g.snapshot():
                self.startup_error = ("no telemetry was received from cFS after enabling TO_LAB output; "
                                      f"log tail: {self.cfs.log_tail()}")
                return
            self.record("layout", self.step_layout)
            self.record("set", self.step_set_cycles)
            self.record("stale", self.step_stale)
            self.record("report", self.step_report)
            self.record("fresh", self.step_fresh)
            self.record("reset", self.step_reset)
        finally:
            self.cfs.stop()
            if self.cfs is not None:
                self.notes["cfs_log_tail"] = self.cfs.log_tail(800)
            self.g.close()


def scenario(ctx: Ctx) -> Scenario:
    """Run the UDP scenario; a scenario with any failure is repeated once on a fresh cFS boot."""
    sc = ctx.cache.get("scenario")
    if sc is None:
        sc = Scenario(ctx)
        sc.run()
        if (sc.startup_error is not None or sc.errors) and ctx.remaining() > 240:
            first = sc
            sc = Scenario(ctx)
            sc.run()
            sc.notes["first_attempt_errors"] = f"{first.startup_error!r} {first.errors}"
        ctx.cache["scenario"] = sc
    return sc  # type: ignore[return-value]


def live_result(ctx: Ctx, step: str) -> None:
    sc = scenario(ctx)
    if sc.startup_error is not None:
        raise Fail(f"scenario could not run: {sc.startup_error}")
    if step in sc.errors:
        raise Fail(sc.errors[step])


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    feedback: str
    fn: Callable[[Ctx], None]


def c_build(ctx: Ctx) -> None:
    ok, log = build_step(ctx)
    diag = [ln for ln in log.splitlines() if re.search(r"\b(warning|error)\b\s*:|CMake (Warning|Error)|\*\*\*", ln, re.I)]
    if not ok:
        raise Fail("build failed:\n" + short("\n".join(diag[:12]) or log[-1500:], 1800))
    if diag:
        raise Fail("build produced diagnostics:\n" + short("\n".join(diag[:12]), 1800))


def c_unit(ctx: Ctx) -> None:
    rc, out = runtest_step(ctx)
    results = ctx.work / "build-native_std" / "test-results"
    problems: list[str] = []
    logs = sorted(results.glob("coverage-hk-*.log")) if results.is_dir() else []
    wanted = {f"coverage-hk-{n}.log" for n in ("hk_app", "hk_cmds", "hk_dispatch", "hk_utils")}
    missing = sorted(wanted - {p.name for p in logs})
    if missing:
        problems.append("missing HK test logs: " + ", ".join(missing))
    for p in logs:
        text = read_text(p)
        fails = [ln for ln in text.splitlines() if ln.startswith("[ FAIL]")]
        bad = re.findall(r"FAIL::([1-9][0-9]*)", text)
        if fails or bad:
            problems.append(f"{p.name}: {len(fails)} failing assertions")
    if rc != 0:
        tail = [ln for ln in out.splitlines() if "***" in ln or "FAIL" in ln][:6]
        problems.append(f"native_std.runtest exit {rc}: " + " | ".join(tail))
    if problems:
        raise Fail("; ".join(problems))


def c_coverage(ctx: Ctx) -> None:
    rc, _ = runtest_step(ctx)
    if rc != 0:
        raise Fail("the unit tests did not pass, so coverage was not evaluated")
    try:
        baseline = json.loads(BASELINE_FILE.read_text())
    except (OSError, ValueError) as exc:
        raise InfraError(f"baseline coverage file unreadable: {exc}") from exc
    now = gcov_line_coverage(ctx.work)
    low: list[str] = []
    for name, base in sorted(baseline["files"].items()):
        cur = now.get(name)
        if cur is None:
            low.append(f"{name}: no coverage data (baseline {base['covered']}/{base['lines']})")
            continue
        # cur.covered / cur.lines >= base.covered / base.lines, in integers.
        if cur[0] * base["lines"] < base["covered"] * cur[1]:
            low.append(f"{name}: {cur[0]}/{cur[1]} lines = {100.0 * cur[0] / cur[1]:.2f}%, "
                       f"baseline {base['covered']}/{base['lines']} = {100.0 * base['covered'] / base['lines']:.2f}%")
    ctx.cache["coverage_now"] = now
    if low:
        raise Fail("; ".join(low))


HIDDEN_AREAS: dict[str, str] = {
    "T01": "the StaleCycles default of 3 in housekeeping telemetry",
    "T02": "SET_STALE_CYCLES accepting 1 to 255 (event 38 with the value, CommandCounter, StaleCycles)",
    "T03": "SET_STALE_CYCLES rejecting 0 (event 39, CommandErrorCounter, threshold unchanged)",
    "T04": "the stale transition (exactly one event 40 naming both message IDs in hex, consecutive missed "
           "cycles counted per copy table entry, StaleInputCount, StaleTransitionCtr)",
    "T05": "a changed threshold taking effect at the next combined send, not immediately, in both directions (raising makes a stale entry fresh with event 41, lowering makes it stale with event 40)",
    "T06": "the fresh transition (exactly one event 41 naming both message IDs, missed cycles restarting)",
    "T07": "the REPORT_STALE packet contents (counts, copy table order, invalid unused slots, 16 entry limit)",
    "T08": "RESET_COUNTERS zeroing StaleTransitionCtr only",
    "T09": "loading a new copy table resetting missed cycles and stale states without events",
    "T10": "length validation of the new commands",
}

HIDDEN_CMAKE = """
add_cfe_coverage_test(hk UNIT_NAME hidden
    TESTCASE_SOURCES ${CMAKE_CURRENT_LIST_DIR}/hk_hidden_tests.c
    SUBJECT_SOURCES ${APP_SRC_FILES})
target_include_directories(coverage-hk-hidden-testrunner PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/../fsw/src
    ${CMAKE_CURRENT_LIST_DIR}/../fsw/inc)
"""


class HiddenFail(Fail):
    def __init__(self, detail: str, feedback: str | None = None) -> None:
        super().__init__(detail)
        self.feedback = feedback


def c_hidden(ctx: Ctx) -> None:
    ok, _ = build_step(ctx)
    if not ok:
        raise Fail("build did not complete")
    cmake = ctx.work / "apps" / "hk" / "unit-test" / "CMakeLists.txt"
    if not cmake.is_file():
        raise HiddenFail("apps/hk/unit-test/CMakeLists.txt is missing")
    shutil.copy2(HIDDEN_TEST, ctx.work / "apps" / "hk" / "unit-test" / "hk_hidden_tests.c")
    with open(cmake, "a") as fh:
        fh.write(HIDDEN_CMAKE)
    rc, out = run_cmd(["make", "native_std.prep"], ctx.work, min(600, ctx.remaining()))
    if rc != 0:
        raise HiddenFail("reconfigure failed:\n" + short(out[-1500:], 1800),
                         "The HK build configuration no longer configures with the HK sources as specified.")
    arch = ctx.work / "build-native_std" / "native" / "default_cpu1"
    rc, out = run_cmd(["make", "-C", str(arch), f"-j{JOBS}", "coverage-hk-hidden-testrunner"], ctx.work,
                      min(900, ctx.remaining()))
    if rc != 0:
        diag = [ln for ln in out.splitlines() if re.search(r"error|undefined reference", ln)]
        raise HiddenFail("compile/link failed:\n" + short("\n".join(diag[:15]) or out[-1500:], 2000),
                         "The HK sources do not compile and link against the names, types, function codes and "
                         "event IDs given in the specification (for example HK_SetStaleCyclesCmd_t, "
                         "HK_ReportStaleCmd_t, HK_StaleReportTlm_t, HK_StaleReport_Payload_t, "
                         "HK_SET_STALE_CYCLES_CC, HK_REPORT_STALE_CC, HK_STALE_RPT_TLM_MID, "
                         "HK_STALE_CYCLES_INF_EID, HK_STALE_CYCLES_ERR_EID, HK_INPUT_STALE_EID, "
                         "HK_INPUT_FRESH_EID, and the appended HK_HkTlm_Payload_t fields).")
    runner = arch / "apps" / "hk" / "unit-test" / "coverage-hk-hidden-testrunner"
    rc, out = run_cmd([str(runner)], runner.parent, min(180, ctx.remaining()))
    names: list[str] = []
    failing: list[str] = []
    for m in re.finditer(r"\[\s*END\]\s+(\d+)\s+(\S+)\s+TOTAL::(\d+)\s+PASS::(\d+)\s+FAIL::(\d+)", out):
        name = m.group(2)
        if name == "SUMMARY":
            continue
        names.append(name)
        if int(m.group(5)) > 0:
            failing.append(name)
    fail_lines = [ln for ln in out.splitlines() if ln.startswith("[ FAIL]")]
    complete = re.search(r"COMPLETE: (\d+) tests", out)
    if rc != 0 or failing or len(names) != EXPECTED_HIDDEN_TESTS or complete is None:
        areas: list[str] = []
        unfinished = EXPECTED_HIDDEN_TESTS - len(names)
        tags = [n.split("_")[0] for n in failing]
        if unfinished > 0 and names:
            # The run stopped early (crash); blame the cases that never reported.
            tags.append(f"T{len(names) + 1:02d}")
        for tag in tags:
            key = re.match(r"T\d+", tag)
            area = HIDDEN_AREAS.get(key.group(0)[:3] if key else "")
            if area and area not in areas:
                areas.append(area)
        detail = (f"runner exit {rc}; reported {len(names)} of {EXPECTED_HIDDEN_TESTS} cases; failing: {failing}; "
                  + short(" | ".join(fail_lines[:12]), 1500) + "\n" + short(out[-800:], 800))
        if not areas:
            areas.append("the stale tracking behavior as a whole")
        raise HiddenFail(detail, "Unit-level exercise of the HK staleness logic found deviations from the "
                                 "specification in: " + "; ".join(sorted(areas)) + ".")


def c_to_lab(ctx: Ctx) -> None:
    path = ctx.repo / "sample_defs" / "tables" / "to_lab_sub.c"
    text = strip_c_comments(read_text(path))
    if not re.search(r"WRAP_VALUE\s*\(\s*HK_STALE_RPT_TLM_MID\s*\)", text):
        raise Fail("no subscription entry with HK_STALE_RPT_TLM_MID in sample_defs/tables/to_lab_sub.c")
    if "hk_msgids.h" not in text:
        raise Fail("hk_msgids.h is not included")


def c_copy_table(ctx: Ctx) -> None:
    path = ctx.repo / "apps" / "hk" / "fsw" / "tables" / "hk_cpy_tbl.c"
    text = strip_c_comments(read_text(path))
    entries = re.findall(r"\{([^{}]*)\}", text)
    token = re.compile(r"CFE_SB_MSGID_WRAP_VALUE\(\s*(\w+)\s*\)|CFE_SB_MSGID_RESERVED|\b(0[xX][0-9a-fA-F]+|\d+)\b")
    found = []
    for body in entries:
        vals: list[object] = []
        for m in token.finditer(body):
            if m.group(1):
                vals.append(m.group(1))
            elif m.group(2):
                vals.append(int(m.group(2), 0))
            else:
                vals.append("RESERVED")
        if len(vals) == 5:
            found.append(vals)
    want = ["SAMPLE_APP_HK_TLM_MID", 12, "HK_COMBINED_PKT2_MID", 12, 2]
    if want not in found:
        near = [v for v in found if v and v[0] == "SAMPLE_APP_HK_TLM_MID"]
        raise Fail(f"no entry {want} in the default copy table; entries for the input: {near}")


def c_layout(ctx: Ctx) -> None:
    live_result(ctx, "layout")


def c_set(ctx: Ctx) -> None:
    live_result(ctx, "set")


def c_stale(ctx: Ctx) -> None:
    live_result(ctx, "stale")


def c_report(ctx: Ctx) -> None:
    live_result(ctx, "report")


def c_fresh(ctx: Ctx) -> None:
    live_result(ctx, "fresh")


def c_reset(ctx: Ctx) -> None:
    live_result(ctx, "reset")


def c_ccdd(ctx: Ctx) -> None:
    root = ctx.repo / "ctf" / "ccdd" / "json"
    if not root.is_dir():
        raise Fail("ctf/ccdd/json does not exist")
    blobs: dict[str, str] = {}
    for p in sorted(root.glob("*.json")):
        text = read_text(p)
        try:
            json.loads(text)
        except ValueError as exc:
            raise Fail(f"{p.name} is not valid JSON: {exc}") from exc
        blobs[p.name] = text
    allt = "\n".join(blobs.values()).lower()
    problems: list[str] = []
    if "set_stale_cycles" not in allt:
        problems.append("no SET_STALE_CYCLES command definition")
    if "report_stale" not in allt:
        problems.append("no REPORT_STALE command definition")
    if "hk_stale_rpt_tlm_mid" not in allt and "0x08a2" not in allt:
        problems.append("no stale report telemetry definition")
    elif "staleinputcount" not in allt and "stalecount" not in allt:
        problems.append("stale report telemetry fields not defined")
    hk_tlm = [t for n, t in blobs.items() if "hk_hk_tlm" in n or "HK_HK_TLM_MID" in t]
    if not any(all(f in t for f in ("StaleInputCount", "StaleTransitionCtr", "StaleCycles")) for t in hk_tlm):
        problems.append("the HK housekeeping telemetry definition lacks StaleInputCount, StaleTransitionCtr, StaleCycles")
    if problems:
        raise Fail("; ".join(problems))


def c_ctf(ctx: Ctx) -> None:
    script = ctx.repo / "ctf" / "tests" / "hk_staleness.json"
    if not script.is_file():
        raise Fail("ctf/tests/hk_staleness.json does not exist")
    text = read_text(script)
    try:
        json.loads(text)
    except ValueError as exc:
        raise Fail(f"ctf/tests/hk_staleness.json is not valid JSON: {exc}") from exc
    missing = [w for w in ("SET_STALE_CYCLES", "SEND_COMBINED_PKT", "StaleInputCount") if w not in text]
    if missing:
        raise Fail("ctf/tests/hk_staleness.json does not reference: " + ", ".join(missing))
    runner = ctx.repo / "ctf" / "run_ctf.sh"
    if not runner.is_file():
        raise Fail("ctf/run_ctf.sh does not exist")
    ok, _ = build_step(ctx)
    if not ok:
        raise Fail("build did not complete")
    await_free_ports(PORT_WAIT_S)
    results = ctx.scratch / "ctf-results"
    results.mkdir(exist_ok=True)
    env = {"CFS_ROOT": str(ctx.work), "CFS_RUN_DIR": str(ctx.work / "build-native_std" / "exe" / "cpu1"),
           "CTF_RESULTS_DIR": str(results)}
    rc, out = run_cmd(["bash", str(runner), str(script)], ctx.repo / "ctf", min(900, ctx.remaining()), env)
    for pid_dir in Path("/proc").glob("[0-9]*"):
        try:
            if (pid_dir / "cwd").resolve() == (ctx.work / "build-native_std" / "exe" / "cpu1").resolve():
                os.kill(int(pid_dir.name), signal.SIGKILL)
        except (OSError, ValueError):
            continue
    if rc != 0:
        if "CTF is not set up" in out:
            raise InfraError("CTF is not set up in the repository under test: " + short(out, 300))
        raise Fail(f"run_ctf.sh exit {rc}: " + short(out[-1500:], 1800))


def c_report_doc(ctx: Ctx) -> None:
    path = ctx.repo / "docs" / "reports" / "hk_staleness.md"
    if not path.is_file():
        raise Fail("docs/reports/hk_staleness.md does not exist")
    text = read_text(path)
    low = text.lower()
    problems: list[str] = []
    if len(text.strip()) < 200:
        problems.append("report is nearly empty")
    if not re.search(r"unit[ -]?tests?", low) or not re.search(r"\d", low):
        problems.append("no unit test counts")
    if "coverage" not in low or not re.search(r"\d+(\.\d+)?\s*%", text):
        problems.append("no coverage figures (percentages)")
    if "ctf" not in low:
        problems.append("no CTF results")
    if problems:
        raise Fail("; ".join(problems))


CHECKS: list[CheckSpec] = [
    CheckSpec("build-no-warnings",
              "`make native_std.prep && make -j native_std.install` must complete with no errors and no compiler "
              "warnings (the build uses -Werror).",
              c_build),
    CheckSpec("unit-tests-pass",
              "`make -j native_std.runtest` must pass: every existing and new HK unit test has to succeed.",
              c_unit),
    CheckSpec("coverage-not-below-baseline",
              "Line coverage of each HK source file in apps/hk/fsw/src must not fall below the value it had before "
              "the change; add unit tests in HK's existing style for every new or changed path.",
              c_coverage),
    CheckSpec("hidden-unit-test",
              "The HK staleness logic must behave as specified at the function level (threshold validation, "
              "stale and fresh transitions with exact event counts, report packet contents, counter reset, copy "
              "table reload).",
              c_hidden),
    CheckSpec("to-lab-subscription",
              "sample_defs/tables/to_lab_sub.c must subscribe TO_LAB to HK_STALE_RPT_TLM_MID so the stale report "
              "packet reaches the ground.",
              c_to_lab),
    CheckSpec("copy-table-entry",
              "The default copy table apps/hk/fsw/tables/hk_cpy_tbl.c must contain the entry: input "
              "SAMPLE_APP_HK_TLM_MID offset 12, output HK_COMBINED_PKT2_MID offset 12, 2 bytes.",
              c_copy_table),
    CheckSpec("hk-tlm-layout",
              "HK housekeeping telemetry (requested with HK_SEND_HK_MID) must append StaleInputCount (uint16), "
              "StaleTransitionCtr (uint16), StaleCycles (uint8) and Spare2[3] directly after MemPoolHandle, with "
              "StaleCycles equal to 3 at startup and both counters 0 before any combined send.",
              c_layout),
    CheckSpec("set-stale-cycles",
              "SET_STALE_CYCLES must accept 1 to 255 (CommandCounter +1, event 38 containing the new value) and "
              "reject 0 (CommandErrorCounter +1, event 39, threshold unchanged), verified on a running cFS with "
              "the values 255, 2 and 0.",
              c_set),
    CheckSpec("stale-transition",
              "With StaleCycles 2, requesting combined packet 2 four times right after a SAMPLE_APP housekeeping "
              "packet arrived must produce exactly one event 40 naming 0x0883 and the combined packet 2 message "
              "ID in hex, and housekeeping must then show StaleInputCount 1 and StaleTransitionCtr 1.",
              c_stale),
    CheckSpec("report-stale",
              "REPORT_STALE must make HK transmit exactly one packet on HK_STALE_RPT_TLM_MID (0x08A2, which TO_LAB "
              "must forward) with StaleCount 1, ReportedCount 1, InputMid[0] 0x0883, OutputMid[0] the combined "
              "packet 2 message ID, all other slots CFE_SB_INVALID_MSG_ID, and CommandCounter +1.",
              c_report),
    CheckSpec("fresh-transition",
              "When the next SAMPLE_APP housekeeping packet arrives for a stale entry, HK must emit exactly one "
              "event 41 naming both message IDs in hex, and StaleInputCount must return to 0.",
              c_fresh),
    CheckSpec("reset-counters",
              "RESET_COUNTERS must set StaleTransitionCtr to 0 and leave StaleInputCount and StaleCycles "
              "unchanged.",
              c_reset),
    CheckSpec("ccdd-definitions",
              "ctf/ccdd/json must define the SET_STALE_CYCLES command, the REPORT_STALE command, the stale report "
              "telemetry packet and the extended HK housekeeping payload (StaleInputCount, StaleTransitionCtr, "
              "StaleCycles) as valid JSON.",
              c_ccdd),
    CheckSpec("ctf-test-passes",
              "ctf/tests/hk_staleness.json must exist, set StaleCycles to 2 with SET_STALE_CYCLES, request "
              "combined packet 2 and check StaleInputCount and the stale and fresh events, and `ctf/run_ctf.sh "
              "ctf/tests/hk_staleness.json` must exit 0 with cFS not running beforehand.",
              c_ctf),
    CheckSpec("report-present",
              "docs/reports/hk_staleness.md must exist and report what changed, the unit test counts, the HK "
              "coverage figures as percentages, the CTF results and open issues.",
              c_report_doc),
]


def evaluate(ctx: Ctx) -> list[Result]:
    results: list[Result] = []
    for spec in CHECKS:
        assert ID_RE.match(spec.check_id), spec.check_id
        if ctx.remaining() < 5:
            raise InfraError(f"time budget exhausted before check {spec.check_id} could run")
        try:
            spec.fn(ctx)
            results.append(Result(spec.check_id, True, "", ""))
        except InfraError:
            raise
        except HiddenFail as exc:
            fb = exc.feedback or spec.feedback
            results.append(Result(spec.check_id, False, fb, str(exc)))
        except Fail as exc:
            detail = str(exc)
            if spec.check_id in LIVE_IDS:
                sc = ctx.cache.get("scenario")
                if isinstance(sc, Scenario):
                    detail += f" | notes={sc.notes}"
            results.append(Result(spec.check_id, False, spec.feedback, detail))
        except Exception:  # noqa: BLE001 - the agent's code produced something the check did not anticipate
            results.append(Result(spec.check_id, False, spec.feedback,
                                  "unexpected exception in the check:\n" + traceback.format_exc()))
    if ctx.remaining() < 0:
        raise InfraError("time budget exhausted")
    return results


LIVE_IDS = {"hk-tlm-layout", "set-stale-cycles", "stale-transition", "report-stale", "fresh-transition",
            "reset-counters"}

FORBIDDEN_FEEDBACK = ("acceptance/", "run.sh", "hk_hidden", "baseline_coverage")


def write_out(path: Path, results: list[Result]) -> None:
    checks = []
    for r in results:
        if not r.passed:
            assert r.feedback.strip(), f"empty feedback for {r.check_id}"
            for bad in FORBIDDEN_FEEDBACK:
                assert bad not in r.feedback, (r.check_id, bad)
        checks.append({"id": r.check_id, "passed": r.passed, "feedback": r.feedback, "detail": r.detail})
    doc = {"passed": all(r.passed for r in results), "checks": checks}
    assert len({c["id"] for c in checks}) == len(checks)
    path.write_text(json.dumps(doc, indent=2) + "\n")


SCRATCH_ROOT = Path("/workspace/tmp-acceptance")


def pick_scratch_parent() -> Path:
    """Scratch space always lives under /workspace (HK_ACCEPT_TMPDIR, which run.sh sets, must be there too)."""
    env = os.environ.get("HK_ACCEPT_TMPDIR")
    p = Path(env) if env else SCRATCH_ROOT
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InfraError(f"cannot create scratch directory {p}: {exc}") from exc
    if not str(p.resolve()).startswith("/workspace/"):
        raise InfraError(f"scratch directory {p} is not under /workspace")
    return p


def measure_baseline(repo: Path, out: Path) -> int:
    deadline = time.monotonic() + TOTAL_BUDGET_S
    parent = pick_scratch_parent()
    scratch = Path(tempfile.mkdtemp(prefix="hk-baseline-", dir=parent))
    try:
        ctx = Ctx(repo, scratch, deadline)
        ok, log = build_step(ctx)
        if not ok:
            raise InfraError("baseline build failed:\n" + log[-1500:])
        rc, text = runtest_step(ctx)
        if rc != 0:
            raise InfraError("baseline unit tests failed:\n" + text[-1500:])
        cov = gcov_line_coverage(ctx.work)
        doc = {"method": "gcov --json-format line coverage of apps/hk/fsw/src/*.c after native_std.runtest",
               "files": {n: {"covered": c, "lines": t} for n, (c, t) in sorted(cov.items())}}
        out.write_text(json.dumps(doc, indent=2) + "\n")
        print(json.dumps(doc, indent=2))
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "--measure-baseline":
        return measure_baseline(Path(argv[2]).resolve(), Path(argv[3]).resolve())
    if len(argv) != 3:
        print("usage: check.py <repo-path> <out.json>", file=sys.stderr)
        return 2
    repo = Path(argv[1]).resolve()
    out = Path(argv[2]).resolve()
    for need in (BASELINE_FILE, HIDDEN_TEST):
        if not need.is_file():
            print(f"pack file missing: {need}", file=sys.stderr)
            return 2
    if not repo.is_dir():
        print(f"repository {repo} not found", file=sys.stderr)
        return 2
    for tool in ("make", "cmake", "gcov", "gcc"):
        if shutil.which(tool) is None:
            print(f"required tool not found: {tool}", file=sys.stderr)
            return 2
    start = time.monotonic()
    deadline = start + TOTAL_BUDGET_S - BUDGET_RESERVE_S
    scratch = Path(tempfile.mkdtemp(prefix="hk-accept-", dir=pick_scratch_parent()))
    try:
        ctx = Ctx(repo, scratch, deadline)
        results = evaluate(ctx)
        write_out(out, results)
        return 0 if all(r.passed for r in results) else 1
    except InfraError as exc:
        print(f"infrastructure error: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - anything unexpected is a pack problem, not an agent failure
        traceback.print_exc()
        return 2
    finally:
        if not os.environ.get("HK_ACCEPT_KEEP"):
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
