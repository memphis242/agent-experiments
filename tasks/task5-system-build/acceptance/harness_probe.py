"""Exercise the agent's tools/udp_harness.py in a separate interpreter and print the observations as JSON.

Usage: python3 -I harness_probe.py <tools-dir> <plan.json>

The plan names the inputs; this script only calls the module and records what it returned or raised,
so that nothing the module does can affect the reviewing process. Standard library only.
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
import traceback


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def call(fn, *args, **kwargs):  # noqa: ANN001, ANN201
    try:
        res = fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - every outcome is an observation
        return {"error": describe(exc), "error_type": type(exc).__name__}
    if isinstance(res, (bytes, bytearray)):
        return {"bytes": bytes(res).hex()}
    if isinstance(res, tuple):
        return {"value": list(res)}
    return {"value": res}


def telemetry_feeder(port: int, seconds: int, stop: threading.Event) -> None:
    """Send a command-type datagram, then telemetry packets whose seconds field is `seconds`, until stopped."""
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd = bytes.fromhex("18e8c0000001") + b"\x00\x00"
    tlm = struct.pack(">HHH", 0x0883, 0xC000, 18 - 7) + struct.pack(">I", seconds) + bytes(8)
    while not stop.is_set():
        tx.sendto(cmd, ("127.0.0.1", port))
        tx.sendto(tlm, ("127.0.0.1", port))
        time.sleep(0.05)
    tx.close()


def main() -> int:
    tools, plan_path = sys.argv[1], sys.argv[2]
    plan = json.loads(open(plan_path).read())
    out: dict = {}
    sys.path.insert(0, tools)
    try:
        import udp_harness as h  # type: ignore[import-not-found]
    except BaseException as exc:  # noqa: BLE001
        print(json.dumps({"import_error": describe(exc), "trace": traceback.format_exc()[-800:]}))
        return 0
    out["constants"] = {}
    for name in ("CMDI_DEFAULT_KEY", "CMDI_DEFAULT_SOURCE_ID", "CMDI_BASE_PORT", "TLMO_BASE_PORT", "HarnessError"):
        v = getattr(h, name, None)
        out["constants"][name] = v.hex() if isinstance(v, bytes) else (v if isinstance(v, int) else repr(v))
    for name in ("node_ports", "crc16_ccitt_false", "build_frame", "onboard_seconds", "send_frame", "send_framed_command"):
        out.setdefault("missing", [])
        if not callable(getattr(h, name, None)):
            out["missing"].append(name)
    if out["missing"]:
        print(json.dumps(out))
        return 0
    out["node_ports"] = {str(n): call(h.node_ports, n) for n in plan["node_ports"]}
    out["crc"] = [call(h.crc16_ccitt_false, bytes.fromhex(x)) for x in plan["crc_inputs"]]
    out["build_frame"] = []
    for case in plan["build_frame"]:
        kw = {k: (bytes.fromhex(v) if k == "key" else v) for k, v in case["kwargs"].items()}
        out["build_frame"].append(call(h.build_frame, bytes.fromhex(case["command"]), **kw))
    rx = {}
    for name, port in plan["ports"].items():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", port))
        s.settimeout(2.0)
        rx[name] = s

    def recv(name: str) -> str | None:
        try:
            return rx[name].recvfrom(65535)[0].hex()
        except OSError:
            return None

    sfc = plan["send_framed_command"]
    stop = threading.Event()
    feeder = threading.Thread(target=telemetry_feeder, args=(plan["tlm_port"], plan["onboard"], stop), daemon=True)
    feeder.start()
    out["onboard_seconds"] = call(h.onboard_seconds, 3.0, port=plan["tlm_port"])
    out["auto_time"] = call(h.send_framed_command, sfc["mid"], sfc["cc"], b"", port=plan["ports"]["cmd"],
                            tlm_port=plan["tlm_port"])
    out["auto_time_received"] = recv("cmd")
    out["auto_time_2"] = call(h.send_framed_command, sfc["mid"], sfc["cc"], b"", port=plan["ports"]["cmd"],
                              tlm_port=plan["tlm_port"])
    out["auto_time_2_received"] = recv("cmd")
    stop.set()
    feeder.join(timeout=2)
    sf = plan["send_frame"]
    out["send_frame"] = call(h.send_frame, bytes.fromhex(sf["frame"]), port=plan["ports"]["frame"])
    out["send_frame_received"] = recv("frame")
    out["send_framed_command"] = call(h.send_framed_command, sfc["mid"], sfc["cc"], bytes.fromhex(sfc["payload"]),
                                      time_seconds=sfc["time_seconds"], sequence=sfc["sequence"],
                                      port=plan["ports"]["cmd"])
    out["send_framed_command_received"] = recv("cmd")
    t0 = time.monotonic()
    out["onboard_timeout"] = call(h.onboard_seconds, 0.5, port=plan["dead_port"])
    out["onboard_timeout_s"] = round(time.monotonic() - t0, 2)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
