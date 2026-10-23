"""Post-startup counter baselines and native invalid-load retirement."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Fail
from node import Node
import spec
import tables as tb


class ObservationTests(unittest.TestCase):
    def node(self) -> Node:
        node = Node.__new__(Node)
        node.cpu, node.id = 'cpu1', 1
        node.sb_baseline = (22, 0, 22)
        node.observation_log_start = len('SB allocator failure during boot\n')
        node.console_log = lambda: 'SB allocator failure during boot\n'
        node.ground = SimpleNamespace(listener_errors=[], since=lambda: [], events=lambda *_args: [])
        node.run_dir = MagicMock()
        node.observation_mark = 0
        node.read_sb_counters = Mock(return_value=(22, 0, 22))
        return node

    def test_startup_error_totals_do_not_fail_clean_envelope(self) -> None:
        self.node().verify_observation_integrity()

    def test_nonallocation_counter_deltas_remain_diagnostic(self) -> None:
        for current in ((23, 0, 23), (22, 1, 22), (0, 0, 0)):
            with self.subTest(current=current):
                node = self.node()
                node.read_sb_counters.return_value = current
                node.verify_observation_integrity()
                writer = (node.run_dir / "acceptance-sb-observations.jsonl").open.return_value.__enter__.return_value
                writer.write.assert_called_once()

    def test_new_native_allocation_event_or_log_is_not_ignored(self) -> None:
        for eid in (16, 20):
            node = self.node()
            node.ground.events = lambda *_args: [SimpleNamespace(eid=eid, text='native allocator failure')]
            with self.subTest(eid=eid), self.assertRaises(Fail):
                node.verify_observation_integrity()
        node = self.node()
        node.console_log = lambda: 'SB allocator failure during boot\nSB allocation failure now\n'
        with self.assertRaises(Fail):
            node.verify_observation_integrity()


class InvalidTableRetirement(unittest.TestCase):
    def test_invalid_load_waits_for_abort_before_returning(self) -> None:
        node = Node.__new__(Node)
        node.cpu, node.id, node.table_loads = 'cpu1', 1, 0
        node.ensure_command_path = lambda: None
        node.table_image = lambda _app: b'image'
        node.cf = MagicMock()
        node.ground = Mock()
        node.ground.mark.return_value = 0
        node.send = Mock()
        node.wait_event = Mock(return_value=[object()])
        node.events = Mock(return_value=[SimpleNamespace(eid=spec.CFE_TBL_VALIDATION_ERR_EID,
                                                        text='STOR.StorageTable invalid')])
        from unittest.mock import patch
        with patch.object(tb, 'with_body', return_value=b'image'):
            node.load_table('STOR', b'invalid', expect_valid=False)
        self.assertEqual(tuple(call.args[1] for call in node.send.call_args_list), (2, 4, 9))
        self.assertEqual(node.send.call_args_list[-1].args[2], spec.tbl_activate_payload('STOR.StorageTable'))
        self.assertEqual(node.wait_event.call_args_list[-1].args[2], spec.CFE_TBL_LOAD_ABORT_INF_EID)
        node.wait_event.side_effect = [[object()], []]
        with patch.object(tb, 'with_body', return_value=b'image'), self.assertRaisesRegex(Fail, 'ABORT_LOAD'):
            node.load_table('STOR', b'invalid', expect_valid=False)

class CleanupFailures(unittest.TestCase):
    def test_node_closes_ground_when_psp_retirement_fails(self) -> None:
        from unittest.mock import patch
        node = Node.__new__(Node)
        node.started, node.owned_pids = False, {101}
        node.cpu, node.id, node.exe = 'cpu1', 1, Path('/owned/cpu1')
        node.psp_baseline = ()
        ground = Mock()
        node.ground = ground
        with patch('node.find_node_pids', return_value=[]), \
             patch('node.lifecycle.retire_psp', side_effect=Fail('retirement failed')):
            with self.assertRaisesRegex(Fail, 'retirement failed'):
                node.stop()
        ground.close.assert_called_once()
        self.assertIsNone(node.ground)

    def test_session_attempts_each_node_before_reporting_cleanup_failure(self) -> None:
        import check
        first, second = Mock(cpu='cpu1'), Mock(cpu='cpu2')
        first.stop.side_effect = Fail('injected failure')
        context = check.Ctx.__new__(check.Ctx)
        context.end_callbacks, context.cache, context.nodes = [], {}, {1: first, 2: second}
        with self.assertRaisesRegex(Fail, 'injected failure'):
            context.shutdown_session()
        first.stop.assert_called_once()
        second.stop.assert_called_once()
        self.assertEqual(context.nodes, {})


if __name__ == '__main__':
    unittest.main()
