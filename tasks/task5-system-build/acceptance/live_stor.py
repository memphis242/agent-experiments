"""Live checks of STOR (S1 base, re-plumbed from Task 4 through CMDI frames and TLMO; S2 transfer)."""
from __future__ import annotations

import re
import struct
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import frames as fr
import spec
import tables as tb
from common import Fail, Problems
from live_common import boot, wait_hk_field
from node import Datagram, Node

if TYPE_CHECKING:
    from check import Ctx

INJ_A = spec.verif_mid(0xE0)
INJ_B = spec.verif_mid(0xE1)
INJ_C = spec.verif_mid(0xE2)
PKT = 700
FILE_FULL = 64 + 5 * PKT
CFE_FS_HDR = 64
PKT_FILE_RE = re.compile(r"^tlm_([0-9A-F]{4})_(\d{8})\.(bin|arc)$")


def stor_packet(mid: int, size: int, index: int) -> bytes:
    """A verification packet of `size` bytes carrying `index` at byte 16 (payload byte 8)."""
    return fr.build_verification(mid, size, counter=index, fill=(index * 7) & 0xFF)


def quiet_table(**kw: object) -> tb.StorTable:
    base = dict(telemetry_quota=65536, file_quota=1048576, max_packet_file=1024, threshold=100, entries=((INJ_A, 10, 1),))
    base.update(kw)
    return tb.StorTable(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PacketFile:
    mid: int
    seq: int
    path: Path
    size: int
    archived: bool


@dataclass(frozen=True)
class EntryStats:
    mid: int
    priority: int
    decimation: int
    received: int
    stored: int
    decimated: int
    dropped: int


@dataclass(frozen=True)
class ListPacket:
    total: int
    index: int
    count: int
    entries: tuple[tuple[str, int], ...]


def parse_stats(raw: bytes) -> dict[int, EntryStats]:
    need = spec.TLM_HDR + 24 * spec.STOR_MAX_ENTRIES
    if len(raw) < need:
        raise Fail(f"statistics packet is {len(raw)} bytes, expected at least {need}")
    out: dict[int, EntryStats] = {}
    for i in range(spec.STOR_MAX_ENTRIES):
        mid, pri, dec, _sp, rx, st, de, dr = struct.unpack_from(spec.STOR_STATS_ENTRY_FMT, raw, spec.TLM_HDR + i * 24)
        if mid == 0:
            continue
        out[mid] = EntryStats(mid, pri, dec, rx, st, de, dr)
    return out


def parse_list(raw: bytes) -> ListPacket:
    need = spec.TLM_HDR + spec.STOR_LIST_SIZE
    if len(raw) < need:
        raise Fail(f"listing packet is {len(raw)} bytes, expected at least {need}")
    total, index, count, _sp = struct.unpack_from("<HHHH", raw, spec.TLM_HDR)
    if count > 4:
        raise Fail(f"listing packet reports EntriesInPacket {count} (> 4)")
    entries = []
    for i in range(count):
        name, size, _s = struct.unpack_from(spec.STOR_LIST_ENTRY_FMT, raw, spec.TLM_HDR + 8 + i * 72)
        entries.append((name.split(b"\0", 1)[0].decode("latin-1"), size))
    return ListPacket(total, index, count, tuple(entries))


def list_packet_files(tlm_dir: Path) -> list[PacketFile]:
    out: list[PacketFile] = []
    if not tlm_dir.is_dir():
        return out
    for p in sorted(tlm_dir.iterdir()):
        m = PKT_FILE_RE.match(p.name)
        if m and p.is_file():
            out.append(PacketFile(int(m.group(1), 16), int(m.group(2)), p, p.stat().st_size, m.group(3) == "arc"))
    return sorted(out, key=lambda f: (f.seq, f.mid))


def read_packets(data: bytes) -> list[bytes]:
    if len(data) < CFE_FS_HDR:
        raise Fail(f"file of {len(data)} bytes is shorter than the {CFE_FS_HDR}-byte header")
    pos = CFE_FS_HDR
    pkts: list[bytes] = []
    while pos < len(data):
        if len(data) - pos < 6:
            raise Fail(f"trailing {len(data) - pos} bytes after the last packet")
        n = fr.packet_len(data[pos:pos + 6])
        if n < 7 or pos + n > len(data):
            raise Fail(f"packet at offset {pos} claims {n} bytes but only {len(data) - pos} remain")
        pkts.append(data[pos:pos + n])
        pos += n
    return pkts


def check_fs_header(data: bytes) -> list[str]:
    problems: list[str] = []
    if len(data) < CFE_FS_HDR:
        return [f"file shorter than {CFE_FS_HDR} bytes"]
    content_le, sub_le = struct.unpack_from("<II", data, 0)
    content_be, sub_be = struct.unpack_from(">II", data, 0)
    if 0x63464531 not in (content_le, content_be):
        problems.append(f"ContentType {content_le:#x} is not 'cFE1'")
    if spec.STOR_FILE_SUBTYPE not in (sub_le, sub_be):
        problems.append(f"SubType {sub_le:#x} is not 0x5354")
    desc = data[32:64].split(b"\0", 1)[0]
    if desc != b"STOR packet file":
        problems.append(f"Description {desc!r} is not 'STOR packet file'")
    return problems


def pstr(text: str) -> bytes:
    raw = text.encode()
    assert len(raw) < spec.STOR_PATH_LEN
    return raw.ljust(spec.STOR_PATH_LEN, b"\0")


def pstr_id(mid: int, value: int) -> bytes:
    return struct.pack("<IB3x", mid, value)


def add_payload(src: str, dest: str) -> bytes:
    return pstr(src) + pstr(dest)


class Stor:
    """STOR on one node, commanded with frames and read through TLMO."""

    def __init__(self, node: Node) -> None:
        self.n = node
        self.tlm_dir = node.cf / "stor" / "tlm"
        self.files_dir = node.cf / "stor" / "files"
        self.cf = node.cf

    def cmd(self, name: str, payload: bytes = b"", gap: float = 0.15) -> None:
        self.n.ensure_command_path()
        self.n.cmd("STOR", name, payload, gap=gap)

    def wakeup(self) -> None:
        self.n.send(spec.app_mid("STOR", "WAKEUP", self.n.id), 0)

    def hk(self) -> dict[str, int]:
        return self.n.hk("STOR")

    def mark(self) -> int:
        return self.n.g.mark()

    def events(self, mark: int, eid: int) -> list:
        return self.n.events(mark, "STOR", eid)

    def wait_event(self, mark: int, eid: int, timeout: float = 4.0, count: int = 1) -> list:
        return self.n.wait_event(mark, "STOR", eid, timeout, count)

    def settle(self, seconds: float = 0.9) -> None:
        time.sleep(seconds)

    def stats(self) -> dict[int, EntryStats]:
        self.n.ensure_command_path()
        mid = spec.app_mid("STOR", "STATS_TLM", self.n.id)
        for _ in range(2):
            got = self.n.request(lambda: self.cmd("SEND_STATS", gap=0), mid)
            if got:
                return parse_stats(got[-1].data)
        raise Fail(f"no statistics packet (MID {mid:#06x}) followed SEND_STATS")

    def listing(self, name: str, payload: bytes = b"") -> list[ListPacket]:
        self.n.ensure_command_path()
        mid = spec.app_mid("STOR", "LIST_TLM", self.n.id)
        got = self.n.request(lambda: self.cmd(name, payload, gap=0), mid, tail=0.8)
        return sorted((parse_list(g.data) for g in got), key=lambda p: p.index)

    def inject(self, mid: int, count: int, size: int, first_index: int = 0) -> None:
        self.n.ensure_command_path()
        self.n.inject([stor_packet(mid, size, first_index + i) for i in range(count)], burst=3, gap=0.25)

    def packet_files(self) -> list[PacketFile]:
        return list_packet_files(self.tlm_dir)

    def load(self, table: tb.StorTable) -> None:
        self.n.load_table("STOR", table.pack())


def session(ctx: "Ctx", table: tb.StorTable | None = None, seed: dict[str, bytes] | None = None,
            default_boot: bool = False) -> Stor:
    """cpu1 with STOR booted quiet (no entries) and `table` loaded through cFE Table Services, or the default table."""
    nodes = boot(ctx, (1,), seed={1: seed or {}}, quiet_stor=not default_boot)
    s = Stor(nodes[1])
    if table is not None:
        s.load(table)
    return s


def wait_until(pred: Callable[[], bool], timeout: float, poll: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() >= end:
            return False
        time.sleep(poll)
    return True


def disk_sum(s: Stor) -> int:
    return sum(f.size for f in s.packet_files())


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


# ============================================================================ S1

def c_scheduled_housekeeping(ctx: "Ctx") -> None:
    nodes = boot(ctx, (1,))
    n = nodes[1]
    mid = spec.app_mid("STOR", "HK_TLM")
    got = n.g.wait_mid(0, mid, 9.0)
    if not got:
        raise Fail(f"no STOR housekeeping (MID {mid:#06x}) arrived within 9 s of start-up without any request")


def c_startup_and_hk(ctx: "Ctx") -> None:
    s = session(ctx, default_boot=True)
    n = s.n
    p = Problems()
    mark = s.mark()
    s.cmd("RESET_COUNTERS")
    if not s.wait_event(mark, spec.EID["STOR"]["RESET_INF"]):
        raise Fail("STOR RESET_COUNTERS produced no acknowledgement after initial profile setup")
    wait_hk_field(n, "STOR", lambda hk: hk["CommandCounter"] == 0 and hk["CommandErrorCounter"] == 0,
                  3.1, "STOR counter reset must be acknowledged before absolute counter assertions")
    from node import console_events
    inits = [e for e in console_events(n.console_log()) if e[0] == "STOR" and e[1] == 1]
    p.check(len(inits) == 1, f"STOR_INIT_INF_EID (1) appears {len(inits)} times in the console event output")
    mid = spec.app_mid("STOR", "HK_TLM")
    got = n.request(lambda: n.send(spec.app_mid("STOR", "SEND_HK"), 0), mid)
    if not got:
        raise Fail(f"no STOR housekeeping (MID {mid:#06x}) followed a framed SEND_HK")
    raw = got[-1].data
    p.check(fr.packet_len(raw) == spec.TLM_HDR + spec.HK_LAYOUTS["STOR"].size,
            f"housekeeping packet is {fr.packet_len(raw)} bytes, expected {spec.TLM_HDR + spec.HK_LAYOUTS['STOR'].size}")
    from node import parse_hk
    hk = parse_hk("STOR", raw)
    p.check(hk["TelemetryQuotaBytes"] == 262144 and hk["FileQuotaBytes"] == 1048576,
            f"quotas {hk['TelemetryQuotaBytes']}/{hk['FileQuotaBytes']}")
    p.check(hk["PurgeThresholdPercent"] == 80 and hk["ActiveEntries"] == 4,
            f"PurgeThresholdPercent {hk['PurgeThresholdPercent']}, ActiveEntries {hk['ActiveEntries']}")
    p.check(hk["CommandCounter"] == 0 and hk["CommandErrorCounter"] == 0, f"counters {hk['CommandCounter']}/{hk['CommandErrorCounter']}")
    p.check(hk["FileCount"] == 0 and hk["FileBytesUsed"] == 0, f"FileCount {hk['FileCount']}, FileBytesUsed {hk['FileBytesUsed']}")
    p.check(hk["XferState"] == 0 and hk["RxState"] == 0 and hk["XferActiveId"] == 0,
            f"transfer state {hk['XferState']}/{hk['RxState']}/{hk['XferActiveId']} before any transfer")
    mark = s.mark()
    s.cmd("NOOP")
    p.check(bool(s.wait_event(mark, 3)), "NOOP did not produce event 3")
    p.check(s.hk()["CommandCounter"] == 1, "CommandCounter is not 1 after one NOOP")
    p.check(wait_until(lambda: len(s.packet_files()) >= 1, 8.0), "no packet file exists after housekeeping traffic was stored")
    p.raise_if()


def c_decimation_and_stats(ctx: "Ctx") -> None:
    p = Problems()
    s = session(ctx, default_boot=True)
    n = s.n
    smid = spec.sample_hk_mid()
    base = s.stats().get(smid)
    if base is None:
        raise Fail(f"the statistics packet has no entry for {smid:#06x} (SAMPLE_APP housekeeping)")
    for _ in range(10):
        n.send(spec.sample_send_hk_mid(), 0)
        time.sleep(0.15)
    s.cmd("CLOSE_FILES")
    st = s.stats()
    e = st.get(smid)
    if e is None:
        raise Fail(f"the statistics packet lost the entry for {smid:#06x}")
    p.check(e.received - base.received >= 10, f"{smid:#06x} Received grew by {e.received - base.received} after 10 packets")
    p.check(e.stored == ceil_div(e.received, 2), f"{smid:#06x} Stored {e.stored} != ceil(Received {e.received} / 2)")
    p.check(e.decimated == e.received - e.stored, f"{smid:#06x} Decimated {e.decimated}")
    p.check((e.priority, e.decimation) == (30, 2), f"{smid:#06x} priority/decimation {e.priority}/{e.decimation}")
    for name, pri in (("CFE_EVS_HK_TLM_MID", 10), ("CFE_ES_HK_TLM_MID", 20), ("CFE_TIME_HK_TLM_MID", 40)):
        mid = spec.named_tlm_mid(name)
        o = st.get(mid)
        if not p.check(o is not None, f"no statistics entry for {name}"):
            continue
        assert o is not None
        p.check((o.priority, o.decimation) == (pri, 1), f"{name} priority/decimation {o.priority}/{o.decimation}")
        p.check(o.stored == o.received and o.decimated == 0, f"{name} stored {o.stored} of {o.received}")
    # Exact rule on a quiet table loaded through cFE Table Services, including decimation 255.
    s = session(ctx, quiet_table(entries=((INJ_A, 10, 1), (INJ_B, 20, 3), (INJ_C, 30, 255))))
    s.inject(INJ_A, 4, 300)
    s.inject(INJ_B, 10, 300)
    s.inject(INJ_C, 10, 300)
    st = s.stats()
    for mid, (rx, stored, dec) in ((INJ_A, (4, 4, 0)), (INJ_B, (10, 4, 6)), (INJ_C, (10, 1, 9))):
        o = st.get(mid)
        if not p.check(o is not None, f"no statistics entry for {mid:#06x}"):
            continue
        assert o is not None
        p.check((o.received, o.stored, o.decimated, o.dropped) == (rx, stored, dec, 0),
                f"{mid:#06x} received/stored/decimated/dropped {o.received}/{o.stored}/{o.decimated}/{o.dropped} != {rx}/{stored}/{dec}/0")
    s.cmd("CLOSE_FILES")
    indices: dict[int, list[int]] = {INJ_A: [], INJ_B: [], INJ_C: []}
    for f in s.packet_files():
        try:
            for pkt in read_packets(f.path.read_bytes()):
                indices.setdefault(f.mid, []).append(struct.unpack_from("<I", pkt, 16)[0])
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}")
    p.check(indices[INJ_B] == [0, 3, 6, 9], f"stored B packets {indices[INJ_B]} != [0, 3, 6, 9]")
    p.check(indices[INJ_C] == [0], f"stored C packets {indices[INJ_C]} != [0]")
    p.check(indices[INJ_A] == [0, 1, 2, 3], f"stored A packets {indices[INJ_A]}")
    hk = s.hk()
    p.check(hk["PacketsStored"] == 9 and hk["PacketsDecimated"] == 15,
            f"PacketsStored/PacketsDecimated {hk['PacketsStored']}/{hk['PacketsDecimated']} != 9/15")
    mark = s.mark()
    s.cmd("SET_DECIMATION", pstr_id(INJ_B, 1))
    s.cmd("SET_PRIORITY", pstr_id(INJ_B, 77))
    p.check(len(s.wait_event(mark, 16, count=2)) == 2, "SET_DECIMATION/SET_PRIORITY did not each produce event 16")
    s.inject(INJ_B, 2, 300, first_index=10)
    o = s.stats().get(INJ_B)
    if o is not None:
        p.check((o.priority, o.decimation, o.received, o.stored) == (77, 1, 12, 6),
                f"B after the run-time change priority/decimation/received/stored {o.priority}/{o.decimation}/{o.received}/{o.stored} != 77/1/12/6")
    p.raise_if()


def c_packet_file_naming(ctx: "Ctx") -> None:
    s = session(ctx, quiet_table(max_packet_file=16384))
    p = Problems()
    s.inject(INJ_A, 3, 200, first_index=2000)
    mark = s.mark()
    s.cmd("CLOSE_FILES")
    p.check(bool(s.wait_event(mark, 27)), "CLOSE_FILES did not produce event 27")
    time.sleep(0.5)
    files = s.packet_files()
    names = sorted(x.name for x in s.tlm_dir.iterdir()) if s.tlm_dir.is_dir() else []
    for nme in names:
        p.check(PKT_FILE_RE.match(nme) is not None and nme.endswith(".bin"), f"file {nme!r} does not match tlm_<MMMM>_<NNNNNNNN>.bin")
    mine = [f for f in files if f.mid == INJ_A]
    if not p.check(bool(mine), f"no tlm_{INJ_A:04X}_NNNNNNNN.bin file exists under /cf/stor/tlm"):
        p.raise_if()
    seen = 0
    for f in mine:
        data = f.path.read_bytes()
        for msg in check_fs_header(data):
            p.check(False, f"{f.path.name}: {msg}")
        try:
            pkts = read_packets(data)
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}")
            continue
        for pkt in pkts:
            idx = struct.unpack_from("<I", pkt, 16)[0]
            p.check(pkt == stor_packet(INJ_A, 200, idx), f"{f.path.name}: stored packet {idx} differs from the packet received")
            seen += 1
    p.check(seen == 3, f"{seen} of the 3 injected packets were found in the files")
    before = disk_sum(s)
    hk = s.hk()
    after = disk_sum(s)
    p.check(before <= hk["TelemetryBytesUsed"] <= after, f"TelemetryBytesUsed {hk['TelemetryBytesUsed']} not in [{before}, {after}] (disk)")
    p.raise_if()


def c_rollover(ctx: "Ctx") -> None:
    s = session(ctx, quiet_table(entries=((INJ_A, 10, 1), (INJ_B, 20, 1))))
    p = Problems()
    s.inject(INJ_A, 10, 300)
    files = [f for f in s.packet_files() if f.mid == INJ_A]
    p.check([f.seq for f in files] == [0, 1, 2, 3], f"sequence numbers {[f.seq for f in files]} != [0, 1, 2, 3]")
    p.check([f.size for f in files] == [964, 964, 964, 364], f"file sizes {[f.size for f in files]} != [964, 964, 964, 364]")
    got: list[int] = []
    for f in files:
        data = f.path.read_bytes()
        for msg in check_fs_header(data):
            p.check(False, f"{f.path.name}: {msg}")
        try:
            got += [struct.unpack_from("<I", x, 16)[0] for x in read_packets(data)]
        except Fail as exc:
            p.check(False, f"{f.path.name}: {exc}")
    p.check(got == list(range(10)), f"packet order across files {got}")
    hk = s.hk()
    p.check(hk["TelemetryBytesUsed"] == disk_sum(s) == 3256, f"TelemetryBytesUsed {hk['TelemetryBytesUsed']}, disk {disk_sum(s)}, expected 3256")
    p.check(hk["OpenPacketFiles"] == 1, f"OpenPacketFiles {hk['OpenPacketFiles']} != 1")
    s.inject(INJ_B, 9, 240)
    bf = [f for f in s.packet_files() if f.mid == INJ_B]
    p.check([f.size for f in bf] == [1024, 1024, 304], f"exact-fit file sizes {[f.size for f in bf]} != [1024, 1024, 304]")
    p.check([f.seq for f in bf] == [0, 1, 2], f"sequence numbers are per MID: second MID has {[f.seq for f in bf]}")
    mark = s.mark()
    s.cmd("CLOSE_FILES")
    hk = s.hk()
    p.check(hk["OpenPacketFiles"] == 0, f"OpenPacketFiles {hk['OpenPacketFiles']} after CLOSE_FILES")
    p.check(bool(s.events(mark, 27)), "no event 27 for CLOSE_FILES")
    p.raise_if()


def c_idle_file_close(ctx: "Ctx") -> None:
    s = session(ctx, quiet_table())
    p = Problems()
    s.inject(INJ_A, 1, 300)
    t_write = time.monotonic()
    p.check(s.hk()["OpenPacketFiles"] == 1, "OpenPacketFiles is not 1 after one packet")
    time.sleep(max(0.0, 8.0 - (time.monotonic() - t_write)))
    p.check(s.hk()["OpenPacketFiles"] == 1, f"the file was closed after only {time.monotonic() - t_write:.1f} s idle")
    time.sleep(max(0.0, 13.0 - (time.monotonic() - t_write)))
    hk = s.hk()
    p.check(hk["OpenPacketFiles"] == 0, f"OpenPacketFiles {hk['OpenPacketFiles']} after {time.monotonic() - t_write:.1f} s idle")
    s.inject(INJ_A, 1, 300, first_index=1)
    p.check(len([f for f in s.packet_files() if f.mid == INJ_A]) == 2, "a packet after the idle close did not go to a new file")
    p.raise_if()


def prefix_violations(universe: list[tuple[int, int, bool]], remaining: set[tuple[int, int, bool]],
                      prio: dict[int, int]) -> list[str]:
    """Missing files must be a lowest-first prefix of the order (archived first, then priority, then sequence)."""
    rank = {k: (0 if k[2] else 1, prio.get(k[0], 0), k[1]) for k in universe}
    bad: list[str] = []
    for x in universe:
        if x in remaining:
            continue
        for y in universe:
            if y in remaining and rank[y] < rank[x]:
                bad.append(f"{y[0]:04X}_{y[1]}{'.arc' if y[2] else ''} survived while "
                           f"{x[0]:04X}_{x[1]}{'.arc' if x[2] else ''} was purged")
    return bad[:4]


def c_purge_order(ctx: "Ctx") -> None:
    quota, thr = 40960, 50
    limit = quota * thr // 100
    s = session(ctx, tb.StorTable(telemetry_quota=quota, max_packet_file=4096, threshold=thr,
                                  entries=((INJ_A, 5, 1), (INJ_B, 200, 1), (INJ_C, 100, 1))))
    p = Problems()
    seen: set[tuple[int, int, bool]] = set()

    def snap() -> set[tuple[int, int, bool]]:
        cur = {(f.mid, f.seq, f.archived) for f in s.packet_files()}
        seen.update(cur)
        return cur

    def inject_tracking(mid: int, n: int) -> None:
        for i in range(0, n, 3):
            s.inject(mid, min(3, n - i), PKT, first_index=i)
            snap()

    inject_tracking(INJ_B, 10)
    s.cmd("CLOSE_FILES")
    inject_tracking(INJ_C, 5)
    s.cmd("CLOSE_FILES")
    snap()
    hk = s.hk()
    p.check(hk["FilesPurged"] == 0 and hk["PurgeCycles"] == 0, f"purged {hk['FilesPurged']} files while below the threshold")
    p.check(hk["TelemetryBytesUsed"] == 3 * FILE_FULL == disk_sum(s),
            f"TelemetryBytesUsed {hk['TelemetryBytesUsed']}, disk {disk_sum(s)}, expected {3 * FILE_FULL}")
    high = {k for k in snap() if k[0] in (INJ_B, INJ_C)}
    inject_tracking(INJ_A, 40)
    s.cmd("CLOSE_FILES")
    s.wakeup()
    time.sleep(2.0)
    rem1 = snap()
    hk = s.hk()
    prio1 = {INJ_A: 5, INJ_B: 200, INJ_C: 100}
    p.check(hk["FilesPurged"] > 0 and hk["PurgeCycles"] > 0, "nothing was purged after the threshold was exceeded")
    p.check(hk["TelemetryBytesUsed"] <= limit, f"TelemetryBytesUsed {hk['TelemetryBytesUsed']} above the threshold {limit}")
    p.check(hk["TelemetryBytesUsed"] == disk_sum(s), f"TelemetryBytesUsed {hk['TelemetryBytesUsed']} != bytes on disk {disk_sum(s)}")
    p.check(high <= rem1, f"higher-priority files were purged: {sorted(high - rem1)}")
    p.check(hk["PacketsDropped"] == 0, f"PacketsDropped {hk['PacketsDropped']} although closed files were purgeable")
    for msg in prefix_violations(sorted(seen), rem1, prio1):
        p.check(False, msg)
    universe2 = set(rem1)
    before = set(seen)
    s.cmd("SET_PRIORITY", pstr_id(INJ_B, 1))
    s.cmd("SET_PRIORITY", pstr_id(INJ_A, 250))
    inject_tracking(INJ_A, 20)
    s.cmd("CLOSE_FILES")
    s.wakeup()
    time.sleep(2.0)
    rem2 = snap()
    universe2 |= (seen - before)
    hk2 = s.hk()
    p.check(hk2["FilesPurged"] > hk["FilesPurged"], "no further purge after the priorities were changed")
    p.check(hk2["TelemetryBytesUsed"] <= limit, f"TelemetryBytesUsed {hk2['TelemetryBytesUsed']} above the threshold (phase 2)")
    for msg in prefix_violations(sorted(universe2), rem2 & universe2, {INJ_A: 250, INJ_B: 1, INJ_C: 100}):
        p.check(False, "after SET_PRIORITY: " + msg)
    p.raise_if()


def c_purge_archived_first(ctx: "Ctx") -> None:
    """An archived (.arc) packet file of a high-priority MID is purged before any .bin file."""
    seed = {f"stor/tlm/tlm_{INJ_B:04X}_00000000.arc": bytes(4000), f"stor/tlm/tlm_{INJ_B:04X}_00000001.bin": bytes(4000)}
    quota = 40960
    s = session(ctx, tb.StorTable(telemetry_quota=quota, max_packet_file=4096, threshold=50,
                                  entries=((INJ_A, 5, 1), (INJ_B, 200, 1))), seed=seed)
    p = Problems()
    hk = s.hk()
    p.check(hk["TelemetryBytesUsed"] == 8000, f"TelemetryBytesUsed {hk['TelemetryBytesUsed']} != 8000 (an .arc file counts)")
    packet_count = 18
    packets_per_file = (4096 - CFE_FS_HDR) // PKT
    assert packets_per_file > 0
    unpurged_bytes = sum(len(data) for data in seed.values()) + packet_count * PKT + ceil_div(packet_count, packets_per_file) * CFE_FS_HDR
    assert unpurged_bytes > quota // 2 and unpurged_bytes - 4000 <= quota // 2
    for i in range(0, packet_count, 3):
        s.inject(INJ_A, 3, PKT, first_index=i)
    s.cmd("CLOSE_FILES")
    s.wakeup()
    time.sleep(2.0)
    names = {f.path.name for f in s.packet_files()}
    hk = s.hk()
    arc = f"tlm_{INJ_B:04X}_00000000.arc"
    binb = f"tlm_{INJ_B:04X}_00000001.bin"
    p.check(hk["FilesPurged"] >= 1, f"FilesPurged {hk['FilesPurged']} after exceeding the threshold")
    p.check(arc not in names, f"{arc} (archived) survived the purge; archived files go first")
    if arc not in names and hk["FilesPurged"] == 1:
        p.check(binb in names, f"{binb} was purged although deleting the archived file sufficed")
    p.check(hk["TelemetryBytesUsed"] == disk_sum(s), "TelemetryBytesUsed differs from the bytes on disk")
    p.raise_if()


def c_quota_drop(ctx: "Ctx") -> None:
    quota = 16384
    s = session(ctx, tb.StorTable(telemetry_quota=quota, max_packet_file=4096, threshold=100, entries=((INJ_A, 10, 1),)))
    p = Problems()
    mark = s.mark()
    s.inject(INJ_A, 30, PKT)
    hk = s.hk()
    st = s.stats().get(INJ_A)
    p.check(hk["PacketsDropped"] > 0, "PacketsDropped is 0 after more packets than the quota can hold")
    p.check(hk["TelemetryBytesUsed"] <= quota, f"TelemetryBytesUsed {hk['TelemetryBytesUsed']} exceeds the quota")
    p.check(hk["PacketsStored"] + hk["PacketsDropped"] == 30, f"stored {hk['PacketsStored']} + dropped {hk['PacketsDropped']} != 30")
    p.check(hk["PacketsStored"] == 22, f"the new file's header counts against the quota: {hk['PacketsStored']} stored, expected 22")
    p.check(hk["FilesPurged"] == 0, "files were purged although the threshold is 100 percent")
    p.check(hk["TelemetryBytesUsed"] == disk_sum(s), f"TelemetryBytesUsed {hk['TelemetryBytesUsed']} != bytes on disk {disk_sum(s)}")
    if st is not None:
        p.check(st.dropped == hk["PacketsDropped"], f"per-entry Dropped {st.dropped} != PacketsDropped {hk['PacketsDropped']}")
    p.check(len(s.events(mark, 13)) == 1, f"event 13 occurred {len(s.events(mark, 13))} times during one drop episode")
    s.cmd("SET_PURGE_THRESHOLD", bytes([50, 0, 0, 0]))
    s.settle()
    hk_p = s.hk()
    p.check(hk_p["FilesPurged"] > 0 and hk_p["TelemetryBytesUsed"] <= quota // 2,
            f"SET_PURGE_THRESHOLD 50 left {hk_p['TelemetryBytesUsed']} bytes (FilesPurged {hk_p['FilesPurged']})")
    dropped_before = hk_p["PacketsDropped"]
    s.inject(INJ_A, 3, PKT, first_index=100)
    hk_s = s.hk()
    p.check(hk_s["PacketsDropped"] == dropped_before and hk_s["PacketsStored"] == hk_p["PacketsStored"] + 3,
            "packets were not stored again after space was freed")
    s.cmd("SET_PURGE_THRESHOLD", bytes([100, 0, 0, 0]))
    s.inject(INJ_A, 30, PKT, first_index=200)
    p.check(s.hk()["PacketsDropped"] > dropped_before, "no drops when refilling the quota")
    p.check(len(s.events(mark, 13)) == 2, f"event 13 occurred {len(s.events(mark, 13))} times over two drop episodes; expected 2")
    p.raise_if()


def c_restart_scan(ctx: "Ctx") -> None:
    seed = {f"stor/tlm/tlm_{INJ_A:04X}_00000007.bin": bytes(1000), f"stor/tlm/tlm_{INJ_B:04X}_00000003.bin": bytes(500),
            f"stor/tlm/tlm_{INJ_A:04X}_00000009.arc": bytes(200),
            "stor/files/x/y.bin": bytes(700), "stor/files/z.bin": bytes(300), "stor/files/x/partial.bin.part": bytes(50)}
    s = session(ctx, quiet_table(entries=((INJ_A, 10, 1), (INJ_B, 10, 1))), seed=seed)
    p = Problems()
    hk = s.hk()
    p.check(hk["TelemetryBytesUsed"] == 1700, f"TelemetryBytesUsed {hk['TelemetryBytesUsed']} != 1700 (packet files incl. .arc)")
    p.check(hk["FileBytesUsed"] == 1000 and hk["FileCount"] == 2, f"FileBytesUsed/FileCount {hk['FileBytesUsed']}/{hk['FileCount']} != 1000/2")
    p.check(not (s.files_dir / "x" / "partial.bin.part").exists(), "a *.part file in the file area was not deleted at start-up")
    s.inject(INJ_A, 1, 300)
    names = sorted(f.path.name for f in s.packet_files())
    p.check(f"tlm_{INJ_A:04X}_00000010.bin" in names, f"the sequence does not continue after the highest (.arc) one: {names}")
    s.inject(INJ_B, 1, 300)
    names = sorted(f.path.name for f in s.packet_files())
    p.check(f"tlm_{INJ_B:04X}_00000004.bin" in names, f"sequence numbers are per MID: no tlm_{INJ_B:04X}_00000004.bin in {names}")
    p.check(s.hk()["TelemetryBytesUsed"] == 1700 + 2 * (64 + 300), "TelemetryBytesUsed after two packets")
    p.raise_if()


def c_set_param_errors(ctx: "Ctx") -> None:
    s = session(ctx, default_boot=True)
    p = Problems()
    sh = spec.sample_hk_mid()
    evs = spec.named_tlm_mid("CFE_EVS_HK_TLM_MID")
    a = s.hk()
    mark = s.mark()
    s.cmd("SET_PRIORITY", pstr_id(0x18EF, 5))
    s.cmd("SET_DECIMATION", pstr_id(sh, 0))
    s.cmd("SET_PRIORITY", pstr_id(evs, 0))
    s.settle()
    b = s.hk()
    p.check(b["CommandErrorCounter"] == a["CommandErrorCounter"] + 3, "three invalid SET_* commands did not each count as errors")
    p.check(len(s.events(mark, 17)) == 3, f"event 17 seen {len(s.events(mark, 17))} times, expected 3")
    p.check(b["CommandCounter"] == a["CommandCounter"], "CommandCounter changed for rejected commands")
    mark = s.mark()
    s.cmd("SET_PRIORITY", pstr_id(evs, 11))
    s.cmd("SET_DECIMATION", pstr_id(evs, 4))
    s.settle()
    p.check(len(s.events(mark, 16)) == 2, "valid SET_PRIORITY/SET_DECIMATION did not each produce event 16")
    st = s.stats().get(evs)
    if st is not None:
        p.check((st.priority, st.decimation) == (11, 4), f"EVS entry priority/decimation {st.priority}/{st.decimation} != 11/4")
    c = s.hk()
    mark = s.mark()
    s.cmd("SET_PURGE_THRESHOLD", bytes([0, 0, 0, 0]))
    s.cmd("SET_PURGE_THRESHOLD", bytes([101, 0, 0, 0]))
    s.settle()
    d = s.hk()
    p.check(len(s.events(mark, 17)) == 2, "thresholds 0 and 101 did not each produce event 17")
    p.check(d["PurgeThresholdPercent"] == c["PurgeThresholdPercent"] == 80, "PurgeThresholdPercent changed by rejected values")
    s.cmd("SET_PURGE_THRESHOLD", bytes([50, 0, 0, 0]))
    s.settle()
    p.check(s.hk()["PurgeThresholdPercent"] == 50, "threshold 50 was not accepted")
    mark = s.mark()
    f0 = s.hk()
    s.n.send(spec.app_mid("STOR", "CMD"), 99)
    s.n.send(spec.app_mid("STOR", "CMD"), 0, b"\0\0\0\0")
    s.settle()
    f1 = s.hk()
    p.check(len(s.events(mark, 2)) == 1, "an invalid function code did not produce event 2")
    p.check(len(s.events(mark, 6)) == 1, "a wrong command length did not produce event 6")
    p.check(f1["CommandErrorCounter"] == f0["CommandErrorCounter"] + 2, "invalid code/length did not each count as errors")
    p.raise_if()


def c_file_add_list_delete(ctx: "Ctx") -> None:
    s = session(ctx)
    p = Problems()
    size = (s.cf / "stor_tbl.tbl").stat().st_size
    start = s.hk()
    pk = s.listing("LIST_ALL")
    p.check(len(pk) == 1 and pk[0].total == 0 and pk[0].count == 0, f"LIST_ALL of an empty file area: {pk}")
    pk = s.listing("LIST_DIR", pstr(""))
    p.check(len(pk) == 1 and pk[0].total == 0, f"LIST_DIR of the empty root: {pk}")
    mark = s.mark()
    s.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", "a/b/copy.tbl"))
    p.check(len(s.wait_event(mark, 22)) == 1, "ADD_FILE did not produce event 22")
    dest = s.files_dir / "a" / "b" / "copy.tbl"
    p.check(dest.is_file() and dest.read_bytes() == (s.cf / "stor_tbl.tbl").read_bytes(), "the added file is missing or differs")
    hk = s.hk()
    p.check(hk["FileCount"] == 1 and hk["FileBytesUsed"] == size, f"FileCount/FileBytesUsed {hk['FileCount']}/{hk['FileBytesUsed']}")
    mark = s.mark()
    pk = s.listing("LIST_DIR", pstr("a"))
    p.check(len(pk) == 1 and pk[0].total == 1 and pk[0].entries == (("b/", 0),), f"LIST_DIR a: {pk}")
    p.check(bool(s.events(mark, 26)), "LIST_DIR did not produce event 26")
    pk = s.listing("LIST_ALL")
    p.check(len(pk) == 1 and pk[0].total == 1 and pk[0].entries == (("a/b/copy.tbl", size),), f"LIST_ALL: {pk}")
    for i in range(5):
        s.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", f"m/f{i}.tbl"))
    pk = s.listing("LIST_DIR", pstr("m"))
    p.check([x.index for x in pk] == [0, 1] and all(x.total == 5 for x in pk) and [x.count for x in pk] == [4, 1],
            f"LIST_DIR m packets {[(x.index, x.count, x.total) for x in pk]}")
    p.check(sorted(nm for x in pk for nm, _ in x.entries) == [f"f{i}.tbl" for i in range(5)], "LIST_DIR m names")
    pk = s.listing("LIST_ALL")
    alln = [nm for x in pk for nm, _ in x.entries]
    p.check(len(pk) == 2 and all(x.total == 6 for x in pk) and
            sorted(alln) == sorted(["a/b/copy.tbl"] + [f"m/f{i}.tbl" for i in range(5)]), f"LIST_ALL after six adds: {alln}")
    m_idx = [i for i, nm in enumerate(alln) if nm.startswith("m/")]
    p.check(bool(m_idx) and m_idx == list(range(m_idx[0], m_idx[0] + len(m_idx))),
            "LIST_ALL entries of one directory are not contiguous (depth-first order)")
    mark = s.mark()
    a = s.hk()
    s.cmd("LIST_DIR", pstr("nosuchdir"))
    p.check(bool(s.wait_event(mark, 25)), "LIST_DIR of a missing directory did not produce event 25")
    mark = s.mark()
    s.cmd("DELETE_FILE", pstr("a/b/copy.tbl"))
    p.check(len(s.wait_event(mark, 24)) == 1, "DELETE_FILE did not produce event 24")
    p.check(not dest.exists(), "the deleted file still exists")
    b = s.hk()
    p.check(b["FileCount"] == a["FileCount"] - 1 and b["FileBytesUsed"] == a["FileBytesUsed"] - size,
            "FileCount/FileBytesUsed after DELETE_FILE")
    mark = s.mark()
    s.cmd("DELETE_FILE", pstr("a/b/copy.tbl"))
    s.cmd("DELETE_FILE", pstr("a"))
    s.settle()
    c = s.hk()
    p.check(len(s.events(mark, 23)) == 2, "deleting a missing file and a directory did not each produce event 23")
    p.check(c["CommandErrorCounter"] == b["CommandErrorCounter"] + 2, "failed deletes did not each count as command errors")
    p.check((c["CommandCounter"] - start["CommandCounter"]) % 256 == 13,
            f"CommandCounter rose by {(c['CommandCounter'] - start['CommandCounter']) % 256}, expected 13 accepted commands")
    p.check((s.files_dir / "a").is_dir(), "DELETE_FILE removed a directory")
    p.raise_if()


def c_file_path_rejection(ctx: "Ctx") -> None:
    s = session(ctx)
    p = Problems()
    a = s.hk()
    mark = s.mark()
    for dest in ("../x", "/abs", "a/../b"):
        s.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", dest))
    s.settle()
    b = s.hk()
    p.check(len(s.events(mark, 18)) == 3, f"event 18 seen {len(s.events(mark, 18))} times for three bad destinations")
    p.check(b["CommandErrorCounter"] == a["CommandErrorCounter"] + 3, "rejected destinations did not each count as errors")
    p.check(b["FileCount"] == 0, f"FileCount {b['FileCount']} after rejections")
    mark = s.mark()
    s.cmd("DELETE_FILE", pstr("../x"))
    s.cmd("LIST_DIR", pstr(".."))
    s.cmd("LIST_DIR", pstr("/etc"))
    s.settle()
    p.check(len(s.events(mark, 18)) == 3, f"event 18 seen {len(s.events(mark, 18))} times for bad DELETE_FILE/LIST_DIR paths")
    stray = [q for q in (s.cf / "x", s.cf / "stor" / "x", s.cf / "b", s.cf / "abs", s.files_dir / "abs") if q.exists()]
    p.check(not stray, f"files were written outside the file area: {stray}")
    p.raise_if()


def c_file_exists_rejection(ctx: "Ctx") -> None:
    s = session(ctx)
    p = Problems()
    s.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", "dup.tbl"))
    a = s.hk()
    p.check(a["FileCount"] == 1, f"FileCount {a['FileCount']} after the first add")
    first = (s.files_dir / "dup.tbl").read_bytes() if (s.files_dir / "dup.tbl").is_file() else b""
    mark = s.mark()
    s.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", "dup.tbl"))
    s.settle()
    b = s.hk()
    p.check(len(s.events(mark, 20)) == 1, "adding to an existing destination did not produce event 20")
    p.check(b["CommandErrorCounter"] == a["CommandErrorCounter"] + 1, "the rejected add did not count as a command error")
    p.check(b["FileCount"] == 1 and b["FileBytesUsed"] == a["FileBytesUsed"], "FileCount/FileBytesUsed changed by the rejected add")
    p.check((s.files_dir / "dup.tbl").read_bytes() == first if (s.files_dir / "dup.tbl").is_file() else False,
            "the existing file was modified")
    mark = s.mark()
    s.cmd("ADD_FILE", add_payload("/cf/nosuchsource.bin", "other.tbl"))
    p.check(bool(s.wait_event(mark, 19)), "a missing source file did not produce event 19")
    p.check(not (s.files_dir / "other.tbl").exists(), "a destination exists for a missing source")
    p.raise_if()


def c_file_quota_rejection(ctx: "Ctx") -> None:
    s = session(ctx)
    p = Problems()
    quota = 1048576
    big = s.cf / "big.bin"
    big.write_bytes(bytes(quota + 1))
    mark = s.mark()
    s.cmd("ADD_FILE", add_payload("/cf/big.bin", "big/dest.bin"))
    p.check(len(s.wait_event(mark, 21)) == 1, "an add beyond FileQuotaBytes did not produce event 21")
    hk = s.hk()
    p.check(hk["FileAddsRejected"] == 1 and hk["CommandErrorCounter"] == 1,
            f"FileAddsRejected/CommandErrorCounter {hk['FileAddsRejected']}/{hk['CommandErrorCounter']} != 1/1")
    left = [str(x.relative_to(s.files_dir)) for x in s.files_dir.rglob("*") if x.is_file()] if s.files_dir.is_dir() else []
    p.check(not left, f"partial files left in the file area: {left}")
    small = (s.cf / "stor_tbl.tbl").stat().st_size
    mark = s.mark()
    s.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", "small.tbl"))
    if len(s.wait_event(mark, 22)) != 1:
        raise Fail("the small file add was not acknowledged before the exact-quota add")
    big.write_bytes(bytes(quota - small))
    mark = s.mark()
    s.cmd("ADD_FILE", add_payload("/cf/big.bin", "fill.bin"))
    p.check(len(s.wait_event(mark, 22)) == 1, "a file that exactly fills FileQuotaBytes was not accepted")
    p.check(s.hk()["FileBytesUsed"] == quota, "FileBytesUsed does not equal the quota after filling it")
    fill = s.files_dir / "fill.bin"
    p.check(fill.is_file() and fill.stat().st_size == quota - small,
            "the exact-quota file is missing or has the wrong size")
    big.write_bytes(bytes(1))
    mark = s.mark()
    s.cmd("ADD_FILE", add_payload("/cf/big.bin", "one-more.bin"))
    p.check(len(s.wait_event(mark, 21)) == 1, "one byte beyond the quota was not rejected with event 21")
    p.check(s.hk()["FileAddsRejected"] == 2, "FileAddsRejected is not 2")
    p.check(not (s.files_dir / "one-more.bin").exists(), "a file exists for the rejected add")
    p.raise_if()


def c_reset_counters(ctx: "Ctx") -> None:
    s = session(ctx, quiet_table(file_quota=2048, entries=((INJ_A, 10, 1), (INJ_B, 20, 2))))
    p = Problems()
    s.inject(INJ_A, 3, 300)
    s.inject(INJ_B, 4, 300)
    s.cmd("NOOP")
    s.cmd("SET_PRIORITY", pstr_id(INJ_A, 42))
    s.cmd("SET_PRIORITY", pstr_id(0x18EF, 2))
    s.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", "keep.tbl"))
    (s.cf / "big.bin").write_bytes(bytes(3000))
    s.cmd("ADD_FILE", add_payload("/cf/big.bin", "toobig.bin"))
    pre = s.hk()
    p.check(pre["PacketsStored"] == 5 and pre["PacketsDecimated"] == 2, f"setup: stored/decimated {pre['PacketsStored']}/{pre['PacketsDecimated']}")
    mark = s.mark()
    s.cmd("RESET_COUNTERS")
    p.check(bool(s.wait_event(mark, 4)), "RESET_COUNTERS did not produce event 4")
    post = s.hk()
    for k in ("CommandCounter", "CommandErrorCounter", "PacketsStored", "PacketsDecimated", "PacketsDropped", "FilesPurged",
              "PurgeCycles", "FileAddsRejected", "XfersCompleted", "XfersFailed", "XferSegmentsSent", "XferSegmentsReceived",
              "FilesReceived", "RxErrors"):
        p.check(post[k] == 0, f"{k} is {post[k]} after RESET_COUNTERS")
    for k in ("TelemetryBytesUsed", "FileBytesUsed", "FileCount", "OpenPacketFiles", "PurgeThresholdPercent",
              "TelemetryQuotaBytes", "ActiveEntries"):
        p.check(post[k] == pre[k], f"{k} changed {pre[k]} -> {post[k]} by RESET_COUNTERS")
    st = s.stats()
    for mid in (INJ_A, INJ_B):
        o = st.get(mid)
        if p.check(o is not None, f"no statistics entry for {mid:#06x}"):
            assert o is not None
            p.check((o.received, o.stored, o.decimated, o.dropped) == (0, 0, 0, 0), f"{mid:#06x} statistics not zero: {o}")
    a = st.get(INJ_A)
    if a is not None:
        p.check(a.priority == 42, f"runtime priority {a.priority} changed by RESET_COUNTERS (was 42)")
    p.raise_if()


def c_table_validation_live(ctx: "Ctx") -> None:
    """A table that fails validation never becomes active (C.4)."""
    s = session(ctx, quiet_table(entries=((INJ_A, 10, 1),)))
    p = Problems()
    before = s.hk()
    try:
        s.n.load_table("STOR", tb.StorTable(threshold=0, entries=((INJ_A, 10, 1),)).pack(), expect_valid=False)
    except Fail as exc:
        p.check(False, str(exc))
    try:
        s.n.load_table("STOR", tb.StorTable(entries=((INJ_A, 10, 1), (INJ_A, 20, 1))).pack(), expect_valid=False)
    except Fail as exc:
        p.check(False, str(exc))
    after = s.hk()
    p.check(after["PurgeThresholdPercent"] == before["PurgeThresholdPercent"] == 100 and after["ActiveEntries"] == 1,
            f"a rejected table changed the active one: threshold {after['PurgeThresholdPercent']}, entries {after['ActiveEntries']}")
    p.raise_if()


# ============================================================================ S2

@dataclass(frozen=True)
class Segment:
    transfer_id: int
    src: int
    dst: int
    area: int
    index: int
    count: int
    file_size: int
    file_crc: int
    offset: int
    length: int
    dest_path: str
    data: bytes
    raw_data: bytes


def parse_segment(pkt: bytes, hdr: int) -> Segment:
    need = hdr + spec.XFER_SEG_SIZE
    if len(pkt) < need:
        raise Fail(f"a transfer segment packet is {len(pkt)} bytes; the full structure is {need}")
    v = struct.unpack_from(spec.XFER_SEG_FMT, pkt, hdr)
    length = v[10]
    data = v[13]
    return Segment(v[0], v[1], v[2], v[3], v[5], v[6], v[7], v[8], v[9], length,
                   v[12].split(b"\0", 1)[0].decode("latin-1"), data[:min(length, len(data))], data)


def judge_segments(segs: list[Segment], content: bytes, *, transfer_id: int, src: int, dst: int, area: int,
                   dest_path: str) -> list[str]:
    """Segment fields of S2.1 for one complete transfer of `content`."""
    p = Problems()
    want_count = max(1, ceil_div(len(content), spec.STOR_XFER_DATA_LEN))
    by_index: dict[int, Segment] = {}
    for sg in segs:
        by_index.setdefault(sg.index, sg)
    p.check(sorted(by_index) == list(range(want_count)), f"segment indices {sorted(by_index)} != 0..{want_count - 1}")
    crc = zlib.crc32(content) & 0xFFFFFFFF
    for i, sg in sorted(by_index.items()):
        exp_len = min(spec.STOR_XFER_DATA_LEN, len(content) - i * spec.STOR_XFER_DATA_LEN) if content else 0
        p.check((sg.transfer_id, sg.src, sg.dst, sg.area) == (transfer_id, src, dst, area),
                f"segment {i}: TransferId/SrcNode/DstNode/Area {sg.transfer_id}/{sg.src}/{sg.dst}/{sg.area} != {transfer_id}/{src}/{dst}/{area}")
        p.check(sg.count == want_count, f"segment {i}: SegmentCount {sg.count} != {want_count}")
        p.check(sg.file_size == len(content) and sg.file_crc == crc,
                f"segment {i}: FileSize/FileCrc {sg.file_size}/{sg.file_crc:#010x} != {len(content)}/{crc:#010x}")
        p.check(sg.offset == i * spec.STOR_XFER_DATA_LEN, f"segment {i}: Offset {sg.offset}")
        p.check(sg.length == exp_len, f"segment {i}: DataLength {sg.length} != {exp_len}")
        p.check(sg.dest_path == dest_path, f"segment {i}: DestPath {sg.dest_path!r} != {dest_path!r}")
        p.check(not any(sg.raw_data[sg.length:]), f"segment {i}: unused Data bytes are not 0")
    data = b"".join(by_index[i].data for i in sorted(by_index))
    p.check(data == content, f"reassembled {len(data)} bytes differ from the {len(content)}-byte source")
    p.check(zlib.crc32(data) & 0xFFFFFFFF == crc, "reassembled CRC-32 differs from FileCrc")
    return p.items


def transfer_payload(area: int, dest: int, action: int, path: str, dest_path: str = "") -> bytes:
    return bytes([area, dest, action, 0]) + pstr(path) + pstr(dest_path)


def xfer_wait_done(s: Stor, mark: int, timeout: float) -> tuple[list, list]:
    """Wait for STOR_XFER_DONE_INF_EID (31) or a failure (32/33); returns (done, failed) events."""
    done: list = []
    failed: list = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        done = s.events(mark, 31)
        failed = s.events(mark, 32) + s.events(mark, 33)
        if done or failed:
            break
        time.sleep(0.2)
    return done, failed


def c_ground_transfer(ctx: "Ctx") -> None:
    content = bytes((i * 31 + 7) & 0xFF for i in range(5000))
    s = session(ctx, seed={"xfer_src.bin": content})
    p = Problems()
    n = s.n
    s.cmd("ADD_FILE", add_payload("/cf/xfer_src.bin", "x/data.bin"))
    before = s.hk()
    p.check(before["FileCount"] == 1 and before["FileBytesUsed"] == len(content), "the source was not added to the file area")
    mark = s.mark()
    s.cmd("TRANSFER", transfer_payload(spec.XFER_AREA["FILE"], 0, spec.XFER_ACTION["DELETE"], "x/data.bin"))
    p.check(bool(s.wait_event(mark, 29, 4.0)), "TRANSFER did not produce STOR_XFER_START_INF_EID (29)")
    done, failed = xfer_wait_done(s, mark, 10.0)
    p.check(bool(done) and not failed, f"the ground transfer did not complete with event 31 ({[e.text for e in failed]})")
    time.sleep(0.5)
    xmid = spec.app_mid("STOR", "XFER_TLM")
    pkts = [d.data for d in n.g.since(mark, xmid)]
    if not p.check(bool(pkts), f"no STOR_XFER_TLM_MID ({xmid:#06x}) packets arrived on UDP 2234"):
        p.raise_if()
    try:
        segs = [parse_segment(x, spec.TLM_HDR) for x in pkts]
        for item in judge_segments(segs, content, transfer_id=1, src=1, dst=0, area=0, dest_path="x/data.bin"):
            p.check(False, item)
    except Fail as exc:
        p.check(False, str(exc))
    after = s.hk()
    p.check(not (s.files_dir / "x" / "data.bin").exists(), "the source still exists after a transfer with DELETE")
    p.check(after["FileCount"] == 0 and after["FileBytesUsed"] == 0, f"FileCount/FileBytesUsed {after['FileCount']}/{after['FileBytesUsed']} after DELETE")
    p.check(after["XfersCompleted"] == 1 and after["XferLastStatus"] == 0 and after["XferState"] == 0,
            f"XfersCompleted/XferLastStatus/XferState {after['XfersCompleted']}/{after['XferLastStatus']}/{after['XferState']}")
    p.check(after["XferSegmentsSent"] == 10 and after["XferActiveId"] == 1,
            f"XferSegmentsSent {after['XferSegmentsSent']} != 10 or XferActiveId {after['XferActiveId']} != 1")
    p.raise_if()


def c_tlm_transfer_archive(ctx: "Ctx") -> None:
    s = session(ctx, quiet_table(entries=((INJ_A, 10, 1),)))
    p = Problems()
    n = s.n
    s.inject(INJ_A, 5, 300)
    s.cmd("CLOSE_FILES")
    name = f"tlm_{INJ_A:04X}_00000000.bin"
    src = s.tlm_dir / name
    if not src.is_file():
        raise Fail(f"no closed packet file {name} to transfer")
    content = src.read_bytes()
    before = s.hk()
    mark = s.mark()
    s.cmd("TRANSFER", transfer_payload(spec.XFER_AREA["TLM"], 0, spec.XFER_ACTION["MARK_ARCHIVED"], name))
    done, failed = xfer_wait_done(s, mark, 10.0)
    p.check(bool(done) and not failed, "the packet-file transfer did not complete with event 31")
    time.sleep(0.5)
    segs = [parse_segment(d.data, spec.TLM_HDR) for d in n.g.since(mark, spec.app_mid("STOR", "XFER_TLM"))]
    for item in judge_segments(segs, content, transfer_id=1, src=1, dst=0, area=1, dest_path=name):
        p.check(False, item)
    arc = s.tlm_dir / name.replace(".bin", ".arc")
    p.check(arc.is_file() and not src.exists(), f"MARK_ARCHIVED did not rename {name} to {arc.name}")
    after = s.hk()
    p.check(after["TelemetryBytesUsed"] == before["TelemetryBytesUsed"], "MARK_ARCHIVED changed TelemetryBytesUsed")
    p.raise_if()


class Relay:
    """Relays segments from cpu1's TLMO to cpu2's CMDI and acknowledgements from cpu2's TLMO to cpu1's CMDI,
    each wrapped in a CMDI frame, at most 4 frames per 120 ms so no software bus message limit is exceeded."""

    def __init__(self, a: Node, b: Node) -> None:
        self.a, self.b = a, b
        self.lock = threading.Lock()
        self.queue: list[tuple[Node, bytes]] = []
        self.relayed = {spec.XFER_SEG_MID: 0, spec.XFER_ACK_MID: 0}
        self.running = True
        a.g.listeners.append(lambda d: self._take(d, spec.XFER_SEG_MID, b))
        b.g.listeners.append(lambda d: self._take(d, spec.XFER_ACK_MID, a))
        self.thread = threading.Thread(target=self._run, daemon=True, name="relay")
        self.thread.start()

    def _take(self, d: Datagram, mid: int, to: Node) -> None:
        if d.mid == mid:
            with self.lock:
                self.queue.append((to, d.data))

    def _run(self) -> None:
        while self.running:
            with self.lock:
                batch, self.queue = self.queue[:4], self.queue[4:]
            for to, pkt in batch:
                try:
                    to.send_raw(to.frame(pkt))
                    self.relayed[fr.packet_mid(pkt)] += 1
                except Fail:
                    pass
            time.sleep(0.12)

    def stop(self) -> None:
        self.running = False
        self.thread.join(timeout=2)


def peer_session(ctx: "Ctx", content: bytes) -> tuple[Stor, Stor, Relay]:
    key = "peer-transfer"
    cached = ctx.cache.get(key)
    if cached is not None and all(st.n.alive() for st in cached[:2]):
        return cached
    nodes = boot(ctx, (1, 2), seed={1: {"xfer_src.bin": content}, 2: {}}, quiet_stor=True)
    s1, s2 = Stor(nodes[1]), Stor(nodes[2])
    for st, mid in ((s1, spec.XFER_SEG_MID), (s2, spec.XFER_ACK_MID)):
        mark = st.mark()
        st.n.ensure_command_path()
        st.n.cmd("TLMO", "ADD_STREAM", struct.pack("<IB3x", mid, 3))
        if not st.n.wait_event(mark, "TLMO", spec.EID["TLMO"]["ADD_STREAM_INF"], 4.0):
            raise Fail(f"{st.n.cpu}: TLMO ADD_STREAM {mid:#06x} produced no TLMO_ADD_STREAM_INF_EID")
    relay = Relay(s1.n, s2.n)
    ctx.cache[key] = (s1, s2, relay)
    ctx.on_session_end(relay.stop)
    return s1, s2, relay


PEER_CONTENT = bytes((i * 13 + 5) & 0xFF for i in range(3000))


def c_peer_transfer(ctx: "Ctx") -> None:
    s1, s2, relay = peer_session(ctx, PEER_CONTENT)
    p = Problems()
    s1.cmd("ADD_FILE", add_payload("/cf/xfer_src.bin", "out/peer.bin"))
    h2 = s2.hk()
    mark = s1.mark()
    s1.cmd("TRANSFER", transfer_payload(spec.XFER_AREA["FILE"], 2, spec.XFER_ACTION["DELETE"], "out/peer.bin", "in/copy.bin"))
    done, failed = xfer_wait_done(s1, mark, 25.0)
    p.check(bool(done) and not failed, f"the transfer to node 2 did not complete with event 31 on cpu1 "
                                       f"({[e.text for e in failed]}; relayed {relay.relayed})")
    dest = s2.files_dir / "in" / "copy.bin"
    p.check(dest.is_file() and dest.read_bytes() == PEER_CONTENT,
            "the file is not byte-identical under cf/stor/files/in/copy.bin in cpu2's run directory")
    p.check(not (s1.files_dir / "out" / "peer.bin").exists(), "the source still exists on cpu1 after DELETE")
    a1 = s1.hk()
    a2 = s2.hk()
    p.check(a1["XfersCompleted"] == 1 and a1["XferLastStatus"] == 0, f"cpu1 XfersCompleted/XferLastStatus {a1['XfersCompleted']}/{a1['XferLastStatus']}")
    p.check(a2["FilesReceived"] == h2["FilesReceived"] + 1 and a2["FileCount"] == h2["FileCount"] + 1,
            f"cpu2 FilesReceived/FileCount {a2['FilesReceived']}/{a2['FileCount']} (before {h2['FilesReceived']}/{h2['FileCount']})")
    p.check(a2["RxState"] == 0, f"cpu2 RxState {a2['RxState']} after the transfer")
    p.check(not list(s2.files_dir.rglob("*.part")), "a .part file remains on cpu2")
    p.raise_if()


def c_peer_exists_nak(ctx: "Ctx") -> None:
    s1, s2, relay = peer_session(ctx, PEER_CONTENT)
    p = Problems()
    s2.cmd("ADD_FILE", add_payload("/cf/stor_tbl.tbl", "in/exists.bin"))
    s1.cmd("ADD_FILE", add_payload("/cf/xfer_src.bin", "out/again.bin"))
    h1, h2 = s1.hk(), s2.hk()
    existing = (s2.files_dir / "in" / "exists.bin").read_bytes() if (s2.files_dir / "in" / "exists.bin").is_file() else b""
    mark1, mark2 = s1.mark(), s2.mark()
    s1.cmd("TRANSFER", transfer_payload(spec.XFER_AREA["FILE"], 2, spec.XFER_ACTION["DELETE"], "out/again.bin", "in/exists.bin"))
    done, failed = xfer_wait_done(s1, mark1, 20.0)
    p.check(bool(failed) and not done, "a transfer to an existing destination did not fail with STOR_XFER_FAIL_ERR_EID")
    a1, a2 = s1.hk(), s2.hk()
    p.check(a1["XfersFailed"] == h1["XfersFailed"] + 1 and a1["XferLastStatus"] == spec.XFER_STATUS["EXISTS"],
            f"cpu1 XfersFailed/XferLastStatus {a1['XfersFailed']}/{a1['XferLastStatus']} (EXISTS is 1)")
    p.check((s1.files_dir / "out" / "again.bin").is_file(), "the source was not left unchanged on cpu1")
    p.check(a2["RxErrors"] == h2["RxErrors"] + 1 and bool(s2.events(mark2, 35)), "cpu2 did not count RxErrors or send event 35")
    p.check((s2.files_dir / "in" / "exists.bin").read_bytes() == existing if existing else False,
            "the existing destination on cpu2 was modified")
    p.raise_if()


def c_transfer_abort_and_rejections(ctx: "Ctx") -> None:
    content = bytes(range(256)) * 80
    s = session(ctx, quiet_table(entries=((INJ_A, 10, 1),)), seed={"big_src.bin": content})
    p = Problems()
    s.cmd("ADD_FILE", add_payload("/cf/big_src.bin", "d/big.bin"))
    s.inject(INJ_A, 1, 300)
    open_name = f"tlm_{INJ_A:04X}_00000000.bin"
    base = s.hk()
    mark = s.mark()
    rejects = [
        ("Area 2", transfer_payload(2, 0, 0, "d/big.bin")),
        ("SuccessAction 3", transfer_payload(0, 0, 3, "d/big.bin")),
        ("Destination is the own node", transfer_payload(0, 1, 0, "d/big.bin", "x.bin")),
        ("a path with ..", transfer_payload(0, 0, 0, "../big.bin")),
        ("a TLM path that is not a packet file name", transfer_payload(1, 0, 0, "big.bin")),
        ("a missing source", transfer_payload(0, 0, 0, "d/none.bin")),
        ("a directory source", transfer_payload(0, 0, 0, "d")),
        ("an open packet file", transfer_payload(1, 0, 0, open_name)),
        ("a peer transfer with an empty DestPath", transfer_payload(0, 2, 0, "d/big.bin", "")),
    ]
    for _label, payload in rejects:
        s.cmd("TRANSFER", payload)
    s.settle()
    a = s.hk()
    n_err = len(s.events(mark, 30))
    p.check(a["CommandErrorCounter"] - base["CommandErrorCounter"] == len(rejects),
            f"{a['CommandErrorCounter'] - base['CommandErrorCounter']} of {len(rejects)} invalid TRANSFER commands counted as errors")
    p.check(n_err == len(rejects), f"STOR_XFER_ERR_EID (30) sent {n_err} times for {len(rejects)} invalid TRANSFER commands")
    p.check(a["XferState"] == 0, "a rejected TRANSFER started a transfer")
    mark = s.mark()
    s.cmd("TRANSFER", transfer_payload(0, 3, 0, "d/big.bin", "far/big.bin"))
    s.cmd("TRANSFER", transfer_payload(0, 3, 0, "d/big.bin", "far/again.bin"))
    p.check(bool(s.wait_event(mark, 29, 2.2)), "the accepted TRANSFER produced no start event 29")
    p.check(len(s.wait_event(mark, 30, 2.2)) == 1, "a TRANSFER while one is active was not rejected exactly once with event 30")
    busy = s.hk()
    p.check(busy["CommandCounter"] == (a["CommandCounter"] + 1) % 256
            and busy["CommandErrorCounter"] == (a["CommandErrorCounter"] + 1) % 256,
            "accepted and busy TRANSFER commands did not count exactly one success and one error")
    wait_until(lambda: s.hk()["XferState"] == 2, 8.0)
    mid = s.hk()
    p.check(mid["XferState"] == 2, f"XferState {mid['XferState']} after the last segment to a peer (AWAITING_ACK is 2)")
    mark = s.mark()
    s.cmd("ABORT_TRANSFER")
    p.check(bool(s.wait_event(mark, 33)), "ABORT_TRANSFER did not produce STOR_XFER_ABORT_INF_EID (33)")
    p.check(not s.events(mark, 32), "ABORT_TRANSFER produced XFER_FAIL_ERR instead of XFER_ABORT_INF")
    after = s.hk()
    p.check(after["XfersFailed"] == mid["XfersFailed"] + 1 and after["XferLastStatus"] == spec.XFER_STATUS["ABORTED"]
            and after["XferState"] == 0, f"after ABORT: XfersFailed {after['XfersFailed']}, XferLastStatus {after['XferLastStatus']}, "
                                         f"XferState {after['XferState']}")
    mark = s.mark()
    s.cmd("ABORT_TRANSFER")
    p.check(bool(s.wait_event(mark, 30, 2.2)) and s.hk()["CommandErrorCounter"] == (after["CommandErrorCounter"] + 1) % 256,
            "ABORT_TRANSFER with no transfer active was not rejected with event 30")
    p.check((s.files_dir / "d" / "big.bin").is_file(), "an aborted transfer applied a success action")
    p.raise_if()


def parse_file_info(packet: bytes) -> dict[str, int | str]:
    expected = spec.TLM_HDR + spec.STOR_FILE_INFO_SIZE
    if len(packet) != expected:
        raise Fail(f"FILE_INFO reply has {len(packet)} bytes, expected exactly {expected}")
    area, raw_path, size, crc = struct.unpack_from(spec.STOR_FILE_INFO_FMT, packet, spec.TLM_HDR)
    if area not in (0, 1) or any(packet[spec.TLM_HDR + 1:spec.TLM_HDR + 4]) or b"\0" not in raw_path:
        raise Fail("FILE_INFO reply has invalid Area, nonzero spares or unterminated Path")
    return {"Area": area, "Path": raw_path.split(b"\0", 1)[0].decode("latin-1"), "Size": size, "Crc": crc}


def file_info(n: Node, area: int, path: str) -> dict[str, int | str]:
    got = n.request(lambda: n.cmd("STOR", "FILE_INFO", bytes((area, 0, 0, 0)) + pstr(path), gap=0),
                    spec.app_mid("STOR", "FILE_INFO_TLM", n.id), timeout=2)
    if not got:
        raise Fail(f"{n.cpu}: FILE_INFO {path!r} produced no telemetry within 2 s")
    return parse_file_info(got[0].data)


def check_priority_bytes(p: Problems, hk: dict[str, int], expected: dict[int, int] | None = None) -> None:
    actual = [hk[f"PriorityBytes{i}"] for i in range(256)]
    p.check(sum(actual) == hk["TelemetryBytesUsed"], f"PriorityBytes sum {sum(actual)} differs from usage {hk['TelemetryBytesUsed']}")
    if expected is not None:
        wanted = [expected.get(i, 0) for i in range(256)]
        p.check(actual == wanted, f"PriorityBytes nonzero rows {[(i, x) for i, x in enumerate(actual) if x]} != {expected}")


def c_quota_file_info(ctx: "Ctx") -> None:
    contents = {"empty.bin": b"", "digits.bin": b"123456789", "binary.bin": bytes(range(256)) + b"\0\xff"}
    table = quiet_table(telemetry_quota=8192, file_quota=4096, entries=((INJ_A, 10, 1), (INJ_B, 20, 1)))
    s = session(ctx, table, seed=contents)
    p = Problems()
    for path, content in contents.items():
        s.cmd("ADD_FILE", add_payload("/cf/" + path, path))
        target = s.files_dir / path
        before = target.read_bytes()
        reply = file_info(s.n, 0, path)
        p.check(reply == {"Area": 0, "Path": path, "Size": len(content), "Crc": zlib.crc32(content) & 0xFFFFFFFF},
                f"FILE_INFO size/CRC/identity mismatch for {path}: {reply}")
        p.check(target.read_bytes() == before, f"FILE_INFO mutated {path}")
    initial = s.hk()
    s.cmd("SET_QUOTA", struct.pack("<II", 2048, 0))
    s.cmd("SET_QUOTA", struct.pack("<II", 1023, 2048))
    rejected = s.hk()
    p.check((rejected["TelemetryQuotaBytes"], rejected["FileQuotaBytes"]) == (8192, 4096), "invalid quota changes neither value")
    p.check(rejected["CommandErrorCounter"] == initial["CommandErrorCounter"] + 2, "two invalid quota commands counted exactly")
    s.cmd("SET_QUOTA", struct.pack("<II", 1024, 1))
    lower = s.hk()
    p.check(lower["FileBytesUsed"] == initial["FileBytesUsed"] and lower["FileQuotaBytes"] == 1,
            "quota below usage keeps existing files")
    s.cmd("ADD_FILE", add_payload("/cf/digits.bin", "over.bin"))
    p.check(not (s.files_dir / "over.bin").exists(), "reduced file quota immediately rejects new addition")
    s.inject(INJ_A, 2, 700)
    p.check(s.hk()["PacketsDropped"] == 1, "reduced telemetry quota immediately drops second packet")
    s.n.load_table("STOR", table.pack())
    restored = s.hk()
    p.check((restored["TelemetryQuotaBytes"], restored["FileQuotaBytes"]) == (8192, 4096), "activation restores both quotas")
    s.inject(INJ_B, 1, 300)
    s.cmd("CLOSE_FILES")
    before = s.hk()
    check_priority_bytes(p, before, {10: 764, 20: 364})
    name = f"tlm_{INJ_A:04X}_00000000.bin"
    packet_file = s.tlm_dir / name
    content = packet_file.read_bytes()
    reply = file_info(s.n, 1, name)
    p.check((reply["Size"], reply["Crc"]) == (len(content), zlib.crc32(content) & 0xFFFFFFFF), "FILE_INFO includes packet file header in CRC")
    s.cmd("SET_PRIORITY", pstr_id(INJ_A, 200))
    check_priority_bytes(p, s.hk(), {200: 764, 20: 364})
    mark = s.mark()
    s.cmd("TRANSFER", transfer_payload(1, 0, spec.XFER_ACTION["MARK_ARCHIVED"], name))
    done, failed = xfer_wait_done(s, mark, 4)
    p.check(bool(done) and not failed, "archive transfer completes")
    check_priority_bytes(p, s.hk(), {200: 764, 20: 364})
    replaced = quiet_table(telemetry_quota=8192, file_quota=4096, entries=((INJ_B, 30, 1),))
    s.n.load_table("STOR", replaced.pack())
    check_priority_bytes(p, s.hk(), {0: 764, 30: 364})
    s.cmd("SET_QUOTA", struct.pack("<II", 1024, 4096))
    # Archived unknown-priority data is selected first by the ordinary purge rule.
    check_priority_bytes(p, s.hk(), {30: 364})
    mark = s.mark()
    s.cmd("TRANSFER", transfer_payload(1, 0, spec.XFER_ACTION["DELETE"], f"tlm_{INJ_B:04X}_00000000.bin"))
    done, failed = xfer_wait_done(s, mark, 4)
    p.check(bool(done) and not failed, "telemetry DELETE transfer completes")
    check_priority_bytes(p, s.hk(), {})
    p.raise_if()
