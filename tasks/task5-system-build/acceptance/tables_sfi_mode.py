"""Native table bodies for expanded SFI and MODE acceptance stimuli."""
from __future__ import annotations

from dataclasses import dataclass, field
import struct
import spec


def cstr(value: str, size: int) -> bytes:
    encoded = value.encode('ascii')
    assert len(encoded) < size
    return encoded.ljust(size, b'\0')


@dataclass(frozen=True)
class SfiTable:
    miss_limit: int = 3
    window: int = 5
    history: int = 60
    apps: tuple[str, ...] = spec.SFI_DEFAULT_APPS
    # Rows can retain the five-field legacy shape or specify all eleven fields.
    watches: tuple[tuple[int, ...], ...] = ()
    app_controls: tuple[tuple[int, int, int, int], ...] = ()
    peer_enabled: int = 1
    peer_timeout: int = 10

    def pack(self) -> bytes:
        assert len(self.apps) <= 16 and len(self.watches) <= 16
        body = struct.pack('<BxHHH', self.miss_limit, self.window, self.history, 0)
        for index in range(16):
            name = self.apps[index] if index < len(self.apps) else ''
            controls = self.app_controls[index] if index < len(self.app_controls) else ((1, 1, 5, 10) if name else (0, 0, 0, 0))
            body += cstr(name, 20) + bytes(controls)
        for index in range(16):
            row = self.watches[index] if index < len(self.watches) else (0,) * 11
            if len(row) == 5:
                row = (*row, 10 if row[3] == 3 else 0, 20 if row[3] == 3 else 0, 3 if row[3] == 3 else 0, index if index < 4 else 255, 2 if row[3] == 3 else 1, 1)
            assert len(row) == 11
            body += struct.pack('<IHBBIIIBBBB', *row)
        body += struct.pack('<BBH', self.peer_enabled, self.peer_timeout, 0)
        assert len(body) == 780
        return body

    @staticmethod
    def unpack(body: bytes) -> 'SfiTable':
        assert len(body) == 780
        miss, window, history, _ = struct.unpack_from('<BxHHH', body)
        names = tuple(body[8+i*24:28+i*24].split(b'\0', 1)[0].decode('ascii') for i in range(16))
        controls = tuple(tuple(body[28+i*24:32+i*24]) for i in range(16))
        watches = tuple(struct.unpack_from('<IHBBIIIBBBB', body, 392+i*24) for i in range(16))
        return SfiTable(miss, window, history, names, watches, controls, body[776], body[777])


@dataclass(frozen=True)
class ModeResponse:
    target: int = 1
    restart: int = 0
    set_priority: int = 0
    transfer: int = 0
    switch_route: int = 0
    route: int = 0
    prefer_backup: int = 1
    priority: int = 200
    cooldown: int = 30
    cap: int = 3
    record_mid: int = 0x0801
    area: int = 0
    destination: int = 0
    success: int = 0
    path: str = 'mode/response.bin'
    dest_path: str = ''

    def pack(self) -> bytes:
        return struct.pack(spec.MODE_RESPONSE_FMT, self.target, self.restart, self.set_priority, self.transfer,
                           self.switch_route, self.route, self.prefer_backup, self.priority, self.cooldown,
                           self.cap, 0, self.record_mid, self.area, self.destination, self.success, 0,
                           cstr(self.path, 64), cstr(self.dest_path, 64))


def default_responses() -> tuple[ModeResponse, ...]:
    flags = ((1,1,0,0,1), (0,1,0,0,1), (0,1,0,0,1), (0,1,0,0,2),
             (0,1,1,0,1), (0,0,0,1,1), (0,0,0,1,1))
    return tuple(ModeResponse(target=t, restart=r, set_priority=p, transfer=x, switch_route=g)
                 for r,p,x,g,t in flags)


@dataclass(frozen=True)
class ModeTable:
    enabled: int = 1
    peer_enabled: int = 1
    peer_timeout: int = 10
    recovery: int = 20
    profiles: tuple[tuple[int, int, int, int], ...] = spec.MODE_DEFAULT_PROFILES
    restarts: tuple[tuple[str, str, str, int, int], ...] = spec.MODE_DEFAULT_RESTARTS
    responses: tuple[ModeResponse, ...] = field(default_factory=default_responses)

    def pack(self) -> bytes:
        assert len(self.profiles) == 3 and len(self.restarts) <= 4 and len(self.responses) == 7
        body = bytes((self.enabled, self.peer_enabled, self.peer_timeout, self.recovery))
        body += b''.join(bytes(profile) for profile in self.profiles)
        for index in range(4):
            name, entry, path, stack, priority = self.restarts[index] if index < len(self.restarts) else ('','','',0,0)
            body += struct.pack(spec.MODE_RESTART_FMT, cstr(name,20), cstr(entry,20), cstr(path,64), stack, priority, 0)
        body += b''.join(response.pack() for response in self.responses)
        assert len(body) == 1500
        return body


def install(t) -> None:  # noqa: ANN001
    t.SfiTable, t.ModeTable, t.ModeResponse = SfiTable, ModeTable, ModeResponse
