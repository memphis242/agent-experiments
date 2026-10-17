#!/usr/bin/env python3
"""Minimal cFS ground harness (standard library only, Python 3.12+).

Commands are sent to CI_LAB on UDP 1234. Telemetry arrives from TO_LAB on
UDP 2234 once TO_LAB output has been enabled.

Library use:
    import udp_harness as h
    h.enable_to_output()
    h.send_command(0x1882, 0)                  # SAMPLE_APP NOOP
    for mid, raw in h.receive_telemetry(6.0, mid=0x0883):
        print(h.payload(raw).hex())

CLI use:
    udp_harness.py enable-to
    udp_harness.py send --mid 0x1882 --cc 0 [--payload-hex 2a000700]
    udp_harness.py recv --seconds 6 [--mid 0x0883]
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

HOST = "127.0.0.1"
CI_LAB_PORT = 1234
TO_LAB_PORT = 2234

TO_LAB_CMD_MID = 0x1880
TO_LAB_OUTPUT_ENABLE_CC = 6
TO_LAB_DEST_IP_LEN = 16

PRIMARY_HEADER_LEN = 6
CMD_SECONDARY_HEADER_LEN = 2   # function code byte, checksum byte
TLM_SECONDARY_HEADER_LEN = 10  # 4-byte seconds, 2-byte subseconds, 4 spare bytes (this mission's layout)
TLM_HEADER_LEN = PRIMARY_HEADER_LEN + TLM_SECONDARY_HEADER_LEN

CCSDS_SEQ_UNSEGMENTED = 0xC000
MID_MAX = 0xFFFF
MAX_PACKET = 65535


class HarnessError(Exception):
    """A problem the user can act on (bad argument, network failure)."""


def build_command(mid: int, cc: int, payload: bytes = b"") -> bytes:
    """Build a CCSDS command packet with the 8-bit XOR checksum filled in."""
    if not 0 <= mid <= MID_MAX:
        raise HarnessError(f"message ID {mid:#x} is outside 0..{MID_MAX:#x}")
    if not 0 <= cc <= 0x7F:
        raise HarnessError(f"command code {cc} is outside 0..127")
    total = PRIMARY_HEADER_LEN + CMD_SECONDARY_HEADER_LEN + len(payload)
    if total > MAX_PACKET:
        raise HarnessError(f"packet of {total} bytes exceeds {MAX_PACKET}")
    pkt = bytearray(struct.pack(">HHH", mid, CCSDS_SEQ_UNSEGMENTED, total - 7))
    pkt += bytes([cc, 0]) + payload
    assert len(pkt) == total
    checksum = 0xFF
    for b in pkt:
        checksum ^= b
    pkt[PRIMARY_HEADER_LEN + 1] = checksum
    return bytes(pkt)


def parse_telemetry(data: bytes) -> tuple[int, int, int]:
    """Return (mid, sequence_count, total_length) from a packet's primary header."""
    if len(data) < PRIMARY_HEADER_LEN:
        raise HarnessError(f"packet of {len(data)} bytes is shorter than a CCSDS header")
    mid, seq, length = struct.unpack(">HHH", data[:PRIMARY_HEADER_LEN])
    return mid, seq & 0x3FFF, length + 7


def payload(raw: bytes) -> bytes:
    """Return the bytes after the telemetry primary and secondary headers."""
    return raw[TLM_HEADER_LEN:]


def send_command(mid: int, cc: int, payload_bytes: bytes = b"", *,
                 host: str = HOST, port: int = CI_LAB_PORT) -> None:
    """Send one command packet to CI_LAB."""
    pkt = build_command(mid, cc, payload_bytes)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            tx.sendto(pkt, (host, port))
    except OSError as exc:
        raise HarnessError(f"cannot send to {host}:{port}: {exc}") from exc


def enable_to_output(dest_ip: str = HOST) -> None:
    """Tell TO_LAB to start streaming telemetry to dest_ip:2234."""
    ip = dest_ip.encode("ascii")
    if len(ip) >= TO_LAB_DEST_IP_LEN:
        raise HarnessError(f"destination IP {dest_ip!r} does not fit in {TO_LAB_DEST_IP_LEN} bytes")
    send_command(TO_LAB_CMD_MID, TO_LAB_OUTPUT_ENABLE_CC, ip.ljust(TO_LAB_DEST_IP_LEN, b"\0"))


def receive_telemetry(seconds: float, mid: int | None = None, *,
                      host: str = HOST, port: int = TO_LAB_PORT) -> list[tuple[int, bytes]]:
    """Collect telemetry for `seconds`, returning (mid, raw_packet) pairs.

    When `mid` is given only packets with that message ID are returned.
    """
    if seconds < 0:
        raise HarnessError("seconds must be non-negative")
    out: list[tuple[int, bytes]] = []
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind((host, port))
    except OSError as exc:
        raise HarnessError(f"cannot listen on {host}:{port} (is another receiver running?): {exc}") from exc
    with rx:
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            rx.settimeout(remaining)
            try:
                data, _ = rx.recvfrom(MAX_PACKET)
            except TimeoutError:
                break
            if len(data) < PRIMARY_HEADER_LEN:
                continue
            pkt_mid = parse_telemetry(data)[0]
            if mid is None or pkt_mid == mid:
                out.append((pkt_mid, data))
    return out


def _int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None


def _hex_bytes(text: str) -> bytes:
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a hex string: {text!r}") from None


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="cFS UDP ground harness")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("enable-to", help="enable TO_LAB telemetry output to 127.0.0.1")
    p_send = sub.add_parser("send", help="send one command to CI_LAB")
    p_send.add_argument("--mid", type=_int, required=True, help="command message ID, e.g. 0x1882")
    p_send.add_argument("--cc", type=_int, required=True, help="function code")
    p_send.add_argument("--payload-hex", type=_hex_bytes, default=b"", help="payload as hex digits")
    p_recv = sub.add_parser("recv", help="print telemetry packets for N seconds")
    p_recv.add_argument("--seconds", type=float, default=5.0)
    p_recv.add_argument("--mid", type=_int, default=None, help="only show this message ID")
    args = ap.parse_args(argv)

    try:
        match args.command:
            case "enable-to":
                enable_to_output()
                print("TO_LAB output enable sent")
            case "send":
                send_command(args.mid, args.cc, args.payload_hex)
                print(f"sent mid={args.mid:#06x} cc={args.cc} payload={len(args.payload_hex)} bytes")
            case "recv":
                packets = receive_telemetry(args.seconds, args.mid)
                for pkt_mid, raw in packets:
                    _, seq, length = parse_telemetry(raw)
                    print(f"mid={pkt_mid:#06x} seq={seq} len={length} payload={payload(raw).hex()}")
                print(f"{len(packets)} packets", file=sys.stderr)
            case _:
                raise AssertionError(args.command)
    except HarnessError as exc:
        print(f"udp_harness: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
