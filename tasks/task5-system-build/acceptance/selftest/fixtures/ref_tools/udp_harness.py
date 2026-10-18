#!/usr/bin/env python3
"""Reference of the S3.5 frame builder (self-test fixture only): the fixture's udp_harness.py plus the pinned
CMDI frame functions and CLI. It exists to exercise the pass path of the frame-builder checks."""
from __future__ import annotations

import argparse
import binascii
import hashlib
import hmac
import socket
import struct
import sys
import time

HOST = "127.0.0.1"
CI_LAB_PORT = 1234
TO_LAB_PORT = 2234
CMDI_DEFAULT_KEY: bytes = b"cFS-cmdval-test-key-0123456789ab"
CMDI_DEFAULT_SOURCE_ID: int = 1
CMDI_BASE_PORT: int = 1234
TLMO_BASE_PORT: int = 2234
_seq = 0


class HarnessError(Exception):
    """A problem the user can act on."""


def build_command(mid: int, cc: int, payload: bytes = b"") -> bytes:
    if not 0 <= mid <= 0xFFFF or not 0 <= cc <= 0x7F:
        raise HarnessError("mid or cc out of range")
    total = 8 + len(payload)
    pkt = bytearray(struct.pack(">HHH", mid, 0xC000, total - 7)) + bytes([cc, 0]) + payload
    x = 0xFF
    for b in pkt:
        x ^= b
    pkt[7] = x
    return bytes(pkt)


def node_ports(node_id: int) -> tuple[int, int]:
    if node_id not in (1, 2):
        raise HarnessError(f"node {node_id} is not 1 or 2")
    return CMDI_BASE_PORT + node_id - 1, TLMO_BASE_PORT + node_id - 1


def crc16_ccitt_false(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xFFFF)


def build_frame(command: bytes, *, time_seconds: int, key: bytes = CMDI_DEFAULT_KEY,
                source_id: int = CMDI_DEFAULT_SOURCE_ID, sequence: int = 0) -> bytes:
    if not 8 <= len(command) <= 1024:
        raise HarnessError(f"command of {len(command)} bytes is outside 8..1024")
    for name, v, hi in (("time_seconds", time_seconds, 0xFFFFFFFF), ("source_id", source_id, 0xFFFF),
                        ("sequence", sequence, 0xFFFFFFFF)):
        if not 0 <= v <= hi:
            raise HarnessError(f"{name} {v} out of range")
    body = struct.pack(">HBBHHII", 0x4356, 1, 0, source_id, len(command), time_seconds, sequence) + command
    body += hmac.new(key, body, hashlib.sha256).digest()
    return body + struct.pack(">H", crc16_ccitt_false(body))


def onboard_seconds(timeout: float = 10.0, *, host: str = HOST, port: int = TLMO_BASE_PORT) -> int:
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        rx.bind((host, port))
    except OSError as exc:
        raise HarnessError(f"cannot listen on {host}:{port}: {exc}") from exc
    with rx:
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            rx.settimeout(left)
            try:
                data, _ = rx.recvfrom(65535)
            except TimeoutError:
                break
            if len(data) >= 10 and not data[0] & 0x10:
                return struct.unpack(">I", data[6:10])[0]
    raise HarnessError(f"no telemetry on {host}:{port} within {timeout} s")


def send_frame(frame: bytes, *, host: str = HOST, port: int = CMDI_BASE_PORT) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            tx.sendto(frame, (host, port))
    except OSError as exc:
        raise HarnessError(f"cannot send to {host}:{port}: {exc}") from exc


def send_framed_command(mid: int, cc: int, payload: bytes = b"", *, key: bytes = CMDI_DEFAULT_KEY,
                        source_id: int = CMDI_DEFAULT_SOURCE_ID, time_seconds: int | None = None,
                        sequence: int | None = None, host: str = HOST, port: int = CMDI_BASE_PORT,
                        tlm_port: int | None = None) -> bytes:
    global _seq
    if time_seconds is None:
        time_seconds = onboard_seconds(host=host, port=tlm_port if tlm_port is not None else port + 1000)
    if sequence is None:
        _seq += 1
        sequence = _seq
    frame = build_frame(build_command(mid, cc, payload), time_seconds=time_seconds, key=key, source_id=source_id,
                        sequence=sequence)
    send_frame(frame, host=host, port=port)
    return frame


def _int(text: str) -> int:
    return int(text, 0)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("send-framed")
    s.add_argument("--mid", type=_int, required=True)
    s.add_argument("--cc", type=_int, required=True)
    s.add_argument("--payload-hex", default="")
    s.add_argument("--node", type=int, default=1)
    s.add_argument("--key-hex", default=CMDI_DEFAULT_KEY.hex())
    s.add_argument("--source-id", type=_int, default=1)
    s.add_argument("--time-seconds", type=_int, default=None)
    s.add_argument("--sequence", type=_int, default=None)
    r = sub.add_parser("recv")
    r.add_argument("--seconds", type=float, default=5.0)
    r.add_argument("--node", type=int, default=1)
    args = ap.parse_args(argv)
    try:
        if args.command == "send-framed":
            cmd_port, tlm_port = node_ports(args.node)
            f = send_framed_command(args.mid, args.cc, bytes.fromhex(args.payload_hex), key=bytes.fromhex(args.key_hex),
                                    source_id=args.source_id, time_seconds=args.time_seconds, sequence=args.sequence,
                                    port=cmd_port, tlm_port=tlm_port)
            print(f"sent frame of {len(f)} bytes, sequence {struct.unpack('>I', f[12:16])[0]}")
        else:
            _c, tlm_port = node_ports(args.node)
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.bind((HOST, tlm_port))
            end = time.monotonic() + args.seconds
            while (left := end - time.monotonic()) > 0:
                rx.settimeout(left)
                try:
                    data, _ = rx.recvfrom(65535)
                except TimeoutError:
                    break
                print(f"mid={struct.unpack('>H', data[:2])[0]:#06x} len={len(data)}")
    except HarnessError as exc:
        print(f"udp_harness: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
