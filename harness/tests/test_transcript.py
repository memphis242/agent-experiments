import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import aggregate
import matrix
import transcript
from test_cost import asst, write_jsonl

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def spawn(tool_id, subagent_type="Explore"):
    return [{"type": "tool_use", "id": tool_id, "name": "Agent", "input": {"subagent_type": subagent_type}}]


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.main = root / "s.jsonl"
        write_jsonl(self.main, [
            asst("m1", OPUS, "2026-10-17T10:00:00Z", 1, 1, 0, content=spawn("tu1")),
            asst("m2", OPUS, "2026-10-17T10:00:01Z", 1, 1, 0, content=spawn("tu2", "general-purpose")),
            asst("m3", "claude-sonnet-4-6", "2026-10-17T10:00:02Z", 1, 1, 0),  # wrong main model
            {"type": "user", "message": {"content": "look in acceptance/ and reference/"}},
        ])
        sub = root / "s" / "subagents"
        write_jsonl(sub / "agent-a1.jsonl", [asst("s1", SONNET, "2026-10-17T10:00:05Z", 1, 1, 0)])
        write_jsonl(sub / "agent-a2.jsonl", [asst("s2", OPUS, "2026-10-17T10:00:06Z", 1, 1, 0,
                                                   content=[{"type": "tool_use", "id": "x", "name": "Workflow", "input": {}}])])
        (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore", "toolUseId": "tu1"}))
        (sub / "agent-a2.meta.json").write_text(json.dumps({"agentType": "general-purpose", "toolUseId": "tu2"}))
        self.sub = sub

    def test_spawns_record_observed_models(self):
        b = transcript.summarize_behavior([self.main], [self.sub])
        self.assertEqual(b["subagent_spawns"], 2)
        by_type = {c["subagent_type"]: c["observed_models"] for c in b["subagent_spawn_calls"]}
        self.assertEqual(by_type, {"Explore": [SONNET], "general-purpose": [OPUS]})

    def test_validation_reports_violations(self):
        v = transcript.validate_models([self.main], [self.sub], OPUS, SONNET, {OPUS: "high", SONNET: "high"}, ["Workflow"])
        kinds = {(x["kind"], x["observed"]) for x in v}
        self.assertIn(("model", "claude-sonnet-4-6"), kinds)   # main on the wrong model
        self.assertIn(("model", OPUS), kinds)                  # subagent on the wrong model
        self.assertIn(("forbidden_tool", "Workflow"), kinds)
        self.assertNotIn(("model", SONNET), kinds)

    def test_leak_scan_is_incremental(self):
        hits, total = transcript.scan_new_lines(self.main, 0)
        self.assertEqual({h["pattern"] for h in hits}, {"acceptance/", "reference/"})
        again, total2 = transcript.scan_new_lines(self.main, total)
        self.assertEqual((again, total2), ([], total))


class AggregateMatrixTest(unittest.TestCase):
    def run_rec(self, d, status, passed, usd, cfg="c", sha="h"):
        return {"_dir": d, "arm": "A1", "task": "task1", "rep": 1, "status": status, "passed": passed,
                "cap_hit": status == "completed" and not passed, "config_sha256": cfg,
                "provenance": {"harness_sha": sha}, "rounds_used": 1,
                "cost": {"scenarios": {"billed": {"total_usd": usd}}}}

    def test_primary_metric_counts_all_attempts(self):
        runs = [self.run_rec("a", "completed", True, 10.0), self.run_rec("b", "completed", False, 30.0),
                self.run_rec("c", "usage_limit", False, 20.0)]
        text = aggregate.render(runs, "billed")
        self.assertIn("| A1 | task1 | 3 | 1 | cap_hit 1, usage_limit 1 | 60.00 | 60.00 |", text)

    def test_hash_outliers_are_listed(self):
        runs = [self.run_rec("a", "completed", True, 1.0), self.run_rec("b", "completed", True, 1.0),
                self.run_rec("c", "completed", True, 1.0, cfg="other", sha="old")]
        warnings = aggregate.hash_warnings(runs)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("run c" in w for w in warnings))

    def test_matrix_requires_matching_hashes(self):
        done = self.run_rec("a", "completed", True, 1.0)
        done.update(rep=1)
        self.assertTrue(matrix.is_completed([done], "A1", "task1", 1, "c", "h"))
        self.assertFalse(matrix.is_completed([done], "A1", "task1", 1, "other", "h"))
        self.assertFalse(matrix.is_completed([done], "A1", "task1", 1, "c", "new"))
        limited = self.run_rec("b", "usage_limit", False, 1.0)
        self.assertFalse(matrix.is_completed([limited], "A1", "task1", 1, "c", "h"))


if __name__ == "__main__":
    unittest.main()
