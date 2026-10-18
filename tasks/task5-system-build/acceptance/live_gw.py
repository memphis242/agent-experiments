"""Live checks of GW (S5): UDP, TCP and SERIAL (PTY) connections carrying CMDI frames, validated with the
default table key; every GW command is itself a CMDI frame."""
from __future__ import annotations

import os
import select
import socket
import struct
import time
import tty
from typing import TYPE_CHECKING

import frames as fr
import spec
import tables as tb
from common import Fail, Problems
from live_common import boot, es_start_app, es_stop_app, sample_counters, wait_hk_field
from node import Node, move_startup_line

if TYPE_CHECKING:
    from check import Ctx

E4, E5, E6 = (spec.verif_mid(t) for t in (0xE4, 0xE5, 0xE6))
EID = spec.EID["GW"]
REASON_FIELDS = {"FORMAT": "FormatErrors", "CRC": "CrcErrors", "HMAC": "HmacErrors", "SOURCE": "SourceErrors",
                 "AGE": "AgeErrors"}
KEY2 = bytes(range(0x30, 0x50))
assert len(KEY2) == 32


def free_port(kind: int = socket.SOCK_DGRAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind((spec.HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def add_conn(n: Node, index: int, conn: tb.ConnDef, expect_ok: bool = True) -> list:
    mark = n.g.mark()
    n.cmd("GW", "ADD_CONN", struct.pack("<B3x", index) + conn.pack())
    eid = EID["CONN_ADD_INF"] if expect_ok else EID["CONN_ADD_ERR"]
    got = n.wait_event(mark, "GW", eid, 3.0)
    if expect_ok and not got:
        errs = [e.text for e in n.events(mark, "GW")]
        raise Fail(f"ADD_CONN {index} ({conn.interface}) produced no GW_CONN_ADD_INF_EID; GW events {errs[:3]}")
    return got


def conn_status(n: Node) -> list[dict[str, int]]:
    n.ensure_command_path()
    mid = spec.app_mid("GW", "CONN_TLM", n.id)
    for _ in range(2):
        got = n.request(lambda: n.cmd("GW", "SEND_CONN_STATUS", gap=0), mid)
        if got:
            raw = got[-1].data
            need = spec.TLM_HDR + 8 * 20
            if len(raw) < need:
                raise Fail(f"GW_CONN_TLM_MID packet is {len(raw)} bytes; the specified payload needs {need}")
            out = []
            for i in range(spec.GW_MAX_CONNECTIONS):
                v = struct.unpack_from(spec.GW_CONN_STATUS_FMT, raw, spec.TLM_HDR + i * 20)
                out.append(dict(zip(("Interface", "Enabled", "State", "SubCount", "TxPackets", "RxPackets", "TxErrors",
                                     "RxErrors"), v)))
            return out
    raise Fail("no GW_CONN_TLM_MID packet followed SEND_CONN_STATUS")


def udp_conn(local: int, remote: int, subs: tuple[int, ...] = (), enabled: int = 1) -> tb.ConnDef:
    return tb.ConnDef(spec.GW_IF["UDP"], enabled, local, "127.0.0.1", remote, "", subs)


class UdpPeer:
    """The test's side of a GW UDP connection."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((spec.HOST, 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)

    def drain(self) -> None:
        try:
            while True:
                self.sock.recvfrom(65535)
        except OSError:
            pass

    def recv_all(self, seconds: float) -> list[bytes]:
        out = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                out.append(self.sock.recvfrom(65535)[0])
            except OSError:
                continue
        return out

    def send(self, port: int, data: bytes) -> None:
        self.sock.sendto(data, (spec.HOST, port))

    def close(self) -> None:
        self.sock.close()


def judge_egress(units: list[bytes], injected: list[bytes], *, key: bytes, node: int, onboard: int,
                 first_seq: int | None = 1) -> list[str]:
    """Every unit is a frame verified with `key`, from SourceId = node, carrying the injected packets in order."""
    p = Problems()
    p.check(len(units) == len(injected), f"{len(units)} frames arrived for {len(injected)} egressed packets")
    seqs = []
    for i, u in enumerate(units):
        try:
            f = fr.parse_frame(u, key)
        except fr.FrameError as exc:
            p.check(False, f"egress unit {i} is not a valid frame under the table key: {exc}")
            continue
        p.check(f.source_id == node, f"egress frame SourceId {f.source_id} != node {node}")
        p.check(abs(f.time_seconds - onboard) <= 5, f"egress frame TimeSeconds {f.time_seconds} far from onboard {onboard}")
        if i < len(injected):
            p.check(f.command == injected[i], f"egress frame {i} does not embed the injected packet unchanged")
        seqs.append(f.sequence)
    if seqs and first_seq is not None:
        p.check(seqs == list(range(first_seq, first_seq + len(seqs))), f"egress Sequence {seqs}, expected consecutive from {first_seq}")
    elif seqs:
        p.check(seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"egress Sequence {seqs} is not consecutive")
    return p.items


def gw_session(ctx: "Ctx") -> tuple[Node, UdpPeer, int]:
    """cpu1 with GW connection 0 (UDP egress of E4 to the test) and 1 (UDP ingress on a local port)."""
    key = "gw-udp"
    cached = ctx.cache.get(key)
    if cached is not None and cached[0].alive():
        return cached
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    peer = UdpPeer()
    local = free_port()
    add_conn(n, 0, udp_conn(0, peer.port, (E4,)))
    add_conn(n, 1, udp_conn(local, peer.port))
    ctx.cache[key] = (n, peer, local)
    ctx.on_session_end(peer.close)
    return n, peer, local


def c_udp_egress(ctx: "Ctx") -> None:
    n, peer, _local = gw_session(ctx)
    p = Problems()
    h1 = n.hk("GW")
    peer.drain()
    pkts = [fr.build_verification(E4, 40, counter=i) for i in range(3)]
    n.inject(pkts, burst=3)
    units = peer.recv_all(1.5)
    for item in judge_egress(units, pkts, key=fr.DEFAULT_KEY, node=1, onboard=n.onboard_seconds()):
        p.check(False, item)
    h2 = n.hk("GW")
    p.check(h2["PacketsEgressed"] - h1["PacketsEgressed"] == 3, f"PacketsEgressed rose by {h2['PacketsEgressed'] - h1['PacketsEgressed']}")
    p.check(h2["ValTableShared"] == 1, f"ValTableShared {h2['ValTableShared']}")
    p.check(h2["ConnectionsConfigured"] == 2 and h2["ConnectionsUp"] == 2, f"ConnectionsConfigured/Up {h2['ConnectionsConfigured']}/{h2['ConnectionsUp']}")
    st = conn_status(n)
    p.check((st[0]["Interface"], st[0]["Enabled"], st[0]["State"], st[0]["SubCount"]) == (1, 1, 1, 1),
            f"connection 0 status {st[0]}")
    p.check(st[0]["TxPackets"] >= 3, f"connection 0 TxPackets {st[0]['TxPackets']}")
    p.raise_if()


def c_udp_ingress(ctx: "Ctx") -> None:
    n, peer, local = gw_session(ctx)
    p = Problems()
    h1 = n.hk("GW")
    s1 = sample_counters(n)
    peer.send(local, n.frame(fr.build_command(spec.sample_cmd_mid(), 0)))
    time.sleep(0.4)
    s2 = sample_counters(n)
    p.check(s2[0] == s1[0] + 1, "a valid frame carrying SAMPLE_APP NOOP sent to the GW UDP connection did not execute")
    peer.send(local, fr.build_command(spec.sample_cmd_mid(), 0))
    time.sleep(0.4)
    s3 = sample_counters(n)
    p.check(s3[0] == s2[0], "an unframed SAMPLE_APP NOOP sent to the GW connection executed")
    h2 = n.hk("GW")
    p.check(h2["PacketsIngressed"] - h1["PacketsIngressed"] == 1, f"PacketsIngressed rose by {h2['PacketsIngressed'] - h1['PacketsIngressed']}")
    p.check(h2["FramesReceived"] - h1["FramesReceived"] == 2, f"FramesReceived rose by {h2['FramesReceived'] - h1['FramesReceived']}")
    p.check(h2["FormatErrors"] - h1["FormatErrors"] == 1, "the unframed datagram was not counted as a FORMAT rejection")
    p.check(conn_status(n)[1]["RxPackets"] >= 1, "connection 1 RxPackets did not count the accepted frame")
    p.raise_if()


def c_ingress_rejections(ctx: "Ctx") -> None:
    n, peer, local = gw_session(ctx)
    p = Problems()
    cmd = fr.build_command(spec.sample_cmd_mid(), 0)
    cases = [
        (fr.corrupt(n.frame(cmd, source_id=2), "crc"), "CRC", 2),
        (fr.corrupt(n.frame(cmd, source_id=2), "mac"), "HMAC", 2),
        (n.frame(cmd, source_id=7), "SOURCE", 7),
        (n.frame(cmd, source_id=2, time_offset=-45), "AGE", 2),
        (n.frame(cmd, magic=0x1234), "FORMAT", None),
        (n.frame(cmd) + b"\0", "FORMAT", None),
    ]
    h1 = n.hk("GW")
    st1 = conn_status(n)[1]
    s1 = sample_counters(n)
    mark = n.g.mark()
    for data, _r, _s in cases:
        peer.send(local, data)
        time.sleep(0.05)
    time.sleep(0.6)
    events = n.events(mark, "GW", EID["FRAME_REJECT_ERR"])
    s2 = sample_counters(n)
    h2 = n.hk("GW")
    st2 = conn_status(n)[1]
    p.check(s2[0] == s1[0], "a rejected frame on the GW connection executed")
    want: dict[str, int] = {}
    for _d, r, _s in cases:
        want[r] = want.get(r, 0) + 1
    for reason, field in REASON_FIELDS.items():
        p.check(h2[field] - h1[field] == want.get(reason, 0), f"{field} rose by {h2[field] - h1[field]}, expected {want.get(reason, 0)}")
    p.check(st2["RxErrors"] - st1["RxErrors"] == len(cases), f"connection RxErrors rose by {st2['RxErrors'] - st1['RxErrors']}")
    p.check(len(events) == len(cases), f"{len(events)} GW_FRAME_REJECT_ERR_EID events for {len(cases)} rejected frames")
    pool = [e.text for e in events]
    for _d, reason, src in cases:
        hit = next((t for t in pool if reason in t.upper() and "1" in t and (src is None or str(src) in t)), None)
        if p.check(hit is not None, f"no GW_FRAME_REJECT_ERR_EID names connection 1, {reason}" + (f" and source {src}" if src else "")):
            pool.remove(hit)
    p.raise_if()


def c_loop_drop(ctx: "Ctx") -> None:
    n, peer, local = gw_session(ctx)
    p = Problems()
    h1 = n.hk("GW")
    peer.drain()
    peer.send(local, n.frame(fr.build_verification(E4, 40, counter=77)))
    time.sleep(0.8)
    echoed = peer.recv_all(0.5)
    h2 = n.hk("GW")
    p.check(h2["LoopDrops"] - h1["LoopDrops"] == 1, f"LoopDrops rose by {h2['LoopDrops'] - h1['LoopDrops']} for a command whose MID "
                                                    f"is in the Subs of an enabled connection")
    p.check(not echoed and h2["PacketsEgressed"] == h1["PacketsEgressed"], "the looped command was published and egressed again")
    p.check(h2["PacketsIngressed"] == h1["PacketsIngressed"], "the looped command was counted as ingressed")
    p.raise_if()


def c_valtable_key_change(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    p = Problems()
    peer = UdpPeer()
    ctx.on_session_end(peer.close)
    local = free_port()
    add_conn(n, 0, udp_conn(local, peer.port, (E4,)))
    n.load_table("CMDI", tb.CmdiTable(key=KEY2).pack())
    n.key = KEY2
    time.sleep(0.5)
    cmd = fr.build_command(spec.sample_cmd_mid(), 0)
    h1 = n.hk("GW")
    s1 = sample_counters(n)
    peer.send(local, n.frame(cmd, key=fr.DEFAULT_KEY))
    time.sleep(0.4)
    s2 = sample_counters(n)
    p.check(s2[0] == s1[0], "after the key change a frame under the old key executed through GW")
    peer.send(local, n.frame(cmd, key=KEY2))
    time.sleep(0.4)
    s3 = sample_counters(n)
    p.check(s3[0] == s2[0] + 1, "after the key change a frame under the new key did not execute through GW")
    h2 = n.hk("GW")
    p.check(h2["HmacErrors"] - h1["HmacErrors"] == 1, f"HmacErrors rose by {h2['HmacErrors'] - h1['HmacErrors']} for one old-key frame")
    p.check(h2["ValTableShared"] == 1, "ValTableShared is not 1 after the table update")
    peer.drain()
    pkts = [fr.build_verification(E4, 40, counter=i) for i in range(2)]
    n.inject(pkts, burst=2)
    units = peer.recv_all(1.2)
    for item in judge_egress(units, pkts, key=KEY2, node=1, onboard=n.onboard_seconds(), first_seq=None):
        p.check(False, "egress under the new key: " + item)
    p.raise_if()


def deframe_all(chunks: bytes) -> list[fr.StreamUnit]:
    d = fr.Deframer()
    return d.feed(chunks)


class TcpServer:
    def __init__(self, port: int) -> None:
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind((spec.HOST, port))
        self.lsock.listen(1)
        self.lsock.settimeout(0.2)
        self.conn: socket.socket | None = None
        self.buf = b""

    def accept(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                self.conn, _ = self.lsock.accept()
                self.conn.settimeout(0.1)
                return True
            except OSError:
                continue
        return False

    def read(self, seconds: float) -> bytes:
        assert self.conn is not None
        end = time.monotonic() + seconds
        out = b""
        while time.monotonic() < end:
            try:
                data = self.conn.recv(65535)
                if not data:
                    break
                out += data
            except OSError:
                continue
        return out

    def write(self, data: bytes) -> None:
        assert self.conn is not None
        self.conn.sendall(data)

    def close_conn(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def close(self) -> None:
        self.close_conn()
        self.lsock.close()


def stream_ingress_battery(n: Node, write, label: str) -> list[str]:  # noqa: ANN001
    """Marker-framed ingress over a stream link: whole, split, garbage before a marker, bad MAC, bad length."""
    p = Problems()
    cmd = fr.build_command(spec.sample_cmd_mid(), 0)
    h1 = n.hk("GW")
    s0 = sample_counters(n)
    write(fr.stream_wrap(n.frame(cmd)))
    time.sleep(0.5)
    s1 = sample_counters(n)
    p.check(s1[0] == s0[0] + 1, f"{label}: a marker-framed frame carrying SAMPLE_APP NOOP did not execute")
    data = fr.stream_wrap(n.frame(cmd))
    for part in (data[:7], data[7:30], data[30:]):
        write(part)
        time.sleep(0.25)
    time.sleep(0.4)
    s2 = sample_counters(n)
    p.check(s2[0] == s1[0] + 1, f"{label}: a frame split across three writes did not execute")
    write(b"\x00\x11garbage\x1a\xcf" + fr.stream_wrap(n.frame(cmd)))
    time.sleep(0.5)
    s3 = sample_counters(n)
    p.check(s3[0] == s2[0] + 1, f"{label}: a frame after garbage bytes did not execute")
    write(fr.stream_wrap(fr.corrupt(n.frame(cmd), "mac")))
    time.sleep(0.5)
    s4 = sample_counters(n)
    p.check(s4[0] == s3[0], f"{label}: a frame with a bad MAC executed")
    bad_hdr = fr.MARKER + struct.pack(">HBBHHII", fr.MAGIC, 1, 0, 1, 2000, n.onboard_seconds(), 1)
    write(bad_hdr + fr.stream_wrap(n.frame(cmd)))
    time.sleep(0.5)
    s5 = sample_counters(n)
    p.check(s5[0] == s4[0] + 1, f"{label}: hunting did not resume after a header with CommandLength 2000")
    h2 = n.hk("GW")
    p.check(h2["HmacErrors"] - h1["HmacErrors"] == 1, f"{label}: HmacErrors rose by {h2['HmacErrors'] - h1['HmacErrors']}")
    p.check(h2["FormatErrors"] - h1["FormatErrors"] == 1, f"{label}: FormatErrors rose by {h2['FormatErrors'] - h1['FormatErrors']} "
                                                          f"for one out-of-range length")
    return p.items


def judge_stream_egress(raw: bytes, injected: list[bytes], *, key: bytes, node: int, onboard: int) -> list[str]:
    units = deframe_all(raw)
    bad = [u for u in units if u.bad_length is not None]
    p = Problems()
    p.check(not bad, "the egress stream holds a header with an out-of-range length")
    p.check(raw[:4] == fr.MARKER if raw else False, "the egress stream does not start with the marker 1A CF FC 1D")
    for item in judge_egress([u.data for u in units if u.bad_length is None], injected, key=key, node=node, onboard=onboard,
                             first_seq=None):
        p.check(False, item)
    return p.items


def c_tcp(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    p = Problems()
    port = free_port(socket.SOCK_STREAM)
    add_conn(n, 2, tb.ConnDef(spec.GW_IF["TCP"], 1, 0, "127.0.0.1", port, "", (E5,)))
    time.sleep(1.5)
    st = conn_status(n)[2]
    p.check(st["State"] == spec.GW_STATE["CONNECTING"], f"with no listener the TCP connection is in state {st['State']}, not CONNECTING (2)")
    srv = TcpServer(port)
    ctx.on_session_end(srv.close)
    mark = n.g.mark()
    if not srv.accept(spec.GW_TCP_ATTEMPT_SEC + 2.5):
        raise Fail(f"GW did not connect within {spec.GW_TCP_ATTEMPT_SEC + 2.5:.1f} s after the listener appeared (attempts "
                   f"start every {spec.GW_TCP_ATTEMPT_SEC} s while CONNECTING)")
    p.check(bool(n.wait_event(mark, "GW", EID["CONN_UP_INF"], 3.0)), "no GW_CONN_UP_INF_EID after the connection was made")
    p.check(wait_hk_field(n, "GW", lambda h: h["ConnectionsUp"] >= 1, 3.0, "ConnectionsUp is 0")["ConnectionsUp"] >= 1, "ConnectionsUp")
    pkts = [fr.build_verification(E5, 40, counter=i) for i in range(2)]
    n.inject(pkts, burst=2)
    raw = srv.read(1.2)
    for item in judge_stream_egress(raw, pkts, key=fr.DEFAULT_KEY, node=1, onboard=n.onboard_seconds()):
        p.check(False, "TCP egress: " + item)
    for item in stream_ingress_battery(n, srv.write, "TCP"):
        p.check(False, item)
    srv.close_conn()
    p.check(srv.accept(3.0), "after the peer closed, GW did not begin a new connect cycle with an attempt at once")
    p.check(wait_hk_field(n, "GW", lambda h: h["ConnectionsUp"] >= 1, 3.0, "ConnectionsUp did not return to 1")["ConnectionsUp"] >= 1,
            "ConnectionsUp after the reconnect")
    p.raise_if()


def c_tcp_down(ctx: "Ctx") -> None:
    """S5.2 connect cycle: four attempts 5 s apart against a peer that never listens, then DOWN with
    GW_CONN_TIMEOUT_ERR_EID about 20 s after the connection was opened, no further attempt, and a new cycle on
    DISABLE_CONN then ENABLE_CONN."""
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    p = Problems()
    port = free_port(socket.SOCK_STREAM)
    mark = n.g.mark()
    t0 = time.monotonic()
    add_conn(n, 4, tb.ConnDef(spec.GW_IF["TCP"], 1, 0, "127.0.0.1", port, "", (E5,)))
    p.check(conn_status(n)[4]["State"] == spec.GW_STATE["CONNECTING"], "a TCP connection to a peer that does not listen is not "
                                                                         "CONNECTING after ADD_CONN")
    down = n.wait_event(mark, "GW", EID["CONN_TIMEOUT_ERR"], 32.0)
    expected = spec.GW_TCP_ATTEMPT_SEC * (1 + spec.GW_TCP_MAX_RETRIES)
    if not p.check(bool(down), f"no GW_CONN_TIMEOUT_ERR_EID within 32 s (expected about {expected} s after ADD_CONN)"):
        p.raise_if()
    took = down[0].t - t0
    p.check(expected - 2 <= took <= expected + 6, f"GW_CONN_TIMEOUT_ERR_EID came {took:.1f} s after ADD_CONN; four attempts "
                                                  f"5 s apart give DOWN at about {expected} s (accepted {expected - 2}..{expected + 6} s)")
    p.check("4" in down[0].text and str(port) in down[0].text,
            f"GW_CONN_TIMEOUT_ERR_EID does not name connection 4 and port {port}: {down[0].text!r}")
    p.check(len(n.events(mark, "GW", EID["CONN_TIMEOUT_ERR"])) == 1, "more than one GW_CONN_TIMEOUT_ERR_EID for one cycle")
    st = conn_status(n)[4]
    p.check(st["State"] == spec.GW_STATE["DOWN"] and st["Enabled"] == 1, f"after the cycle the connection status is {st} "
                                                                          f"(DOWN is 3, Enabled stays 1)")
    h1 = n.hk("GW")
    n.inject([fr.build_verification(E5, 40, counter=1)], burst=1)
    time.sleep(0.5)
    p.check(n.hk("GW")["EgressErrors"] > h1["EgressErrors"], "a packet for a DOWN connection was not counted as an egress error")
    srv = TcpServer(port)
    ctx.on_session_end(srv.close)
    p.check(not srv.accept(spec.GW_TCP_ATTEMPT_SEC + 2.5), "GW attempted a connection again while DOWN")
    mark = n.g.mark()
    n.cmd("GW", "DISABLE_CONN", struct.pack("<B3x", 4))
    n.cmd("GW", "ENABLE_CONN", struct.pack("<B3x", 4))
    p.check(srv.accept(3.0), "DISABLE_CONN then ENABLE_CONN did not begin a new cycle with an attempt at once")
    p.check(bool(n.wait_event(mark, "GW", EID["CONN_UP_INF"], 3.0)), "no GW_CONN_UP_INF_EID after the new cycle connected")
    p.raise_if()


class Pty:
    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def read(self, seconds: float) -> bytes:
        out = b""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _w, _x = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    out += os.read(self.master, 65536)
                except OSError:
                    break
        return out

    def close(self) -> None:
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def serial_conn(path: str, subs: tuple[int, ...]) -> tb.ConnDef:
    return tb.ConnDef(spec.GW_IF["SERIAL"], 1, 0, "", 0, path, subs)


def pty_traffic(n: Node, pty: Pty, label: str) -> list[str]:
    p = Problems()
    pty.read(0.3)
    pkts = [fr.build_verification(E6, 40, counter=i) for i in range(2)]
    n.inject(pkts, burst=2)
    raw = pty.read(1.2)
    for item in judge_stream_egress(raw, pkts, key=fr.DEFAULT_KEY, node=1, onboard=n.onboard_seconds()):
        p.check(False, f"{label} egress: " + item)
    for item in stream_ingress_battery(n, pty.write, label):
        p.check(False, item)
    return p.items


def serial_session(ctx: "Ctx") -> tuple[Node, Pty]:
    key = "gw-serial"
    cached = ctx.cache.get(key)
    if cached is not None and cached[0].alive():
        return cached
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    pty = Pty()
    ctx.on_session_end(pty.close)
    add_conn(n, 3, serial_conn(pty.path, (E6,)))
    ctx.cache[key] = (n, pty)
    return n, pty


def c_pty(ctx: "Ctx") -> None:
    n, pty = serial_session(ctx)
    items = pty_traffic(n, pty, "SERIAL")
    if items:
        raise Fail("; ".join(items))


def c_pty_idle_probe(ctx: "Ctx") -> None:
    """Pilot probe: zero-timeout reads of an idle PTY through OSAL never block GW."""
    n, _pty = serial_session(ctx)
    p = Problems()
    sfi1 = n.hk("SFI")
    send = spec.app_mid("GW", "SEND_HK")
    late = 0
    for _ in range(6):
        t0 = time.monotonic()
        got = n.request(lambda: n.send(send, 0), spec.app_mid("GW", "HK_TLM"), timeout=1.0, tail=0.0)
        if not got or got[0].t - t0 > 1.0:
            late += 1
        time.sleep(0.7)
    p.check(late == 0, f"{late} of 6 GW housekeeping requests went unanswered for more than 1 s with an idle PTY connection")
    sfi2 = n.hk("SFI")
    p.check(sfi2["HbMissEvents"] == sfi1["HbMissEvents"] and sfi2["AppsMissing"] == 0,
            f"SFI reported a heartbeat miss while the PTY was idle (HbMissEvents {sfi1['HbMissEvents']} -> {sfi2['HbMissEvents']})")
    p.raise_if()


def c_restart_serial_probe(ctx: "Ctx") -> None:
    """Pilot probe: GW restarted with ES STOP_APP/START_APP over an existing /dev/pts mapping carries SERIAL traffic."""
    n, pty = serial_session(ctx)
    p = Problems()
    pty.write(fr.stream_wrap(n.frame(fr.build_command(spec.sample_cmd_mid(), 0))))
    time.sleep(0.5)
    es_stop_app(n, "GW")
    time.sleep(1.0)
    mark = n.g.mark()
    es_start_app(n, "GW", "GW_AppMain", "/cf/gw.so", 70, 131072)
    p.check(bool(n.wait_event(mark, "GW", 1, 5.0)), "the restarted GW sent no GW_INIT_INF_EID")
    errs = [e.text for e in n.events(mark, "GW") if e.etype == 3 and e.eid != EID["VALTBL_ERR"]]
    p.check(not errs, f"the restarted GW reported errors: {errs[:3]}")
    pty2 = Pty()
    ctx.on_session_end(pty2.close)
    try:
        add_conn(n, 3, serial_conn(pty2.path, (E6,)))
    except Fail as exc:
        p.check(False, str(exc))
        p.raise_if()
    for item in pty_traffic(n, pty2, "SERIAL after restart"):
        p.check(False, item)
    ctx.cache.pop("gw-serial", None)
    p.raise_if()


def c_conn_commands(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    p = Problems()
    peer = UdpPeer()
    ctx.on_session_end(peer.close)
    local = free_port()
    add_conn(n, 0, udp_conn(0, peer.port, (E4, E5)))
    add_conn(n, 1, udp_conn(local, peer.port, (E5,)))
    hk = n.hk("GW")
    base = hk
    p.check(hk["ConnectionsConfigured"] == 2 and hk["Subscriptions"] == 2, f"ConnectionsConfigured/Subscriptions {hk['ConnectionsConfigured']}/"
                                                                            f"{hk['Subscriptions']} (union of two MIDs)")
    bad = [
        ("index 8", 8, udp_conn(0, peer.port)),
        ("a used slot", 0, udp_conn(0, peer.port)),
        ("an invalid address", 2, tb.ConnDef(1, 1, 0, "300.1.1.1", 5000, "", ())),
        ("a zero RemotePort", 2, tb.ConnDef(1, 1, 0, "127.0.0.1", 0, "", ())),
        ("TCP with a LocalPort", 2, tb.ConnDef(2, 1, 5, "127.0.0.1", 5000, "", ())),
        ("a relative DevicePath", 2, tb.ConnDef(3, 1, 0, "", 0, "dev/x", ())),
        ("duplicate Subs", 2, udp_conn(0, peer.port, (E4, E4))),
        ("Interface NONE", 2, tb.ConnDef(0, 1, 0, "127.0.0.1", 5000, "", ())),
        ("Interface 4", 2, tb.ConnDef(4, 1, 0, "127.0.0.1", 5000, "", ())),
        ("Enabled 2", 2, udp_conn(0, peer.port, (), enabled=2)),
        ("a LocalPort already used", 2, udp_conn(local, peer.port)),
    ]
    mark = n.g.mark()
    for _l, idx, conn in bad:
        n.cmd("GW", "ADD_CONN", struct.pack("<B3x", idx) + conn.pack())
    time.sleep(0.6)
    got = len(n.events(mark, "GW", EID["CONN_ADD_ERR"]))
    p.check(got == len(bad), f"{got} GW_CONN_ADD_ERR_EID events for {len(bad)} invalid ADD_CONN commands")
    after = n.hk("GW")
    p.check(after["ConnectionsConfigured"] == 2, "an invalid ADD_CONN configured a connection")
    p.check((after["CommandErrorCounter"] - base["CommandErrorCounter"]) % 256 == len(bad) and
            after["CommandCounter"] == base["CommandCounter"],
            f"{len(bad)} rejected ADD_CONN commands moved CommandErrorCounter/CommandCounter by "
            f"{(after['CommandErrorCounter'] - base['CommandErrorCounter']) % 256}/{after['CommandCounter'] - base['CommandCounter']}")
    mark = n.g.mark()
    n.cmd("GW", "DISABLE_CONN", struct.pack("<B3x", 0))
    p.check(bool(n.wait_event(mark, "GW", EID["CONN_DISABLE_INF"])), "DISABLE_CONN produced no event 17")
    st = conn_status(n)[0]
    p.check(st["Enabled"] == 0 and st["State"] == 0, f"connection 0 after DISABLE_CONN: {st}")
    peer.drain()
    n.inject([fr.build_verification(E4, 40, counter=1)], burst=1)
    p.check(not peer.recv_all(0.8), "a disabled connection still egresses")
    e0 = n.hk("GW")["CommandErrorCounter"]
    mark = n.g.mark()
    n.cmd("GW", "DISABLE_CONN", struct.pack("<B3x", 0))
    n.cmd("GW", "ENABLE_CONN", struct.pack("<B3x", 1))
    n.cmd("GW", "REMOVE_CONN", struct.pack("<B3x", 5))
    time.sleep(0.5)
    p.check((n.hk("GW")["CommandErrorCounter"] - e0) % 256 == 3, "three rejected connection commands did not each count as an error")
    for eid, label in ((EID["CONN_DISABLE_ERR"], "DISABLE of a disabled"), (EID["CONN_ENABLE_ERR"], "ENABLE of an enabled"),
                       (EID["CONN_REMOVE_ERR"], "REMOVE of an unused")):
        p.check(bool(n.events(mark, "GW", eid)), f"{label} connection produced no error event {eid}")
    mark = n.g.mark()
    n.cmd("GW", "ENABLE_CONN", struct.pack("<B3x", 0))
    p.check(bool(n.wait_event(mark, "GW", EID["CONN_ENABLE_INF"])), "ENABLE_CONN produced no event 15")
    peer.drain()
    n.inject([fr.build_verification(E4, 40, counter=2)], burst=1)
    p.check(len(peer.recv_all(0.8)) == 1, "an enabled connection did not egress again")
    mark = n.g.mark()
    n.cmd("GW", "REMOVE_CONN", struct.pack("<B3x", 1))
    p.check(bool(n.wait_event(mark, "GW", EID["CONN_REMOVE_INF"])), "REMOVE_CONN produced no event 13")
    p.check(conn_status(n)[1]["Interface"] == 0, "a removed slot is still used")
    mark = n.g.mark()
    n.cmd("GW", "RELOAD")
    p.check(bool(n.wait_event(mark, "GW", EID["RELOAD_INF"])), "RELOAD produced no event 19")
    st = conn_status(n)
    p.check(all(s["Interface"] == 0 for s in st), "RELOAD did not re-apply the (empty) table, discarding command changes")
    hk = n.hk("GW")
    p.check(hk["ConnectionsConfigured"] == 0 and hk["Subscriptions"] == 0, f"after RELOAD ConnectionsConfigured/Subscriptions "
                                                                           f"{hk['ConnectionsConfigured']}/{hk['Subscriptions']}")
    mark = n.g.mark()
    n.cmd("GW", "RESET_COUNTERS")
    p.check(bool(n.wait_event(mark, "GW", 4)), "RESET_COUNTERS produced no event 4")
    hk = n.hk("GW")
    p.check(hk["CommandCounter"] == 0 and hk["PacketsEgressed"] == 0 and hk["FramesReceived"] == 0,
            f"RESET_COUNTERS left counters {hk}")
    p.raise_if()


def c_valtable_share_probe(ctx: "Ctx") -> None:
    """Pilot probe: CFE_TBL_Share works when GW starts before CMDI registers CMDI.ValTable, and across a CMDI restart."""
    nodes = boot(ctx, (1,), startup_edit=lambda t: move_startup_line(t, "gw", before="cmdi"))
    n = nodes[1]
    p = Problems()
    n.ensure_command_path()
    hk = wait_hk_field(n, "GW", lambda h: h["ValTableShared"] == 1, 6.0, "GW did not share CMDI.ValTable after starting before CMDI")
    peer = UdpPeer()
    ctx.on_session_end(peer.close)
    local = free_port()
    add_conn(n, 0, udp_conn(local, peer.port))
    cmd = fr.build_command(spec.sample_cmd_mid(), 0)
    s1 = sample_counters(n)
    peer.send(local, n.frame(cmd))
    time.sleep(0.5)
    p.check(sample_counters(n)[0] == s1[0] + 1, "with GW started before CMDI, a frame through GW did not execute")
    n.send(spec.es_cmd_mid(), spec.CFE_ES_RESTART_APP_CC, spec.es_app_name_payload("CMDI"))
    time.sleep(3.0)
    n.command_path_ok = False
    try:
        n.ensure_command_path()
    except Fail as exc:
        raise Fail(f"after ES RESTART_APP CMDI the command path did not come back: {exc}") from None
    try:
        hk = wait_hk_field(n, "GW", lambda h: h["ValTableShared"] == 1, 6.0, "GW did not share CMDI.ValTable again after CMDI restarted")
    except Fail as exc:
        p.check(False, str(exc))
    s2 = sample_counters(n)
    peer.send(local, n.frame(cmd))
    time.sleep(0.5)
    p.check(sample_counters(n)[0] == s2[0] + 1, "after CMDI restarted, a frame through GW did not execute")
    p.check(hk.get("ValTableShared") == 1, "ValTableShared is not 1 at the end")
    p.raise_if()
