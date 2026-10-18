"""Live checks of SFI (S6): heartbeat misses of real apps, injected heartbeats and clustering, watches."""
from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import frames as fr
import spec
from common import Fail, Problems
from live_common import boot, es_start_app, es_stop_app
from node import Node

if TYPE_CHECKING:
    from check import Ctx

EID = spec.EID["SFI"]
EC, ED = spec.verif_mid(0xEC), spec.verif_mid(0xED)
HB_MID = spec.app_mid("SFI", "HEARTBEAT")


@dataclass(frozen=True)
class AppStatus:
    name: str
    state: int
    missed: int
    total_misses: int
    last_secs: int
    last_subsecs: int


@dataclass(frozen=True)
class Status:
    apps_missing: int
    correlation: int
    history_count: int
    cluster_count: int
    largest: int
    apps: tuple[AppStatus, ...]
    history: tuple[tuple[int, int, int], ...]
    watches: tuple[tuple[int, int], ...]

    def app(self, name: str) -> AppStatus | None:
        return next((a for a in self.apps if a.name == name), None)


def parse_status(pkt: bytes) -> Status:
    need = spec.TLM_HDR + spec.SFI_STATUS_SIZE
    if len(pkt) < need:
        raise Fail(f"the SFI status packet is {len(pkt)} bytes; the specified payload needs {need}")
    o = spec.TLM_HDR
    missing, corr, hcount, ccount, largest = struct.unpack_from("<BBHHH", pkt, o)
    apps = []
    for i in range(spec.SFI_MAX_APPS):
        name, st, mc, tm, ls, lss = struct.unpack_from(spec.SFI_APP_STATUS_FMT, pkt, o + 8 + i * 32)
        apps.append(AppStatus(name.split(b"\0", 1)[0].decode("latin-1"), st, mc, tm, ls, lss))
    hist = tuple(struct.unpack_from(spec.SFI_MISS_RECORD_FMT, pkt, o + 8 + 512 + i * 12) for i in range(spec.SFI_HISTORY_LEN))
    watches = tuple(struct.unpack_from(spec.SFI_WATCH_STATUS_FMT, pkt, o + 8 + 512 + 192 + i * 8)
                    for i in range(spec.SFI_MAX_WATCHES))
    return Status(missing, corr, hcount, ccount, largest, tuple(apps), hist, watches)


def status(n: Node) -> Status:
    n.ensure_command_path()
    mid = spec.app_mid("SFI", "STATUS_TLM", n.id)
    for _ in range(2):
        got = n.request(lambda: n.cmd("SFI", "SEND_STATUS", gap=0), mid)
        if got:
            return parse_status(got[-1].data)
    raise Fail("no SFI_STATUS_TLM_MID packet followed SEND_STATUS")


def heartbeat(name: str, seq: int) -> bytes:
    return fr.build_command(HB_MID, 0, struct.pack("<20sI", name.encode(), seq))


class HeartbeatPump(threading.Thread):
    """Sends a heartbeat frame for each active name every 0.5 s."""

    def __init__(self, node: Node, names: set[str]) -> None:
        super().__init__(daemon=True, name="hb-pump")
        self.node = node
        self.lock = threading.Lock()
        self.names = set(names)
        self.seq = {n: 0 for n in ("SFI_T1", "SFI_T2", "SFI_T3")}
        self.running = True
        self.start()

    def set(self, names: set[str]) -> None:
        with self.lock:
            self.names = set(names)

    def run(self) -> None:
        while self.running:
            with self.lock:
                names = sorted(self.names)
            for name in names:
                self.seq[name] = self.seq.get(name, 0) + 1
                try:
                    self.node.send_raw(self.node.frame(heartbeat(name, self.seq[name])))
                except Fail:
                    pass
            time.sleep(0.5)

    def stop(self) -> None:
        self.running = False
        self.join(timeout=2)


def c_apps_ok_after_startup(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    time.sleep(4.0)
    st = status(n)
    for name in ("STOR", "CMDI", "TLMO", "GW"):
        a = st.app(name)
        if p.check(a is not None, f"the status packet does not list {name}"):
            assert a is not None
            p.check(a.state == spec.SFI_APP_STATE["OK"] and a.total_misses == 0,
                    f"{name} is in state {a.state} with {a.total_misses} misses after start-up (OK is 1)")
    hk = n.hk("SFI")
    p.check(hk["HbMissEvents"] == 0 and hk["AppsMissing"] == 0 and hk["TotalHeartbeats"] > 0,
            f"after start-up HbMissEvents/AppsMissing/TotalHeartbeats {hk['HbMissEvents']}/{hk['AppsMissing']}/{hk['TotalHeartbeats']}")
    p.raise_if()


def c_gw_stop_miss(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    time.sleep(3.0)
    mark = n.g.mark()
    es_stop_app(n, "GW")
    t0 = time.monotonic()
    miss = n.wait_event(mark, "SFI", EID["HB_MISS_ERR"], 7.0, text="GW")
    if p.check(bool(miss), "no SFI_HB_MISS_ERR_EID naming GW within N + 2 seconds of ES STOP_APP"):
        p.check(miss[0].t - t0 <= 6.5, f"the miss event came {miss[0].t - t0:.1f} s after the stop (N + 2 is 5 s)")
    hk = n.hk("SFI")
    p.check(hk["AppsMissing"] == 1, f"AppsMissing {hk['AppsMissing']} with GW stopped")
    st = status(n)
    gw = st.app("GW")
    if p.check(gw is not None, "the status packet does not list GW"):
        assert gw is not None
        p.check(gw.state == spec.SFI_APP_STATE["MISSING"] and gw.last_secs != 0,
                f"GW status state {gw.state}, LastMissSeconds {gw.last_secs} (MISSING is 2 with an onset time)")
        idx = st.apps.index(gw)
        p.check(st.history_count >= 1 and any(r[0] == idx and r[1] == gw.last_secs for r in st.history[:st.history_count]),
                "the miss history has no record of GW's onset")
    mark = n.g.mark()
    es_start_app(n, "GW", "GW_AppMain", "/cf/gw.so", 70, 131072)
    p.check(bool(n.wait_event(mark, "SFI", EID["HB_RESTORED_INF"], 5.0, text="GW")),
            "no SFI_HB_RESTORED_INF_EID naming GW after ES START_APP")
    hk = n.hk("SFI")
    p.check(hk["AppsMissing"] == 0, f"AppsMissing {hk['AppsMissing']} after GW was started again")
    p.raise_if()


def c_cluster_and_clear(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    p = Problems()
    pump = HeartbeatPump(n, {"SFI_T1", "SFI_T2", "SFI_T3"})
    ctx.on_session_end(pump.stop)
    time.sleep(3.0)
    mark = n.g.mark()
    pump.set({"SFI_T3"})
    got = n.wait_event(mark, "SFI", EID["HB_MISS_ERR"], 8.0, count=2)
    p.check(len(got) >= 2 and {("SFI_T1" in e.text) or ("SFI_T2" in e.text) for e in got} == {True},
            f"stopping SFI_T1 and SFI_T2 at once gave {len(got)} miss events naming them")
    time.sleep(1.5)
    p.check(bool(n.events(mark, "SFI", EID["CLUSTER_ERR"])), "two misses at the same wakeup produced no SFI_CLUSTER_ERR_EID")
    hk = n.hk("SFI")
    p.check(hk["Correlation"] == spec.SFI_CORRELATION["CLUSTERED"] and hk["LargestClusterSize"] == 2,
            f"Correlation/LargestClusterSize {hk['Correlation']}/{hk['LargestClusterSize']} (CLUSTERED is 2, size 2)")
    mark = n.g.mark()
    n.cmd("SFI", "CLEAR_HISTORY")
    p.check(bool(n.wait_event(mark, "SFI", EID["HISTORY_CLEAR_INF"])), "CLEAR_HISTORY produced no SFI_HISTORY_CLEAR_INF_EID")
    time.sleep(1.2)
    hk = n.hk("SFI")
    p.check(hk["Correlation"] == 0 and hk["RecentMisses"] == 0, f"after CLEAR_HISTORY Correlation/RecentMisses {hk['Correlation']}/{hk['RecentMisses']}")
    pump.set({"SFI_T1", "SFI_T2", "SFI_T3"})
    time.sleep(2.5)
    mark = n.g.mark()
    pump.set({"SFI_T2", "SFI_T3"})
    p.check(bool(n.wait_event(mark, "SFI", EID["HB_MISS_ERR"], 7.0, text="SFI_T1")), "no miss event for SFI_T1")
    time.sleep(6.5)
    pump.set({"SFI_T3"})
    p.check(bool(n.wait_event(mark, "SFI", EID["HB_MISS_ERR"], 7.0, text="SFI_T2")), "no miss event for SFI_T2")
    time.sleep(1.5)
    hk = n.hk("SFI")
    p.check(hk["RecentMisses"] == 2 and hk["LargestClusterSize"] == 1 and hk["Correlation"] == spec.SFI_CORRELATION["UNCORRELATED"],
            f"two misses more than W apart gave RecentMisses/LargestClusterSize/Correlation {hk['RecentMisses']}/"
            f"{hk['LargestClusterSize']}/{hk['Correlation']} (UNCORRELATED is 1)")
    pump.stop()
    p.raise_if()


def watch_packet(mid: int, value: int, width: int, k: int) -> bytes:
    return fr.build_verification(mid, 32, counter=value, counter_width=width, fill=k)


def c_stall_rate(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    p = Problems()

    def feed(mid: int, values: list[int], width: int) -> None:
        for k, v in enumerate(values):
            n.send_raw(n.frame(watch_packet(mid, v, width, k)))
            time.sleep(0.3)
        time.sleep(0.4)

    h0 = n.hk("SFI")
    mark = n.g.mark()
    feed(EC, [5, 5, 5, 5, 5], 4)
    stall = n.events(mark, "SFI", EID["STALL_ERR"])
    p.check(len(stall) == 1, f"{len(stall)} SFI_STALL_ERR_EID events for a value unchanged in 3 and then 4 packets (one expected)")
    if stall:
        p.check(f"{EC:04X}" in stall[0].text.upper() and "16" in stall[0].text, f"the stall event does not name 0x{EC:04X} and offset 16")
    h1 = n.hk("SFI")
    p.check(h1["StallFaults"] - h0["StallFaults"] == 1 and h1["WatchesInFault"] >= 1, "StallFaults/WatchesInFault after the stall")
    mark = n.g.mark()
    feed(EC, [6], 4)
    p.check(bool(n.events(mark, "SFI", EID["WATCH_CLEAR_INF"])), "a changed value did not clear the STALL fault with event 17")
    mark = n.g.mark()
    feed(ED, [10, 14, 30, 40, 43, 20, 22, 1], 2)
    rate = n.events(mark, "SFI", EID["RATE_ERR"])
    clear = n.events(mark, "SFI", EID["WATCH_CLEAR_INF"])
    p.check(len(rate) == 2, f"{len(rate)} SFI_RATE_ERR_EID events; values 10,14,30,40,43,20,22,1 with limit 5 fault at 30 "
                            f"and at the counter reset to 20 (delta 20)")
    if rate:
        p.check("16" in rate[0].text, f"the first rate event does not name the delta 16: {rate[0].text!r}")
    p.check(len(clear) == 2, f"{len(clear)} SFI_WATCH_CLEAR_INF_EID events; the RATE watch clears at 43 and at 22")
    h2 = n.hk("SFI")
    p.check(h2["RateFaults"] - h1["RateFaults"] == 2, f"RateFaults rose by {h2['RateFaults'] - h1['RateFaults']}")
    p.check(h2["WatchesInFault"] == 0, f"WatchesInFault {h2['WatchesInFault']} after both watches cleared")
    p.raise_if()


def c_sfi_commands(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    h0 = n.hk("SFI")
    mark = n.g.mark()
    n.cmd("SFI", "SET_MISS_LIMIT", struct.pack("<B3x", 0))
    n.cmd("SFI", "SET_MISS_LIMIT", struct.pack("<B3x", 61))
    n.cmd("SFI", "SET_CLUSTER_WINDOW", struct.pack("<HH", 0, 0))
    n.cmd("SFI", "SET_CLUSTER_WINDOW", struct.pack("<HH", 601, 0))
    p.check(len(n.wait_event(mark, "SFI", EID["SET_PARAM_ERR"], count=4)) == 4, "out-of-range SET_* commands did not each produce event 20")
    mark = n.g.mark()
    n.cmd("SFI", "SET_MISS_LIMIT", struct.pack("<B3x", 5))
    n.cmd("SFI", "SET_CLUSTER_WINDOW", struct.pack("<HH", 10, 0))
    p.check(len(n.wait_event(mark, "SFI", EID["SET_PARAM_INF"], count=2)) == 2, "valid SET_* commands did not each produce event 19")
    hk = n.hk("SFI")
    p.check(hk["HbMissLimit"] == 5 and hk["ClusterWindowSec"] == 10, f"HbMissLimit/ClusterWindowSec {hk['HbMissLimit']}/{hk['ClusterWindowSec']}")
    p.check(hk["CommandErrorCounter"] == h0["CommandErrorCounter"] + 4 and hk["CommandCounter"] == h0["CommandCounter"] + 2,
            f"four rejected and two accepted SET_* commands moved CommandErrorCounter/CommandCounter by "
            f"{hk['CommandErrorCounter'] - h0['CommandErrorCounter']}/{hk['CommandCounter'] - h0['CommandCounter']} (expected 4/2)")
    mark = n.g.mark()
    status(n)
    p.check(bool(n.events(mark, "SFI", EID["STATUS_INF"])), "SEND_STATUS produced no event 21")
    p.check(n.hk("SFI")["CommandCounter"] == hk["CommandCounter"] + 1, "SEND_STATUS did not increment CommandCounter")
    mark = n.g.mark()
    n.cmd("SFI", "RESET_COUNTERS")
    p.check(bool(n.wait_event(mark, "SFI", EID["RESET_INF"])), "RESET_COUNTERS produced no event 4")
    hk = n.hk("SFI")
    p.check(hk["CommandCounter"] == 0 and hk["CommandErrorCounter"] == 0 and hk["HbMissLimit"] == 5,
            f"after RESET_COUNTERS counters {hk['CommandCounter']}/{hk['CommandErrorCounter']}, HbMissLimit {hk['HbMissLimit']}")
    mark = n.g.mark()
    n.send(spec.app_mid("SFI", "CMD"), 99)
    n.send(spec.app_mid("SFI", "CMD"), 0, b"\0\0\0\0")
    time.sleep(0.5)
    p.check(bool(n.events(mark, "SFI", 2)) and bool(n.events(mark, "SFI", 6)), "an unknown code and a wrong length did not produce events 2 and 6")
    h2 = n.hk("SFI")
    p.check(h2["CommandErrorCounter"] == hk["CommandErrorCounter"] + 2 and h2["CommandCounter"] == hk["CommandCounter"],
            "an unknown code and a wrong length did not each count once in CommandErrorCounter (and not in CommandCounter)")
    p.raise_if()
