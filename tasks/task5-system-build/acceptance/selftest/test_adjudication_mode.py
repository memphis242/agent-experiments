from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

from common import Fail
from live_mode import RestartStopTimeoutEvidence, judge_restart_stop_timeout


class RestartStopTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid = RestartStopTimeoutEvidence(
            response_time=100.5, error_times=(106.5,), stop_complete_time=109.0, stop_targets=("GW",),
            start_count=0, action_error_delta=1, restart_delta=0,
            restart_phase=0, attempts_before=1, attempts_after=1,
            cooldown_before=130, cooldown_after=130)

    def test_native_cleanup_outliving_deadline_is_valid(self) -> None:
        judge_restart_stop_timeout(self.valid)

    def test_deadline_boundaries_include_live_tolerance(self) -> None:
        for elapsed in (5.0, 8.0):
            with self.subTest(elapsed=elapsed):
                judge_restart_stop_timeout(replace(self.valid, error_times=(100.5 + elapsed,)))

    def test_invalid_timeout_evidence_fails(self) -> None:
        mutations = (
            {"error_times": ()},
            {"error_times": (106.5, 107.5)},
            {"error_times": (105.49,)},
            {"error_times": (108.51,)},
            {"stop_complete_time": 104.5},
            {"stop_targets": ()},
            {"stop_targets": ("STOR",)},
            {"stop_targets": ("GW", "GW")},
            {"start_count": 1},
            {"action_error_delta": 0},
            {"action_error_delta": 2},
            {"restart_delta": 1},
            {"restart_phase": 1},
            {"attempts_before": 0, "attempts_after": 0},
            {"attempts_after": 2},
            {"cooldown_after": 131},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(Fail):
                judge_restart_stop_timeout(replace(self.valid, **mutation))


if __name__ == "__main__":
    unittest.main()
