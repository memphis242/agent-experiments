"""Retire only subprocess sessions and native PSP segments created by this scenario."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess

from common import Fail


@dataclass(frozen=True)
class Segment:
    key: int
    shmid: int
    creator: int
    created: int
    attached: int
    uid: int

    @property
    def identity(self) -> tuple[int, int, int]:
        return self.shmid, self.creator, self.created


def segments() -> tuple[Segment, ...]:
    lines = Path('/proc/sysvipc/shm').read_text().splitlines()
    columns = lines[0].split()
    result = []
    for line in lines[1:]:
        row = dict(zip(columns, line.split(), strict=True))
        result.append(Segment(int(row['key']), int(row['shmid']), int(row['cpid']),
                              int(row['ctime']), int(row['nattch']), int(row['uid'])))
    return tuple(result)


def file_key(path: Path) -> int:
    lib = ctypes.CDLL(None, use_errno=True)
    lib.ftok.argtypes = (ctypes.c_char_p, ctypes.c_int)
    lib.ftok.restype = ctypes.c_int
    key = lib.ftok(os.fsencode(path), ord('R'))
    if key == -1:
        raise Fail(f'PSP lifecycle: ftok({path}) failed: {os.strerror(ctypes.get_errno())}')
    return key


def remove_segment(segment: Segment) -> None:
    # Revalidate creation identity to avoid acting on a recycled shmid.
    current = next((row for row in segments() if row.shmid == segment.shmid), None)
    if current is None:
        return
    if current != segment or current.attached:
        raise Fail(f'PSP lifecycle: segment {segment.shmid} changed or remains attached; refusing removal')
    lib = ctypes.CDLL(None, use_errno=True)
    lib.shmctl.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    lib.shmctl.restype = ctypes.c_int
    if lib.shmctl(segment.shmid, 0, None) != 0:  # Linux IPC_RMID
        raise Fail(f'PSP lifecycle: IPC_RMID {segment.shmid} failed: {os.strerror(ctypes.get_errno())}')


def retire_psp(exe: Path, baseline: tuple[Segment, ...], owners: frozenset[int]) -> None:
    """After all owned nodes exit, remove their new keyfile-backed PSP objects.

    Objects present before this lifetime are never candidates. Keyfiles stay in
    place until retirement, so ftok cannot map a recycled inode to stale memory.
    """
    keys = {file_key(path) for name in ('.cdskeyfile', '.resetkeyfile', '.reservedkeyfile')
            if (path := exe / name).is_file()}
    previous = {row.identity for row in baseline}
    for row in segments():
        if row.key not in keys or row.identity in previous:
            continue
        if row.uid != os.geteuid() or row.attached or row.creator not in owners:
            raise Fail(f'PSP lifecycle: owned key {row.key:#x} has foreign creator/uid or attached segment {row.shmid}')
        remove_segment(row)


def run_owned_session(argv: list[str], *, timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Bound a complete child session, including descendants retaining output pipes."""
    process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, errors='replace', start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        # This process group was created by this call, never discovered by name.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)
