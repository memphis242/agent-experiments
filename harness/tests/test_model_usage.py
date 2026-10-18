import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import cost
import reanalyze
import run
from test_cost import OPUS, asst, write_jsonl

T0 = "2026-10-17T10:00:00Z"
MODEL_USAGE = {OPUS: {"inputTokens": 150, "outputTokens": 3000, "cacheReadInputTokens": 10_000,
                      "cacheCreationInputTokens": 2000, "costUSD": 9.99}}


def transcript_file(root: Path) -> Path:
    path = root / "s.jsonl"
    write_jsonl(path, [asst("m1", OPUS, T0, 100, 1000, 10_000, w1h=2000)])
    return path


class ModelUsageTest(unittest.TestCase):
    def test_cli_counts_exceed_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = cost.compute_cost(cost.load_requests(transcript_file(Path(tmp))))
        mu = cost.billed_from_model_usage(report, MODEL_USAGE, OPUS)
        self.assertAlmostEqual(mu["total_usd"], (150 * 4 + 3000 * 20 + 10_000 * 0.2 + 2000 * 8) / 1e6, places=9)
        self.assertAlmostEqual(report.scenarios["billed"].total_usd, (100 * 4 + 1000 * 20 + 10_000 * 0.2 + 2000 * 8) / 1e6,
                               places=9)
        gap = mu["untranscribed"][OPUS]
        self.assertEqual(gap["tokens"], {"input": 50, "output": 2000, "cache_read": 0, "cache_creation": 0})
        self.assertAlmostEqual(gap["usd"], (50 * 4 + 2000 * 20) / 1e6, places=9)

    def test_split_defaults_without_transcript_writes(self):
        report = cost.compute_cost(())
        usage = {"claude-sonnet-5-5": {"cacheCreationInputTokens": 1000}, OPUS: {"cacheCreationInputTokens": 1000}}
        mu = cost.billed_from_model_usage(report, usage, OPUS)
        self.assertAlmostEqual(mu["by_model"]["claude-sonnet-5-5"], 1000 * 2.5 / 1e6, places=12)  # subagent: 5m
        self.assertAlmostEqual(mu["by_model"][OPUS], 1000 * 8.0 / 1e6, places=12)                 # main: 1h


class ReanalyzeTest(unittest.TestCase):
    def test_reanalyze_fake_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "abcd1234"
            (run_dir / "transcripts").mkdir(parents=True)
            transcript_file(run_dir / "transcripts")
            (run_dir / "transcripts" / "s.jsonl").rename(run_dir / "transcripts" / "sess1.jsonl")
            cli = {"type": "result", "session_id": "sess1", "total_cost_usd": 9.99, "modelUsage": MODEL_USAGE}
            (run_dir / "cli-round1.stdout.json").write_text(json.dumps(cli))
            record = {"arm": "A1", "custom": 5, "session_ids": ["sess1"], "resume_request_indices": [],
                      "rounds": [{"round": 1, "started_epoch": 0.0, "wall_time_s": 1.0,
                                  "cli": {"total_cost_usd": 1.0}}]}
            (run_dir / "run.json").write_text(json.dumps(record))
            before, after = reanalyze.reanalyze_run(run_dir, Path(tmp) / "gaps.json")
            self.assertIsNone(before)
            out = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(out["custom"], 5)
            self.assertEqual(out["reanalyzed_with"], run.harness_git()[0])
            self.assertEqual(out["cost"]["billed_basis"], "cli_model_usage")
            self.assertAlmostEqual(after, (150 * 4 + 3000 * 20 + 10_000 * 0.2 + 2000 * 8) / 1e6, places=6)
            self.assertGreater(out["cost"]["billed_transcript_usd"] - 0, 0)
            self.assertLess(out["cost"]["billed_transcript_usd"], after)
            self.assertIn("exclude untranscribed", out["cost"]["scenarios_note"])
            self.assertEqual(out["totals"]["billed_usd"], after)
            self.assertEqual(out["behavior"]["max_context_main"], 100 + 10_000 + 2000)
            self.assertIsNone(out["behavior"]["max_context_subagent"])

    def test_missing_model_usage_falls_back_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r"
            (run_dir / "transcripts").mkdir(parents=True)
            write_jsonl(run_dir / "transcripts" / "sess1.jsonl", [asst("m1", OPUS, T0, 1, 1, 0)])
            (run_dir / "run.json").write_text(json.dumps({
                "arm": "A1", "session_ids": ["sess1"], "rounds": [{"round": 1, "started_epoch": 0.0,
                "wall_time_s": 1.0, "cli": {}}]}))
            reanalyze.reanalyze_run(run_dir, Path(tmp) / "g.json")
            out = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(out["cost"]["billed_basis"], "transcript")
            self.assertTrue(out["cost"]["warnings"])


if __name__ == "__main__":
    unittest.main()
