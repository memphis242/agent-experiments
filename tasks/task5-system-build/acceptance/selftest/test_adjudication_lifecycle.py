"""Owned subprocess and PSP resources cannot leak into the next scenario."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle


class OwnedSessions(unittest.TestCase):
    def test_timeout_terminates_shell_and_inherited_child(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'child.pid'
            command = [sys.executable, '-c',
                       'import subprocess,sys,time; '
                       'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                       'open(sys.argv[1],"w").write(str(p.pid)); time.sleep(60)', str(marker)]
            with self.assertRaises(subprocess.TimeoutExpired):
                lifecycle.run_owned_session(command, timeout=0.3)
            pid = int(marker.read_text())
            state = Path(f'/proc/{pid}/stat')
            self.assertTrue(not state.exists() or state.read_text().split()[2] == 'Z')


class PspRetirement(unittest.TestCase):
    def test_removes_only_new_unattached_segment_matching_owned_key(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            keyfile = Path(root) / '.cdskeyfile'
            keyfile.touch()
            old = lifecycle.Segment(42, 1, 100, 10, 0, os.geteuid())
            owned = lifecycle.Segment(43, 2, 101, 11, 0, os.geteuid())
            foreign = lifecycle.Segment(44, 3, 102, 12, 0, os.geteuid())
            with patch.object(lifecycle, 'segments', return_value=(old, owned, foreign)), \
                 patch.object(lifecycle, 'file_key', return_value=43), \
                 patch.object(lifecycle, 'remove_segment') as remove:
                lifecycle.retire_psp(Path(root), (old,), frozenset({101}))
            remove.assert_called_once_with(owned)

    def test_does_not_remove_preexisting_or_attached_segment(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / '.resetkeyfile').touch()
            old = lifecycle.Segment(42, 1, 100, 10, 0, os.geteuid())
            with patch.object(lifecycle, 'segments', return_value=(old,)), \
                 patch.object(lifecycle, 'file_key', return_value=42), \
                 patch.object(lifecycle, 'remove_segment') as remove:
                lifecycle.retire_psp(Path(root), (old,), frozenset({101}))
            remove.assert_not_called()
            active = lifecycle.Segment(42, 2, 100, 11, 1, os.geteuid())
            with patch.object(lifecycle, 'segments', return_value=(active,)), \
                 patch.object(lifecycle, 'file_key', return_value=42), \
                 patch.object(lifecycle, 'remove_segment') as remove:
                with self.assertRaisesRegex(lifecycle.Fail, 'attached'):
                    lifecycle.retire_psp(Path(root), (), frozenset({100}))
            remove.assert_not_called()

    def test_same_uid_new_segment_with_foreign_creator_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / '.cdskeyfile').touch()
            foreign = lifecycle.Segment(42, 2, 999, 11, 0, os.geteuid())
            with patch.object(lifecycle, 'segments', return_value=(foreign,)), \
                 patch.object(lifecycle, 'file_key', return_value=42), \
                 patch.object(lifecycle, 'remove_segment') as remove:
                with self.assertRaisesRegex(lifecycle.Fail, 'foreign creator'):
                    lifecycle.retire_psp(Path(root), (), frozenset({100}))
            remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()
