#!/usr/bin/env python3
"""Acceptance checks for Task 2 (SAMPLE_APP status telemetry and configurable rate).

Usage:
    check.py <repo-path> <out.json>
    check.py --baseline <repo-path> <baseline.json>   (measure per-file line coverage)

Exit status: 0 every check passed, 1 at least one failed, 2 infrastructure
error (the pack itself could not run; the agent is not at fault).

The repository is never modified: everything is built and run in scratch
copies. Network activity (UDP 1234 / 2234) and /dev/shm are isolated by run.sh
in private namespaces when the host permits it.
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
import traceback
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PACK = Path(__file__).resolve().parent
HIDDEN = PACK / "hidden_tests"
BASELINE_FILE = PACK / "baseline_coverage.json"

TOTAL_BUDGET_S = 19 * 60  # the harness allows 20 minutes
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Wire facts (this mission: 8-byte command header, 16-byte telemetry header).
CI_LAB_PORT = 1234
TO_LAB_PORT = 2234
HOST = "127.0.0.1"
TLM_HDR = 16
TO_LAB_CMD_MID = 0x1880
TO_LAB_OUTPUT_ENABLE_CC = 6
SAMPLE_CMD_MID = 0x1882
SAMPLE_SEND_HK_MID = 0x1883
SAMPLE_HK_TLM_MID = 0x0883
SAMPLE_STATUS_TLM_MID = 0x088F
EVS_LONG_EVENT_MID = 0x0808
CC_NOOP, CC_RESET, CC_PROCESS, CC_DISPLAY, CC_SET_RATE = 0, 1, 2, 3, 4
EVT_INFORMATION, EVT_ERROR = 2, 3
STATUS_PAYLOAD = struct.Struct("<IIHH")  # TableCrc, ProcessCmdCount, StatusDivisor, HkCycleCount
EVS_APP_NAME_LEN = 20
EVS_MESSAGE_OFFSET = 32


class InfraError(Exception):
    """The pack cannot run (missing tool, host problem); not the agent's fault."""


class Fail(Exception):
    """A check did not hold. args[0] is the experimenter detail."""


def short(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------------------
# Feedback (specification terms; never names the pack, its files, or the runner)
# ---------------------------------------------------------------------------

FB = {
    "build-no-warnings": (
        "The mission must configure and build with `make native_std.prep && make -j native_std.install` "
        "with no compiler warnings (the mission compiles with -Werror). The build failed or printed warnings."),
    "unit-tests-pass": (
        "`make -j native_std.runtest` must pass: every sample_app coverage test segment must complete with "
        "zero failures. At least one test failed, crashed, or did not run."),
    "coverage-not-below-baseline": (
        "Line coverage of each sample_app source file must not fall below its value before this change. "
        "Every new or changed code path needs a coverage test in apps/sample_app/unit-test/coveragetest/."),
    "hidden-coverage-test": (
        "A coverage test compiled against the specified names (SAMPLE_APP_SetStatusRateCmd_t, "
        "SAMPLE_APP_SET_STATUS_RATE_CC, SAMPLE_APP_STATUS_RATE_INF_EID, SAMPLE_APP_STATUS_RATE_ERR_EID, "
        "SAMPLE_APP_StatusTlm_t, SAMPLE_APP_PLATFORM_DEFAULT_STATUS_DIVISOR) found a behaviour that differs "
        "from the specification: divisor validation (1 to 100), the emission rule (HkCycleCount incremented "
        "first, packet after housekeeping when it is a multiple of the current divisor), RESET_COUNTERS "
        "(clears ProcessCmdCount and HkCycleCount, keeps the divisor), or ProcessCmdCount (only successful "
        "PROCESS commands)."),
    "to-lab-subscription": (
        "SAMPLE_APP_STATUS_TLM_MID must be in the TO_LAB subscription table "
        "sample_defs/tables/to_lab_sub.c so the status packet reaches the ground."),
    "status-packet-default-divisor": (
        "With the default divisor 2, SAMPLE_APP must send the status packet (SAMPLE_APP_STATUS_TLM_MID, "
        "0x088F) through TO_LAB after the housekeeping packet on every second SEND_HK (HkCycleCount 2, 4, 6 "
        "and so on) and never on the other cycles, with StatusDivisor 2 in the payload."),
    "set-rate-valid": (
        "SET_STATUS_RATE (function code 4, payload Divisor then Spare, both uint16) with a divisor from 1 to "
        "100 must store the divisor, increment CommandCounter, and send event 13 (INFORMATION) whose text "
        "contains the new divisor. The status packet must then follow the new divisor."),
    "set-rate-invalid": (
        "SET_STATUS_RATE with a divisor outside 1 to 100 (0 or 101) must leave the divisor unchanged, "
        "increment CommandErrorCounter, and send event 14 (ERROR) whose text contains the rejected value. "
        "A command of the wrong length must be rejected by the existing length check "
        "(SAMPLE_APP_CMD_LEN_ERR_EID) and not change the divisor."),
    "reset-counters": (
        "RESET_COUNTERS must reset ProcessCmdCount and HkCycleCount to 0 in addition to the existing "
        "counters, and must not change the divisor. The next housekeeping cycle then counts from 1."),
    "process-count": (
        "ProcessCmdCount must increase by one for each PROCESS command that completes successfully "
        "(returns CFE_SUCCESS) and must not change for a PROCESS command rejected by the length check."),
    "ccdd-definitions": (
        "Add command and telemetry definitions for SET_STATUS_RATE (function code 4) and for the status "
        "packet SAMPLE_APP_STATUS_TLM_MID (message ID 0x088F, with TableCrc, ProcessCmdCount, StatusDivisor "
        "and HkCycleCount) under ctf/ccdd/json/ following ctf/ccdd/README.md, including the message ID list."),
    "ctf-test-passes": (
        "ctf/tests/sample_app_status_rate.json must exist and pass with ctf/run_ctf.sh: it sets divisor 1, "
        "confirms a status packet arrives with StatusDivisor 1, then sends divisor 0 and confirms "
        "CommandErrorCounter increments."),
    "report-present": (
        "docs/reports/sample_app_status_rate.md must exist and state what changed (files and interfaces), "
        "the unit test results (segment and assertion counts), the sample_app coverage figures, the CTF "
        "results, and any open issues."),
}
NEEDS_BUILD = (
    " This could not be verified because the mission does not build and install; fix the build first.")
NEEDS_CFS = (
    " cFS did not start with the installed build, or no SAMPLE_APP housekeeping telemetry reached the ground "
    "through TO_LAB after output was enabled.")

CHECK_ORDER = list(FB)


@dataclass
class Result:
    check_id: str
    passed: bool
    feedback: str
    detail: str = ""


@dataclass
class Budget:
    deadline: float = field(default_factory=lambda: time.monotonic() + TOTAL_BUDGET_S)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def cap(self, wanted: float) -> float:
        """Timeout for a step: the wanted time, bounded by what is left."""
        return max(20.0, min(wanted, self.remaining() - 15.0))


# ---------------------------------------------------------------------------
# Process and filesystem helpers
# ---------------------------------------------------------------------------

def run_cmd(args: list[str], cwd: Path, timeout: float, env: dict[str, str] | None = None) -> tuple[int, str, bool]:
    """Run a command; return (exit status, combined output, timed_out)."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.Popen(args, cwd=cwd, env=merged, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        return -9, out or "", True


def scratch_root() -> Path:
    for cand in (os.environ.get("ACCEPT_SCRATCH"), "/workspace/tmp-acceptance"):
        if not cand:
            continue
        try:
            Path(cand).mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(cand, os.W_OK):
            return Path(cand)
    if os.environ.get("ACCEPT_ALLOW_SYSTEM_TMP") == "1" and os.access(tempfile.gettempdir(), os.W_OK):
        return Path(tempfile.gettempdir())
    raise InfraError("no writable scratch directory")


COPY_SKIP_TOP = {".git", "build-native_std"}


def copy_tree(repo: Path, dest: Path, with_ctf_env: bool) -> None:
    """Copy the repository without git data, build output, or CTF results."""
    assert not dest.exists()

    def ignore(directory: str, names: list[str]) -> set[str]:
        here = Path(directory)
        skip: set[str] = {n for n in names if n == "__pycache__"}
        if here == repo:
            skip |= COPY_SKIP_TOP & set(names)
        if here == repo / "ctf":
            skip |= {"results"} & set(names)
            if not with_ctf_env:
                skip |= {"CTF", "venv"} & set(names)
        return skip

    try:
        shutil.copytree(repo, dest, symlinks=True, ignore=ignore)
    except (OSError, shutil.Error) as exc:
        raise InfraError(f"cannot copy {repo} to scratch: {exc}") from exc


def isolation_note() -> str:
    if os.environ.get("ACCEPT_ISOLATED") != "1":
        return "not isolated (ports 1234/2234 and /dev/shm are shared with the host)"
    return "isolated network, mount and process namespaces"


# ---------------------------------------------------------------------------
# Build, unit tests, coverage
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    root: Path
    build_ok: bool = False
    build_detail: str = ""
    warnings: list[str] = field(default_factory=list)
    install_dir: Path | None = None


def build(ws: Workspace, budget: Budget) -> None:
    """make native_std.prep and native_std.install; fills ws.build_ok/warnings."""
    out_all = ""
    for target, cmd, limit in (("prep", ["make", "native_std.prep"], 240),
                               ("install", ["make", "-j16", "native_std.install"], 420)):
        rc, out, timed_out = run_cmd(cmd, ws.root, budget.cap(limit))
        out_all += out
        ws.warnings += [ln for ln in out.splitlines() if "warning:" in ln]
        if rc != 0 or timed_out:
            ws.build_detail = f"make native_std.{target} {'timed out' if timed_out else f'exit {rc}'}\n{tail(out, 40)}"
            return
    exe = ws.root / "build-native_std" / "exe" / "cpu1"
    if not (exe / "core-cpu1").is_file():
        ws.build_detail = f"{exe}/core-cpu1 missing after install"
        return
    ws.install_dir = exe
    ws.build_ok = not ws.warnings
    ws.build_detail = (f"{len(ws.warnings)} warning line(s):\n" + "\n".join(ws.warnings[:15])) if ws.warnings else "ok"


SUMMARY_RE = re.compile(r"SUMMARY\s+TOTAL::(\d+)\s+PASS::(\d+)\s+FAIL::(\d+)\s+MIR::(\d+)\s+TSF::(\d+)\s+TTF::(\d+)")


def parse_ut_log(text: str) -> tuple[int, int, int] | None:
    """Return (total, bad, mir) from a ut_assert log's final SUMMARY line, or None."""
    found = SUMMARY_RE.findall(text)
    if not found:
        return None
    total, _passed, fail, mir, tsf, ttf = (int(x) for x in found[-1])
    return total, fail + tsf + ttf, mir


def run_unit_tests(ws: Workspace, budget: Budget) -> tuple[bool, str]:
    rc, out, timed_out = run_cmd(["make", "-j16", "native_std.runtest"], ws.root, budget.cap(300))
    logs = sorted((ws.root / "build-native_std" / "test-results").glob("coverage-sample_app-*.log"))
    problems: list[str] = []
    if timed_out:
        problems.append("runtest timed out")
    elif rc != 0:
        problems.append(f"runtest exit {rc}")
    if not logs:
        problems.append("no coverage-sample_app logs produced")
    for log in logs:
        parsed = parse_ut_log(log.read_text(errors="replace"))
        if parsed is None:
            problems.append(f"{log.name}: no SUMMARY line")
        elif parsed[1] or parsed[0] == 0:
            problems.append(f"{log.name}: total {parsed[0]}, failing {parsed[1]}")
    detail = "; ".join(problems) if problems else f"{len(logs)} sample_app logs, all clean"
    if problems:
        detail += "\n" + tail(out, 30)
    return not problems, detail


SRC_RE = re.compile(r"apps/sample_app/fsw/src/([^/]+\.c)$")


def measure_coverage(ws: Workspace) -> dict[str, dict[str, int]]:
    """Per-file line coverage of apps/sample_app/fsw/src/*.c from gcov data.

    A line counts as covered when any test executable executed it.
    """
    gcov = shutil.which("gcov")
    if gcov is None:
        raise InfraError("gcov is not installed; cannot measure line coverage")
    covered: dict[str, dict[int, bool]] = {}
    gcdas = sorted((ws.root / "build-native_std").rglob("*.gcda"))
    for gcda in gcdas:
        if "/apps/sample_app/" not in str(gcda):
            continue
        rc, out, _ = run_cmd([gcov, "--json-format", "--stdout", gcda.name], gcda.parent, 120)
        if rc != 0:
            raise InfraError(f"gcov failed on {gcda}: {tail(out, 5)}")
        for doc in re.findall(r"^\{.*\}$", out, flags=re.M):
            for entry in json.loads(doc).get("files", []):
                match = SRC_RE.search(entry["file"])
                if not match:
                    continue
                lines = covered.setdefault(match.group(1), {})
                for ln in entry["lines"]:
                    lines[ln["line_number"]] = lines.get(ln["line_number"], False) or ln["count"] > 0
    return {name: {"lines": len(v), "hit": sum(v.values())} for name, v in sorted(covered.items())}


def check_coverage(ws: Workspace) -> tuple[bool, str]:
    if not BASELINE_FILE.is_file():
        raise InfraError(f"missing {BASELINE_FILE.name}")
    base = json.loads(BASELINE_FILE.read_text())["files"]
    now = measure_coverage(ws)
    problems, rows = [], []
    for name, b in base.items():
        cur = now.get(name)
        if cur is None:
            problems.append(f"{name}: no coverage data (baseline {b['hit']}/{b['lines']})")
            continue
        # cur.hit/cur.lines >= b.hit/b.lines, compared exactly
        if cur["hit"] * b["lines"] < b["hit"] * cur["lines"]:
            problems.append(f"{name}: {cur['hit']}/{cur['lines']} below baseline {b['hit']}/{b['lines']}")
        rows.append(f"{name} {cur['hit']}/{cur['lines']} (baseline {b['hit']}/{b['lines']})")
    extra = sorted(set(now) - set(base))
    rows += [f"{n} {now[n]['hit']}/{now[n]['lines']} (new file)" for n in extra]
    return not problems, "; ".join(problems) if problems else "; ".join(rows)


# ---------------------------------------------------------------------------
# Hidden coverage test
# ---------------------------------------------------------------------------

def run_hidden_coverage(repo: Path, work: Path, budget: Budget) -> tuple[bool, str]:
    tree = work / "hidden-tree"
    copy_tree(repo, tree, with_ctf_env=False)
    unit = tree / "apps" / "sample_app" / "unit-test"
    if not (tree / "apps" / "sample_app" / "CMakeLists.txt").is_file():
        return False, "apps/sample_app/CMakeLists.txt missing"
    shutil.rmtree(unit, ignore_errors=True)
    (unit / "coveragetest").mkdir(parents=True)
    shutil.copy2(HIDDEN / "CMakeLists.txt", unit / "CMakeLists.txt")
    shutil.copy2(HIDDEN / "stub_placeholder.c", unit / "stub_placeholder.c")
    shutil.copy2(HIDDEN / "coveragetest_sample_app_hidden.c", unit / "coveragetest" / "coveragetest_sample_app_hidden.c")

    rc, out, timed_out = run_cmd(["make", "native_std.prep"], tree, budget.cap(240))
    if rc != 0 or timed_out:
        return False, f"prep failed ({'timeout' if timed_out else rc})\n{tail(out, 30)}"
    cpu_dir = tree / "build-native_std" / "native" / "default_cpu1"
    rc, out, timed_out = run_cmd(["make", "-j16", "coverage-sample_app-hidden-testrunner"], cpu_dir, budget.cap(420))
    if rc != 0 or timed_out:
        errors = [ln for ln in out.splitlines() if re.search(r"error:|undefined reference|fatal|\*\*\*", ln)]
        return False, f"hidden test did not compile or link ({'timeout' if timed_out else rc})\n" + "\n".join(errors[:25])
    runner = cpu_dir / "apps" / "sample_app" / "unit-test" / "coverage-sample_app-hidden-testrunner"
    rc, out, timed_out = run_cmd([str(runner)], runner.parent, budget.cap(120))
    parsed = parse_ut_log(out)
    failures = [ln for ln in out.splitlines() if ln.startswith(("[ FAIL]", "[  TSF]", "[  TTF]", "[ABORT]"))]
    ok = rc == 0 and not timed_out and parsed is not None and parsed[1] == 0 and parsed[0] > 0
    return ok, (f"runner exit {rc}{' (timeout)' if timed_out else ''}; summary {parsed}\n" + "\n".join(failures[:30]))


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def check_to_lab(repo: Path) -> None:
    path = repo / "sample_defs" / "tables" / "to_lab_sub.c"
    if not path.is_file():
        raise Fail(f"{path} missing")
    text = re.sub(r"/\*.*?\*/", "", path.read_text(errors="replace"), flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    if not re.search(r"\{\s*CFE_SB_MSGID_WRAP_VALUE\(\s*SAMPLE_APP_STATUS_TLM_MID\s*\)", text):
        raise Fail("SAMPLE_APP_STATUS_TLM_MID has no subscription entry in to_lab_sub.c")


def load_json_dir(directory: Path) -> list[tuple[Path, object]]:
    docs = []
    for path in sorted(directory.glob("*.json")):
        try:
            docs.append((path, json.loads(path.read_text())))
        except (OSError, ValueError) as exc:
            raise Fail(f"{path.name} is not valid JSON: {exc}") from exc
    return docs


def walk(node: object):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def to_int(value: object) -> int | None:
    try:
        return int(str(value), 0)
    except ValueError:
        return None


def check_ccdd(repo: Path) -> None:
    directory = repo / "ctf" / "ccdd" / "json"
    if not directory.is_dir():
        raise Fail("ctf/ccdd/json is missing")
    docs = load_json_dir(directory)
    cc_ok = False
    tlm_fields = set()
    mid_ok = False
    for _path, doc in docs:
        for node in walk(doc):
            if not isinstance(node, dict):
                continue
            if "SET_STATUS_RATE" in str(node.get("cc_name", "")) and to_int(node.get("cc_value")) == CC_SET_RATE:
                names = {n.get("name") for n in walk(node) if isinstance(n, dict)}
                cc_ok = {"Divisor", "Spare"} <= names
            if node.get("tlm_mid_name") == "SAMPLE_APP_STATUS_TLM_MID":
                tlm_fields = {n.get("name") for n in walk(node) if isinstance(n, dict)}
            if node.get("mid_name") == "SAMPLE_APP_STATUS_TLM_MID" and to_int(node.get("mid_value")) == SAMPLE_STATUS_TLM_MID:
                mid_ok = True
    missing = []
    if not cc_ok:
        missing.append("no command with cc_name containing SET_STATUS_RATE, cc_value 4, and Divisor/Spare parameters")
    need = {"TableCrc", "ProcessCmdCount", "StatusDivisor", "HkCycleCount"}
    if not need <= tlm_fields:
        missing.append(f"no telemetry definition for SAMPLE_APP_STATUS_TLM_MID with fields {sorted(need)}")
    if not mid_ok:
        missing.append("SAMPLE_APP_STATUS_TLM_MID with value 0x088F is not in the MID list")
    if missing:
        raise Fail("; ".join(missing))


def check_report(repo: Path) -> None:
    path = repo / "docs" / "reports" / "sample_app_status_rate.md"
    if not path.is_file():
        raise Fail("docs/reports/sample_app_status_rate.md missing")
    text = path.read_text(errors="replace")
    low = text.lower()
    wanted: dict[str, bool] = {
        "unit tests": bool(re.search(r"unit[ -]?tests?", low)) and bool(re.search(r"segment|assertion|pass", low)),
        "coverage": "coverage" in low and bool(re.search(r"\d+(\.\d+)?\s*%", text)),
        "CTF": bool(re.search(r"\bctf\b", low)),
        "changes": "sample_app" in low and bool(re.search(r"changed|changes|interface|files", low)),
        "open issues": bool(re.search(r"open issue|known issue|issues|limitation|none", low)),
    }
    missing = [k for k, ok in wanted.items() if not ok]
    if len(text.strip()) < 200:
        missing.append("content (report is nearly empty)")
    if missing:
        raise Fail("report lacks: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Running cFS and driving it over UDP
# ---------------------------------------------------------------------------

def build_command(mid: int, cc: int, payload: bytes = b"") -> bytes:
    total = 8 + len(payload)
    pkt = bytearray(struct.pack(">HHH", mid, 0xC000, total - 7))
    pkt += bytes([cc, 0]) + payload
    checksum = 0xFF
    for b in pkt:
        checksum ^= b
    pkt[7] = checksum
    return bytes(pkt)


@dataclass(frozen=True)
class Packet:
    t: float
    mid: int
    raw: bytes

    @property
    def payload(self) -> bytes:
        return self.raw[TLM_HDR:]


@dataclass(frozen=True)
class Status:
    t: float
    length: int
    table_crc: int
    process_count: int
    divisor: int
    cycle: int


@dataclass(frozen=True)
class Hk:
    t: float
    cmd: int
    err: int


@dataclass(frozen=True)
class Event:
    t: float
    app: str
    event_id: int
    event_type: int
    text: str


class Ground:
    """UDP ground station: commands to CI_LAB, a background receiver for TO_LAB."""

    def __init__(self) -> None:
        self.packets: list[Packet] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
        try:
            self._rx.bind((HOST, TO_LAB_PORT))
        except OSError as exc:
            raise InfraError(f"cannot listen on UDP {TO_LAB_PORT}: {exc}") from exc
        self._rx.settimeout(0.2)
        self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._rx.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            if len(data) >= 6:
                mid = struct.unpack(">H", data[:2])[0]
                with self._lock:
                    self.packets.append(Packet(time.monotonic(), mid, data))

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._rx.close()
        self._tx.close()

    def send(self, mid: int, cc: int, payload: bytes = b"") -> None:
        self._tx.sendto(build_command(mid, cc, payload), (HOST, CI_LAB_PORT))

    def enable_to(self) -> None:
        self.send(TO_LAB_CMD_MID, TO_LAB_OUTPUT_ENABLE_CC, b"127.0.0.1".ljust(16, b"\0"))

    def send_hk(self) -> None:
        self.send(SAMPLE_SEND_HK_MID, 0)

    def rate(self, divisor: int, extra: bytes = b"") -> None:
        self.send(SAMPLE_CMD_MID, CC_SET_RATE, struct.pack("<HH", divisor, 0) + extra)

    def snapshot(self) -> list[Packet]:
        with self._lock:
            return list(self.packets)

    # -- decoding ------------------------------------------------------------
    def hks(self, since: float) -> list[Hk]:
        return [Hk(p.t, p.payload[0], p.payload[1]) for p in self.snapshot()
                if p.mid == SAMPLE_HK_TLM_MID and p.t > since and len(p.payload) >= 2]

    def statuses(self, since: float) -> list[Status]:
        out = []
        for p in self.snapshot():
            if p.mid == SAMPLE_STATUS_TLM_MID and p.t > since:
                if len(p.payload) < STATUS_PAYLOAD.size:
                    raise Fail(f"status packet payload is {len(p.payload)} bytes, expected {STATUS_PAYLOAD.size}")
                crc, proc, div, cyc = STATUS_PAYLOAD.unpack_from(p.payload)
                out.append(Status(p.t, len(p.raw), crc, proc, div, cyc))
        return out

    def events(self, since: float) -> list[Event]:
        out = []
        for p in self.snapshot():
            if p.mid != EVS_LONG_EVENT_MID or p.t <= since or len(p.payload) <= EVS_MESSAGE_OFFSET:
                continue
            body = p.payload
            app = body[:EVS_APP_NAME_LEN].split(b"\0")[0].decode("ascii", "replace")
            eid, etype = struct.unpack_from("<HH", body, EVS_APP_NAME_LEN)
            text = body[EVS_MESSAGE_OFFSET:].split(b"\0")[0].decode("ascii", "replace")
            out.append(Event(p.t, app, eid, etype, text))
        return out

    def order_since(self, since: float) -> list[str]:
        """Arrival order of housekeeping ('H') and status ('S') packets."""
        return ["H" if p.mid == SAMPLE_HK_TLM_MID else "S" for p in self.snapshot()
                if p.t > since and p.mid in (SAMPLE_HK_TLM_MID, SAMPLE_STATUS_TLM_MID)]


class Cfs:
    """core-cpu1 started from an installed build directory."""

    MARKER = "CFE_ES_Main entering OPERATIONAL state"

    def __init__(self, exe_dir: Path, log: Path) -> None:
        self.exe_dir, self.log = exe_dir, log
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self, timeout: float = 40.0) -> None:
        # A cold boot with an empty RAM disk, as CTF does.
        shutil.rmtree("/dev/shm/osal:RAM", ignore_errors=True)
        with self.log.open("wb") as fh:
            self.proc = subprocess.Popen(["./core-cpu1", "--reset", "PO"], cwd=self.exe_dir, stdout=fh,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         start_new_session=True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.MARKER in self.log.read_text(errors="replace"):
                return
            if self.proc.poll() is not None:
                raise Fail(f"core-cpu1 exited with {self.proc.returncode} before OPERATIONAL\n{tail(self.log.read_text(errors='replace'), 30)}")
            time.sleep(0.2)
        raise Fail(f"no OPERATIONAL marker within {timeout} s\n{tail(self.log.read_text(errors='replace'), 30)}")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
        shutil.rmtree("/dev/shm/osal:RAM", ignore_errors=True)


class Disturbed(Exception):
    """A scenario window saw telemetry it did not cause (a scheduler housekeeping request); retry."""


def sync(g: Ground, timeout: float = 8.0) -> tuple[float, Hk]:
    """Wait for a housekeeping packet that the scheduler (not this pack) requested.

    SCH_LAB sends SEND_HK every 5.4 s, so the 5 s after this call are free of
    scheduler housekeeping. Returns the start of the scenario window and the packet's counters.
    """
    mark = time.monotonic()
    end = mark + timeout
    while time.monotonic() < end:
        hks = g.hks(mark)
        if hks:
            # Let the status packet that may follow this housekeeping packet arrive, so it is
            # not counted in the scenario's window.
            time.sleep(0.4)
            return time.monotonic(), hks[-1]
        time.sleep(0.05)
    raise Fail("no SAMPLE_APP housekeeping packet within "
               f"{timeout:.0f} s (scheduler housekeeping request expected every 5.4 s)")


def inject_hk(g: Ground, count: int, gap: float = 0.5) -> None:
    for i in range(count):
        g.send_hk()
        time.sleep(gap if i + 1 < count else 0.9)


def require_window(g: Ground, t0: float, hk_expected: int) -> list[Hk]:
    hks = g.hks(t0)
    if len(hks) != hk_expected:
        raise Disturbed(f"{len(hks)} housekeeping packets in the window, expected {hk_expected}")
    return hks


def consecutive(cycles: list[int], step: int) -> bool:
    return all(b - a == step for a, b in zip(cycles, cycles[1:]))


def scenario_default_divisor(g: Ground) -> None:
    t0, _ = sync(g)
    inject_hk(g, 4)
    require_window(g, t0, 4)
    sts = g.statuses(t0)
    cycles = [s.cycle for s in sts]
    if len(sts) != 2:
        raise Fail(f"{len(sts)} status packets for 4 housekeeping cycles at the default divisor, expected 2 "
                   f"(HkCycleCount values {cycles})")
    if any(c % 2 for c in cycles) or not consecutive(cycles, 2):
        raise Fail(f"status packets on HkCycleCount {cycles}, expected consecutive even values")
    if any(s.divisor != 2 for s in sts):
        raise Fail(f"StatusDivisor values {[s.divisor for s in sts]}, expected 2")
    if any(s.length != TLM_HDR + STATUS_PAYLOAD.size for s in sts):
        raise Fail(f"status packet lengths {[s.length for s in sts]}, expected {TLM_HDR + STATUS_PAYLOAD.size}")
    order = "".join(g.order_since(t0))
    if "SS" in order or order.startswith("S"):
        raise Fail(f"status packet not directly after a housekeeping packet (arrival order {order})")


def scenario_set_rate_valid(g: Ground) -> None:
    t0, base = sync(g)
    g.rate(100)
    time.sleep(0.4)
    g.rate(1)
    time.sleep(0.4)
    inject_hk(g, 2)
    hks = require_window(g, t0, 2)
    events = [e for e in g.events(t0) if e.app == "SAMPLE_APP" and e.event_id == 13]
    if len(events) != 2:
        raise Fail(f"{len(events)} event 13 messages from SAMPLE_APP for two accepted commands, expected 2")
    if any(e.event_type != EVT_INFORMATION for e in events):
        raise Fail(f"event 13 types {[e.event_type for e in events]}, expected INFORMATION ({EVT_INFORMATION})")
    for event, value in zip(events, ("100", "1")):
        if not re.search(rf"(?<!\d){value}(?!\d)", event.text):
            raise Fail(f"event 13 text {event.text!r} does not contain the divisor {value}")
    if (hks[-1].cmd - base.cmd) % 256 != 2:
        raise Fail(f"CommandCounter went from {base.cmd} to {hks[-1].cmd}, expected +2")
    if hks[-1].err != base.err:
        raise Fail(f"CommandErrorCounter went from {base.err} to {hks[-1].err}, expected no change")
    sts = g.statuses(t0)
    if len(sts) != 2 or any(s.divisor != 1 for s in sts) or not consecutive([s.cycle for s in sts], 1):
        raise Fail(f"after divisor 1, status packets {[(s.cycle, s.divisor) for s in sts]}, "
                   "expected two packets on consecutive cycles with StatusDivisor 1")


def scenario_set_rate_invalid(g: Ground) -> None:
    t0, base = sync(g)
    g.rate(3)
    time.sleep(0.3)
    g.rate(0)
    time.sleep(0.3)
    g.rate(101)
    time.sleep(0.3)
    g.rate(7, extra=b"\x00")  # one byte too long: the length check applies
    time.sleep(0.3)
    inject_hk(g, 3)
    hks = require_window(g, t0, 3)
    events = g.events(t0)
    errs = [e for e in events if e.app == "SAMPLE_APP" and e.event_id == 14]
    if len(errs) != 2:
        raise Fail(f"{len(errs)} event 14 messages for two out-of-range divisors, expected 2")
    if any(e.event_type != EVT_ERROR for e in errs):
        raise Fail(f"event 14 types {[e.event_type for e in errs]}, expected ERROR ({EVT_ERROR})")
    for event, value in zip(errs, ("0", "101")):
        if not re.search(rf"(?<!\d){value}(?!\d)", event.text):
            raise Fail(f"event 14 text {event.text!r} does not contain the rejected value {value}")
    if not any(e.app == "SAMPLE_APP" and e.event_id == 6 for e in events):
        raise Fail("no length-error event (6) for the SET_STATUS_RATE command of the wrong length")
    if (hks[-1].err - base.err) % 256 != 3:
        raise Fail(f"CommandErrorCounter went from {base.err} to {hks[-1].err}, expected +3 "
                   "(two rejected divisors and one wrong length)")
    if (hks[-1].cmd - base.cmd) % 256 != 1:
        raise Fail(f"CommandCounter went from {base.cmd} to {hks[-1].cmd}, expected +1 (the valid divisor 3)")
    sts = g.statuses(t0)
    if len(sts) != 1 or sts[0].divisor != 3 or sts[0].cycle % 3:
        raise Fail(f"after rejected commands, status packets {[(s.cycle, s.divisor) for s in sts]}, "
                   "expected exactly one packet with StatusDivisor 3 on a multiple of 3")


def scenario_process_count(g: Ground) -> None:
    t0, _ = sync(g)
    g.rate(1)
    time.sleep(0.3)
    inject_hk(g, 1, gap=0.3)
    first = g.statuses(t0)
    if len(first) != 1:
        raise Fail(f"{len(first)} status packets after setting divisor 1, expected 1")
    g.send(SAMPLE_CMD_MID, CC_PROCESS)
    time.sleep(0.3)
    g.send(SAMPLE_CMD_MID, CC_PROCESS)
    time.sleep(0.3)
    g.send(SAMPLE_CMD_MID, CC_PROCESS, b"\x00")  # wrong length: not counted
    time.sleep(0.3)
    mark = time.monotonic()
    inject_hk(g, 1, gap=0.3)
    require_window(g, t0, 2)
    second = g.statuses(mark)
    if len(second) != 1:
        raise Fail(f"{len(second)} status packets for the second cycle, expected 1")
    a, b = first[0], second[0]
    if b.cycle - a.cycle != 1:
        raise Disturbed(f"HkCycleCount {a.cycle} then {b.cycle}")
    if b.process_count - a.process_count != 2:
        raise Fail(f"ProcessCmdCount went from {a.process_count} to {b.process_count} after two PROCESS commands "
                   "and one of the wrong length, expected +2")


def scenario_reset(g: Ground) -> None:
    t0, _ = sync(g)
    g.rate(1)
    time.sleep(0.3)
    g.send(SAMPLE_CMD_MID, CC_PROCESS)
    time.sleep(0.3)
    g.send(SAMPLE_CMD_MID, CC_PROCESS)
    time.sleep(0.3)
    inject_hk(g, 1, gap=0.3)
    before = g.statuses(t0)
    if len(before) != 1 or before[0].process_count < 2:
        raise Fail(f"before reset, status packets {[(s.cycle, s.process_count) for s in before]}: "
                   "expected one packet with ProcessCmdCount of at least 2, so the reset cannot be verified")
    mark = time.monotonic()
    g.send(SAMPLE_CMD_MID, CC_RESET)
    time.sleep(0.4)
    inject_hk(g, 2)
    require_window(g, t0, 3)
    after = g.statuses(mark)
    if len(after) != 2:
        raise Fail(f"{len(after)} status packets in the two cycles after reset at divisor 1, expected 2")
    if [s.cycle for s in after] != [1, 2]:
        raise Fail(f"HkCycleCount after reset is {[s.cycle for s in after]}, expected [1, 2]")
    if any(s.process_count != 0 for s in after):
        raise Fail(f"ProcessCmdCount after reset is {[s.process_count for s in after]}, expected 0")
    if any(s.divisor != 1 for s in after):
        raise Fail(f"StatusDivisor after reset is {[s.divisor for s in after]}, expected 1 (unchanged)")
    last = g.hks(mark)[-1]
    if (last.cmd, last.err) != (0, 0):
        raise Fail(f"housekeeping counters after reset are ({last.cmd}, {last.err}), expected (0, 0)")


SCENARIOS: list[tuple[str, Callable[[Ground], None]]] = [
    ("status-packet-default-divisor", scenario_default_divisor),  # must run first: needs the boot-time divisor
    ("set-rate-valid", scenario_set_rate_valid),
    ("set-rate-invalid", scenario_set_rate_invalid),
    ("process-count", scenario_process_count),
    ("reset-counters", scenario_reset),
]


def run_scenario(g: Ground, fn: Callable[[Ground], None], budget: Budget) -> str:
    """Run a scenario, retrying when scheduler telemetry disturbed its window."""
    notes = []
    for attempt in range(1, 4):
        if budget.remaining() < 40:
            raise Fail("time budget exhausted before the scenario could run")
        try:
            fn(g)
            return f"passed on attempt {attempt}" + (f" (earlier: {'; '.join(notes)})" if notes else "")
        except Disturbed as exc:
            notes.append(str(exc))
    raise Fail("telemetry window disturbed on every attempt: " + "; ".join(notes))


def run_network_checks(ws: Workspace, work: Path, budget: Budget, results: dict[str, Result]) -> None:
    pending = [cid for cid, _ in SCENARIOS]

    def fail_all(feedback_suffix: str, detail: str) -> None:
        for cid in pending:
            results[cid] = Result(cid, False, FB[cid] + feedback_suffix, detail)

    if not ws.build_ok and ws.install_dir is None:
        fail_all(NEEDS_BUILD, ws.build_detail)
        return
    assert ws.install_dir is not None
    if os.environ.get("ACCEPT_ISOLATED") != "1" and not port_free(CI_LAB_PORT):
        raise InfraError(f"UDP {CI_LAB_PORT} is in use on the host and namespaces are unavailable")
    ground = Ground()
    cfs = Cfs(ws.install_dir, work / "cfs.log")
    try:
        try:
            cfs.start()
        except Fail as exc:
            fail_all(NEEDS_CFS, str(exc))
            return
        # CI_LAB needs a moment after OPERATIONAL; repeat the enable until telemetry flows.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not ground.snapshot():
            ground.enable_to()
            time.sleep(0.5)
        if not ground.snapshot():
            fail_all(NEEDS_CFS, "no telemetry within 20 s of enabling TO_LAB output\n"
                     + tail(cfs.log.read_text(errors="replace"), 25))
            return
        for cid, fn in SCENARIOS:
            try:
                detail = run_scenario(ground, fn, budget)
                results[cid] = Result(cid, True, "", detail)
            except Fail as exc:
                results[cid] = Result(cid, False, FB[cid], str(exc))
    finally:
        cfs.stop()
        ground.close()


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------
# CTF
# ---------------------------------------------------------------------------

def check_ctf_content(test_path: Path) -> None:
    """The script must do what the specification names, not merely exit 0."""
    try:
        doc = json.loads(test_path.read_text())
    except ValueError as exc:
        raise Fail(f"test script is not valid JSON: {exc}") from exc
    nodes = [n for n in walk(doc) if isinstance(n, dict)]
    sets = [n.get("args", {}) for n in nodes if n.get("cc") in ("SAMPLE_APP_SET_STATUS_RATE_CC", 4, "4")]
    divisors = {a.get("Payload", {}).get("Divisor") for a in sets if isinstance(a, dict)}
    if not {1, 0} <= {d for d in divisors if isinstance(d, int)}:
        raise Fail(f"script sends SET_STATUS_RATE with divisors {sorted(map(str, divisors))}, needs 1 and 0")
    checks = [n for n in nodes if n.get("instruction") == "CheckTlmValue"]
    status_check = any(n.get("data", {}).get("mid") == "SAMPLE_APP_STATUS_TLM_MID"
                       and "StatusDivisor" in json.dumps(n) for n in checks)
    error_check = any("CommandErrorCounter" in json.dumps(n) for n in checks)
    if not status_check:
        raise Fail("script has no CheckTlmValue on StatusDivisor of SAMPLE_APP_STATUS_TLM_MID")
    if not error_check:
        raise Fail("script has no CheckTlmValue on CommandErrorCounter")


def run_ctf(ws: Workspace, work: Path, budget: Budget) -> str:
    root = ws.root
    script = root / "ctf" / "run_ctf.sh"
    test = root / "ctf" / "tests" / "sample_app_status_rate.json"
    if not script.is_file():
        raise Fail("ctf/run_ctf.sh is missing")
    if not test.is_file():
        raise Fail("ctf/tests/sample_app_status_rate.json is missing")
    check_ctf_content(test)
    if not ws.build_ok and ws.install_dir is None:
        raise Fail("build missing; cannot run CTF")
    if not ((root / "ctf" / "venv" / "bin" / "python").exists() and (root / "ctf" / "CTF" / "ctf").exists()):
        # The harness runs ctf/setup_ctf.sh before the agent starts; a repository without it
        # is a fault of the run environment, not of the agent.
        raise Fail("ctf/CTF or ctf/venv missing in the repository copy (the CTF environment is set up before the agent starts)")
    env = {"CFS_ROOT": str(root), "CTF_RESULTS_DIR": str(work / "ctf-results")}
    rc, out, timed_out = run_cmd(["bash", str(script), "ctf/tests/sample_app_status_rate.json"], root,
                                 budget.cap(240), env=env)
    # The runner exits 2 for refusals unrelated to the test (port busy, CTF not set up).
    if rc == 2 and re.search(r"already in use|not set up", out):
        raise InfraError(f"ctf/run_ctf.sh refused to start: {tail(out, 5)}")
    if timed_out:
        raise Fail(f"CTF run timed out\n{tail(out, 30)}")
    if rc != 0:
        raise Fail(f"run_ctf.sh exit {rc}\n{tail(out, 40)}")
    return tail(out, 8)


def stop_stray_cfs(root: Path) -> None:
    """Stop any core-cpu1 whose working directory is under the scratch tree."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "core-cpu1":
                continue
            if str((entry / "cwd").resolve()).startswith(str(root)):
                os.kill(int(entry.name), signal.SIGKILL)
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def write_results(out_path: Path, results: dict[str, Result]) -> int:
    checks = []
    for cid in CHECK_ORDER:
        r = results.get(cid)
        assert r is not None, f"no result for {cid}"
        assert ID_RE.match(cid)
        assert r.passed or r.feedback.strip(), f"failing check {cid} without feedback"
        checks.append({"id": cid, "passed": r.passed, "feedback": "" if r.passed else r.feedback,
                       "detail": short(r.detail, 4000)})
    passed = all(c["passed"] for c in checks)
    out_path.write_text(json.dumps({"passed": passed, "checks": checks}, indent=2) + "\n")
    return 0 if passed else 1


def guarded(cid: str, fn: Callable[[], str | None], results: dict[str, Result], needs: str = "") -> None:
    """Run one check body; Fail becomes a failed result, InfraError propagates."""
    try:
        detail = fn()
        results[cid] = Result(cid, True, "", detail or "")
    except Fail as exc:
        results[cid] = Result(cid, False, FB[cid] + needs, str(exc))
    except Exception as exc:  # noqa: BLE001
        results[cid] = Result(cid, False, FB[cid] + needs, "unexpected error while checking:\n" + "".join(traceback.format_exception(exc)))


def measure_baseline(repo: Path, out_path: Path) -> int:
    budget = Budget()
    work = Path(tempfile.mkdtemp(prefix="task2-baseline-", dir=scratch_root()))
    try:
        tree = work / "tree"
        copy_tree(repo, tree, with_ctf_env=False)
        ws = Workspace(tree)
        build(ws, budget)
        if ws.install_dir is None:
            raise InfraError(f"baseline build failed: {ws.build_detail}")
        ok, detail = run_unit_tests(ws, budget)
        if not ok:
            raise InfraError(f"baseline unit tests failed: {detail}")
        files = measure_coverage(ws)
        out_path.write_text(json.dumps({
            "method": "gcov --json-format over the sample_app coverage test objects; a line is covered when "
                      "any test executed it",
            "files": files}, indent=2) + "\n")
        print(json.dumps(files, indent=2))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "--baseline":
        return measure_baseline(Path(argv[2]).resolve(), Path(argv[3]).resolve())
    if len(argv) != 3:
        print("usage: check.py <repo-path> <out.json>", file=sys.stderr)
        return 2
    repo, out_path = Path(argv[1]).resolve(), Path(argv[2]).resolve()
    if not repo.is_dir():
        print(f"repo {repo} not found", file=sys.stderr)
        return 2
    for needed in (HIDDEN / "coveragetest_sample_app_hidden.c", HIDDEN / "CMakeLists.txt", BASELINE_FILE):
        if not needed.is_file():
            print(f"pack file {needed.name} missing", file=sys.stderr)
            return 2

    # Pre-flight: everything that can be judged before the agent's code is touched.
    if shutil.which("gcov") is None:
        print("infrastructure error: gcov is not installed", file=sys.stderr)
        return 2
    budget = Budget()
    results: dict[str, Result] = {}
    try:
        work = Path(tempfile.mkdtemp(prefix="task2-accept-", dir=scratch_root()))
    except (InfraError, OSError) as exc:
        print(f"infrastructure error: {exc}", file=sys.stderr)
        return 2
    notes = isolation_note()
    state: dict[str, Workspace] = {}

    def failing(ids: list[str], exc: BaseException, feedback_suffix: str = "") -> None:
        trace = "".join(traceback.format_exception(exc))
        for cid in ids:
            if cid not in results:
                results[cid] = Result(cid, False, FB[cid] + feedback_suffix, "unexpected error while checking:\n" + trace)

    def step(ids: list[str], fn: Callable[[], None]) -> None:
        """Run a step; an unexpected exception fails the checks it was to produce."""
        try:
            fn()
        except Fail as exc:
            for cid in ids:
                if cid not in results:
                    results[cid] = Result(cid, False, FB[cid], str(exc))
        except Exception as exc:  # noqa: BLE001 - every error after pre-flight is a check failure
            failing(ids, exc)

    def do_static() -> None:
        guarded("to-lab-subscription", lambda: check_to_lab(repo), results)
        guarded("ccdd-definitions", lambda: check_ccdd(repo), results)
        guarded("report-present", lambda: check_report(repo), results)

    def do_build() -> None:
        tree = work / "tree"
        copy_tree(repo, tree, with_ctf_env=True)
        ws = Workspace(tree)
        state["ws"] = ws
        build(ws, budget)
        results["build-no-warnings"] = Result("build-no-warnings", ws.build_ok, "" if ws.build_ok else FB["build-no-warnings"],
                                              ws.build_detail + f"\n[{notes}]")

    def do_tests() -> None:
        ws = state["ws"]
        if ws.install_dir is None:
            for cid in ("unit-tests-pass", "coverage-not-below-baseline"):
                results[cid] = Result(cid, False, FB[cid] + NEEDS_BUILD, ws.build_detail)
            return
        ok, detail = run_unit_tests(ws, budget)
        results["unit-tests-pass"] = Result("unit-tests-pass", ok, "" if ok else FB["unit-tests-pass"], detail)
        if not ok:
            results["coverage-not-below-baseline"] = Result(
                "coverage-not-below-baseline", False,
                FB["coverage-not-below-baseline"] + " The unit tests did not all pass, so coverage was not measured.", detail)
            return
        step(["coverage-not-below-baseline"], do_coverage)

    def do_coverage() -> None:
        cov_ok, cov_detail = check_coverage(state["ws"])
        results["coverage-not-below-baseline"] = Result(
            "coverage-not-below-baseline", cov_ok, "" if cov_ok else FB["coverage-not-below-baseline"], cov_detail)

    def do_hidden() -> None:
        hidden_ok, hidden_detail = run_hidden_coverage(repo, work, budget)
        results["hidden-coverage-test"] = Result("hidden-coverage-test", hidden_ok,
                                                 "" if hidden_ok else FB["hidden-coverage-test"], hidden_detail)

    def do_network() -> None:
        run_network_checks(state["ws"], work, budget, results)

    def do_ctf() -> None:
        ws = state["ws"]
        guarded("ctf-test-passes", lambda: run_ctf(ws, work, budget), results,
                needs=NEEDS_BUILD if ws.install_dir is None else "")

    try:
        step(["to-lab-subscription", "ccdd-definitions", "report-present"], do_static)
        step(["build-no-warnings"], do_build)
        if "ws" not in state:
            for cid in ("unit-tests-pass", "coverage-not-below-baseline", "ctf-test-passes", *[c for c, _ in SCENARIOS]):
                results.setdefault(cid, Result(cid, False, FB[cid] + NEEDS_BUILD, results["build-no-warnings"].detail))
        else:
            step(["unit-tests-pass", "coverage-not-below-baseline"], do_tests)
            step(["status-packet-default-divisor", "set-rate-valid", "set-rate-invalid", "process-count", "reset-counters"],
                 do_network)
            step(["ctf-test-passes"], do_ctf)
        step(["hidden-coverage-test"], do_hidden)
        for cid in CHECK_ORDER:  # a step that died without reporting its checks
            results.setdefault(cid, Result(cid, False, FB[cid], "no result was produced"))
        return write_results(out_path, results)
    finally:
        stop_stray_cfs(work)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
