"""Live-system helpers for the STOR acceptance checks.

Drives a built cFS tree over UDP (CI_LAB 1234 in, TO_LAB 2234 out), reads
events from the cFS console log, and inspects the packet files STOR writes
under /cf/stor (which the native build maps to <tree>/build-native_std/exe/cpu1/cf).

Standard library only.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HOST = "127.0.0.1"
CI_LAB_PORT = 1234
TO_LAB_PORT = 2234

# Message IDs pinned by the specification (command 0x1800 + topic, telemetry 0x0800 + topic).
CMD_MID = 0x18C4
SEND_HK_MID = 0x18C5
WAKEUP_MID = 0x18C6
HK_MID = 0x08C4
LIST_MID = 0x08C5
STATS_MID = 0x08C6

TO_LAB_CMD_MID = 0x1880
TO_LAB_OUTPUT_ENABLE_CC = 6

ES_HK_MID = 0x0800
EVS_HK_MID = 0x0801
TIME_HK_MID = 0x0805
SAMPLE_HK_MID = 0x0883
EVS_LONG_EVENT_MID = 0x0808

# Telemetry MIDs nothing else in the mission publishes; used to inject packets.
INJ_A = 0x08E0
INJ_B = 0x08E1
INJ_C = 0x08E2

CC_NOOP = 0
CC_RESET = 1
CC_SET_PRIORITY = 2
CC_SET_DECIMATION = 3
CC_SET_PURGE_THRESHOLD = 4
CC_ADD_FILE = 5
CC_DELETE_FILE = 6
CC_LIST_DIR = 7
CC_LIST_ALL = 8
CC_SEND_STATS = 9
CC_CLOSE_FILES = 10

PATH_LEN = 64
MAX_ENTRIES = 32
TLM_HDR = 16
CFE_FS_HDR = 64

# CI_LAB reads at most 11 datagrams per 0.5 s cycle and truncates anything over
# 768 bytes; STOR subscribes with the SB default per-pipe message limit of 4 at
# the least, so no more than three packets are offered per cycle.
CI_LAB_MAX_PACKET = 768
INJECT_BATCH = 3
INJECT_GAP_S = 0.55
CMD_GAP_S = 0.3
SETTLE_S = 0.9
TLM_WINDOW_S = 1.6

TABLE_NAME_SIZE = 400  # sizeof(STOR_StorageTable_t) per the specification
TABLE_FILE_HDR = 116   # CFE_FS_Header_t (64) + CFE_TBL_File_Hdr_t (52)

EVENT_RE = re.compile(rb"EVS Port\d+ \S+ \d+/\d+/STOR (\d+): ?(.*)")
# cFE TIME leaves flywheel mode about one second after boot and steps the clock back to the tone;
# scenarios start only after that, so idle-time arithmetic never sees time run backwards.
FLYWHEEL_STOP = b"CFE_TIME 21: Stop FLYWHEEL"
FLYWHEEL_WAIT_S = 8.0
BOOT_SETTLE_MIN_S = 3.0
# Console lines that are bus noise from other apps; they are left out of the log excerpts in failure details.
STOR_EXIT_RE = re.compile(rb"Application STOR called CFE_ES_ExitApp|ErrExit Application STOR")
LOG_NOISE_RE = re.compile(r"Pipe Overflow|CFE_SB 25:|SBN|sbn")


class Fail(Exception):
    """A check did not hold; the message is the experimenter detail.

    `feedback`, when given, names the failing behaviour and replaces the check's standard feedback text.
    """

    def __init__(self, message: str = "", feedback: str | None = None) -> None:
        super().__init__(message)
        self.feedback = feedback


class SharedFail(Fail):
    """A failure whose cause is shared by several checks (the build, the table image, cFS startup).

    The first check that meets it reports the feedback in full; later checks refer to it. `retry` tells whether
    a fresh attempt can change the outcome.
    """

    retry = False


class StartFail(SharedFail):
    """cFS with STOR could not be brought into a state where a scenario can begin."""

    retry = True


# -- packet construction and parsing -----------------------------------------

def build_command(mid: int, cc: int, payload: bytes = b"") -> bytes:
    total = 8 + len(payload)
    pkt = bytearray(struct.pack(">HHH", mid, 0xC000, total - 7))
    pkt += bytes([cc, 0]) + payload
    checksum = 0xFF
    for b in pkt:
        checksum ^= b
    pkt[7] = checksum
    assert len(pkt) == total
    return bytes(pkt)


def build_telemetry(mid: int, total: int, index: int, seq: int = 0) -> bytes:
    """Telemetry packet of `total` bytes whose first payload word is `index`."""
    assert 20 <= total <= CI_LAB_MAX_PACKET
    pkt = bytearray(total)
    pkt[0:6] = struct.pack(">HHH", mid, 0xC000 | (seq & 0x3FFF), total - 7)
    pkt[TLM_HDR:TLM_HDR + 4] = struct.pack("<I", index)
    for i in range(TLM_HDR + 4, total):
        pkt[i] = (index + i) & 0xFF
    return bytes(pkt)


def tlm_mid(raw: bytes) -> int:
    return struct.unpack(">H", raw[:2])[0]


def tlm_len(raw: bytes) -> int:
    return struct.unpack(">H", raw[4:6])[0] + 7


@dataclass(frozen=True)
class Hk:
    command_counter: int
    command_error_counter: int
    purge_threshold: int
    telemetry_used: int
    telemetry_quota: int
    file_used: int
    file_quota: int
    packets_stored: int
    packets_decimated: int
    packets_dropped: int
    files_purged: int
    purge_cycles: int
    file_adds_rejected: int
    file_count: int
    active_entries: int
    open_files: int


HK_FMT = "<BBBBIIIIIIIIIHHHH"
HK_SIZE = struct.calcsize(HK_FMT)


def parse_hk(raw: bytes) -> Hk:
    if len(raw) < TLM_HDR + HK_SIZE:
        raise Fail(f"housekeeping packet is {len(raw)} bytes, expected at least {TLM_HDR + HK_SIZE}")
    v = struct.unpack_from(HK_FMT, raw, TLM_HDR)
    return Hk(v[0], v[1], v[2], v[4], v[5], v[6], v[7], v[8], v[9], v[10], v[11], v[12], v[13], v[14], v[15], v[16])


@dataclass(frozen=True)
class EntryStats:
    mid: int
    priority: int
    decimation: int
    received: int
    stored: int
    decimated: int
    dropped: int


STATS_ENTRY_FMT = "<IBBHIIII"
STATS_ENTRY_SIZE = struct.calcsize(STATS_ENTRY_FMT)


def parse_stats(raw: bytes) -> dict[int, EntryStats]:
    need = TLM_HDR + STATS_ENTRY_SIZE * MAX_ENTRIES
    if len(raw) < need:
        raise Fail(f"statistics packet is {len(raw)} bytes, expected at least {need}")
    out: dict[int, EntryStats] = {}
    for i in range(MAX_ENTRIES):
        mid, pri, dec, _sp, rx, st, de, dr = struct.unpack_from(STATS_ENTRY_FMT, raw, TLM_HDR + i * STATS_ENTRY_SIZE)
        if mid == 0 or (pri == 0 and dec == 0 and rx == 0):
            continue
        out[mid] = EntryStats(mid, pri, dec, rx, st, de, dr)
    return out


@dataclass(frozen=True)
class ListPacket:
    total: int
    index: int
    count: int
    entries: tuple[tuple[str, int], ...]


LIST_ENTRY_FMT = f"<{PATH_LEN}sII"
LIST_ENTRY_SIZE = struct.calcsize(LIST_ENTRY_FMT)


def parse_list(raw: bytes) -> ListPacket:
    need = TLM_HDR + 8 + 4 * LIST_ENTRY_SIZE
    if len(raw) < need:
        raise Fail(f"listing packet is {len(raw)} bytes, expected at least {need}")
    total, index, count, _sp = struct.unpack_from("<HHHH", raw, TLM_HDR)
    if count > 4:
        raise Fail(f"listing packet reports EntriesInPacket {count} (> 4)")
    entries = []
    for i in range(count):
        name, size, _s = struct.unpack_from(LIST_ENTRY_FMT, raw, TLM_HDR + 8 + i * LIST_ENTRY_SIZE)
        entries.append((name.split(b"\0", 1)[0].decode("latin-1"), size))
    return ListPacket(total, index, count, tuple(entries))


def pstr_id(mid: int, value: int) -> bytes:
    """Payload of SET_PRIORITY / SET_DECIMATION: MsgId then one value byte and three spare bytes."""
    return struct.pack("<IB3x", mid, value)


def pstr(text: str) -> bytes:
    raw = text.encode()
    assert len(raw) < PATH_LEN
    return raw.ljust(PATH_LEN, b"\0")


# -- table file --------------------------------------------------------------

@dataclass(frozen=True)
class TableSpec:
    telemetry_root: str = "/cf/stor/tlm"
    file_root: str = "/cf/stor/files"
    telemetry_quota: int = 262144
    file_quota: int = 1048576
    max_packet_file: int = 16384
    threshold: int = 80
    entries: tuple[tuple[int, int, int], ...] = ()  # (mid, priority, decimation)


def pack_table(spec: TableSpec) -> bytes:
    body = pstr(spec.telemetry_root) + pstr(spec.file_root)
    body += struct.pack("<IIIB3x", spec.telemetry_quota, spec.file_quota, spec.max_packet_file, spec.threshold)
    assert len(spec.entries) <= MAX_ENTRIES
    for i in range(MAX_ENTRIES):
        mid, pri, dec = spec.entries[i] if i < len(spec.entries) else (0, 0, 0)
        body += struct.pack("<IBBH", mid, pri, dec, 0)
    assert len(body) == TABLE_NAME_SIZE
    return body


def unpack_table(body: bytes) -> TableSpec:
    if len(body) != TABLE_NAME_SIZE:
        raise Fail(f"table image is {len(body)} bytes, expected {TABLE_NAME_SIZE}")
    root = body[0:PATH_LEN].split(b"\0", 1)[0].decode("latin-1")
    froot = body[PATH_LEN:2 * PATH_LEN].split(b"\0", 1)[0].decode("latin-1")
    tq, fq, mf, thr = struct.unpack_from("<IIIB", body, 2 * PATH_LEN)
    entries = []
    for i in range(MAX_ENTRIES):
        mid, pri, dec, _sp = struct.unpack_from("<IBBH", body, 2 * PATH_LEN + 16 + i * 8)
        if mid != 0:
            entries.append((mid, pri, dec))
    return TableSpec(root, froot, tq, fq, mf, thr, tuple(entries))


# -- packet files on disk ------------------------------------------------------

PKT_FILE_RE = re.compile(r"^tlm_([0-9A-F]{4})_(\d{8})\.bin$")


@dataclass(frozen=True)
class PacketFile:
    mid: int
    seq: int
    path: Path
    size: int


def list_packet_files(tlm_dir: Path) -> list[PacketFile]:
    out: list[PacketFile] = []
    if not tlm_dir.is_dir():
        return out
    for p in sorted(tlm_dir.iterdir()):
        m = PKT_FILE_RE.match(p.name)
        if m and p.is_file():
            out.append(PacketFile(int(m.group(1), 16), int(m.group(2)), p, p.stat().st_size))
    return sorted(out, key=lambda f: (f.seq, f.mid))


def read_packets(data: bytes) -> list[bytes]:
    """Split a packet file image into packets; raises Fail when malformed."""
    if len(data) < CFE_FS_HDR:
        raise Fail(f"file of {len(data)} bytes is shorter than the {CFE_FS_HDR}-byte header")
    pos = CFE_FS_HDR
    pkts: list[bytes] = []
    while pos < len(data):
        if len(data) - pos < 6:
            raise Fail(f"trailing {len(data) - pos} bytes after the last packet")
        n = tlm_len(data[pos:pos + 6])
        if n < 7 or pos + n > len(data):
            raise Fail(f"packet at offset {pos} claims {n} bytes but only {len(data) - pos} remain")
        pkts.append(data[pos:pos + n])
        pos += n
    return pkts


def check_fs_header(data: bytes) -> list[str]:
    """Problems with the CFE_FS_Header_t at the start of a packet file."""
    problems: list[str] = []
    if len(data) < CFE_FS_HDR:
        return [f"file shorter than {CFE_FS_HDR} bytes"]
    content_le, sub_le = struct.unpack_from("<II", data, 0)
    content_be, sub_be = struct.unpack_from(">II", data, 0)
    if 0x63464531 not in (content_le, content_be):
        problems.append(f"ContentType {content_le:#x} is not 'cFE1'")
    if 0x5354 not in (sub_le, sub_be):
        problems.append(f"SubType {sub_le:#x} is not 0x5354")
    desc = data[32:64].split(b"\0", 1)[0]
    if desc != b"STOR packet file":
        problems.append(f"Description {desc!r} is not 'STOR packet file'")
    return problems


# -- the running system ------------------------------------------------------

STOR_ERROR_EVENTS = {2: "CC_ERR", 5: "MID_ERR", 6: "CMD_LEN_ERR", 7: "PIPE_ERR", 8: "TBL_REG_ERR", 9: "TBL_LOAD_ERR",
                     10: "TBL_VAL_ERR", 11: "SUB_ERR", 12: "DIR_CREATE_ERR", 15: "FILE_WRITE_ERR"}


def describe_table(t: TableSpec) -> str:
    entries = "; ".join(f"0x{m:04X} priority {p} decimation {d}" for m, p, d in t.entries)
    return (f"TelemetryQuotaBytes {t.telemetry_quota}, FileQuotaBytes {t.file_quota}, MaxPacketFileBytes "
            f"{t.max_packet_file}, PurgeThresholdPercent {t.threshold} and entries [{entries}]")


class Receiver(threading.Thread):
    """Collects every datagram TO_LAB sends, with arrival times."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        try:
            self.sock.bind((HOST, TO_LAB_PORT))
        except OSError as exc:
            self.sock.close()
            raise StartFail(f"cannot listen on {HOST}:{TO_LAB_PORT} for TO_LAB telemetry: {exc}",
                            "cFS could not be driven for this scenario because the telemetry port was in use; "
                            "STOR and its test setup must not leave cFS processes running.") from exc
        self.sock.settimeout(0.2)
        self.lock = threading.Lock()
        self.packets: list[tuple[float, int, bytes]] = []
        self.running = True

    def run(self) -> None:
        while self.running:
            try:
                data, _ = self.sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            if len(data) >= 6:
                with self.lock:
                    self.packets.append((time.monotonic(), tlm_mid(data), data))

    def mark(self) -> int:
        with self.lock:
            return len(self.packets)

    def since(self, mark: int, mid: int) -> list[bytes]:
        with self.lock:
            return [d for (_t, m, d) in self.packets[mark:] if m == mid]

    def close(self) -> None:
        self.running = False
        self.join(timeout=2)
        self.sock.close()


class Cfs:
    """One cFS instance built in `tree`, started fresh for each scenario."""

    def __init__(self, tree: Path, pristine_table: bytes) -> None:
        self.tree = tree
        self.exe = tree / "build-native_std" / "exe" / "cpu1"
        self.cf = self.exe / "cf"
        self.run_dir = tree / "build-native_std" / "run"
        self.log = self.run_dir / "cfs.log"
        self.pristine_table = pristine_table
        self.rx: Receiver | None = None
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = False
        self.tlm_dir = self.cf / "stor" / "tlm"
        self.files_dir = self.cf / "stor" / "files"
        self.boot_note = ""

    # -- lifecycle ---------------------------------------------------------
    def stop(self) -> None:
        env = dict(os.environ, CFS_RUN_DIR=str(self.run_dir))
        script = self.tree / "tools" / "stop_cfs.sh"
        if script.is_file():
            subprocess.run(["bash", str(script)], env=env, capture_output=True, timeout=30, check=False)
        pidfile = self.run_dir / "cfs.pid"
        if pidfile.is_file():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        if self.rx is not None:
            self.rx.close()
            self.rx = None
        self.running = False

    def restore_table(self) -> None:
        """Put the table image the build installed back in /cf (sessions may have replaced it)."""
        (self.cf / "stor_tbl.tbl").write_bytes(self.pristine_table)

    def start(self, table: TableSpec | None = None, wipe: bool = True,
              seed: dict[str, bytes] | None = None) -> None:
        """Boot cFS with a fresh /cf/stor; `table` replaces the default table image."""
        self.stop()
        if wipe:
            shutil.rmtree(self.cf / "stor", ignore_errors=True)
        for stray in self.cf.glob("big*.bin"):
            stray.unlink()
        for rel, data in (seed or {}).items():
            dest = self.cf / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        image = self.pristine_table
        if table is not None:
            image = image[:TABLE_FILE_HDR] + pack_table(table)
        (self.cf / "stor_tbl.tbl").write_bytes(image)
        what = "the default StorageTable" if table is None else f"a StorageTable with {describe_table(table)}"
        self.rx = Receiver()
        self.rx.start()
        env = dict(os.environ, CFS_RUN_DIR=str(self.run_dir), CFS_TIMEOUT="40")
        try:
            proc = subprocess.run(["bash", str(self.tree / "tools" / "run_cfs.sh")], env=env,
                                  capture_output=True, text=True, errors="replace", timeout=90, check=False)
        except subprocess.TimeoutExpired:
            self.stop()
            raise StartFail(f"tools/run_cfs.sh did not return within 90 s ({what}); {self.log_excerpt()}",
                            f"With {what}, cFS did not reach its operational state; STOR's initialization must "
                            f"complete and return control to its main loop.") from None
        if proc.returncode != 0:
            self.stop()
            raise StartFail(f"cFS did not reach OPERATIONAL ({what}): {proc.stderr[-600:]}; {self.log_excerpt()}",
                            f"With {what}, cFS did not reach its operational state{self.stor_errors_phrase()}; "
                            f"STOR's initialization must complete and return control to its main loop.")
        self.running = True
        booted = time.monotonic()
        deadline = booted + 15
        while time.monotonic() < deadline:
            if self.events(0, 1):
                break
            if not self.alive() or STOR_EXIT_RE.search(self.log_bytes()):
                break
            time.sleep(0.2)
        else:
            excerpt = self.log_excerpt()
            phrase = self.stor_errors_phrase()
            self.stop()
            raise StartFail(f"STOR did not report its initialization event (ID 1) within 15 s ({what}); {excerpt}",
                            f"With {what}, STOR did not send its initialization event (ID 1) within 15 s of "
                            f"boot{phrase}.")
        if not self.events(0, 1):
            excerpt = self.log_excerpt()
            phrase = self.stor_errors_phrase()
            exited = STOR_EXIT_RE.search(self.log_bytes()) is not None
            self.stop()
            if exited:
                raise StartFail(f"STOR exited during initialization ({what}); {excerpt}",
                                f"With {what}, STOR exited during initialization (CFE_ES_ExitApp) without sending its "
                                f"initialization event (ID 1){phrase}.")
            raise StartFail(f"cFS exited during startup ({what}); {excerpt}",
                            f"With {what}, cFS stopped running during startup{phrase}; STOR must initialize "
                            f"without crashing or exiting.")
        # Let cFE TIME settle (see FLYWHEEL_STOP) before any scenario step.
        fly_deadline = booted + FLYWHEEL_WAIT_S
        while FLYWHEEL_STOP not in self.log_bytes() and time.monotonic() < fly_deadline:
            time.sleep(0.2)
        self.boot_note = "flywheel stop seen" if FLYWHEEL_STOP in self.log_bytes() else "no flywheel stop in the log"
        self.send(build_command(TO_LAB_CMD_MID, TO_LAB_OUTPUT_ENABLE_CC, b"127.0.0.1".ljust(16, b"\0")))
        time.sleep(max(0.9, BOOT_SETTLE_MIN_S - (time.monotonic() - booted)))
        if not self.alive():
            excerpt = self.log_excerpt()
            phrase = self.stor_errors_phrase()
            self.stop()
            raise StartFail(f"cFS exited right after STOR initialized ({what}); {excerpt}",
                            f"With {what}, cFS stopped running within seconds of STOR's initialization{phrase}.")

    # -- log access -------------------------------------------------------------
    def log_bytes(self) -> bytes:
        try:
            return self.log.read_bytes()
        except OSError:
            return b""

    def log_excerpt(self, limit: int = 40) -> str:
        """The last console lines that are not bus noise (for the experimenter detail)."""
        lines = [ln for ln in self.log_bytes().decode("latin-1").splitlines() if ln.strip() and not LOG_NOISE_RE.search(ln)]
        stor = [ln for ln in lines if "STOR" in ln or "stor" in ln]
        return "STOR log lines: " + " | ".join(stor[-limit:]) + " || last log lines: " + " | ".join(lines[-12:])

    def stor_errors_phrase(self) -> str:
        """`; STOR reported events ...` naming the STOR error events in the log, or an empty string."""
        errs = sorted({e for e, _ in self.events(0) if e in STOR_ERROR_EVENTS})
        if not errs:
            return ""
        return "; STOR reported " + ", ".join(f"event {e} ({STOR_ERROR_EVENTS[e]})" for e in errs)

    # -- events (from the cFS console log) --------------------------------------
    def log_mark(self) -> int:
        return self.log.stat().st_size if self.log.is_file() else 0

    def events(self, mark: int, eid: int | None = None) -> list[tuple[int, str]]:
        if not self.log.is_file():
            return []
        with open(self.log, "rb") as fh:
            fh.seek(mark)
            data = fh.read()
        out = []
        for m in EVENT_RE.finditer(data):
            e = int(m.group(1))
            if eid is None or e == eid:
                out.append((e, m.group(2).decode("latin-1")))
        return out

    # -- commands -----------------------------------------------------------
    def send(self, pkt: bytes) -> None:
        self.tx.sendto(pkt, (HOST, CI_LAB_PORT))

    def cmd(self, cc: int, payload: bytes = b"", mid: int = CMD_MID, gap: float = CMD_GAP_S) -> None:
        self.send(build_command(mid, cc, payload))
        time.sleep(gap)

    def settle(self, seconds: float = SETTLE_S) -> None:
        time.sleep(seconds)

    def alive(self) -> bool:
        pidfile = self.run_dir / "cfs.pid"
        try:
            os.kill(int(pidfile.read_text().strip()), 0)
            return True
        except (OSError, ValueError):
            return False

    # -- telemetry ------------------------------------------------------------
    def _request(self, mid: int, cc: int, payload: bytes, reply_mid: int) -> list[bytes]:
        """Send a command and return the `reply_mid` packets that arrive in the following window."""
        assert self.rx is not None
        mark = self.rx.mark()
        self.send(build_command(mid, cc, payload))
        time.sleep(TLM_WINDOW_S)
        got = self.rx.since(mark, reply_mid)
        for _ in range(10):
            if got:
                break
            time.sleep(0.3)
            got = self.rx.since(mark, reply_mid)
        return got

    def hk(self) -> Hk:
        """Housekeeping after everything sent so far (the last packet in the window)."""
        got = self._request(SEND_HK_MID, 0, b"", HK_MID) or self._request(SEND_HK_MID, 0, b"", HK_MID)
        self.require_alive()
        if not got:
            raise Fail("no housekeeping packet (message ID 0x08C4) followed SEND_HK",
                       "STOR must answer every SEND_HK with a housekeeping packet on STOR_HK_TLM_MID (0x08C4) that "
                       "reaches the ground through TO_LAB; during this scenario one SEND_HK went unanswered.")
        return parse_hk(got[-1])

    def stats(self) -> dict[int, EntryStats]:
        got = self._request(CMD_MID, CC_SEND_STATS, b"", STATS_MID) or self._request(CMD_MID, CC_SEND_STATS, b"", STATS_MID)
        self.require_alive()
        if not got:
            raise Fail("no statistics packet (message ID 0x08C6) followed SEND_STATS",
                       "SEND_STATS must transmit one STOR_StatsTlm_t on STOR_STATS_TLM_MID (0x08C6) that reaches "
                       "the ground through TO_LAB; during this scenario one SEND_STATS went unanswered.")
        return parse_stats(got[-1])

    def require_alive(self) -> None:
        if not self.alive():
            raise Fail(f"cFS stopped running during the scenario; {self.log_excerpt()}",
                       f"cFS stopped running while STOR was handling the commands and packets of this "
                       f"scenario{self.stor_errors_phrase()}; STOR must not crash or exit.")

    def listing(self, cc: int, payload: bytes = b"") -> list[ListPacket]:
        got = self._request(CMD_MID, cc, payload, LIST_MID)
        return sorted((parse_list(g) for g in got), key=lambda p: p.index)

    def unsolicited(self, mid: int, seconds: float) -> list[bytes]:
        assert self.rx is not None
        mark = self.rx.mark()
        time.sleep(seconds)
        return self.rx.since(mark, mid)

    # -- injection ----------------------------------------------------------------
    def inject(self, mid: int, count: int, size: int, first_index: int = 0) -> None:
        """Offer `count` telemetry packets on `mid`, paced for CI_LAB (see module notes)."""
        for i in range(count):
            self.send(build_telemetry(mid, size, first_index + i))
            if (i + 1) % INJECT_BATCH == 0 and i + 1 < count:
                time.sleep(INJECT_GAP_S)
        time.sleep(INJECT_GAP_S)

    def packet_files(self) -> list[PacketFile]:
        return list_packet_files(self.tlm_dir)


@dataclass
class Problems:
    """Collects assertion failures so one check can report every deviation.

    Each failure carries the experimenter detail and, optionally, one sentence naming the failing behaviour;
    the feedback of the raised Fail lists the distinct sentences in order of first occurrence. A failure
    whose sentence is None makes the check fall back to its standard feedback; an empty sentence marks a
    detail-only note.
    """
    items: list[tuple[str, str | None]] = field(default_factory=list)

    def check(self, cond: bool, msg: str, behaviour: str | None = None) -> bool:
        if not cond:
            self.items.append((msg, behaviour))
        return cond

    def raise_if(self) -> None:
        if not self.items:
            return
        sentences: list[str] = []
        for _msg, b in self.items:
            if b and b not in sentences:
                sentences.append(b)
        generic = any(b is None for _m, b in self.items)
        raise Fail("; ".join(m for m, _b in self.items), " ".join(sentences) if sentences and not generic else None)
