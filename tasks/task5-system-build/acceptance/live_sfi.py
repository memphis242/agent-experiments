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
    enabled: int
    event_count: int


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
    largest_apps_mask: int
    largest_watches_mask: int
    active: tuple[int, ...]
    latched: tuple[int, ...]
    faults: tuple[FaultReport, ...]

    def app(self, name: str) -> AppStatus | None:
        return next((a for a in self.apps if a.name == name), None)


@dataclass(frozen=True)
class FaultReport:
    sequence: int
    fault_type: int
    source: int
    severity: int
    active: int
    latched: int
    node: int
    name: str
    onset_secs: int
    onset_subsecs: int
    change_secs: int
    change_subsecs: int
    raw: bytes

    @property
    def slot(self) -> int:
        if self.fault_type == 1:
            return self.source
        if self.fault_type in (2, 3, 4):
            return 16 + self.source
        if self.fault_type == 5:
            return 32 + self.source
        if self.fault_type == 6:
            return 48 + self.source
        if self.fault_type == 7:
            return 56
        raise Fail(f"invalid fault report type {self.fault_type}")


def parse_fault(payload: bytes) -> FaultReport:
    if len(payload) != 48:
        raise Fail(f"SFI report payload length {len(payload)}, expected 48")
    values = list(struct.unpack(spec.SFI_FAULT_FMT, payload))
    if b"\0" not in values[7]:
        raise Fail("SFI report AppName is not terminated")
    values[7] = values[7].split(b"\0", 1)[0].decode("ascii")
    return FaultReport(*values, payload)


def parse_status(pkt: bytes) -> Status:
    need = spec.TLM_HDR + spec.SFI_STATUS_SIZE
    if len(pkt) != need:
        raise Fail(f"the SFI status packet is {len(pkt)} bytes; the specified full packet needs {need}")
    o = spec.TLM_HDR
    missing, corr, hcount, ccount, largest = struct.unpack_from("<BBHHH", pkt, o)
    if hcount > 16:
        raise Fail(f"SFI HistoryCount {hcount} exceeds 16")
    apps = []
    for i in range(16):
        name, st, mc, tm, ls, lss, enabled, events, spare = struct.unpack_from(spec.SFI_APP_STATUS_FMT, pkt, o + 8 + i * 36)
        apps.append(AppStatus(name.split(b"\0", 1)[0].decode("ascii"), st, mc, tm, ls, lss, enabled, events))
    hist = tuple(struct.unpack_from(spec.SFI_MISS_RECORD_FMT, pkt, o + 584 + i * 16) for i in range(16))
    watches = tuple(struct.unpack_from("<B3xI", pkt, o + 840 + i * 12) for i in range(16))
    amask, wmask = struct.unpack_from("<HH", pkt, o + 1032)
    active = tuple(pkt[o + 1036:o + 1093])
    latched = tuple(pkt[o + 1093:o + 1150])
    faults = tuple(parse_fault(pkt[o + 1152 + i * 48:o + 1200 + i * 48]) for i in range(57))
    return Status(missing, corr, hcount, ccount, largest, tuple(apps), hist, watches, amask, wmask, active, latched, faults)


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
    for name in ("STOR", "CMDI", "TLMO", "GW", "MODE"):
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
        p.check(miss[0].t - t0 <= 5.0, f"the miss event came {miss[0].t - t0:.1f} s after the stop (N + 2 is 5 s)")
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


def reports(n: Node, mark: int = 0) -> list[FaultReport]:
    """Read mirrored reports and require matching command-form payloads on the raw tap."""
    out = [parse_fault(d.data[spec.TLM_HDR:]) for d in n.g.since(mark, spec.app_mid('SFI', 'FAULT_TLM', n.id))]
    commands = [d.data[8:] for d in n.g.since(mark, spec.app_mid('SFI', 'FAULT', n.id))]
    for report in out:
        if report.raw not in commands:
            raise Fail(f'SFI report {report.sequence} has no byte-identical SFI_FAULT command payload')
    return out


def wait_report(n: Node, mark: int, slot: int, active: int, timeout: float = 4.0) -> FaultReport:
    found: list[FaultReport] = []
    def ready() -> bool:
        found[:] = [r for r in reports(n, mark) if r.slot == slot and r.active == active]
        return bool(found)
    if not n.g.wait(ready, timeout):
        raise Fail(f'no SFI report for slot {slot} Active {active} within {timeout}s')
    return found[-1]


def feed(n: Node, topic: int, values: tuple[int, ...], width: int = 4) -> None:
    """Paced verification stimuli stay below the live burst envelope."""
    for index, value in enumerate(values):
        n.send_raw(n.frame(watch_packet(spec.verif_mid(topic, n.id), value, width, index)))
        time.sleep(0.65)


def parse_fault_log(data: bytes) -> tuple[int, tuple[FaultReport, ...]]:
    if len(data) < 16:
        raise Fail('fault log shorter than its 16-byte header')
    magic, version, size, count, last = struct.unpack_from('<IHHII', data)
    if magic != 0x5346494C or version != 1 or size != 48 or count > 64 or len(data) != 16 + count * 48:
        raise Fail(f'invalid fault log header/length {magic:#x}/{version}/{size}/{count}/{len(data)}')
    result = tuple(parse_fault(data[16+i*48:64+i*48]) for i in range(count))
    if result and (result[-1].sequence != last or any(((b.sequence-a.sequence)&0xFFFFFFFF) != 1 for a,b in zip(result,result[1:]))):
        raise Fail('fault log serials are not consecutive oldest-first or LastSequence differs')
    if not result and last != 0:
        raise Fail('empty initial fault log has nonzero LastSequence')
    return last, result


def c_limit_latch_enable(ctx: 'Ctx') -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    mark = n.g.mark()
    feed(n, 0xEF, (9, 9, 10, 9, 9))
    p.check(not [r for r in reports(n, mark) if r.slot == 22], 'LIMIT triggered before three consecutive out-of-range samples')
    feed(n, 0xEF, (9,))
    onset = wait_report(n, mark, 22, 1)
    p.check(onset.fault_type == 4 and onset.severity == 2 and onset.latched == 1 and onset.node == 1,
            'LIMIT report type/severity/latch/node differs from critical watch6')
    errors = n.hk('SFI')['CommandErrorCounter']
    n.cmd('SFI','ACK_FAULT',bytes((22,0,0,0)))
    p.check(n.hk('SFI')['CommandErrorCounter'] == (errors+1)%256, 'active ACK did not reject once')
    feed(n, 0xEF, (10,))
    clear = wait_report(n, mark, 22, 0)
    p.check((clear.onset_secs,clear.onset_subsecs) == (onset.onset_secs,onset.onset_subsecs) and clear.latched == 1,
            'clear changed onset identity or dropped retained latch')
    st = status(n)
    p.check(st.active[22] == 0 and st.latched[22] == 1, 'inactive fault must retain latch')
    n.cmd('SFI','ACK_FAULT',bytes((22,0,0,0)))
    st = status(n)
    p.check(st.latched[22] == 0 and st.faults[22].raw == clear.raw, 'ACK must only change current latch array')
    n.cmd('SFI','SET_WATCH_ENABLE',bytes((6,0,0,0)))
    feed(n,0xEF,(9,9,9))
    p.check(status(n).active[22] == 0, 'disabled LIMIT consumed samples')
    n.cmd('SFI','SET_WATCH_ENABLE',bytes((6,1,0,0)))
    feed(n,0xEF,(9,9))
    p.check(status(n).active[22] == 0, 're-enable retained consecutive baseline')
    feed(n,0xEF,(9,))
    p.check(status(n).active[22] == 1, 're-enabled LIMIT did not trigger third violation')
    # Disable and re-enable a verification app, with disabled heartbeats ignored.
    n.cmd('SFI','SET_APP_ENABLE',bytes((4,0,0,0)))
    n.send_raw(n.frame(heartbeat('SFI_T1',1)))
    time.sleep(1.2)
    app = status(n).app('SFI_T1')
    p.check(app is not None and app.state == 0 and app.enabled == 0, 'disabled app heartbeat armed monitoring')
    n.cmd('SFI','SET_APP_ENABLE',bytes((4,1,0,0)))
    p.check(status(n).app('SFI_T1').state == 0, 're-enabled app must start unarmed')
    p.raise_if()


def c_event_rate(ctx: 'Ctx') -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    mark=n.g.mark()
    for _ in range(7):
        n.cmd('GW','NOOP',gap=0.65)
    p.check(not [r for r in reports(n,mark) if r.slot==35], 'GW INFORMATION events triggered ERROR rate detector')
    mark=n.g.mark()
    for _ in range(6):
        n.send(spec.app_mid('GW','CMD',n.id),127)
        time.sleep(0.65)
    onset=wait_report(n,mark,35,1)
    p.check(onset.fault_type==5 and onset.name=='GW', 'event-rate source identity differs')
    clear=wait_report(n,mark,35,0,13)
    p.check((clear.onset_secs,clear.onset_subsecs)==(onset.onset_secs,onset.onset_subsecs), 'event expiry clear lost onset')
    p.check(len([r for r in reports(n,mark) if r.slot==35])==2, 'event rate emitted duplicate edges')
    p.raise_if()


def c_mixed_cluster(ctx: 'Ctx') -> None:
    n=boot(ctx,(1,))[1]
    p=Problems()
    n.cmd('SFI','CLEAR_HISTORY')
    n.send_raw(n.frame(heartbeat('SFI_T1',1)))
    mark=n.g.mark()
    wait_report(n,mark,4,1,6)
    feed(n,0xEC,(5,5,5,5))
    wait_report(n,mark,20,1)
    st=status(n)
    p.check(st.history_count==2 and st.largest==2 and st.correlation==2,
            f'mixed heartbeat/STALL history metrics {st.history_count}/{st.largest}/{st.correlation}')
    p.check(st.largest_apps_mask==1<<4 and st.largest_watches_mask==1<<4,
            f'mixed cluster attribution masks {st.largest_apps_mask:#x}/{st.largest_watches_mask:#x}')
    n.cmd('SFI','CLEAR_HISTORY')
    feed(n,0xEC,(6,6,6,6))
    time.sleep(6.1)
    feed(n,0xEF,(9,9,9))
    st=status(n)
    p.check(st.history_count==2 and st.largest==1 and st.correlation==1,'onsets separated outside W must be uncorrelated')
    p.raise_if()


def c_peer_health(ctx: 'Ctx') -> None:
    from live_gw import setup_dual_links
    nodes=boot(ctx,(1,2))
    p=Problems()
    for n in nodes.values():
        p.check(n.hk('SFI')['PeerArmed']==0,'standalone startup armed an absent peer')
        for src,dst,fc,length in ((n.id,3-n.id,0,12),(3, n.id,0,12),(3-n.id,3-n.id,0,12),
                                  (3-n.id,n.id,1,12),(3-n.id,n.id,0,11)):
            payload=struct.pack('<4BII',src,dst,0,0,1,0)
            n.send(spec.SFI_HEALTH_MID,fc,payload[:length])
            time.sleep(0.15)
    time.sleep(11)
    for n in nodes.values():
        hk=n.hk('SFI')
        p.check(hk['PeerArmed']==0 and hk['PeerLost']==0,'own/malformed health armed or refreshed an absent peer')
    links=setup_dual_links(ctx,nodes)
    time.sleep(3)
    for n in nodes.values():
        p.check(n.hk('SFI')['PeerArmed']==1 and n.hk('SFI')['PeerLost']==0,'valid health did not arm peer')
    marks={i:n.g.mark() for i,n in nodes.items()}
    cut=time.monotonic();links.set_enabled(False)
    for i,n in nodes.items():
        report=wait_report(n,marks[i],56,1,13)
        edge = [d for d in n.g.since(marks[i],spec.app_mid('SFI','FAULT_TLM',i)) if d.data[16:] == report.raw]
        p.check(bool(edge) and 9 <= edge[0].t-cut <= 12, 'NODE_LOSS onset outside[9,12]s from steady relay cut')
        # Report change time is exact onboard evidence independent of HK observation.
        health=n.g.since(0,spec.SFI_HEALTH_MID)
        p.check(report.node==i and report.fault_type==7,'NODE_LOSS reports local node identity')
    links.set_enabled(True)
    for i,n in nodes.items():
        wait_report(n,marks[i],56,0,5)
    p.raise_if()


def ground_capture(n: Node, mark: int, path: str) -> bytes:
    """Validate all native segment metadata and reconstruct a completed ground transfer."""
    from live_stor import parse_segment, judge_segments
    segments=[parse_segment(d.data,spec.TLM_HDR) for d in n.g.since(mark,spec.app_mid('STOR','XFER_TLM',n.id))]
    if not segments:
        raise Fail('ground transfer produced no segment telemetry')
    first=segments[0]
    indexes=[segment.index for segment in segments]
    if len(set(indexes))!=len(indexes) or sorted(indexes)!=list(range(first.count)):
        raise Fail(f'missing/duplicate STOR segments {indexes}')
    content=b''.join(segment.data for segment in sorted(segments,key=lambda segment:segment.index))
    errors=judge_segments(segments,content,transfer_id=first.transfer_id,src=n.id,dst=0,area=0,dest_path=path)
    if errors:
        raise Fail('invalid STOR ground transfer: '+'; '.join(errors))
    return content


def ground_file(n: Node, path: str) -> bytes:
    from live_stor import transfer_payload
    mark=n.g.mark()
    n.cmd('STOR','TRANSFER',transfer_payload(0,0,0,path))
    if not n.wait_event(mark,'STOR',spec.EID['STOR']['XFER_DONE_INF'],12):
        raise Fail(f'STOR ground transfer of {path} did not complete')
    return ground_capture(n,mark,path)


def c_log_ground(ctx: 'Ctx') -> None:
    import zlib
    from live_stor import add_payload, file_info
    n=boot(ctx,(1,),quiet_stor=True)[1]
    n.cmd('TLMO','SET_BUDGET',struct.pack('<H2x',256))
    mark=n.g.mark()
    feed(n,0xEF,(9,9,9,10))
    observed=reports(n,mark)
    if len([r for r in observed if r.slot==22])!=2:
        raise Fail('expected observed LIMIT onset and clear before fault dump')
    dumpmark=n.g.mark();n.cmd('SFI','DUMP_FAULT_LOG')
    if not n.wait_event(dumpmark,'SFI',spec.EID['SFI']['DUMP_INF'],2):
        raise Fail('fault dump did not complete with DUMP_INF')
    n.cmd('STOR','ADD_FILE',add_payload('/cf/sfi_faults.bin','sfi/log.bin'))
    info=file_info(n,0,'sfi/log.bin')
    content=ground_file(n,'sfi/log.bin')
    last,entries=parse_fault_log(content)
    if info['Size']!=len(content) or info['Crc']!=(zlib.crc32(content)&0xFFFFFFFF):
        raise Fail('FILE_INFO size/CRC differ from ground fault log')
    payloads=[r.raw for r in entries]
    if not all(r.raw in payloads for r in observed) or last!=entries[-1].sequence:
        raise Fail('ground dump does not retain exact observed report bytes in chronology')
