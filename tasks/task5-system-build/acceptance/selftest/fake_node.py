"""A fake processor for self-tests of the live checks (not a reference of the flight software).

It implements only what the live S3, S4 and node-level checks observe from outside: CMDI frame ingest on
UDP 1234 + node - 1 (validation order, counters, events, SET_MAX_AGE, SET_ENABLE_MASK), TLMO prioritised
output on UDP 2234 + node - 1 (classes, queue, eviction, budget, streams, class map, table updates), cFE
Table Services LOAD/VALIDATE/ACTIVATE for CMDI.ValTable and TLMO.PriorityTable, SAMPLE_APP NOOP and
housekeeping, periodic housekeeping of the apps the checks expect, console event lines and a RAM disk
directory placed as the POSIX OSAL places it. Run as an executable named core-cpuN (see live_fake.py).
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import time
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK))

import frames as fr  # noqa: E402
import spec  # noqa: E402
import tables as tb  # noqa: E402

TICK = 0.1
MUTANT = os.environ.get("T5_FAKE_MUTANT", "")  # seeded fault for the self-test of the checks


class FakeNode:
    def __init__(self, node: int) -> None:
        self.node = node
        self.t0 = time.monotonic()
        self.cf = Path.cwd() / "cf"
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind((spec.HOST, spec.cmdi_port(node)))
        self.rx.setblocking(False)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tlm_dest = (spec.HOST, spec.tlmo_port(node))
        self.cmdi = tb.CmdiTable.unpack(self.table_body("CMDI"))
        self.max_age, self.mask = self.cmdi.max_age, self.cmdi.mask
        self.cmdi_cc = self.cmdi_cec = 0
        self.frames = {"FramesReceived": 0, "FramesAccepted": 0, "FormatErrors": 0, "CrcErrors": 0, "HmacErrors": 0,
                       "SourceErrors": 0, "AgeErrors": 0}
        self.sample_cc = self.sample_cec = 0
        self.tlmo_cc = self.tlmo_cec = 0
        self.received = 0
        self.sent = [0, 0, 0, 0]
        self.dropped = [0, 0, 0, 0]
        self.drop_reported = [False] * 4
        self.queue: list[tuple[int, int, bytes]] = []  # (class, arrival number, packet)
        self.arrivals = 0
        self.apply_tlmo(tb.TlmoTable.unpack(self.table_body("TLMO")), initial=True)
        self.tbl_working: dict[str, tuple[str, bytes, bool]] = {}  # name -> (app, body, validated)
        self.tbl_pending: list[tuple[str, bytes]] = []
        self.next_hk = time.monotonic() + 0.5
        self.make_ram_disk()

    # -- helpers ------------------------------------------------------------------
    def table_body(self, app: str) -> bytes:
        return tb.split_image((self.cf / spec.TABLES[app][0]).read_bytes(), app)[1]

    def mid(self, app: str, name: str) -> int:
        return spec.app_mid(app, name, self.node)

    def secs(self) -> int:
        return 1000000 + int(time.monotonic() - self.t0)

    def tlm(self, mid: int, payload: bytes) -> bytes:
        return struct.pack(">HHH", mid, 0xC000, 16 + len(payload) - 7) + struct.pack(">IH", self.secs(), 0) + bytes(4) + payload

    def event(self, app: str, eid: int, etype: int, text: str) -> None:
        print(f"EVS Port1 1980-001-00:00:00.00000 66/{self.node}/{app} {eid}: {text}", flush=True)
        payload = struct.pack("<20sHHII", app.encode(), eid, etype, 0x42, self.node) + text.encode()[:121].ljust(122, b"\0") + bytes(2)
        self.bus(self.tlm(spec.evs_long_mid(self.node), payload))

    def make_ram_disk(self) -> None:
        for base in ("/dev/shm", os.environ.get("TMPDIR"), "/var/tmp", "/tmp"):
            if base and os.access(base, os.W_OK):
                Path(base, "osal:RAM").mkdir(exist_ok=True)
                return

    # -- software bus ---------------------------------------------------------------
    def bus(self, pkt: bytes) -> None:
        mid = fr.packet_mid(pkt)
        if mid and any(m == mid for m, _c in self.slots):
            self.enqueue(pkt)
        if fr.is_command(pkt):
            self.dispatch(pkt)

    def dispatch(self, pkt: bytes) -> None:
        mid, cc, body = fr.packet_mid(pkt), pkt[6] & 0x7F, pkt[8:]
        n = self.node
        if mid == self.mid("CMDI", "CMD"):
            self.cmdi_cmd(cc, body)
        elif mid == self.mid("CMDI", "SEND_HK"):
            self.bus(self.tlm(self.mid("CMDI", "HK_TLM"), self.cmdi_hk()))
        elif mid == self.mid("TLMO", "CMD"):
            self.tlmo_cmd(cc, body)
        elif mid == self.mid("TLMO", "SEND_HK"):
            self.bus(self.tlm(self.mid("TLMO", "HK_TLM"), self.tlmo_hk()))
        elif mid == spec.sample_cmd_mid(n):
            if cc == 0 and not body:
                self.sample_cc += 1
                self.event("SAMPLE_APP", 3, 2, "SAMPLE: NOOP command")
            else:
                self.sample_cec += 1
        elif mid == spec.sample_send_hk_mid(n):
            self.bus(self.tlm(spec.sample_hk_mid(n), bytes([self.sample_cc & 0xFF, self.sample_cec & 0xFF]) + bytes(2)))
        elif mid == spec.cmd_mid(spec.CFE_SB_SEND_HK_TOPIC, n):
            self.bus(self.tlm(spec.named_tlm_mid("CFE_SB_HK_TLM_MID", n), bytes(28)))
        elif mid == spec.tbl_cmd_mid(n):
            self.tbl_cmd(cc, body)

    # -- CMDI -------------------------------------------------------------------------
    def cmdi_hk(self) -> bytes:
        f = self.frames
        return struct.pack(spec.HK_LAYOUTS["CMDI"].fmt, self.cmdi_cc & 0xFF, self.cmdi_cec & 0xFF, self.mask, 0,
                           f["FramesReceived"], f["FramesAccepted"], f["FormatErrors"], f["CrcErrors"], f["HmacErrors"],
                           f["SourceErrors"], f["AgeErrors"], self.max_age)

    def validate(self, data: bytes) -> tuple[str | None, int]:
        """CMDVAL_ValidateFrame order: FORMAT, then CRC, HMAC, SOURCE, AGE as enabled."""
        if not fr.OVERHEAD + fr.MIN_CMD <= len(data) <= fr.MAX_FRAME:
            return "FORMAT", 0
        magic, ver, flags, src, length, secs, _seq = struct.unpack(">HBBHHII", data[:16])
        cmd = data[16:16 + length]
        if (magic, ver, flags) != (fr.MAGIC, 1, 0) or not 8 <= length <= 1024 or len(data) != 50 + length \
                or fr.packet_len(cmd) != length or not fr.is_command(cmd):
            return "FORMAT", 0
        if self.mask & 0x01 and struct.unpack(">H", data[-2:])[0] != fr.crc16_ccitt_false(data[:-2]):
            return "CRC", src
        if self.mask & 0x02 and data[16 + length:48 + length] != fr.hmac_sha256(self.cmdi.key, data[:16 + length]):
            return "HMAC", src
        if self.mask & 0x04 and (src == 0 or src not in [x for x in self.cmdi.sources if x]):
            return "SOURCE", src
        if self.mask & 0x08 and abs(self.secs() - secs) > self.max_age:
            return "AGE", src
        return None, src

    def ingest(self) -> None:
        for _ in range(16):
            try:
                data = self.rx.recv(4096)
            except BlockingIOError:
                return
            self.frames["FramesReceived"] += 1
            reason, src = self.validate(data)
            if reason == "FORMAT" and MUTANT == "format-as-crc":
                reason = "CRC"
            if reason is not None:
                self.frames[{"FORMAT": "FormatErrors", "CRC": "CrcErrors", "HMAC": "HmacErrors", "SOURCE": "SourceErrors",
                             "AGE": "AgeErrors"}[reason]] += 1
                self.event("CMDI", 12, 3, f"CMDI: frame rejected: {reason}" + (f", source {src}" if reason != "FORMAT" else ""))
                continue
            self.frames["FramesAccepted"] += 1
            length = struct.unpack(">H", data[6:8])[0]
            self.bus(data[16:16 + length])

    def cmdi_cmd(self, cc: int, body: bytes) -> None:
        want = {0: 0, 1: 0, 2: 4, 3: 4}
        if cc not in want:
            self.cmdi_cec += 1
            self.event("CMDI", 2, 3, f"CMDI: invalid command code {cc}")
            return
        if len(body) != want[cc]:
            self.cmdi_cec += 1
            self.event("CMDI", 6, 3, "CMDI: invalid length")
            return
        if cc == 0:
            self.cmdi_cc += 1
            self.event("CMDI", 3, 2, "CMDI: NOOP")
        elif cc == 1:
            self.cmdi_cc = self.cmdi_cec = 0
            self.frames = {k: 0 for k in self.frames}
            self.event("CMDI", 4, 2, "CMDI: counters reset")
        elif cc == 2:
            v = struct.unpack("<I", body)[0]
            if 1 <= v <= 86400:
                self.max_age = v if MUTANT != "max-age-ignored" else self.max_age
                self.cmdi_cc += 1
                self.event("CMDI", 14, 2, f"CMDI: MaxAgeSeconds {v}")
            else:
                self.cmdi_cec += 1
                self.event("CMDI", 15, 3, f"CMDI: MaxAgeSeconds {v} rejected")
        else:
            v = body[0]
            if v <= 0x0F:
                self.mask = v
                self.cmdi_cc += 1
                self.event("CMDI", 14, 2, f"CMDI: EnableMask {v}")
            else:
                self.cmdi_cec += 1
                self.event("CMDI", 15, 3, f"CMDI: EnableMask {v} rejected")

    # -- TLMO -------------------------------------------------------------------------
    def apply_tlmo(self, t: tb.TlmoTable, initial: bool = False) -> None:
        self.budget, self.capacity = t.budget, t.capacity
        self.slots = [tuple(s) for s in t.streams]
        keep = {m for m, _c in self.slots if m}
        self.queue = [q for q in self.queue if fr.packet_mid(q[2]) in keep]
        self.drop_reported = [False] * 4
        if not initial:
            self.event("TLMO", 19, 2, "TLMO: table updated")

    def cls(self, mid: int) -> int | None:
        return next((c for m, c in self.slots if m == mid and m), None)

    def enqueue(self, pkt: bytes) -> None:
        c = self.cls(fr.packet_mid(pkt))
        if c is None:
            return
        self.received += 1
        if len(pkt) > 1024:
            self.drop(c)
            return
        self.arrivals += 1
        if len(self.queue) >= self.capacity:
            lowest = max(q[0] for q in self.queue)
            if lowest > c:
                pick = max if MUTANT == "evict-newest" else min
                victim = pick((q for q in self.queue if q[0] == lowest), key=lambda q: q[1])
                self.queue.remove(victim)
                self.drop(lowest)
            else:
                self.drop(c)
                return
        self.queue.append((c, self.arrivals, pkt))

    def drop(self, c: int) -> None:
        self.dropped[c] += 1
        if not self.drop_reported[c]:
            self.drop_reported[c] = True
            self.event("TLMO", 17, 2, f"TLMO: first drop in class {c}")

    def drain(self) -> None:
        for _ in range(len(self.queue) if MUTANT == "budget-ignored" and self.budget else self.budget):
            if not self.queue:
                return
            q = min(self.queue, key=lambda x: (x[0], x[1]))
            self.queue.remove(q)
            self.tx.sendto(q[2], self.tlm_dest)
            self.sent[q[0]] += 1

    def tlmo_hk(self) -> bytes:
        used = sum(1 for m, _c in self.slots if m)
        return struct.pack(spec.HK_LAYOUTS["TLMO"].fmt, self.tlmo_cc & 0xFF, self.tlmo_cec & 0xFF, self.budget, len(self.queue),
                           self.capacity, used, 0, self.received, *self.sent, *self.dropped, 0)

    def tlmo_err(self, eid: int, text: str) -> None:
        self.tlmo_cec += 1
        self.event("TLMO", eid, 3, text)

    def tlmo_cmd(self, cc: int, body: bytes) -> None:
        want = {0: 0, 1: 0, 2: 8, 3: 4, 4: 0, 5: 8, 6: 4}
        if cc not in want:
            return self.tlmo_err(2, f"TLMO: invalid command code {cc}")
        if len(body) != want[cc]:
            return self.tlmo_err(6, "TLMO: invalid length")
        if cc == 0:
            self.tlmo_cc += 1
            self.event("TLMO", 3, 2, "TLMO: NOOP")
        elif cc == 1:
            self.tlmo_cc = self.tlmo_cec = 0
            self.received, self.sent, self.dropped = 0, [0] * 4, [0] * 4
            self.drop_reported = [False] * 4
            self.event("TLMO", 4, 2, "TLMO: reset")
        elif cc == 2:
            mid, c = struct.unpack("<IB3x", body)
            idx = next((i for i, s in enumerate(self.slots) if s[0] == mid and mid), None)
            if idx is None or c > 3:
                return self.tlmo_err(14, f"TLMO: SET_CLASS 0x{mid:04X} {c} rejected")
            self.slots[idx] = (mid, c)
            self.tlmo_cc += 1
            self.event("TLMO", 13, 2, f"TLMO: 0x{mid:04X} now class {c}")
        elif cc == 3:
            v = struct.unpack("<HH", body)[0]
            if v > 256:
                return self.tlmo_err(16, f"TLMO: budget {v} rejected")
            self.budget = v
            self.tlmo_cc += 1
            self.event("TLMO", 15, 2, f"TLMO: budget {v}")
        elif cc == 4:
            used = sum(1 for m, _c in self.slots if m)
            payload = struct.pack("<HH", self.budget, used) + b"".join(struct.pack("<IB3x", m, c) for m, c in self.slots)
            self.tlmo_cc += 1
            self.bus(self.tlm(self.mid("TLMO", "CLASS_TLM"), payload))
            self.event("TLMO", 18, 2, "TLMO: class map sent")
        elif cc == 5:
            mid, c = struct.unpack("<IB3x", body)
            free = next((i for i, s in enumerate(self.slots) if not s[0]), None)
            if mid == 0 or c > 3 or free is None or any(s[0] == mid for s in self.slots):
                return self.tlmo_err(21, f"TLMO: ADD_STREAM 0x{mid:04X} rejected")
            self.slots[free] = (mid, c)
            self.tlmo_cc += 1
            self.event("TLMO", 20, 2, f"TLMO: added 0x{mid:04X} class {c}")
        elif cc == 6:
            mid = struct.unpack("<I", body)[0]
            idx = next((i for i, s in enumerate(self.slots) if s[0] == mid and mid), None)
            if idx is None:
                return self.tlmo_err(23, f"TLMO: REMOVE_STREAM 0x{mid:04X} rejected")
            self.slots[idx] = (0, 0)
            self.queue = [q for q in self.queue if fr.packet_mid(q[2]) != mid]
            self.tlmo_cc += 1
            self.event("TLMO", 22, 2, f"TLMO: removed 0x{mid:04X}")

    # -- Table Services -----------------------------------------------------------------
    def tbl_cmd(self, cc: int, body: bytes) -> None:
        if cc == spec.CFE_TBL_LOAD_CC:
            path = body[:64].split(b"\0", 1)[0].decode()
            f = self.cf / path.removeprefix("/cf/")
            try:
                img = f.read_bytes()
            except OSError:
                self.event("CFE_TBL", 53, 3, f"cannot open {path}")
                return
            name = tb.header_name(img)
            app = next((a for a, (_f, n, _s) in spec.TABLES.items() if n == name), None)
            if app is None:
                self.event("CFE_TBL", 57, 3, f"no such table {name}")
                return
            self.tbl_working[name] = (app, img[spec.TBL_FILE_HDR:], False)
            self.event("CFE_TBL", 12, 2, f"Successful load into working buffer {name}")
        elif cc == spec.CFE_TBL_VALIDATE_CC:
            name = body[2:42].split(b"\0", 1)[0].decode()
            if name not in self.tbl_working:
                return
            app, data, _v = self.tbl_working[name]
            ok = self.valid(app, data)
            self.tbl_working[name] = (app, data, ok)
            if ok:
                self.event("CFE_TBL", 36, 2, f"{app} validation successful for Inactive '{name}'")
            else:
                self.event("CFE_TBL", 96, 3, f"{app} validation failed for Inactive '{name}'")
        elif cc == spec.CFE_TBL_ACTIVATE_CC:
            name = body[:40].split(b"\0", 1)[0].decode()
            entry = self.tbl_working.get(name)
            if entry is not None and entry[2]:
                self.tbl_pending.append((name, entry[1]))
                del self.tbl_working[name]

    def valid(self, app: str, data: bytes) -> bool:
        if app == "TLMO":
            t = tb.TlmoTable.unpack(data)
            used = [m for m, _c in t.streams if m]
            return t.budget <= 256 and 1 <= t.capacity <= 64 and all(c <= 3 for m, c in t.streams if m) and len(set(used)) == len(used)
        if app == "CMDI":
            t = tb.CmdiTable.unpack(data)
            src = [s for s in t.sources if s]
            return any(t.key) and 1 <= t.max_age <= 86400 and t.mask <= 0x0F and len(set(src)) == len(src) and (src or not t.mask & 4)
        return True

    def activate_pending(self) -> None:
        for name, data in self.tbl_pending:
            app = next(a for a, (_f, n, _s) in spec.TABLES.items() if n == name)
            self.event("CFE_TBL", 37, 2, f"{app} Successfully Updated '{name}'")
            if app == "TLMO":
                self.apply_tlmo(tb.TlmoTable.unpack(data))
            elif app == "CMDI":
                self.cmdi = tb.CmdiTable.unpack(data)
                self.max_age, self.mask = self.cmdi.max_age, self.cmdi.mask
        self.tbl_pending = []

    # -- periodic -------------------------------------------------------------------------
    def periodic_hk(self) -> None:
        n = self.node
        for app in spec.NEW_APPS:
            payload = self.cmdi_hk() if app == "CMDI" else self.tlmo_hk() if app == "TLMO" else bytes(spec.HK_LAYOUTS[app].size)
            self.bus(self.tlm(self.mid(app, "HK_TLM"), payload))
        for name in ("CFE_ES_HK_TLM_MID", "CFE_EVS_HK_TLM_MID", "CFE_TBL_HK_TLM_MID", "CFE_TIME_HK_TLM_MID"):
            self.bus(self.tlm(spec.named_tlm_mid(name, n), bytes(32)))
        self.bus(self.tlm(spec.named_tlm_mid("CFE_SB_HK_TLM_MID", n), bytes(28)))
        self.bus(self.tlm(spec.sample_hk_mid(n), bytes([self.sample_cc & 0xFF, self.sample_cec & 0xFF]) + bytes(2)))

    def run(self) -> None:
        for app in spec.NEW_APPS:
            self.event(app, 1, 2, f"{app} initialized")
        print("CFE_ES_Main entering OPERATIONAL state", flush=True)
        while True:
            start = time.monotonic()
            self.activate_pending()
            self.ingest()
            if start >= self.next_hk:
                self.periodic_hk()
                self.next_hk = start + 4.0
            self.drain()
            time.sleep(max(0.0, TICK - (time.monotonic() - start)))


def main() -> None:
    node = int(Path(sys.argv[0]).name.removeprefix("core-cpu"))
    FakeNode(node).run()


if __name__ == "__main__":
    main()
