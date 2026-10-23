"""Live checks of CMDVAL/CMDI (S3): frames on UDP 1234/1235, counters in CMDI housekeeping, events through TLMO."""
from __future__ import annotations

import struct
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import frames as fr
import spec
from common import Fail, Problems, short
from live_common import boot, sample_counters
from node import Node

if TYPE_CHECKING:
    from check import Ctx

REASON_FIELDS = {"FORMAT": "FormatErrors", "CRC": "CrcErrors", "HMAC": "HmacErrors", "SOURCE": "SourceErrors",
                 "AGE": "AgeErrors", "REPLAY": "ReplayErrors"}
REJECT_EID = spec.EID["CMDI"]["REJECT_ERR"]


def deltas(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: b[k] - a[k] for k in a if k in b}


def noop_cmd(node: Node) -> bytes:
    return fr.build_command(spec.sample_cmd_mid(node.id), spec.SAMPLE_NOOP_CC)


def wait_sample_counter(node: Node, expected: int, timeout: float = 3.2) -> None:
    """Observe GW delivery through SAMPLE_APP execution and a following housekeeping reply."""
    end = time.monotonic() + timeout
    actual = sample_counters(node)[0]
    while actual != expected and time.monotonic() < end:
        time.sleep(0.1)
        actual = sample_counters(node)[0]
    if actual != expected:
        raise Fail(f"{node.cpu}: SAMPLE_APP CommandCounter {actual}, expected {expected} after GW ingress within {timeout:.1f} s")


def judge_reason_deltas(d: dict[str, int], want: dict[str, int]) -> list[str]:
    """Each reason counter must have risen by exactly `want` (missing reasons: 0)."""
    p = Problems()
    for reason, field in REASON_FIELDS.items():
        got = d.get(field, 0)
        p.check(got == want.get(reason, 0), f"{field} rose by {got}, expected {want.get(reason, 0)}")
    return p.items


def judge_reject_events(events: list, expected: list[tuple[str, int | None]]) -> list[str]:
    """`expected`: (reason, source id or None) per rejected frame; each needs one REJECT event naming them."""
    p = Problems()
    texts = [e.text for e in events]
    p.check(len(texts) == len(expected), f"{len(texts)} CMDI_REJECT_ERR_EID events for {len(expected)} rejected frames")
    pool = list(texts)
    for reason, src in expected:
        hit = next((t for t in pool if reason in t.upper() and (src is None or str(src) in t)), None)
        if p.check(hit is not None, f"no REJECT event names {reason}" + (f" and source {src}" if src is not None else "")):
            pool.remove(hit)
    p.check(all(e.etype == 3 for e in events), "a CMDI_REJECT_ERR_EID event is not of ERROR type")
    return p.items


def c_valid_frame_executes(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    h1 = n.hk("CMDI")
    s1 = sample_counters(n)
    n.send_raw(n.frame(noop_cmd(n)))
    s2 = sample_counters(n)
    h2 = n.hk("CMDI")
    d = deltas(h1, h2)
    p.check(s2[0] == s1[0] + 1, f"SAMPLE_APP CommandCounter {s1[0]} -> {s2[0]} after one valid frame carrying its NOOP")
    p.check(d["FramesReceived"] >= 4 and d["FramesAccepted"] == d["FramesReceived"],
            f"FramesReceived/FramesAccepted rose by {d['FramesReceived']}/{d['FramesAccepted']} for 4 valid frames")
    for item in judge_reason_deltas(d, {}):
        p.check(False, item)
    p.check(h2["EnableMask"] == 0x0F and h2["MaxAgeSeconds"] == 30, f"EnableMask/MaxAgeSeconds {h2['EnableMask']:#x}/{h2['MaxAgeSeconds']}")
    p.raise_if()


def c_unframed_format(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    h1 = n.hk("CMDI")
    s1 = sample_counters(n)
    mark = n.g.mark()
    n.send_raw(noop_cmd(n))
    s2 = sample_counters(n)
    h2 = n.hk("CMDI")
    d = deltas(h1, h2)
    p.check(s2[0] == s1[0], "an unframed SAMPLE_APP NOOP sent to UDP 1234 executed")
    for item in judge_reason_deltas(d, {"FORMAT": 1}):
        p.check(False, item)
    p.check(d["FramesReceived"] - d["FramesAccepted"] == 1, "the unframed packet was not counted as a received, rejected frame")
    for item in judge_reject_events(n.events(mark, "CMDI", REJECT_EID), [("FORMAT", None)]):
        p.check(False, item)
    p.raise_if()


def c_rejection_reasons(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    cmd = noop_cmd(n)
    good = n.frame(cmd, source_id=2)
    cases: list[tuple[str, bytes, str, int | None]] = [
        ("bad magic", n.frame(cmd, magic=0x4357), "FORMAT", None),
        ("bad version", n.frame(cmd, version=2), "FORMAT", None),
        ("nonzero flags", n.frame(cmd, flags=1), "FORMAT", None),
        ("CommandLength not matching the size", n.frame(cmd, length_field=9), "FORMAT", None),
        ("a datagram over 1074 bytes", good + bytes(1100 - len(good)), "FORMAT", None),
        ("bad CRC", fr.corrupt(n.frame(cmd, source_id=2), "crc"), "CRC", 2),
        ("bad CRC and bad MAC", fr.corrupt(n.frame(cmd, source_id=2), "crc-and-mac"), "CRC", 2),
        ("bad MAC", fr.corrupt(n.frame(cmd, source_id=2), "mac"), "HMAC", 2),
        ("unlisted source", n.frame(cmd, source_id=7), "SOURCE", 7),
        ("timestamp 45 s old", n.frame(cmd, source_id=2, time_offset=-45), "AGE", 2),
        ("timestamp 45 s ahead", n.frame(cmd, source_id=2, time_offset=45), "AGE", 2),
    ]
    h1 = n.hk("CMDI")
    s1 = sample_counters(n)
    mark = n.g.mark()
    for _label, data, _r, _s in cases:
        n.send_raw(data)
        time.sleep(0.06)
    time.sleep(0.6)
    events = n.events(mark, "CMDI", REJECT_EID)
    s2 = sample_counters(n)
    h2 = n.hk("CMDI")
    d = deltas(h1, h2)
    p.check(s2[0] == s1[0], f"SAMPLE_APP CommandCounter rose by {s2[0] - s1[0]}: a rejected frame executed")
    want: dict[str, int] = {}
    for _l, _d, r, _s in cases:
        want[r] = want.get(r, 0) + 1
    for item in judge_reason_deltas(d, want):
        p.check(False, item)
    p.check(d["FramesReceived"] - d["FramesAccepted"] == len(cases),
            f"{d['FramesReceived'] - d['FramesAccepted']} frames rejected of {len(cases)} invalid ones")
    for item in judge_reject_events(events, [(r, s) for _l, _d, r, s in cases]):
        p.check(False, item)
    p.raise_if()


def mask_payload(mask: int) -> bytes:
    return struct.pack("<B3x", mask)


def c_set_enable_mask(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    cmdi = spec.app_mid("CMDI", "CMD")
    cc = spec.CC["CMDI"]["SET_ENABLE_MASK"]
    mark = n.g.mark()
    n.cmd("CMDI", "SET_ENABLE_MASK", mask_payload(0x0D))
    p.check(bool(n.wait_event(mark, "CMDI", spec.EID["CMDI"]["SET_PARAM_INF"])), "SET_ENABLE_MASK 0x0D produced no event 14")
    h = n.hk("CMDI")
    p.check(h["EnableMask"] == 0x0D, f"EnableMask {h['EnableMask']:#x} after SET_ENABLE_MASK 0x0D")
    s1 = sample_counters(n)
    n.send_raw(fr.corrupt(n.frame(noop_cmd(n)), "mac"))
    s2 = sample_counters(n)
    p.check(s2[0] == s1[0] + 1, "with the HMAC bit cleared a frame with a bad MAC did not execute")
    mark = n.g.mark()
    n.cmd("CMDI", "SET_ENABLE_MASK", mask_payload(0x10))
    p.check(bool(n.wait_event(mark, "CMDI", spec.EID["CMDI"]["SET_PARAM_ERR"])), "SET_ENABLE_MASK 0x10 produced no event 15")
    p.check(n.hk("CMDI")["EnableMask"] == 0x0D, "a rejected SET_ENABLE_MASK changed the mask")
    n.cmd("CMDI", "SET_ENABLE_MASK", mask_payload(0x0F))
    p.check(n.hk("CMDI")["EnableMask"] == 0x0F, "SET_ENABLE_MASK 0x0F did not restore the mask")
    h1 = n.hk("CMDI")
    n.send_raw(fr.build_command(cmdi, cc, mask_payload(0x00)))
    n.send_raw(fr.corrupt(n.frame(fr.build_command(cmdi, cc, mask_payload(0x00))), "mac"))
    time.sleep(0.5)
    h2 = n.hk("CMDI")
    d = deltas(h1, h2)
    p.check(h2["EnableMask"] == 0x0F, f"an unframed or bad-MAC SET_ENABLE_MASK changed the mask to {h2['EnableMask']:#x}")
    for item in judge_reason_deltas(d, {"FORMAT": 1, "HMAC": 1}):
        p.check(False, item)
    s3 = sample_counters(n)
    n.send_raw(fr.corrupt(n.frame(noop_cmd(n)), "mac"))
    s4 = sample_counters(n)
    p.check(s4[0] == s3[0], "after restoring the mask a bad-MAC frame executed")
    p.raise_if()


def c_set_max_age(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    cmdi = spec.app_mid("CMDI", "CMD")
    cc = spec.CC["CMDI"]["SET_MAX_AGE"]
    s0 = sample_counters(n)
    n.send_raw(n.frame(noop_cmd(n), time_offset=-80))
    s1 = sample_counters(n)
    p.check(s1[0] == s0[0], "with MaxAgeSeconds 30 a frame 80 s old executed")
    mark = n.g.mark()
    n.cmd("CMDI", "SET_MAX_AGE", struct.pack("<I", 120))
    p.check(bool(n.wait_event(mark, "CMDI", spec.EID["CMDI"]["SET_PARAM_INF"])), "SET_MAX_AGE 120 produced no event 14")
    p.check(n.hk("CMDI")["MaxAgeSeconds"] == 120, "MaxAgeSeconds is not 120 after SET_MAX_AGE 120")
    n.send_raw(n.frame(noop_cmd(n), time_offset=-80))
    s2 = sample_counters(n)
    p.check(s2[0] == s1[0] + 1, "after SET_MAX_AGE 120 a frame 80 s old did not execute")
    mark = n.g.mark()
    n.cmd("CMDI", "SET_MAX_AGE", struct.pack("<I", 0))
    n.cmd("CMDI", "SET_MAX_AGE", struct.pack("<I", 86401))
    p.check(len(n.wait_event(mark, "CMDI", spec.EID["CMDI"]["SET_PARAM_ERR"], count=2)) == 2,
            "SET_MAX_AGE 0 and 86401 did not each produce event 15")
    n.send_raw(fr.build_command(cmdi, cc, struct.pack("<I", 5)))
    n.send_raw(fr.corrupt(n.frame(fr.build_command(cmdi, cc, struct.pack("<I", 5))), "mac"))
    time.sleep(0.5)
    p.check(n.hk("CMDI")["MaxAgeSeconds"] == 120, "an invalid, unframed or bad-MAC SET_MAX_AGE changed MaxAgeSeconds")
    p.raise_if()


def c_cmdi_commands(ctx: "Ctx") -> None:
    n = boot(ctx, (1,))[1]
    p = Problems()
    eid = spec.EID["CMDI"]
    mark = n.g.mark()
    n.cmd("CMDI", "NOOP")
    p.check(bool(n.wait_event(mark, "CMDI", eid["NOOP_INF"])), "NOOP produced no event 3")
    n.send_raw(fr.corrupt(n.frame(noop_cmd(n)), "crc"))
    h = n.hk("CMDI")
    p.check(h["CommandCounter"] >= 1 and h["CrcErrors"] >= 1, f"setup counters {h}")
    mark = n.g.mark()
    n.cmd("CMDI", "RESET_COUNTERS")
    p.check(bool(n.wait_event(mark, "CMDI", eid["RESET_INF"])), "RESET_COUNTERS produced no event 4")
    h = n.hk("CMDI")
    p.check(h["CommandCounter"] == 0 and h["CommandErrorCounter"] == 0 and h["CrcErrors"] == 0,
            f"RESET_COUNTERS left command or reason counters: {h}")
    p.check(h["FramesReceived"] <= 2 and h["FramesAccepted"] <= 2, f"RESET_COUNTERS did not zero the frame counters: {h}")
    mark = n.g.mark()
    n.send(spec.app_mid("CMDI", "CMD"), 99)
    n.send(spec.app_mid("CMDI", "CMD"), 0, b"\0\0\0\0")
    time.sleep(0.5)
    h2 = n.hk("CMDI")
    p.check(bool(n.events(mark, "CMDI", eid["CC_ERR"])), "an unknown function code produced no CMDI_CC_ERR_EID")
    p.check(bool(n.events(mark, "CMDI", eid["CMD_LEN_ERR"])), "a wrong length produced no CMDI_CMD_LEN_ERR_EID")
    p.check(h2["CommandErrorCounter"] == h["CommandErrorCounter"] + 2, "invalid commands did not each count as errors")
    p.raise_if()


def c_harness_live(ctx: "Ctx") -> None:
    """A frame built and sent by the agent's own `send-framed` is accepted by cpu1."""
    n = boot(ctx, (1,))[1]
    tools = ctx.tree / "tools" / "udp_harness.py"
    if not tools.is_file():
        raise Fail("tools/udp_harness.py does not exist")
    s1 = sample_counters(n)
    secs = n.onboard_seconds()
    try:
        proc = subprocess.run([sys.executable, str(tools), "send-framed", "--mid", f"{spec.sample_cmd_mid():#06x}", "--cc", "0",
                               "--node", "1", "--time-seconds", str(secs), "--sequence", "900"],
                              capture_output=True, text=True, errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        raise Fail("udp_harness.py send-framed did not return within 20 s") from None
    if proc.returncode != 0:
        raise Fail(f"udp_harness.py send-framed exited {proc.returncode}: {short(proc.stderr, 300)}")
    s2 = sample_counters(n)
    if s2[0] != s1[0] + 1:
        raise Fail(f"a SAMPLE_APP NOOP sent with udp_harness.py send-framed did not execute ({s1[0]} -> {s2[0]})")


def c_cpu2_frame(ctx: "Ctx") -> None:
    n = boot(ctx, (2,))[2]
    s1 = sample_counters(n)
    n.send_raw(n.frame(noop_cmd(n)))
    s2 = sample_counters(n)
    if s2[0] != s1[0] + 1:
        raise Fail(f"cpu2: a valid frame carrying SAMPLE_APP NOOP ({spec.sample_cmd_mid(2):#06x}) sent to UDP 1235 did not "
                   f"execute ({s1[0]} -> {s2[0]})")


SOURCE_COUNTERS = ("Accepted", "FormatErrors", "CrcErrors", "HmacErrors", "SourceErrors", "AgeErrors", "ReplayErrors")


def parse_source_stats(packet: bytes) -> tuple[dict[str, int], ...]:
    need = spec.TLM_HDR + spec.CMDI_SOURCE_SIZE
    if len(packet) != need:
        raise Fail(f"CMDI source statistics packet has {len(packet)} bytes, expected exactly {need}")
    rows = []
    for i in range(9):
        source, spare, *values = struct.unpack_from(spec.CMDI_SOURCE_FMT, packet, spec.TLM_HDR + i * 32)
        if spare:
            raise Fail(f"CMDI source row {i} has nonzero Spare")
        rows.append({"SourceId": source, **dict(zip(SOURCE_COUNTERS, values))})
    return tuple(rows)


def source_stats(n: Node) -> tuple[dict[str, int], ...]:
    got = n.request(lambda: n.cmd("CMDI", "SEND_SOURCE_STATS", gap=0),
                    spec.app_mid("CMDI", "SOURCE_TLM", n.id), timeout=2)
    if not got:
        raise Fail(f"{n.cpu}: SEND_SOURCE_STATS produced no reply within 2 s")
    rows = parse_source_stats(got[0].data)
    if tuple(row["SourceId"] for row in rows) != (1, 2, 0, 0, 0, 0, 0, 0, 0):
        raise Fail(f"{n.cpu}: source statistics do not follow the active AllowedSources order")
    return rows


def check_source_row(p: Problems, row: dict[str, int], **expected: int) -> None:
    for field in SOURCE_COUNTERS:
        p.check(row[field] == expected.get(field, 0), f"source {row['SourceId']} {field}={row[field]}, expected {expected.get(field, 0)}")


def c_replay_window(ctx: "Ctx") -> None:
    """Serial-window boundaries use source 1; all observations consume only source 2."""
    import tables as tb
    nodes = boot(ctx, (1, 2))
    n, peer = nodes[1], nodes[2]
    for node in nodes.values():
        node.load_table("CMDI", tb.CmdiTable().pack())
        node.cmd("CMDI", "RESET_COUNTERS")
    p = Problems()
    before = sample_counters(n)[0]
    mark = n.g.mark()
    frame100 = n.frame(noop_cmd(n), source_id=1, sequence=100)
    for frame in (frame100, n.frame(noop_cmd(n), source_id=1, sequence=37),
                  n.frame(noop_cmd(n), source_id=1, sequence=36), frame100):
        n.send_raw(frame)
        time.sleep(0.15)
    p.check(sample_counters(n)[0] == before + 2, "only serial100 and unseen37 execute;36 and duplicate100 do not")
    rows = source_stats(n)
    check_source_row(p, rows[0], Accepted=2, ReplayErrors=2)
    for row in rows[2:]:
        check_source_row(p, row)
    hk = n.hk("CMDI")
    p.check(hk["ReplayErrors"] == 2 and hk["FramesReceived"] - hk["FramesAccepted"] == 2,
            "aggregate CMDI counters count exactly two REPLAY rejections")
    for item in judge_reject_events(n.events(mark, "CMDI", REJECT_EID), [("REPLAY", 1)] * 2):
        p.check(False, item)
    peer.send_raw(frame100)
    peer.settle()
    check_source_row(p, source_stats(peer)[0], Accepted=1)
    # A successful table activation provides the clean window required for wrap.
    n.load_table("CMDI", tb.CmdiTable().pack())
    before = sample_counters(n)[0]
    for serial in (0xFFFFFFFF, 0, 0xFFFFFFFF):
        n.send_raw(n.frame(noop_cmd(n), source_id=1, sequence=serial))
        time.sleep(0.15)
    check_source_row(p, source_stats(n)[0], Accepted=2, ReplayErrors=1)
    p.check(sample_counters(n)[0] == before + 2, "FFFFFFFF->0 wraps forward; duplicateFFFFFFFF does not execute")
    p.raise_if()


def c_key_rotation(ctx: "Ctx") -> None:
    """Authenticated native table staging, ACTIVE switch, retired-key and replay rejection."""
    import socket
    import tables as tb
    n = boot(ctx, (1,))[1]
    p = Problems()
    original = n.key
    next_key = bytes(range(1, 33))
    n.load_table("CMDI", tb.CmdiTable(key=original, next_key=next_key, next_valid=1).pack())
    p.check(n.hk("CMDI")["NextValid"] == 1, "native LOAD/VALIDATE/ACTIVATE stages NEXT")
    n.cmd("CMDI", "RESET_COUNTERS")
    baseline = sample_counters(n)[0]
    n.send_raw(n.frame(noop_cmd(n), source_id=1, sequence=500))
    n.cmd("CMDI", "SWITCH_KEY", source_id=1, sequence=501)
    n.key = next_key
    hk = n.hk("CMDI")
    p.check(hk["KeySwitches"] == 1 and hk["NextValid"] == 0, "SWITCH_KEY consumes NEXT and increments KeySwitches")
    mark = n.g.mark()
    n.send_raw(n.frame(noop_cmd(n), source_id=1, sequence=502, key=original))
    n.send_raw(n.frame(noop_cmd(n), source_id=1, sequence=500))
    n.send_raw(n.frame(noop_cmd(n), source_id=1, sequence=502))
    n.settle()
    p.check(sample_counters(n)[0] == baseline + 2, "retired key and replay do not execute; fresh NEXT-key command executes")
    check_source_row(p, source_stats(n)[0], Accepted=3, HmacErrors=1, ReplayErrors=1)
    for item in judge_reject_events(n.events(mark, "CMDI", REJECT_EID), [("HMAC", 1), ("REPLAY", 1)]):
        p.check(False, item)
    # GW must share the active working copy for both ingress and egress.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as endpoint:
        endpoint.bind((spec.HOST, 51022))
        endpoint.settimeout(2)
        conn = tb.ConnDef(interface=spec.GW_IF["UDP"], enabled=1, local_port=51021,
                          remote_addr=spec.HOST, remote_port=51022, subs=(spec.verif_mid(0xEA),))
        n.cmd("GW", "ADD_CONN", bytes((0, 0, 0, 0)) + conn.pack())
        n.inject([fr.build_verification(spec.verif_mid(0xEA), 40, counter=701)])
        raw, _address = endpoint.recvfrom(16384)
        decoded = fr.parse_frame(raw, key=next_key)
        p.check(decoded.command == fr.build_verification(spec.verif_mid(0xEA), 40, counter=701),
                "GW egress authenticates under the switched shared ACTIVE key")
        endpoint.sendto(n.frame(noop_cmd(n), key=next_key, source_id=1, sequence=700), (spec.HOST, 51021))
        wait_sample_counter(n, (baseline + 3) % 256)
        # Table mask applies to GW as well as CMDI. Staging retains ACTIVE.
        n.load_table("CMDI", tb.CmdiTable(key=next_key, mask=0x0D, next_key=original, next_valid=1).pack())
        switch = fr.build_command(spec.app_mid("CMDI", "CMD"), spec.CC["CMDI"]["SWITCH_KEY"])
        bad = fr.corrupt(n.frame(switch, key=next_key, source_id=1, sequence=800), "mac")
        before_cmdi, before_gw = n.hk("CMDI"), n.hk("GW")
        n.send_raw(bad)
        endpoint.sendto(bad, (spec.HOST, 51021))
        n.last_send = time.monotonic()
        n.settle(1.1)
        after_cmdi, after_gw = n.hk("CMDI"), n.hk("GW")
        p.check(after_cmdi["HmacErrors"] == before_cmdi["HmacErrors"] + 1 and after_cmdi["NextValid"] == 1,
                "CMDI enforces mandatory SWITCH_KEY HMAC with ordinary HMAC disabled")
        p.check(after_gw["HmacErrors"] == before_gw["HmacErrors"] + 1 and after_gw["PacketsIngressed"] == before_gw["PacketsIngressed"],
                "GW enforces mandatory SWITCH_KEY HMAC before publishing")
        n.cmd("CMDI", "SWITCH_KEY", source_id=1, sequence=800)
        n.key = original
        n.cmd("CMDI", "SET_ENABLE_MASK", mask_payload(0x0F))
        n.load_table("CMDI", tb.CmdiTable(key=original).pack())
        n.cmd("GW", "REMOVE_CONN", bytes((0, 0, 0, 0)))
    p.raise_if()


def c_same_wakeup_order(ctx: "Ctx") -> None:
    """A tightly queued socket batch supplements the deterministic hidden single-wakeup vectors."""
    import tables as tb
    n = boot(ctx, (1,))[1]
    original = n.key
    next_key = bytes(range(32, 64))
    n.load_table("CMDI", tb.CmdiTable(key=original, next_key=next_key, next_valid=1).pack())
    p = Problems()
    before = sample_counters(n)[0]
    switch = fr.build_command(spec.app_mid("CMDI", "CMD"), spec.CC["CMDI"]["SWITCH_KEY"])
    batch = (n.frame(switch, source_id=1, sequence=100),
             n.frame(noop_cmd(n), source_id=1, sequence=101),
             n.frame(noop_cmd(n), key=next_key, source_id=1, sequence=102))
    n.send_batch(batch)
    n.key = next_key
    n.settle()
    p.check(sample_counters(n)[0] == before + 1, "queued key switch rejects retired-key command and accepts fresh-key command")
    check_source_row(p, source_stats(n)[0], Accepted=2, HmacErrors=1)
    for serial, name, payload, offset, corrupt in ((103, "SET_ENABLE_MASK", mask_payload(0x0D), 0, True),
                                                    (105, "SET_MAX_AGE", struct.pack("<I", 120), -90, False)):
        before = sample_counters(n)[0]
        command = n.frame(fr.build_command(spec.app_mid("CMDI", "CMD"), spec.CC["CMDI"][name], payload),
                          source_id=1, sequence=serial)
        packet = n.frame(noop_cmd(n), source_id=1, sequence=serial + 1, time_offset=offset)
        n.send_batch((command, fr.corrupt(packet, "mac") if corrupt else packet))
        n.settle()
        p.check(sample_counters(n)[0] == before + 1, f"queued {name} applies before following datagram")
    n.load_table("CMDI", tb.CmdiTable(key=next_key, next_key=original, next_valid=1).pack())
    n.cmd("CMDI", "SWITCH_KEY")
    n.key = original
    n.load_table("CMDI", tb.CmdiTable().pack())
    p.raise_if()
