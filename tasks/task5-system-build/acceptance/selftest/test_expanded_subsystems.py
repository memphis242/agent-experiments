"""Exact ABI and adversarial decoding tests for the expanded S1/S3/S4 checks."""
from __future__ import annotations

import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_cmdi as cmdi
import live_stor as stor
import live_tlmo as tlmo
import spec
import tables
from common import Fail, Problems


def telemetry(payload: bytes) -> bytes:
    return bytes(16) + payload


def snapshot(rows: tuple[tuple[int, ...], ...]) -> bytes:
    body = struct.pack('<HH', len(rows), 0)
    body += b''.join(struct.pack(spec.TLMO_STREAM_FMT, *row) for row in rows)
    return telemetry(body + bytes((64 - len(rows)) * 32))


class NativeAbi(unittest.TestCase):
    def test_key_slots_roundtrip(self) -> None:
        value = tables.CmdiTable(next_key=bytes(range(32)), next_valid=1)
        image = value.pack()
        self.assertEqual(len(image), 92)
        self.assertEqual(image[56:88], value.next_key)
        self.assertEqual(image[88:], b'\1\0\0\0')
        decoded = tables.CmdiTable.unpack(image)
        self.assertEqual(decoded.key, value.key)
        self.assertEqual(decoded.next_key, value.next_key)
        self.assertEqual(decoded.next_valid, 1)

    def test_profiles_and_sparse_streams(self) -> None:
        value = tables.TlmoTable(0, 32, ((0x18EA, 3, 255), (0, 0, 0), (0x18EB, 2)))
        image = value.pack()
        self.assertEqual(len(image), 540)
        self.assertEqual(image[9], 255)
        decoded = tables.TlmoTable.unpack(image)
        self.assertEqual(decoded.streams[:3], ((0x18EA, 3, 255), (0, 0, 0), (0x18EB, 2, 1)))
        self.assertEqual(decoded.profiles, value.profiles)
        self.assertEqual(decoded.used(), {0x18EA: 3, 0x18EB: 2})

    def test_default_slot_order(self) -> None:
        for node in (1, 2):
            mids = tuple(spec.tlmo_default_streams(node))
            self.assertEqual(len(mids), 57)
            self.assertEqual(mids[38], spec.verif_mid(0xEA, node))
            self.assertEqual(mids[42], spec.verif_mid(0xEB, node))
            self.assertEqual(mids[-3:], (spec.cmd_mid(0xF3, node), 0x187A, 0x187B))
            self.assertEqual(mids[43:48], tuple(spec.tlm_mid(mid, node) for mid in range(0xFB, 0x100)))

    def test_expanded_hk_sizes(self) -> None:
        self.assertEqual({name: spec.HK_LAYOUTS[name].size for name in ('STOR', 'CMDI', 'TLMO')},
                         {'STOR': 1096, 'CMDI': 48, 'TLMO': 60})
        self.assertEqual(spec.TLMO_STREAM_SIZE, 2052)
        self.assertEqual(spec.CMDI_SOURCE_SIZE, 288)


class RequestDecoding(unittest.TestCase):
    def test_source_rows_full_order_and_errors(self) -> None:
        rows = [struct.pack(spec.CMDI_SOURCE_FMT, source, 0, *range(7)) for source in (1, 2, 0, 0, 0, 0, 0, 0, 0)]
        packet = telemetry(b''.join(rows))
        actual = cmdi.parse_source_stats(packet)
        self.assertEqual(len(actual), 9)
        self.assertEqual(actual[0]['SourceId'], 1)
        self.assertEqual(actual[0]['ReplayErrors'], 6)
        for invalid in (packet[:-1], packet + b'\0'):
            with self.assertRaises(Fail):
                cmdi.parse_source_stats(invalid)
        bad = bytearray(packet); bad[18] = 1
        with self.assertRaises(Fail):
            cmdi.parse_source_stats(bytes(bad))
        p = Problems()
        cmdi.check_source_row(p, actual[0], Accepted=0, FormatErrors=1, CrcErrors=2, HmacErrors=3,
                              SourceErrors=4, AgeErrors=5, ReplayErrors=6)
        self.assertEqual(p.items, [])
        cmdi.check_source_row(p, actual[0], Accepted=0)
        self.assertTrue(p.items)

    def test_stream_snapshot_sparse_slot_and_zero_tail(self) -> None:
        rows = ((0, 0, 0x18EA, 2, 2, 6, 12, 0, 0, 6, 0),
                (7, 0, 0x18EB, 3, 3, 4, 12, 0, 0, 8, 0))
        packet = snapshot(rows)
        decoded = tlmo.parse_stream_stats(packet)
        self.assertEqual([r.slot for r in decoded], [0, 7])
        p = Problems()
        tlmo.check_stream(p, decoded[0], received=12, sent=0, queued=6, decimated=6)
        self.assertEqual(p.items, [])
        tlmo.check_stream(p, decoded[0], received=12, sent=6, queued=0, decimated=6)
        self.assertTrue(p.items)
        for offset in (18, 22, 20 + 64):
            bad = bytearray(packet); bad[offset] = 1
            with self.assertRaises(Fail):
                tlmo.parse_stream_stats(bytes(bad))
        with self.assertRaises(Fail):
            tlmo.parse_stream_stats(packet[:-32])
        with self.assertRaises(Fail):
            tlmo.parse_stream_stats(snapshot((rows[1], rows[0])))
        with self.assertRaises(Fail):
            tlmo.parse_stream_stats(snapshot((rows[0], rows[0])))
        self.assertEqual(tlmo.parse_stream_stats(snapshot(())), ())

    def test_file_info_exact_size_crc_identity(self) -> None:
        for content in (b'', b'123456789', bytes(range(256))):
            packet = telemetry(struct.pack(spec.STOR_FILE_INFO_FMT, 0, b'data.bin', len(content), zlib.crc32(content)))
            self.assertEqual(stor.parse_file_info(packet), {'Area': 0, 'Path': 'data.bin', 'Size': len(content), 'Crc': zlib.crc32(content)})
        for bad in (packet[:-1], packet + b'\0'):
            with self.assertRaises(Fail):
                stor.parse_file_info(bad)
        bad = bytearray(packet); bad[17] = 1
        with self.assertRaises(Fail):
            stor.parse_file_info(bytes(bad))

    def test_priority_accounting_exact_rows(self) -> None:
        hk = {f'PriorityBytes{i}': 0 for i in range(256)}
        hk.update(TelemetryBytesUsed=100, PriorityBytes0=30, PriorityBytes200=70)
        p = Problems(); stor.check_priority_bytes(p, hk, {0: 30, 200: 70})
        self.assertEqual(p.items, [])
        hk['PriorityBytes200'] -= 1
        stor.check_priority_bytes(p, hk)
        self.assertTrue(p.items)


if __name__ == '__main__':
    unittest.main()
