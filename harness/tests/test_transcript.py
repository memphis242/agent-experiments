import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import aggregate
import matrix
import provenance
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
        for r, minutes in zip(runs, (12.0, 30.0, 6.0)):
            r["totals"] = {"wall_time_s": minutes * 60, "turns": 5}
        text = aggregate.render(runs, "billed")
        self.assertIn("| A1 | solo Opus 5.5 | task1 | 1/3 | 60.00 | 10.00 to 30.00 | 20.00 | 6.0 to 30.0 | 5 | ", text)
        runs[0]["behavior"] = {"max_context_main": 120_000, "max_context_subagent": None}
        runs[1]["behavior"] = {"max_context_main": 90_500, "max_context_subagent": 40_000}
        self.assertTrue(aggregate.render(runs, "billed").split("\n")[4].endswith("| 90.5 to 120.0 | 40.0 |"))

    def test_single_value_ranges_and_task_filter(self):
        one = self.run_rec("a", "completed", True, 1.0)
        other = self.run_rec("b", "completed", True, 2.0)
        other["task"] = "task2"
        text = aggregate.render([one, other], "billed", ["task2-sample-app-status"])
        self.assertIn("task2", text)
        self.assertNotIn("task1", text)
        self.assertEqual(aggregate._range([1.0, 1.0], "{:.2f}"), "1.00")
        self.assertEqual(aggregate._range([1.15, 1.66], "{:.2f}"), "1.15 to 1.66")

    def test_hash_outliers_are_listed(self):
        runs = [self.run_rec("a", "completed", True, 1.0), self.run_rec("b", "completed", True, 1.0),
                self.run_rec("c", "completed", True, 1.0, cfg="other", sha="old")]
        warnings = aggregate.hash_warnings(runs, resolve=lambda commit: {"h": "code1", "old": "code2"}[commit])
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("run c" in w for w in warnings))

    def test_matrix_requires_matching_hashes(self):
        done = self.run_rec("a", "completed", True, 1.0)
        done.update(rep=1)
        # Commits "h" and "docs" share run code; "new" changed it.
        resolve = {"h": "code1", "docs": "code1", "new": "code2"}.get
        self.assertTrue(matrix.is_completed([done], "A1", "task1", 1, "c", "code1", resolve))
        self.assertFalse(matrix.is_completed([done], "A1", "task1", 1, "other", "code1", resolve))
        self.assertFalse(matrix.is_completed([done], "A1", "task1", 1, "c", "code2", resolve))
        limited = self.run_rec("b", "usage_limit", False, 1.0)
        self.assertFalse(matrix.is_completed([limited], "A1", "task1", 1, "c", "code1", resolve))

    def test_equivalent_run_code_counts_as_completed(self):
        done = self.run_rec("a", "completed", True, 1.0, sha="old")
        equivalent = next(iter(provenance.EQUIVALENT_RUN_CODES))
        self.assertTrue(matrix.is_completed([done], "A1", "task1", 1, "c", "code1", {"old": equivalent}.get))
        self.assertFalse(matrix.is_completed([done], "A1", "task1", 1, "c", "code1", {"old": "unreviewed"}.get))

    def test_docs_only_commits_keep_runs_reusable(self):
        done = self.run_rec("a", "completed", True, 1.0, sha="docs")
        resolve = {"h": "code1", "docs": "code1", "new": "code2"}.get
        self.assertTrue(matrix.is_completed([done], "A1", "task1", 1, "c", "code1", resolve))
        unknown = self.run_rec("b", "completed", True, 1.0, sha="gone")
        self.assertFalse(matrix.is_completed([unknown], "A1", "task1", 1, "c", "code1", resolve))


if __name__ == "__main__":
    unittest.main()
