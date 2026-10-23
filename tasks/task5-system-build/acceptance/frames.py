"""Independent CMDI frame codec (spec S3.1), CCSDS packet builders and the GW stream deframer (S5.2).

Standard library only. Nothing here imports the code under review: the HMAC is built from
hashlib.sha256 per RFC 2104 and the CRC is a bitwise CRC-16/CCITT-FALSE, so a fault shared
with the agent's frame builder cannot hide itself.
"""
from __future__ import annotations

import hashlib
import struct
import threading
from dataclasses import dataclass

MAGIC = 0x4356
VERSION = 1
HDR_LEN = 16
MAC_LEN = 32
CRC_LEN = 2
OVERHEAD = 50
MIN_CMD = 8
MAX_CMD = 1024
MAX_FRAME = OVERHEAD + MAX_CMD  # 1074
MARKER = bytes.fromhex("1ACFFC1D")
DEFAULT_KEY = b"cFS-cmdval-test-key-0123456789ab"
DEFAULT_SOURCE = 1
OBSERVATION_SOURCE = 2

assert len(DEFAULT_KEY) == 32
assert HDR_LEN + MAC_LEN + CRC_LEN == OVERHEAD


class SerialAllocator:
    """One serial history per node/source, shared by all command producers in a run.

    Explicit replay vectors do not advance this allocator. A new node lifetime may
    use a fresh allocator; application counter resets must retain the existing one.
    """

    def __init__(self) -> None:
        self._last: dict[tuple[int, int], int] = {}
        self._lock = threading.Lock()

    def next(self, node: int, source: int) -> int:
        assert node in (1, 2) and 0 <= source <= 0xFFFF
        with self._lock:
            key = (node, source)
            value = (self._last.get(key, 0) + 1) & 0xFFFFFFFF
            self._last[key] = value
            return value


SERIALS = SerialAllocator()


class FrameError(Exception):
    """A received unit is not a valid CMDI frame; `reason` is FORMAT, CRC or HMAC."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


# ----------------------------------------------------------------------------- checksums

def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, not reflected, init 0xFFFF, xorout 0."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def hmac_sha256(key: bytes, data: bytes) -> bytes:
    """HMAC-SHA-256 per RFC 2104: keys longer than the 64-byte block are hashed first."""
    block = 64
    if len(key) > block:
        key = hashlib.sha256(key).digest()
    key = key.ljust(block, b"\0")
    inner = hashlib.sha256(bytes(k ^ 0x36 for k in key) + data).digest()
    return hashlib.sha256(bytes(k ^ 0x5C for k in key) + inner).digest()


# ----------------------------------------------------------------------------- CCSDS packets

def ccsds_checksum_fill(pkt: bytearray) -> None:
    """Fill the command secondary header checksum (byte 7) so the XOR of all bytes is 0xFF."""
    assert len(pkt) >= 8
    pkt[7] = 0
    x = 0xFF
    for b in pkt:
        x ^= b
    pkt[7] = x


def build_command(mid: int, cc: int, payload: bytes = b"", *, seq: int = 0) -> bytes:
    """CCSDS command packet: 6-byte primary header, function code, checksum, payload."""
    assert 0 <= mid <= 0xFFFF and 0 <= cc <= 0x7F
    total = 8 + len(payload)
    pkt = bytearray(struct.pack(">HHH", mid, 0xC000 | (seq & 0x3FFF), total - 7))
    pkt += bytes([cc, 0]) + payload
    ccsds_checksum_fill(pkt)
    assert len(pkt) == total
    return bytes(pkt)


def build_verification(mid: int, total: int, counter: int | None = None, *, fill: int = 0, cc: int = 0,
                       counter_width: int = 4, seq: int = 0) -> bytes:
    """Verification packet (spec C.3): a command packet of `total` bytes. `counter`, when given, is written
    little-endian at packet byte offset 16 (payload byte 8); the rest of the payload is a pattern of `fill`."""
    assert total >= 8
    assert counter is None or total >= 16 + counter_width
    body = bytearray(((fill + i) & 0xFF) for i in range(total - 8))
    if counter is not None:
        body[8:8 + counter_width] = int(counter).to_bytes(counter_width, "little")
    return build_command(mid, cc, bytes(body), seq=seq)


def packet_mid(pkt: bytes) -> int:
    return struct.unpack(">H", pkt[:2])[0]


def packet_len(pkt: bytes) -> int:
    return struct.unpack(">H", pkt[4:6])[0] + 7


def is_command(pkt: bytes) -> bool:
    return len(pkt) >= 1 and bool(pkt[0] & 0x10)


def tlm_seconds(pkt: bytes) -> int:
    """Seconds field of a telemetry secondary header (bytes 6 to 9, big-endian)."""
    assert len(pkt) >= 10
    return struct.unpack(">I", pkt[6:10])[0]


# ----------------------------------------------------------------------------- frames

def build_frame(command: bytes, *, time_seconds: int, key: bytes = DEFAULT_KEY, source_id: int = DEFAULT_SOURCE,
                sequence: int = 0, magic: int = MAGIC, version: int = VERSION, flags: int = 0,
                length_field: int | None = None) -> bytes:
    """The S3.1 frame around `command`. `magic`, `version`, `flags` and `length_field` may be set to invalid
    values for negative tests; MAC and CRC are always computed over whatever header results."""
    assert 0 <= time_seconds <= 0xFFFFFFFF and 0 <= sequence <= 0xFFFFFFFF and 0 <= source_id <= 0xFFFF
    length = len(command) if length_field is None else length_field
    hdr = struct.pack(">HBBHHII", magic, version, flags, source_id, length & 0xFFFF, time_seconds, sequence)
    assert len(hdr) == HDR_LEN
    body = hdr + command
    mac = hmac_sha256(key, body)
    framed = body + mac
    return framed + struct.pack(">H", crc16_ccitt_false(framed))


def recrc(frame: bytes) -> bytes:
    """Recompute the trailing CRC of a (possibly modified) frame."""
    assert len(frame) >= CRC_LEN
    body = frame[:-CRC_LEN]
    return body + struct.pack(">H", crc16_ccitt_false(body))


def corrupt(frame: bytes, kind: str) -> bytes:
    """A copy of a valid frame with one defect: 'mac' (one MAC bit flipped, CRC recomputed), 'crc' (one CRC
    bit flipped), 'crc-and-mac' (both)."""
    assert len(frame) >= OVERHEAD + MIN_CMD
    b = bytearray(frame)
    mac_at = len(frame) - CRC_LEN - MAC_LEN
    if kind == "mac":
        b[mac_at + 5] ^= 0x01
        return recrc(bytes(b))
    if kind == "crc":
        b[-1] ^= 0x01
        return bytes(b)
    if kind == "crc-and-mac":
        b[mac_at + 5] ^= 0x01
        b[-1] ^= 0x01
        return bytes(b)
    raise AssertionError(f"unknown corruption {kind}")


@dataclass(frozen=True)
class Frame:
    source_id: int
    length: int
    time_seconds: int
    sequence: int
    command: bytes
    magic: int = MAGIC
    version: int = VERSION
    flags: int = 0


def parse_frame(unit: bytes, key: bytes = DEFAULT_KEY) -> Frame:
    """Verify one received unit in CMDVAL_ValidateFrame order (FORMAT, CRC, HMAC); raises FrameError."""
    if len(unit) < OVERHEAD + MIN_CMD:
        raise FrameError("FORMAT", f"{len(unit)} bytes is shorter than the minimum frame of {OVERHEAD + MIN_CMD}")
    magic, version, flags, source, length, secs, seq = struct.unpack(">HBBHHII", unit[:HDR_LEN])
    if magic != MAGIC:
        raise FrameError("FORMAT", f"magic {magic:#06x}")
    if version != VERSION:
        raise FrameError("FORMAT", f"version {version}")
    if flags != 0:
        raise FrameError("FORMAT", f"flags {flags}")
    if not MIN_CMD <= length <= MAX_CMD:
        raise FrameError("FORMAT", f"CommandLength {length} outside {MIN_CMD}..{MAX_CMD}")
    if len(unit) != OVERHEAD + length:
        raise FrameError("FORMAT", f"{len(unit)} bytes but CommandLength {length} implies {OVERHEAD + length}")
    command = unit[HDR_LEN:HDR_LEN + length]
    if packet_len(command) != length:
        raise FrameError("FORMAT", f"embedded CCSDS length {packet_len(command)} differs from CommandLength {length}")
    if not is_command(command):
        raise FrameError("FORMAT", "embedded packet is not a command (type bit clear)")
    body = unit[:HDR_LEN + length + MAC_LEN]
    crc = struct.unpack(">H", unit[-CRC_LEN:])[0]
    if crc != crc16_ccitt_false(body):
        raise FrameError("CRC", f"CRC {crc:#06x} does not match {crc16_ccitt_false(body):#06x}")
    mac = unit[HDR_LEN + length:HDR_LEN + length + MAC_LEN]
    if mac != hmac_sha256(key, unit[:HDR_LEN + length]):
        raise FrameError("HMAC", "MAC does not verify with the expected key")
    return Frame(source, length, secs, seq, bytes(command), magic, version, flags)


# ----------------------------------------------------------------------------- stream framing

def stream_wrap(frame: bytes) -> bytes:
    """A frame as carried on TCP and SERIAL connections: the 4-byte marker, then the frame."""
    return MARKER + frame


@dataclass(frozen=True)
class StreamUnit:
    """One result of deframing: a complete frame (`data`), or a header rejected for its length (`bad_length`)."""
    data: bytes
    bad_length: int | None = None


class Deframer:
    """Marker-hunting stream deframer with the S5.2 rules: hunt for 1A CF FC 1D, read the 16-byte header,
    take L from CommandLength; L outside 8..1024 is a FORMAT rejection without reading further and hunting
    resumes after that marker; otherwise read L + 34 more bytes. Partial frames are kept across feeds."""

    def __init__(self, max_len: int = MAX_CMD) -> None:
        self.buf = bytearray()
        self.max_len = max_len
        self.skipped = 0  # bytes discarded while hunting

    def feed(self, data: bytes) -> list[StreamUnit]:
        self.buf += data
        out: list[StreamUnit] = []
        while True:
            at = self.buf.find(MARKER)
            if at < 0:
                keep = len(MARKER) - 1  # a marker may straddle two feeds
                if len(self.buf) > keep:
                    self.skipped += len(self.buf) - keep
                    del self.buf[:len(self.buf) - keep]
                return out
            if at:
                self.skipped += at
                del self.buf[:at]
            if len(self.buf) < len(MARKER) + HDR_LEN:
                return out
            length = struct.unpack(">H", self.buf[len(MARKER) + 6:len(MARKER) + 8])[0]
            if not MIN_CMD <= length <= self.max_len:
                out.append(StreamUnit(bytes(self.buf[len(MARKER):len(MARKER) + HDR_LEN]), bad_length=length))
                del self.buf[:len(MARKER)]
                continue
            need = len(MARKER) + OVERHEAD + length
            if len(self.buf) < need:
                return out
            out.append(StreamUnit(bytes(self.buf[len(MARKER):need])))
            del self.buf[:need]
