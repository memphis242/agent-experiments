"""S3.5: the frame builder in tools/udp_harness.py against the independent encoder (frames.py)."""
from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import frames as fr
import spec
from common import Fail, Problems, short

if TYPE_CHECKING:
    from check import Ctx

PACK = Path(__file__).resolve().parent
KEY2 = bytes(range(0xA0, 0xC0))
assert len(KEY2) == 32


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_plan() -> dict:
    noop = fr.build_command(0x1882, 0)
    big = fr.build_verification(0x18E8, 1024, counter=7)
    ports = {"frame": free_port(), "cmd": free_port()}
    return {
        "node_ports": [1, 2, 3, 0],
        "crc_inputs": [b"123456789".hex(), bytes(range(256)).hex(), ""],
        "build_frame": [
            {"command": noop.hex(), "kwargs": {"time_seconds": 1000123}},
            {"command": noop.hex(), "kwargs": {"time_seconds": 0, "key": KEY2.hex(), "source_id": 2, "sequence": 0xFFFFFFFF}},
            {"command": big.hex(), "kwargs": {"time_seconds": 0xFFFFFFFF, "sequence": 5}},
            {"command": noop[:7].hex(), "kwargs": {"time_seconds": 1}},
            {"command": fr.build_verification(0x18E8, 1025).hex(), "kwargs": {"time_seconds": 1}},
            {"command": noop.hex(), "kwargs": {"time_seconds": 1, "source_id": 70000}},
            {"command": noop.hex(), "kwargs": {"time_seconds": -1}},
            {"command": noop.hex(), "kwargs": {"time_seconds": 1, "sequence": 1 << 32}},
        ],
        "ports": ports,
        "send_frame": {"frame": fr.build_frame(noop, time_seconds=42, sequence=9).hex()},
        "send_framed_command": {"mid": 0x1882, "cc": 3, "payload": "2a00070041420000000000000000", "time_seconds": 777,
                                "sequence": 31},
        "tlm_port": free_port(),
        "onboard": 1000777,
        "dead_port": free_port(),
    }


def judge_functions(plan: dict, obs: dict) -> list[str]:
    """Problems found comparing the module's observed behaviour with the independent encoder."""
    p = Problems()
    if "import_error" in obs:
        return [f"tools/udp_harness.py cannot be imported: {obs['import_error']}"]
    if obs.get("missing"):
        return [f"tools/udp_harness.py lacks {', '.join(obs['missing'])}"]
    c = obs["constants"]
    p.check(c.get("CMDI_DEFAULT_KEY") == fr.DEFAULT_KEY.hex(), f"CMDI_DEFAULT_KEY is {c.get('CMDI_DEFAULT_KEY')}")
    p.check(c.get("CMDI_DEFAULT_SOURCE_ID") == 1, f"CMDI_DEFAULT_SOURCE_ID is {c.get('CMDI_DEFAULT_SOURCE_ID')}")
    p.check(c.get("CMDI_BASE_PORT") == 1234 and c.get("TLMO_BASE_PORT") == 2234,
            f"base ports are {c.get('CMDI_BASE_PORT')}/{c.get('TLMO_BASE_PORT')}")
    np_ = obs["node_ports"]
    p.check(np_["1"].get("value") == [1234, 2234], f"node_ports(1) gave {np_['1']}")
    p.check(np_["2"].get("value") == [1235, 2235], f"node_ports(2) gave {np_['2']}")
    for n in ("3", "0"):
        p.check(np_[n].get("error_type") == "HarnessError", f"node_ports({n}) gave {np_[n]} instead of HarnessError")
    p.check(len(obs["crc"]) == len(plan["crc_inputs"]) and len(obs["build_frame"]) == len(plan["build_frame"]),
            "the module's answers are incomplete")
    for inp, got in zip(plan["crc_inputs"], obs["crc"]):
        want = fr.crc16_ccitt_false(bytes.fromhex(inp))
        p.check(got.get("value") == want, f"crc16_ccitt_false({inp[:16]}...) = {got} != {want:#06x}")
    for case, got in zip(plan["build_frame"], obs["build_frame"]):
        cmd = bytes.fromhex(case["command"])
        kw = dict(case["kwargs"])
        label = f"build_frame(len {len(cmd)}, {kw})"
        valid = (8 <= len(cmd) <= 1024 and 0 <= kw.get("source_id", 1) <= 0xFFFF and
                 0 <= kw["time_seconds"] <= 0xFFFFFFFF and 0 <= kw.get("sequence", 0) <= 0xFFFFFFFF)
        if valid:
            if "key" in kw:
                kw["key"] = bytes.fromhex(kw["key"])
            want = fr.build_frame(cmd, **kw)
            p.check(got.get("bytes") == want.hex(), f"{label} is not byte-identical to the S3.1 frame ({short(str(got), 120)})")
        else:
            p.check(got.get("error_type") == "HarnessError", f"{label} gave {short(str(got), 120)} instead of HarnessError")
    want = bytes.fromhex(plan["send_frame"]["frame"]).hex()
    p.check(obs.get("send_frame_received") == want, "send_frame did not send the frame unchanged as one datagram")
    sfc = plan["send_framed_command"]
    cmd = fr.build_command(sfc["mid"], sfc["cc"], bytes.fromhex(sfc["payload"]))
    want = fr.build_frame(cmd, time_seconds=sfc["time_seconds"], sequence=sfc["sequence"]).hex()
    p.check(obs["send_framed_command"].get("bytes") == want, "send_framed_command did not return the S3.1 frame")
    p.check(obs.get("send_framed_command_received") == want, "send_framed_command did not send the frame it returned")
    p.check(obs["onboard_seconds"].get("value") == plan["onboard"],
            f"onboard_seconds returned {obs['onboard_seconds']} instead of the seconds field {plan['onboard']} of the "
            f"first telemetry packet (command-type packets skipped)")
    seqs = []
    for key in ("auto_time", "auto_time_2"):
        got = obs[key].get("bytes")
        if not p.check(got is not None, f"send_framed_command with time_seconds None failed: {obs[key]}"):
            continue
        try:
            f = fr.parse_frame(bytes.fromhex(got))
        except fr.FrameError as exc:
            p.check(False, f"send_framed_command returned an invalid frame: {exc}")
            continue
        p.check(f.time_seconds == plan["onboard"], f"time_seconds None used {f.time_seconds}, not the onboard {plan['onboard']}")
        p.check(obs.get(f"{key}_received") == got, f"{key}: the returned frame was not the one sent")
        seqs.append(f.sequence)
    if len(seqs) == 2:
        p.check(seqs == [1, 2], f"sequence None gave sequences {seqs}; a per-process counter starting at 1 gives [1, 2]")
    p.check(obs["onboard_timeout"].get("error_type") == "HarnessError" and obs.get("onboard_timeout_s", 99) < 3,
            f"onboard_seconds with no telemetry gave {obs['onboard_timeout']} after {obs.get('onboard_timeout_s')} s")
    return p.items


def c_harness_functions(ctx: "Ctx") -> None:
    ctx.copy_tree()
    tools = ctx.tree / "tools"
    if not (tools / "udp_harness.py").is_file():
        raise Fail("tools/udp_harness.py does not exist")
    plan = make_plan()
    plan_file = ctx.work / "harness-plan.json"
    plan_file.write_text(json.dumps(plan))
    try:
        proc = subprocess.run([sys.executable, "-I", "-B", str(PACK / "harness_probe.py"), str(tools), str(plan_file)],
                              capture_output=True, text=True, errors="replace", timeout=ctx.timeout_for(90), cwd=ctx.work)
    except subprocess.TimeoutExpired:
        raise Fail("exercising tools/udp_harness.py did not finish within 90 s (a call blocked)") from None
    try:
        obs = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Fail(f"exercising tools/udp_harness.py produced no result: {short(proc.stdout + proc.stderr, 800)}") from None
    items = judge_functions(plan, obs)
    if items:
        raise Fail("; ".join(items))


# ----------------------------------------------------------------------------- CLI (fixed ports: run with no node up)

def judge_cli(results: dict) -> list[str]:
    p = Problems()
    a = results["send_framed_node2"]
    want = fr.build_frame(fr.build_command(0x1882, 0), time_seconds=1234, sequence=7).hex()
    p.check(a["rc"] == 0, f"send-framed --node 2 exited {a['rc']}: {a['err']}")
    p.check(a["datagram"] == want, "send-framed --node 2 --time-seconds 1234 --sequence 7 did not send the S3.1 frame to UDP 1235")
    p.check("58" in a["out"] and "7" in a["out"], f"send-framed did not print the frame length and sequence: {a['out']!r}")
    b = results["send_framed_options"]
    cmd = fr.build_command(0x18D4, 3, bytes.fromhex("10000000"))
    want = fr.build_frame(cmd, time_seconds=5, key=KEY2, source_id=2, sequence=3).hex()
    p.check(b["rc"] == 0 and b["datagram"] == want,
            f"send-framed with --payload-hex --key-hex --source-id gave exit {b['rc']} and a frame that differs ({b['err']})")
    c = results["send_framed_auto_time"]
    p.check(c["rc"] == 0 and c["datagram"] is not None, f"send-framed without --time-seconds failed: exit {c['rc']} {c['err']}")
    if c["datagram"]:
        try:
            f = fr.parse_frame(bytes.fromhex(c["datagram"]))
            p.check(f.time_seconds == results["auto_seconds"],
                    f"send-framed read onboard time {f.time_seconds}, not {results['auto_seconds']} from node 1's TLMO port")
        except fr.FrameError as exc:
            p.check(False, f"send-framed sent an invalid frame: {exc}")
    d = results["recv_node2"]
    p.check(d["rc"] == 0 and any(x in d["out"].lower() for x in ("0x0983", "0x983", "2435")),
            f"recv --node 2 did not show telemetry arriving on UDP 2235 (exit {d['rc']}, output {d['out'][:200]!r})")
    e = results["bad_node"]
    lines = [ln for ln in e["err"].strip().splitlines() if ln.strip()]
    p.check(e["rc"] == 1 and len(lines) == 1, f"send-framed --node 3 gave exit {e['rc']} with {len(lines)} lines of error output")
    return p.items


def _run_cli(tools: Path, args: list[str], listen: int | None, feed: tuple[int, int] | None,
             timeout: float = 20) -> dict:
    """Run the CLI; capture one datagram on `listen`; feed telemetry (port, seconds) while it runs."""
    rx = None
    if listen is not None:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind((spec.HOST, listen))
        rx.settimeout(timeout)
    stop = threading.Event()
    th = None
    if feed is not None:
        def run() -> None:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            pkt = struct.pack(">HHH", 0x0983 if feed[0] == 2235 else 0x0883, 0xC000, 11) + struct.pack(">I", feed[1]) + bytes(8)
            while not stop.is_set():
                tx.sendto(pkt, (spec.HOST, feed[0]))
                time.sleep(0.05)
            tx.close()
        th = threading.Thread(target=run, daemon=True)
        th.start()
    try:
        proc = subprocess.run([sys.executable, str(tools / "udp_harness.py"), *args], capture_output=True, text=True,
                              errors="replace", timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", "timed out"
    data = None
    if rx is not None:
        rx.settimeout(0.5)
        try:
            data = rx.recvfrom(65535)[0].hex()
        except OSError:
            data = None
        rx.close()
    stop.set()
    if th is not None:
        th.join(timeout=2)
    return {"rc": rc, "out": out, "err": err, "datagram": data}


def c_harness_cli(ctx: "Ctx") -> None:
    ctx.copy_tree()
    tools = ctx.tree / "tools"
    if not (tools / "udp_harness.py").is_file():
        raise Fail("tools/udp_harness.py does not exist")
    res = {
        "send_framed_node2": _run_cli(tools, ["send-framed", "--mid", "0x1882", "--cc", "0", "--node", "2",
                                              "--time-seconds", "1234", "--sequence", "7"], 1235, None),
        "send_framed_options": _run_cli(tools, ["send-framed", "--mid", "0x18D4", "--cc", "3", "--payload-hex", "10000000",
                                                "--key-hex", KEY2.hex(), "--source-id", "2", "--time-seconds", "5",
                                                "--sequence", "3"], 1234, None),
        "auto_seconds": 1000456,
        "send_framed_auto_time": _run_cli(tools, ["send-framed", "--mid", "0x1882", "--cc", "0"], 1234, (2234, 1000456)),
        "recv_node2": _run_cli(tools, ["recv", "--node", "2", "--seconds", "2"], None, (2235, 1000999)),
        "bad_node": _run_cli(tools, ["send-framed", "--mid", "0x1882", "--cc", "0", "--node", "3", "--time-seconds", "1"],
                             None, None),
    }
    items = judge_cli(res)
    if items:
        raise Fail("; ".join(items))
