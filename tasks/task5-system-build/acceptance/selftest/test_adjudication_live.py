"""Behavioral regressions for live-check setup, stream rows and asynchronous observations."""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_cmdi as cmdi
import live_stor as stor
import live_tlmo as tlmo
import spec
import tables
from common import Fail


def class_packet(table: tables.TlmoTable) -> bytes:
    body = table.pack()
    count = sum(row[0] != 0 for row in table.streams)
    return bytes(spec.TLM_HDR) + struct.pack('<HH', table.budget, count) + body[4:516]


def default_tlmo() -> tables.TlmoTable:
    return tables.TlmoTable(streams=tuple((mid, cls, 1) for mid, cls in spec.tlmo_default_streams(1).items()))


class TlmoLiveRegressions(unittest.TestCase):
    def class_node(self, table: tables.TlmoTable) -> Mock:
        node = Mock(id=1)
        node.hk.side_effect = [{'CommandCounter': 0}, {'CommandCounter': 1}]
        node.request.return_value = [SimpleNamespace(data=class_packet(table))]
        node.wait_event.return_value = [object()]
        return node

    def test_default_class_map_compares_all_stream_fields(self) -> None:
        table = default_tlmo()
        node = self.class_node(table)
        with patch.object(tlmo, 'boot', return_value={1: node}), patch.object(tables, 'split_image', return_value=(b'', table.pack())):
            tlmo.c_class_map(Mock())
        decoded = tlmo.parse_class_map(class_packet(table))
        self.assertEqual(decoded.decimation, (1,) * 57 + (0,) * 7)
        changed = tables.TlmoTable(streams=((table.streams[0][0], 0, 2),) + table.streams[1:])
        node = self.class_node(changed)
        with patch.object(tlmo, 'boot', return_value={1: node}), patch.object(tables, 'split_image', return_value=(b'', table.pack())):
            with self.assertRaises(Fail):
                tlmo.c_class_map(Mock())

    def test_table_class_changes_preserve_decimation_and_profiles(self) -> None:
        table = tables.TlmoTable(streams=((tlmo.E8, 0, 7), (tlmo.E9, 1, 3)),
                                 profiles=((21, (0, 1, 2, 3)), (11, (0, 0, 2, 2)), (3, (0, 1, 1, 1))))
        node = Mock(id=1)
        node.hk.return_value = {'BudgetPacketsPerCycle': 5}
        node.wait_event.return_value = [object()]
        node.g.since.return_value = []
        observed = tlmo.parse_class_map(class_packet(tables.TlmoTable(budget=5, streams=((tlmo.E8, 3, 7), (tlmo.E9, 1, 3)))))
        with patch.object(tlmo, 'boot', return_value={1: node}), patch.object(tables, 'split_image', return_value=(b'', table.pack())), \
                patch.object(tlmo, 'class_map', return_value=observed), patch.object(tlmo.time, 'sleep'):
            tlmo.c_table_update(Mock())
        images = [tables.TlmoTable.unpack(call.args[1]) for call in node.load_table.call_args_list]
        self.assertEqual(images[0].streams[:2], ((tlmo.E8, 3, 7), (tlmo.E9, 1, 3)))
        self.assertEqual(images[1].streams[:2], ((tlmo.E8, 3, 7), (tlmo.E9, 4, 3)))
        self.assertTrue(all(image.profiles == table.profiles for image in images))

    def test_add_stream_uses_first_unused_default_slot(self) -> None:
        table = default_tlmo()
        node = Mock(id=1)
        node.wait_event.side_effect = [[object()], [object()] * 3, [object()], [object()]]
        node.g.since.side_effect = [[object()] * 3, [object()] * 3, [], []]
        observed = tlmo.parse_class_map(class_packet(tables.TlmoTable(streams=table.streams + ((tlmo.EE, 2, 1),))))
        with patch.object(tlmo, 'boot', return_value={1: node}), patch.object(tlmo, 'class_map', return_value=observed), \
                patch.object(tlmo.time, 'sleep'):
            tlmo.c_add_remove_stream(Mock())


class StorLiveRegressions(unittest.TestCase):
    def test_startup_resets_profile_command_before_absolute_counters(self) -> None:
        counters = {'CommandCounter': 1, 'CommandErrorCounter': 0}
        session = Mock()
        node = session.n
        values = dict(TelemetryQuotaBytes=262144, FileQuotaBytes=1048576, PurgeThresholdPercent=80,
                      ActiveEntries=4, FileCount=0, FileBytesUsed=0, XferState=0, RxState=0, XferActiveId=0)
        session.hk.side_effect = lambda: values | counters
        node.hk.side_effect = lambda _app: session.hk()
        node.cpu = 'cpu1'
        session.packet_files.return_value = [object()]
        session.wait_event.return_value = [object()]

        def command(name: str, *_args: object, **_kwargs: object) -> None:
            if name == 'RESET_COUNTERS':
                counters.update(CommandCounter=0, CommandErrorCounter=0)
            elif name == 'NOOP':
                counters['CommandCounter'] += 1

        session.cmd.side_effect = command
        node.request.side_effect = lambda send, _mid: (send(), [SimpleNamespace(data=bytes(1112))])[1]
        with patch.object(stor, 'session', return_value=session), patch('node.console_events', return_value=[('STOR', 1, '')]), \
                patch('node.parse_hk', side_effect=lambda _app, _raw: session.hk()), patch.object(stor.fr, 'packet_len', return_value=1112):
            stor.c_startup_and_hk(Mock())
        self.assertEqual(session.cmd.call_args_list[0].args[0], 'RESET_COUNTERS')
        self.assertEqual(counters['CommandCounter'], 1)

    def test_archived_first_stimulus_crosses_threshold_once(self) -> None:
        state = {'packets': 0, 'purged': 0, 'peak': 8000}
        session = Mock()
        arc = f'tlm_{stor.INJ_B:04X}_00000000.arc'
        ordinary = f'tlm_{stor.INJ_B:04X}_00000001.bin'

        def files() -> list[SimpleNamespace]:
            entries = [] if state['purged'] else [SimpleNamespace(path=Path(arc), size=4000)]
            entries.append(SimpleNamespace(path=Path(ordinary), size=4000))
            for index in range(stor.ceil_div(state['packets'], 5)):
                packets = min(5, state['packets'] - index * 5)
                entries.append(SimpleNamespace(path=Path(f'new-{index}.bin'), size=64 + packets * stor.PKT))
            return entries

        def inject(_mid: int, count: int, _size: int, **_kwargs: object) -> None:
            state['packets'] += count
            state['peak'] = max(state['peak'], sum(entry.size for entry in files()))

        def wakeup() -> None:
            if sum(entry.size for entry in files()) > 40960 // 2:
                state['purged'] = 1

        session.inject.side_effect = inject
        session.wakeup.side_effect = wakeup
        session.packet_files.side_effect = files
        session.hk.side_effect = lambda: {'TelemetryBytesUsed': sum(entry.size for entry in files()), 'FilesPurged': state['purged']}
        with patch.object(stor, 'session', return_value=session), patch.object(stor.time, 'sleep'):
            stor.c_purge_archived_first(Mock())
        self.assertGreater(state['peak'], 40960 // 2)
        self.assertLessEqual(state['peak'] - 4000, 40960 // 2)
        self.assertEqual(state['purged'], 1)

    def transfer_session(self, *, count_busy_error: bool = True) -> Mock:
        session = Mock()
        state = dict(CommandCounter=0, CommandErrorCounter=0, XferState=0, XfersFailed=0, XferLastStatus=0)
        events: list[tuple[int, int]] = []
        pending = False
        transfers = 0
        session.mark.side_effect = lambda: len(events)
        session.hk.side_effect = lambda: dict(state)
        session.files_dir = MagicMock()
        session.files_dir.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True

        def command(name: str, *_args: object, **_kwargs: object) -> None:
            nonlocal transfers, pending
            if name == 'TRANSFER':
                transfers += 1
                if transfers <= 9:
                    state['CommandErrorCounter'] += 1
                    events.append((len(events), 30))
                elif state['XferState']:
                    pending = True
                else:
                    state['CommandCounter'] += 1
                    state['XferState'] = 2
                    events.append((len(events), 29))
            elif name == 'ABORT_TRANSFER':
                if state['XferState']:
                    state.update(XferState=0, XfersFailed=1, XferLastStatus=spec.XFER_STATUS['ABORTED'])
                    state['CommandCounter'] += 1
                    events.append((len(events), 33))
                else:
                    state['CommandErrorCounter'] += 1
                    events.append((len(events), 30))

        def observed(mark: int, eid: int) -> list[tuple[int, int]]:
            return [event for event in events[mark:] if event[1] == eid]

        def wait_event(mark: int, eid: int, *_args: object, **_kwargs: object) -> list[tuple[int, int]]:
            nonlocal pending
            if eid == 30 and pending:
                pending = False
                state['CommandErrorCounter'] += int(count_busy_error)
                events.append((len(events), 30))
            return observed(mark, eid)

        session.cmd.side_effect = command
        session.events.side_effect = observed
        session.wait_event.side_effect = wait_event
        return session

    def test_busy_rejection_is_observed_after_start_and_counted(self) -> None:
        for count_error in (True, False):
            with self.subTest(count_error=count_error):
                session = self.transfer_session(count_busy_error=count_error)
                with patch.object(stor, 'session', return_value=session):
                    if count_error:
                        stor.c_transfer_abort_and_rejections(Mock())
                    else:
                        with self.assertRaises(Fail):
                            stor.c_transfer_abort_and_rejections(Mock())


class KeyRotationLiveRegressions(unittest.TestCase):
    def test_gw_ingress_waits_for_execution_and_times_out(self) -> None:
        node = Mock(cpu='cpu1')
        for observed, expected_error in (([(2, 0), (2, 0), (3, 0)], False), ([(2, 0)] * 4, True)):
            with self.subTest(expected_error=expected_error):
                clock = iter((0.0, 0.2, 0.4, 0.6, 1.2, 1.4, 1.6))
                with patch.object(cmdi, 'sample_counters', side_effect=observed), \
                        patch.object(cmdi.time, 'monotonic', side_effect=lambda: next(clock)), patch.object(cmdi.time, 'sleep'):
                    if expected_error:
                        with self.assertRaises(Fail):
                            cmdi.wait_sample_counter(node, 3, timeout=1.0)
                    else:
                        cmdi.wait_sample_counter(node, 3, timeout=1.0)


if __name__ == '__main__':
    unittest.main()
