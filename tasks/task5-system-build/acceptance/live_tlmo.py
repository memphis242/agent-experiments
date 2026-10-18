"""Live checks of TLMO (S4): always-on output, priority classes, budget, eviction, streams, tables, probes."""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import frames as fr
import spec
import tables as tb
from common import Fail, Problems
from live_common import boot
from node import Node

if TYPE_CHECKING:
    from check import Ctx

E8, E9, EA, EB, EE, EF = (spec.verif_mid(t) for t in (0xE8, 0xE9, 0xEA, 0xEB, 0xEE, 0xEF))
CLASS_OF = {E8: 0, E9: 1, EA: 2, EB: 3}
TLMO_HK = spec.app_mid("TLMO", "HK_TLM")
CLASS_TLM = spec.app_mid("TLMO", "CLASS_TLM")
EVS = spec.evs_long_mid()
EID = spec.EID["TLMO"]
VERIF_SIZE = 40


def verif(mid: int, index: int) -> bytes:
    return fr.build_verification(mid, VERIF_SIZE, counter=index, fill=index & 0xFF)


def index_of(pkt: bytes) -> int:
    return struct.unpack_from("<I", pkt, 16)[0]


def stream_payload(mid: int, cls: int) -> bytes:
    return struct.pack("<IB3x", mid, cls)


def budget_payload(n: int) -> bytes:
    return struct.pack("<HH", n, 0)


@dataclass(frozen=True)
class ClassMap:
    budget: int
    count: int
    streams: tuple[tuple[int, int], ...]  # 64 slots (mid, class)


def parse_class_map(pkt: bytes) -> ClassMap:
    need = spec.TLM_HDR + spec.TLMO_CLASS_SIZE
    if len(pkt) < need:
        raise Fail(f"the class map packet is {len(pkt)} bytes; the specified payload needs {need}")
    budget, count = struct.unpack_from("<HH", pkt, spec.TLM_HDR)
    streams = tuple(struct.unpack_from("<IB", pkt, spec.TLM_HDR + 4 + i * 8) for i in range(spec.TLMO_MAX_STREAMS))
    return ClassMap(budget, count, streams)


def class_map(n: Node) -> ClassMap:
    n.ensure_command_path()
    for _ in range(2):
        got = n.request(lambda: n.cmd("TLMO", "SEND_CLASS_MAP", gap=0), spec.app_mid("TLMO", "CLASS_TLM", n.id))
        if got:
            return parse_class_map(got[-1].data)
    raise Fail(f"{n.cpu}: no TLMO_CLASS_TLM_MID packet followed SEND_CLASS_MAP")


HOLD_TABLE = tb.TlmoTable(budget=16, capacity=8, streams=((E8, 0), (E9, 1), (EA, 2), (EB, 3), (TLMO_HK, 0), (EVS, 0),
                                                          (CLASS_TLM, 2)))


def hold_session(ctx: "Ctx") -> Node:
    """cpu1 with the verification streams of each class, TLMO's own housekeeping, events and the class map, a
    queue of 8 packets, loaded through cFE Table Services."""
    key = "tlmo-hold"
    cached = ctx.cache.get(key)
    if cached is not None and cached.alive():
        return cached
    n = boot(ctx, (1,))[1]

    def confirm() -> bool:
        try:
            cm = class_map(n)
        except Fail:
            return False
        return cm.count == 7 and dict(x for x in cm.streams if x[0]).get(EB) == 3

    n.load_table("TLMO", HOLD_TABLE.pack(), confirm=confirm)
    ctx.cache[key] = n
    return n


def hold(n: Node, bursts: list[list[tuple[int, int]]], release_budget: int, gap: float = 0.6,
         collect_s: float = 1.6) -> list[tuple[float, int, int]]:
    """Hold output (budget 0) with events and TLMO housekeeping removed, inject `bursts` of (mid, index), then
    raise the budget and return the verification datagrams that arrive as (time, mid, index)."""
    n.cmd("TLMO", "REMOVE_STREAM", struct.pack("<I", EVS))
    n.cmd("TLMO", "REMOVE_STREAM", struct.pack("<I", TLMO_HK))
    n.cmd("TLMO", "SET_BUDGET", budget_payload(0))
    time.sleep(0.5)
    for i, burst in enumerate(bursts):
        n.inject([verif(m, k) for m, k in burst], burst=8, gap=0.0)
        if i + 1 < len(bursts):
            time.sleep(gap)
    time.sleep(0.8)
    mark = n.g.mark()
    n.cmd("TLMO", "SET_BUDGET", budget_payload(release_budget))
    time.sleep(collect_s)
    out = [(d.t, d.mid, index_of(d.data)) for d in n.g.since(mark) if d.mid in CLASS_OF or d.mid in (EE, EF)]
    n.cmd("TLMO", "SET_BUDGET", budget_payload(HOLD_TABLE.budget))
    n.cmd("TLMO", "ADD_STREAM", stream_payload(TLMO_HK, 0))
    n.cmd("TLMO", "ADD_STREAM", stream_payload(EVS, 0))
    time.sleep(0.3)
    return out


def background_between(n: Node, start_mark: int, end_mark: int) -> int:
    """Class-0 background datagrams (TLMO housekeeping, events) TLMO forwarded between two marks."""
    return sum(1 for d in n.g.since(start_mark)[: max(0, end_mark - start_mark)] if d.mid in (TLMO_HK, EVS))


def hk_with_mark(n: Node) -> tuple[dict[str, int], int]:
    """TLMO housekeeping and the ground mark at which that packet arrived."""
    n.ensure_command_path()
    send = spec.app_mid("TLMO", "SEND_HK")
    n.settle()
    mark = n.g.mark()
    n.send(send, 0)
    got = n.g.wait_mid(mark, TLMO_HK, 3.0)
    if not got:
        raise Fail("no TLMO housekeeping followed a framed SEND_HK")
    idx = next(i for i, d in enumerate(n.g.since(0)) if d is got[0])
    from node import parse_hk
    return parse_hk("TLMO", got[0].data), idx


DROP_BURSTS = [
    [(EB, 100 + i) for i in range(6)],
    [(EA, 200 + i) for i in range(4)],
    [(E9, 300 + i) for i in range(5)],
    [(E8, 400 + i) for i in range(3)],
    [(EA, 206), (EA, 207), (E8, 403)],
]


def expected_queue(bursts: list[list[tuple[int, int]]], capacity: int, cls: dict[int, int]) -> tuple[list[tuple[int, int]], list[int]]:
    """Reference model of S4.2 enqueue and eviction: the queue in drain order and the drops per class."""
    queue: list[tuple[int, int, int]] = []  # (class, mid, index) in arrival order
    drops = [0, 0, 0, 0]
    for burst in bursts:
        for mid, index in burst:
            c = cls[mid]
            if len(queue) < capacity:
                queue.append((c, mid, index))
                continue
            lowest = max(q[0] for q in queue)
            if lowest > c:
                victim = next(i for i, q in enumerate(queue) if q[0] == lowest)
                drops[lowest] += 1
                del queue[victim]
                queue.append((c, mid, index))
            else:
                drops[c] += 1
    order = [(m, k) for c, m, k in sorted(queue, key=lambda q: q[0])]  # stable: arrival order within a class
    return order, drops


def c_hold_drop_arithmetic(ctx: "Ctx") -> None:
    n = hold_session(ctx)
    p = Problems()
    want_order, want_drops = expected_queue(DROP_BURSTS, 8, CLASS_OF)
    assert want_drops == [0, 1, 6, 6] and len(want_order) == 8
    hk_b, mark_b = hk_with_mark(n)
    got = hold(n, DROP_BURSTS, release_budget=2)
    hk_a, mark_a = hk_with_mark(n)
    order = [(m, k) for _t, m, k in got]
    p.check(order == want_order, f"datagrams after the hold arrived as {[(hex(m), k) for m, k in order]}; "
                                 f"expected {[(hex(m), k) for m, k in want_order]}")
    drops = [hk_a[f"Dropped{c}"] - hk_b[f"Dropped{c}"] for c in range(4)]
    p.check(drops == want_drops, f"DroppedByClass rose by {drops}; the eviction rule gives {want_drops}")
    bg = background_between(n, mark_b, mark_a)
    injected = sum(len(b) for b in DROP_BURSTS)
    sent = [hk_a[f"Sent{c}"] - hk_b[f"Sent{c}"] for c in range(4)]
    p.check(sent == [4 + bg, 4, 0, 0], f"SentByClass rose by {sent}; expected {[4 + bg, 4, 0, 0]}")
    rec = hk_a["PacketsReceived"] - hk_b["PacketsReceived"]
    p.check(rec == injected + bg, f"PacketsReceived rose by {rec}; expected {injected + bg} ({injected} injected, {bg} background)")
    p.check(hk_a["QueuedPackets"] == 0, f"QueuedPackets {hk_a['QueuedPackets']} after the queue drained")
    p.raise_if()


def c_drain_order(ctx: "Ctx") -> None:
    n = hold_session(ctx)
    p = Problems()
    burst = [[(EB, 500), (E9, 501), (EA, 502), (E8, 503), (EB, 504), (EA, 505), (E9, 506), (E8, 507)]]
    want, drops = expected_queue(burst, 8, CLASS_OF)
    assert drops == [0, 0, 0, 0]
    hk_b, _ = hk_with_mark(n)
    got = hold(n, burst, release_budget=3)
    hk_a, _ = hk_with_mark(n)
    order = [(m, k) for _t, m, k in got]
    p.check(order == want, f"datagrams arrived as {[(hex(m), k) for m, k in order]}; expected class 0 first, then 1, 2, 3, "
                           f"each class in arrival order: {[(hex(m), k) for m, k in want]}")
    if len(got) == 8:
        p.check(got[-1][0] - got[0][0] >= 0.12, f"8 packets with a budget of 3 arrived within {got[-1][0] - got[0][0]:.2f} s "
                                                f"(at most 3 per 10 Hz wakeup)")
    p.check(all(hk_a[f"Dropped{c}"] == hk_b[f"Dropped{c}"] for c in range(4)), "packets were dropped below capacity")
    p.raise_if()


def c_set_class(ctx: "Ctx") -> None:
    n = hold_session(ctx)
    p = Problems()
    mark = n.g.mark()
    n.cmd("TLMO", "SET_CLASS", stream_payload(EB, 0))
    p.check(bool(n.wait_event(mark, "TLMO", EID["SET_CLASS_INF"])), "SET_CLASS produced no TLMO_SET_CLASS_INF_EID")
    cm = class_map(n)
    p.check(dict(x for x in cm.streams if x[0]).get(EB) == 0, "the class map does not show the new class of the stream")
    got = hold(n, [[(E9, 600), (EB, 601), (E9, 602), (EB, 603)]], release_budget=4)
    order = [(m, k) for _t, m, k in got]
    p.check(order == [(EB, 601), (EB, 603), (E9, 600), (E9, 602)],
            f"after SET_CLASS to 0 the stream did not drain first: {[(hex(m), k) for m, k in order]}")
    mark = n.g.mark()
    n.cmd("TLMO", "SET_CLASS", stream_payload(EE, 1))
    n.cmd("TLMO", "SET_CLASS", stream_payload(E9, 4))
    p.check(len(n.wait_event(mark, "TLMO", EID["SET_CLASS_ERR"], count=2)) == 2,
            "SET_CLASS of an unused stream and of class 4 did not each produce TLMO_SET_CLASS_ERR_EID")
    n.cmd("TLMO", "SET_CLASS", stream_payload(EB, 3))
    p.raise_if()


def c_set_budget(ctx: "Ctx") -> None:
    n = hold_session(ctx)
    p = Problems()
    before = n.hk("TLMO")["BudgetPacketsPerCycle"]
    mark = n.g.mark()
    n.cmd("TLMO", "SET_BUDGET", budget_payload(257))
    p.check(bool(n.wait_event(mark, "TLMO", EID["SET_BUDGET_ERR"])), "SET_BUDGET 257 produced no TLMO_SET_BUDGET_ERR_EID")
    p.check(n.hk("TLMO")["BudgetPacketsPerCycle"] == before, "a rejected SET_BUDGET changed the budget")
    mark = n.g.mark()
    n.cmd("TLMO", "SET_BUDGET", budget_payload(256))
    p.check(bool(n.wait_event(mark, "TLMO", EID["SET_BUDGET_INF"])), "SET_BUDGET 256 produced no TLMO_SET_BUDGET_INF_EID")
    p.check(n.hk("TLMO")["BudgetPacketsPerCycle"] == 256, "BudgetPacketsPerCycle is not 256 after SET_BUDGET 256")
    got = hold(n, [[(E9, 700 + i) for i in range(6)]], release_budget=1, collect_s=1.8)
    p.check(len(got) == 6, f"{len(got)} of 6 held packets arrived after SET_BUDGET 1")
    if len(got) == 6:
        p.check(got[-1][0] - got[0][0] >= 0.35, f"6 packets with a budget of 1 arrived within {got[-1][0] - got[0][0]:.2f} s "
                                                f"(one per 10 Hz wakeup)")
    n.cmd("TLMO", "SET_BUDGET", budget_payload(16))
    p.raise_if()


def c_add_remove_stream(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    mark = n.g.mark()
    n.cmd("TLMO", "ADD_STREAM", stream_payload(EE, 2))
    p.check(bool(n.wait_event(mark, "TLMO", EID["ADD_STREAM_INF"])), "ADD_STREAM produced no TLMO_ADD_STREAM_INF_EID")
    mark = n.g.mark()
    n.inject([verif(EE, 800 + i) for i in range(3)], burst=3)
    time.sleep(0.6)
    p.check(len(n.g.since(mark, EE)) == 3, f"{len(n.g.since(mark, EE))} of 3 packets of an added stream arrived")
    cm = class_map(n)
    p.check(cm.count == 49 and dict(x for x in cm.streams if x[0]).get(EE) == 2, f"class map after ADD_STREAM: count {cm.count}")
    p.check(cm.streams.index(next(x for x in cm.streams if x[0] == EE)) == 48 if any(x[0] == EE for x in cm.streams) else False,
            "the added stream does not fill the lowest unused slot (48)")
    mark = n.g.mark()
    n.cmd("TLMO", "ADD_STREAM", stream_payload(EE, 1))
    n.cmd("TLMO", "ADD_STREAM", stream_payload(EF, 4))
    n.cmd("TLMO", "ADD_STREAM", stream_payload(0, 1))
    p.check(len(n.wait_event(mark, "TLMO", EID["ADD_STREAM_ERR"], count=3)) == 3,
            "ADD_STREAM of a used stream, of class 4 and of an invalid MID did not each produce TLMO_ADD_STREAM_ERR_EID")
    mark = n.g.mark()
    n.cmd("TLMO", "REMOVE_STREAM", struct.pack("<I", EE))
    p.check(bool(n.wait_event(mark, "TLMO", EID["REMOVE_STREAM_INF"])), "REMOVE_STREAM produced no TLMO_REMOVE_STREAM_INF_EID")
    mark = n.g.mark()
    n.inject([verif(EE, 810 + i) for i in range(3)], burst=3)
    time.sleep(0.8)
    p.check(not n.g.since(mark, EE), "packets of a removed stream still arrive")
    mark = n.g.mark()
    n.cmd("TLMO", "REMOVE_STREAM", struct.pack("<I", EF))
    p.check(bool(n.wait_event(mark, "TLMO", EID["REMOVE_STREAM_ERR"])), "REMOVE_STREAM of an unused stream produced no "
                                                                          "TLMO_REMOVE_STREAM_ERR_EID")
    n.cmd("TLMO", "REMOVE_STREAM", struct.pack("<I", E8))
    mark = n.g.mark()
    n.inject([verif(E8, 820)], burst=1)
    time.sleep(0.6)
    p.check(not n.g.since(mark, E8), "a table stream removed by REMOVE_STREAM still arrives")
    p.raise_if()


def c_table_update(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    n.cmd("TLMO", "ADD_STREAM", stream_payload(EE, 2))
    base = tb.TlmoTable.unpack(tb.split_image(n.table_image("TLMO"), "TLMO")[1])
    streams = tuple((m, 3 if m == E8 else c) for m, c in base.streams)
    new = tb.TlmoTable(budget=5, capacity=64, streams=streams)
    mark = n.g.mark()
    n.load_table("TLMO", new.pack())
    p.check(bool(n.wait_event(mark, "TLMO", EID["TBL_UPDATE_INF"], 3.0)), "a table update produced no TLMO_TBL_UPDATE_INF_EID")
    hk = n.hk("TLMO")
    p.check(hk["BudgetPacketsPerCycle"] == 5, f"BudgetPacketsPerCycle {hk['BudgetPacketsPerCycle']} after a table with budget 5")
    cm = class_map(n)
    used = dict(x for x in cm.streams if x[0])
    p.check(used.get(E8) == 3, "the class map does not show the class of the updated table")
    p.check(EE not in used, "a stream added by ADD_STREAM survived the table update")
    mark = n.g.mark()
    n.inject([verif(EE, 900)], burst=1)
    time.sleep(0.6)
    p.check(not n.g.since(mark, EE), "packets of an added stream still arrive after the table update")
    bad = tb.TlmoTable(budget=5, capacity=64, streams=tuple((m, 4 if m == E9 else c) for m, c in streams))
    try:
        n.load_table("TLMO", bad.pack(), expect_valid=False)
    except Fail as exc:
        p.check(False, str(exc))
    p.check(n.hk("TLMO")["BudgetPacketsPerCycle"] == 5, "a rejected table changed the active one")
    p.raise_if()


def c_class_map(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    mark = n.g.mark()
    cm = class_map(n)
    p.check(bool(n.wait_event(mark, "TLMO", EID["CLASS_MAP_INF"])), "SEND_CLASS_MAP produced no TLMO_CLASS_MAP_INF_EID")
    image = tb.TlmoTable.unpack(tb.split_image(n.table_image("TLMO"), "TLMO")[1])
    p.check(cm.budget == 16 and cm.count == 48, f"class map budget/StreamCount {cm.budget}/{cm.count} != 16/48")
    p.check(tuple(cm.streams) == tuple(image.streams), "the class map slots differ from the runtime list loaded from the table")
    p.raise_if()


def c_tlmo_commands(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    hk = n.hk("TLMO")
    p.check(hk["QueueCapacity"] == 64 and hk["ActiveStreams"] == 48 and hk["BudgetPacketsPerCycle"] == 16,
            f"QueueCapacity/ActiveStreams/Budget {hk['QueueCapacity']}/{hk['ActiveStreams']}/{hk['BudgetPacketsPerCycle']}")
    p.check(hk["PacketsReceived"] > 0 and hk["Sent1"] + hk["Sent0"] > 0, "PacketsReceived/SentByClass do not count forwarded telemetry")
    mark = n.g.mark()
    n.cmd("TLMO", "NOOP")
    p.check(bool(n.wait_event(mark, "TLMO", EID["NOOP_INF"])), "NOOP produced no event 3")
    n.cmd("TLMO", "SET_BUDGET", budget_payload(999))
    mark = n.g.mark()
    n.cmd("TLMO", "RESET_COUNTERS")
    p.check(bool(n.wait_event(mark, "TLMO", EID["RESET_INF"])), "RESET_COUNTERS produced no event 4")
    h2 = n.hk("TLMO")
    p.check(h2["CommandCounter"] == 0 and h2["CommandErrorCounter"] == 0, "RESET_COUNTERS did not zero the command counters")
    p.check(h2["PacketsReceived"] < hk["PacketsReceived"] and h2["SocketErrors"] == 0, "RESET_COUNTERS did not zero PacketsReceived")
    mark = n.g.mark()
    n.send(spec.app_mid("TLMO", "CMD"), 99)
    n.send(spec.app_mid("TLMO", "CMD"), 0, b"\0\0\0\0")
    time.sleep(0.5)
    p.check(bool(n.events(mark, "TLMO", EID["CC_ERR"])) and bool(n.events(mark, "TLMO", EID["CMD_LEN_ERR"])),
            "an unknown function code and a wrong length did not produce events 2 and 6")
    p.raise_if()


DEFAULT_TLM_NAMES = ("CFE_ES_HK_TLM_MID", "CFE_EVS_HK_TLM_MID", "CFE_SB_HK_TLM_MID", "CFE_TBL_HK_TLM_MID",
                     "CFE_TIME_HK_TLM_MID", "SAMPLE_APP_HK_TLM_MID", "STOR_HK_TLM_MID", "CMDI_HK_TLM_MID",
                     "TLMO_HK_TLM_MID", "GW_HK_TLM_MID", "SFI_HK_TLM_MID", "CFE_EVS_LONG_EVENT_MSG_MID")


def judge_default_telemetry(mids_seen: set[int], node: int) -> list[str]:
    missing = [n for n in DEFAULT_TLM_NAMES if spec.named_tlm_mid(n, node) not in mids_seen]
    return [f"no {', '.join(missing)} arrived"] if missing else []


def c_default_telemetry(ctx: "Ctx") -> None:
    """Housekeeping of cFE, SAMPLE_APP and the new apps, and events, arrive with no command at all."""
    n = boot(ctx, (1,))[1]
    first = n.g.since(0)[0].t
    end = first + 9.0
    while time.monotonic() < end:
        if not judge_default_telemetry({d.mid for d in n.g.since(0)}, 1):
            return
        time.sleep(0.3)
    items = judge_default_telemetry({d.mid for d in n.g.since(0)}, 1)
    if items:
        raise Fail("; ".join(items) + " within 9 s of the first datagram, with no enable command sent")


def c_cpu2_telemetry(ctx: "Ctx") -> None:
    n = boot(ctx, (2,))[2]
    first = n.g.since(0)[0].t
    end = first + 9.0
    while time.monotonic() < end and judge_default_telemetry({d.mid for d in n.g.since(0)}, 2):
        time.sleep(0.3)
    items = judge_default_telemetry({d.mid for d in n.g.since(0)}, 2)
    if items:
        raise Fail("cpu2 on UDP 2235: " + "; ".join(items))
    wrong = sorted({d.mid for d in n.g.since(0) if 0x0800 <= d.mid <= 0x08FF})
    if wrong:
        raise Fail(f"cpu2 sent cpu1 telemetry MIDs on UDP 2235: {[hex(m) for m in wrong[:5]]}")


def sb_counters(n: Node, mark: int) -> tuple[int, int] | None:
    got = n.g.since(mark, spec.named_tlm_mid("CFE_SB_HK_TLM_MID"))
    if not got:
        return None
    raw = got[-1].data
    return struct.unpack_from("<H", raw, spec.SB_HK_OVERFLOW_OFFSET)[0], struct.unpack_from("<H", raw, spec.SB_HK_MSGLIM_OFFSET)[0]


def c_pipe_depth_probe(ctx: "Ctx") -> None:
    """Pilot probe: TLMO's pipe (depth 48, limit 32 per stream) takes the 48 default streams and a burst of
    verification packets without software bus pipe overflows or message-limit errors."""
    n = boot(ctx, (1,))[1]
    n.ensure_command_path()
    sb_send_hk = spec.cmd_mid(spec.CFE_SB_SEND_HK_TOPIC)
    time.sleep(6.0)
    mark = n.g.mark()
    n.send(sb_send_hk, 0)
    n.g.wait_mid(mark, spec.named_tlm_mid("CFE_SB_HK_TLM_MID"), 3.0)
    before = sb_counters(n, mark)
    if before is None:
        raise Fail("no cFE SB housekeeping arrived after a framed CFE_SB SEND_HK")
    burst = [verif(m, 1000 + i) for i in range(8) for m in (E8, E9, EA, EB)]
    n.inject(burst, burst=32, gap=0.0)
    time.sleep(5.0)
    mark = n.g.mark()
    n.send(sb_send_hk, 0)
    n.g.wait_mid(mark, spec.named_tlm_mid("CFE_SB_HK_TLM_MID"), 3.0)
    after = sb_counters(n, mark)
    if after is None:
        raise Fail("no cFE SB housekeeping arrived after the burst")
    if after != before:
        raise Fail(f"software bus PipeOverflowErrorCounter/MsgLimitErrorCounter went from {before} to {after} during steady "
                   f"state and a burst of 32 verification packets")
