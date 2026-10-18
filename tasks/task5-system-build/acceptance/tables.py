"""Table images for the five applications (layouts of spec S1.1, S3.3, S4.1, S5.1, S6.1).

A loadable table file is the 116-byte header of the image the build installed for that node
(CFE_FS_Header_t + CFE_TBL_File_Hdr_t, which names the table) followed by a packed body.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import spec

INVALID_MID = 0


def _cstr(text: str, size: int) -> bytes:
    raw = text.encode()
    assert len(raw) < size, f"{text!r} does not fit in {size} bytes"
    return raw.ljust(size, b"\0")


def _uncstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1")


class TableImageError(Exception):
    """An installed table image does not have the size or name the specification implies."""


def header_name(image: bytes) -> str:
    if len(image) < spec.TBL_FILE_HDR:
        raise TableImageError(f"table file of {len(image)} bytes is shorter than its {spec.TBL_FILE_HDR}-byte headers")
    return _uncstr(image[spec.CFE_FS_HDR + 12:spec.CFE_FS_HDR + 52])


def header_num_bytes(image: bytes) -> int:
    return struct.unpack(">I", image[spec.CFE_FS_HDR + 8:spec.CFE_FS_HDR + 12])[0]


def split_image(image: bytes, app: str) -> tuple[bytes, bytes]:
    """(header, body) of an installed table file, checked against the size and name the spec implies."""
    _file, name, size = spec.TABLES[app]
    if len(image) != spec.TBL_FILE_HDR + size:
        raise TableImageError(f"the installed {name} image is {len(image)} bytes; a {size}-byte table as specified "
                              f"makes {spec.TBL_FILE_HDR + size}")
    got = header_name(image)
    if got != name:
        raise TableImageError(f"the installed table file names the table {got!r}, not {name!r}")
    return image[:spec.TBL_FILE_HDR], image[spec.TBL_FILE_HDR:]


def with_body(image: bytes, app: str, body: bytes) -> bytes:
    hdr, old = split_image(image, app)
    assert len(body) == len(old), (app, len(body), len(old))
    return hdr + body


# ----------------------------------------------------------------------------- STOR (S1.1)

@dataclass(frozen=True)
class StorTable:
    telemetry_root: str = "/cf/stor/tlm"
    file_root: str = "/cf/stor/files"
    telemetry_quota: int = 262144
    file_quota: int = 1048576
    max_packet_file: int = 16384
    threshold: int = 80
    entries: tuple[tuple[int, int, int], ...] = ()  # (mid, priority, decimation)

    def pack(self) -> bytes:
        body = _cstr(self.telemetry_root, spec.STOR_PATH_LEN) + _cstr(self.file_root, spec.STOR_PATH_LEN)
        body += struct.pack("<IIIB3x", self.telemetry_quota, self.file_quota, self.max_packet_file, self.threshold)
        assert len(self.entries) <= spec.STOR_MAX_ENTRIES
        for i in range(spec.STOR_MAX_ENTRIES):
            mid, pri, dec = self.entries[i] if i < len(self.entries) else (INVALID_MID, 0, 0)
            body += struct.pack("<IBBH", mid, pri, dec, 0)
        assert len(body) == spec.STOR_TABLE_SIZE
        return body

    @staticmethod
    def unpack(body: bytes) -> "StorTable":
        assert len(body) == spec.STOR_TABLE_SIZE
        tq, fq, mf, thr = struct.unpack_from("<IIIB", body, 128)
        entries = []
        for i in range(spec.STOR_MAX_ENTRIES):
            mid, pri, dec, _sp = struct.unpack_from("<IBBH", body, 144 + i * 8)
            if mid != INVALID_MID:
                entries.append((mid, pri, dec))
        return StorTable(_uncstr(body[:64]), _uncstr(body[64:128]), tq, fq, mf, thr, tuple(entries))


# ----------------------------------------------------------------------------- CMDI (S3.3)

@dataclass(frozen=True)
class CmdiTable:
    key: bytes = b"cFS-cmdval-test-key-0123456789ab"
    sources: tuple[int, ...] = (1, 2)
    max_age: int = 30
    mask: int = 0x0F

    def pack(self) -> bytes:
        assert len(self.key) == 32 and len(self.sources) <= spec.CMDVAL_MAX_SOURCES
        src = list(self.sources) + [0] * (spec.CMDVAL_MAX_SOURCES - len(self.sources))
        body = self.key + struct.pack("<8HIB3x", *src, self.max_age, self.mask)
        assert len(body) == spec.CMDI_TABLE_SIZE
        return body

    @staticmethod
    def unpack(body: bytes) -> "CmdiTable":
        assert len(body) == spec.CMDI_TABLE_SIZE
        vals = struct.unpack_from("<8HIB", body, 32)
        return CmdiTable(bytes(body[:32]), tuple(vals[:8]), vals[8], vals[9])


# ----------------------------------------------------------------------------- TLMO (S4.1)

@dataclass(frozen=True)
class TlmoTable:
    budget: int = 16
    capacity: int = 64
    streams: tuple[tuple[int, int], ...] = ()  # (mid, class) in slot order; mid 0 = unused slot

    def pack(self) -> bytes:
        assert len(self.streams) <= spec.TLMO_MAX_STREAMS
        body = struct.pack("<HH", self.budget, self.capacity)
        for i in range(spec.TLMO_MAX_STREAMS):
            mid, cls = self.streams[i] if i < len(self.streams) else (INVALID_MID, 0)
            body += struct.pack("<IB3x", mid, cls)
        assert len(body) == spec.TLMO_TABLE_SIZE
        return body

    @staticmethod
    def unpack(body: bytes) -> "TlmoTable":
        assert len(body) == spec.TLMO_TABLE_SIZE
        budget, cap = struct.unpack_from("<HH", body, 0)
        streams = tuple(struct.unpack_from("<IB", body, 4 + i * 8) for i in range(spec.TLMO_MAX_STREAMS))
        return TlmoTable(budget, cap, streams)

    def used(self) -> dict[int, int]:
        return {mid: cls for mid, cls in self.streams if mid != INVALID_MID}


# ----------------------------------------------------------------------------- GW (S5.1)

@dataclass(frozen=True)
class ConnDef:
    interface: int = 0
    enabled: int = 0
    local_port: int = 0
    remote_addr: str = ""
    remote_port: int = 0
    device_path: str = ""
    subs: tuple[int, ...] = ()

    def pack(self) -> bytes:
        assert len(self.subs) <= spec.GW_MAX_SUBS
        subs = list(self.subs) + [INVALID_MID] * (spec.GW_MAX_SUBS - len(self.subs))
        addr = self.remote_addr.encode()[:16].ljust(16, b"\0")
        dev = self.device_path.encode()[:64].ljust(64, b"\0")
        body = struct.pack(spec.GW_CONN_DEF_FMT, self.interface, self.enabled, self.local_port, addr,
                           self.remote_port, 0, dev, *subs)
        assert len(body) == spec.GW_CONN_DEF_SIZE
        return body


def gw_table(conns: dict[int, ConnDef] | None = None) -> bytes:
    conns = conns or {}
    body = b"".join((conns.get(i) or ConnDef()).pack() for i in range(spec.GW_MAX_CONNECTIONS))
    assert len(body) == spec.GW_TABLE_SIZE
    return body


def gw_table_used_slots(body: bytes) -> list[int]:
    assert len(body) == spec.GW_TABLE_SIZE
    return [i for i in range(spec.GW_MAX_CONNECTIONS) if body[i * spec.GW_CONN_DEF_SIZE] != 0]


# ----------------------------------------------------------------------------- SFI (S6.1)

@dataclass(frozen=True)
class SfiTable:
    miss_limit: int = 3
    window: int = 5
    history: int = 60
    apps: tuple[str, ...] = spec.SFI_DEFAULT_APPS
    watches: tuple[tuple[int, int, int, int, int], ...] = ()  # (mid, offset, width, rule, limit)

    def pack(self) -> bytes:
        body = struct.pack("<BxHHH", self.miss_limit, self.window, self.history, 0)
        for i in range(spec.SFI_MAX_APPS):
            body += _cstr(self.apps[i], spec.API_LEN) if i < len(self.apps) else bytes(spec.API_LEN)
        for i in range(spec.SFI_MAX_WATCHES):
            mid, off, width, rule, limit = self.watches[i] if i < len(self.watches) else (0, 0, 0, 0, 0)
            body += struct.pack("<IHBBI", mid, off, width, rule, limit)
        assert len(body) == spec.SFI_TABLE_SIZE
        return body

    @staticmethod
    def unpack(body: bytes) -> "SfiTable":
        assert len(body) == spec.SFI_TABLE_SIZE
        n, w, h, _ = struct.unpack_from("<BxHHH", body, 0)
        apps = tuple(_uncstr(body[8 + i * 20:8 + (i + 1) * 20]) for i in range(spec.SFI_MAX_APPS))
        watches = []
        for i in range(spec.SFI_MAX_WATCHES):
            row = struct.unpack_from("<IHBBI", body, 8 + 320 + i * 12)
            if row[3] != 0:
                watches.append(row)
        return SfiTable(n, w, h, tuple(a for a in apps if a), tuple(watches))

