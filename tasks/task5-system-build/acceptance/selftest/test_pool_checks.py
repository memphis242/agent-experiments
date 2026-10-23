"""Allocator sizing uses native descriptor and bucket evidence, not packet bytes alone."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from static_checks import judge_sb_pool


class PoolSizing(unittest.TestCase):
    PROBE = "POOL 24000 80 16 16 3\nBUCKET 256\nBUCKET 2048\nBUCKET 4096\n"

    def test_descriptors_buckets_and_allocator_metadata_are_counted(self) -> None:
        errors, evidence = judge_sb_pool(self.PROBE, (1112, 3904, 2068))
        self.assertEqual(errors, [])
        self.assertEqual(evidence["minimum_envelope_bytes"], 8 * (2048 + 16) + 4096 + 16)
        self.assertEqual(len(evidence["allocations"]), 9)
        self.assertIn("live", evidence["background_verification"])
        errors, _evidence = judge_sb_pool(self.PROBE.replace("24000", "20000"), (1112, 3904, 2068))
        self.assertTrue(any("allocator-rounded minimum" in error for error in errors))

    def test_missing_or_inconsistent_native_evidence_fails(self) -> None:
        for probe in ("", self.PROBE.replace("16 3", "16 4"), self.PROBE.replace("16 16 3", "16 3 3"),
                      self.PROBE.replace("BUCKET 4096", "BUCKET 512")):
            with self.subTest(probe=probe):
                errors, _evidence = judge_sb_pool(probe, (3904,))
                self.assertTrue(errors)
        errors, _evidence = judge_sb_pool(self.PROBE, (8193,))
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
