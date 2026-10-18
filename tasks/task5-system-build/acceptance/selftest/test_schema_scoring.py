"""Self-test of the result file against the harness contract, the feedback texts, and per-subsystem scoring."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
sys.path.insert(0, str(PACK))
HARNESS = PACK.parents[2] / "harness"
sys.path.insert(0, str(HARNESS))

import acceptance as harness_acceptance  # noqa: E402  (harness/acceptance.py)
import check  # noqa: E402
import hidden_glue as hg  # noqa: E402


def synthetic(passing: set[str]) -> list[dict[str, object]]:
    out = []
    for c in check.CHECKS:
        ok = c.check_id in passing
        out.append({"id": c.check_id, "passed": ok, "feedback": "" if ok else c.feedback, "detail": "synthetic"})
    return out


class Contract(unittest.TestCase):
    def test_every_feedback_is_contract_clean(self) -> None:
        for c in check.CHECKS:
            fb = c.feedback
            self.assertTrue(fb.strip() and c.text.strip(), c.check_id)
            self.assertIsNone(harness_acceptance._LEAK_RE.search(fb), f"{c.check_id} feedback leaks the pack: {fb}")
            self.assertNotIn(chr(0x2014), fb, "no em dash")
            self.assertTrue(fb.startswith(check.SUBSYSTEMS[c.sub]), f"{c.check_id} feedback does not name its subsystem")

    def test_hidden_feedback_is_contract_clean(self) -> None:
        for cid, spec_ in hg.load_case_specs(PACK).items():
            label = check.BY_ID[cid].label
            for case in spec_.cases:
                fb = hg.feedback_for(spec_, [case], label)
                self.assertIsNone(harness_acceptance._LEAK_RE.search(fb), f"{cid}/{case}: {fb}")
                self.assertNotIn(chr(0x2014), fb)
                for word in ("hidden", "acceptance", "selftest", "t5_model", "model", "stub"):
                    self.assertNotIn(word, (spec_.base + " " + spec_.cases[case]).lower(), f"{cid}/{case} mentions {word}")
            all_fb = hg.feedback_for(spec_, list(spec_.cases), label)
            self.assertTrue(all_fb.startswith(label))

    def test_result_schema_all_fail_and_all_pass(self) -> None:
        for passing in (set(), set(check.BY_ID)):
            checks = synthetic(passing)
            ok = all(c["passed"] for c in checks)
            res = harness_acceptance.parse_result({"passed": ok, "checks": checks}, 0 if ok else 1)
            self.assertEqual(res.passed, ok)
            if not ok:
                fb = harness_acceptance.compose_feedback(res.failures())
                self.assertEqual(fb, harness_acceptance.compose_feedback(res.failures()), "deterministic")
                self.assertIn("1. ", fb)

    def test_failures_sorted_by_subsystem_prefix(self) -> None:
        checks = synthetic(set())
        res = harness_acceptance.parse_result({"passed": False, "checks": checks}, 1)
        ids = [c.check_id for c in res.failures()]
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(ids[0].startswith("n-") and ids[-1].startswith("s7-"))

    def test_run_order_and_lanes(self) -> None:
        self.assertEqual(sorted(sum(check.RUN_ORDER.values(), [])), sorted(check.BY_ID))
        for c in check.CHECKS:
            self.assertEqual(c.check_id.split("-", 1)[0].upper(), c.sub)
        self.assertTrue(all(c.lane == "isolated" for c in check.CHECKS if c.live))


class Scoring(unittest.TestCase):
    def test_summary_fractions(self) -> None:
        passing = {c.check_id for c in check.CHECKS if c.sub in ("S1", "N")}
        checks = synthetic(passing)
        ran = {c["id"] for c in checks} - {"s7-st-stor-transfer"}
        s = check.summarize(checks, ran)
        self.assertTrue(s["preliminary"])
        self.assertTrue(s["subsystems"]["S1"]["passed_all"])
        self.assertEqual(s["subsystems"]["S1"]["fraction"], 1.0)
        self.assertFalse(s["subsystems"]["S5"]["passed_all"])
        self.assertEqual(s["subsystems"]["S5"]["fraction"], 0.0)
        self.assertEqual(s["subsystems"]["S7"]["run"], s["subsystems"]["S7"]["checks"] - 1)
        total = sum(v["checks"] for v in s["subsystems"].values())
        self.assertEqual(total, len(check.CHECKS))
        self.assertEqual(s["overall"]["passed"], len(passing))
        self.assertFalse(s["overall"]["passed_all"])
        json.dumps(s)

    def test_summary_all_pass(self) -> None:
        checks = synthetic(set(check.BY_ID))
        s = check.summarize(checks, {c["id"] for c in checks})
        self.assertTrue(s["overall"]["passed_all"])
        self.assertTrue(all(v["passed_all"] for v in s["subsystems"].values()))

    def test_results_writer_partial(self) -> None:
        r = check.Results()
        r.put("n-targets", False, check.BY_ID["n-targets"].feedback, "x")
        r.put("n-readme", True, "", "y")
        r.put("n-schedule", False, check.BY_ID["n-schedule"].feedback, "not run", ran=False)
        self.assertEqual(r.ran, {"n-targets", "n-readme"})
        self.assertEqual(r.items["n-readme"]["feedback"], "")


class HiddenParsing(unittest.TestCase):
    OUT = """[BEGIN] 01 S1 Startup and housekeeping
[ PASS] 01.001 x.c:10 - ok
[  END] 01 S1 Startup and housekeeping TOTAL::1 PASS::1 FAIL::0
[BEGIN] 02 S2 Transfer rejections
[ FAIL] 02.001 x.c:20 - TRANSFER while active was accepted
[ FAIL] 02.002 x.c:21 - unmodelled call: OS_TaskCreate (outside ...)
[  END] 02 S2 Transfer rejections TOTAL::2 PASS::0 FAIL::2
"""

    def test_parse(self) -> None:
        seen, failed = hg.parse_output(self.OUT)
        self.assertEqual(seen, ["S1 Startup and housekeeping", "S2 Transfer rejections"])
        self.assertEqual(list(failed), ["S2 Transfer rejections"])

    def _ht(self, out: str, rc: int, built: bool = True) -> hg.HiddenTests:
        ht = hg.HiddenTests.__new__(hg.HiddenTests)
        ht.lock = threading.Lock()
        ht.specs = {"s1-hidden-stor": hg.CaseSpec("hidden", "S1 ", "STOR base.", {"S1 Startup and housekeeping": "start-up"}),
                    "s2-hidden-stor-transfer": hg.CaseSpec("hidden", "S2 ", "STOR transfer.",
                                                           {"S2 Transfer rejections": "TRANSFER is rejected when active"})}
        ht.done = True
        ht.prep_ok = True
        ht.present = {"stor": True}
        res = hg.RunnerResult(built=built, ran=True, returncode=rc, output=out)
        res.seen_cases, res.failed_cases = hg.parse_output(out)
        ht.results = {("stor", "hidden"): res}
        return ht

    def test_verdicts(self) -> None:
        ht = self._ht(self.OUT, 1)
        ht.verdict("s1-hidden-stor", "S1 STOR base - x", lambda w: w)
        with self.assertRaises(hg.Fail) as cm:
            ht.verdict("s2-hidden-stor-transfer", "S2 STOR transfer - x", lambda w: w)
        self.assertIn("TRANSFER is rejected when active", cm.exception.feedback)
        self.assertIn("OS_TaskCreate", cm.exception.feedback)
        self.assertIn("UNMODELLED", str(cm.exception))

    def test_crash_and_build_failure(self) -> None:
        ht = self._ht("[BEGIN] 01 S1 Startup and housekeeping\n", -11)
        with self.assertRaises(hg.Fail) as cm:
            ht.verdict("s1-hidden-stor", "S1 STOR base - x", lambda w: w)
        self.assertIn("must not crash", cm.exception.feedback)
        ht = self._ht("", 0, built=False)
        with self.assertRaises(hg.Fail) as cm:
            ht.verdict("s1-hidden-stor", "S1 STOR base - x", lambda w: w)
        self.assertIn("compile and link", cm.exception.feedback)


class SidecarName(unittest.TestCase):
    def test_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "acceptance-result-round1.json"
            self.assertEqual(out.with_name(out.stem + ".subsystems.json").name, "acceptance-result-round1.subsystems.json")


class IsolatedLane(unittest.TestCase):
    def test_groups_cover_the_lane_once(self) -> None:
        ids = [c for _name, group in check._GROUPS for c in group]
        self.assertEqual(sorted(ids), sorted(check.RUN_ORDER["isolated"]))
        self.assertEqual(len(ids), len(set(ids)))
        ctf = dict(check._GROUPS)["ctf"]
        self.assertTrue(all(check.BY_ID[c].sub == "S7" for c in ctf))

    def test_group_jobs(self) -> None:
        import os
        old = os.environ.pop("T5_PACK_JOBS", None)
        try:
            self.assertEqual(check.group_jobs(), check.DEFAULT_GROUP_JOBS)
            for good, want in (("1", 1), ("32", 32)):
                os.environ["T5_PACK_JOBS"] = good
                self.assertEqual(check.group_jobs(), want)
            for bad in ("0", "-2", "eight", "", "65", "4.5"):
                os.environ["T5_PACK_JOBS"] = bad
                with self.assertRaises(ValueError) as cm:
                    check.group_jobs()
                self.assertIn("T5_PACK_JOBS", str(cm.exception))
        finally:
            os.environ.pop("T5_PACK_JOBS", None)
            if old is not None:
                os.environ["T5_PACK_JOBS"] = old

    def test_boot_record_is_shared_between_groups(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a = check.Ctx(Path(d), Path(d) / "a", 0.0)
            b = check.Ctx(Path(d), Path(d) / "b", 0.0)
            a.boot_fail_dir = b.boot_fail_dir = Path(d) / "boot"
            with a.boot_record() as rec:
                rec[1].append("first")
            with b.boot_record() as rec:
                self.assertEqual(rec[1], ["first"])
                rec[1].append("second")
            with a.boot_record() as rec:
                self.assertEqual((rec[1], rec[2]), (["first", "second"], []))
                rec[1] = []
            with b.boot_record() as rec:
                self.assertEqual(rec[1], [])


if __name__ == "__main__":
    unittest.main()
