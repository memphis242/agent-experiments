"""Exact-quota acceptance waits for the preceding asynchronous ADD acknowledgment."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_stor
import spec


class DelayedEventsStor:
    """A small STOR model that publishes ADD events only when the ground waits."""

    def __init__(self, root: Path) -> None:
        self.cf = root / "cf"
        self.files_dir = self.cf / "stor" / "files"
        self.cf.mkdir()
        self.files_dir.mkdir(parents=True)
        (self.cf / "stor_tbl.tbl").write_bytes(b"table image")
        self.events: list[int] = []
        self.pending: list[int] = []
        self.file_bytes = 0
        self.file_adds_rejected = 0
        self.command_errors = 0

    def mark(self) -> int:
        return len(self.events)

    def cmd(self, name: str, payload: bytes) -> None:
        if name != "ADD_FILE":
            raise AssertionError(name)
        source = self.cf / payload[:spec.STOR_PATH_LEN].split(b"\0", 1)[0].decode().removeprefix("/cf/")
        dest_name = payload[spec.STOR_PATH_LEN:2 * spec.STOR_PATH_LEN].split(b"\0", 1)[0].decode()
        size = source.stat().st_size
        if self.file_bytes + size > 1048576:
            self.file_adds_rejected += 1
            self.command_errors += 1
            self.pending.append(21)
            return
        dest = self.files_dir / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        self.file_bytes += size
        self.pending.append(22)

    def wait_event(self, mark: int, event_id: int) -> list[object]:
        self.events.extend(self.pending)
        self.pending.clear()
        return [object() for observed in self.events[mark:] if observed == event_id]

    def hk(self) -> dict[str, int]:
        return {"FileAddsRejected": self.file_adds_rejected,
                "CommandErrorCounter": self.command_errors,
                "FileBytesUsed": self.file_bytes}


class ExactQuotaRegression(unittest.TestCase):
    def test_small_add_ack_is_drained_before_exact_fill_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stor = DelayedEventsStor(Path(temp))
            with patch.object(live_stor, "session", return_value=stor):
                live_stor.c_file_quota_rejection(object())


if __name__ == "__main__":
    unittest.main()
