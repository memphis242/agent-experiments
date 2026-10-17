import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import cost
import transcript


def asst(msg_id, model, ts, inp, out, read, w1h=0, w5m=0, content=None):
    return {"type": "assistant", "timestamp": ts, "uuid": f"u-{msg_id}-{out}",
            "message": {"id": msg_id, "model": model, "content": content or [{"type": "text", "text": "x"}],
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": w1h + w5m,
                                  "cache_creation": {"ephemeral_1h_input_tokens": w1h,
                                                     "ephemeral_5m_input_tokens": w5m}}}}


def write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


OPUS, SONNET46 = "claude-opus-5-5", "claude-sonnet-4-6"
AGENT_USE = {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"subagent_type": "explorer"}}


class CostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.main = root / "sess.jsonl"
        write_jsonl(self.main, [
            asst("m1", OPUS, "2026-10-17T10:00:00Z", 100, 1000, 10_000, w1h=2000,
                 content=[AGENT_USE]),
            # Same message id repeated by a parallel tool call: must count once.
            asst("m1", OPUS, "2026-10-17T10:00:01Z", 100, 1000, 10_000, w1h=2000,
                 content=[{"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]),
            {"type": "system", "subtype": "compact_boundary", "uuid": "c1"},
            asst("m2", OPUS, "2026-10-17T10:01:00Z", 50, 500, 20_000),
            asst("synth", "<synthetic>", "2026-10-17T10:02:00Z", 0, 0, 0),
        ])
        write_jsonl(root / "sess" / "subagents" / "agent-a.jsonl", [
            asst("s1", SONNET46, "2026-10-17T10:00:30Z", 1000, 2000, 30_000, w5m=4000),
        ])
        self.sub_dir = root / "sess" / "subagents"

    def test_dedup_and_grouping(self):
        reqs = cost.load_requests(self.main, self.sub_dir)
        self.assertEqual([r.message_id for r in reqs], ["m1", "m2", "s1"])
        report = cost.compute_cost(reqs)
        self.assertEqual(set(report.tokens), {f"{OPUS}|main", f"{SONNET46}|subagent"})
        self.assertEqual(report.tokens[f"{OPUS}|main"]["requests"], 2)
        self.assertEqual(report.tokens[f"{OPUS}|main"]["cache_read"], 30_000)

    def test_scenarios_without_pauses(self):
        report = cost.compute_cost(cost.load_requests(self.main, self.sub_dir))
        # Opus main: input 150, output 1500, read 30000, writes 2000 (1h).
        # Sonnet 4.6 sub: input 1000, output 2000, read 30000, writes 4000 (5m recorded).
        none = (150 + 30_000 + 2000) * 4.0 + 1500 * 20.0 + (1000 + 30_000 + 4000) * 3.0 + 2000 * 15.0
        m5 = 150 * 4.0 + 30_000 * 0.2 + 2000 * 5.0 + 1500 * 20.0 \
            + 1000 * 3.0 + 30_000 * 0.3 + 4000 * 3.75 + 2000 * 15.0
        h1 = 150 * 4.0 + 30_000 * 0.2 + 2000 * 8.0 + 1500 * 20.0 \
            + 1000 * 3.0 + 30_000 * 0.3 + 4000 * 6.0 + 2000 * 15.0
        self.assertAlmostEqual(report.scenarios["none"].total_usd, none / 1e6, places=9)
        self.assertAlmostEqual(report.scenarios["5m"].total_usd, m5 / 1e6, places=9)
        self.assertAlmostEqual(report.scenarios["1h"].total_usd, h1 / 1e6, places=9)
        self.assertAlmostEqual(report.scenarios["1h"].by_source["subagent"] + report.scenarios["1h"].by_source["main"],
                               report.scenarios["1h"].total_usd, places=12)

    def test_pause_rebilling(self):
        reqs = cost.load_requests(self.main, self.sub_dir)
        base = cost.compute_cost(reqs)
        # Main index 1 is m2 (20000 cache reads). A 600 s pause hits 5m only.
        mid = cost.compute_cost(reqs, {1: 600.0})
        self.assertAlmostEqual(mid.scenarios["5m"].total_usd - base.scenarios["5m"].total_usd,
                               20_000 * (5.0 - 0.2) / 1e6, places=9)
        self.assertAlmostEqual(mid.scenarios["1h"].total_usd, base.scenarios["1h"].total_usd, places=12)
        self.assertAlmostEqual(mid.scenarios["none"].total_usd, base.scenarios["none"].total_usd, places=12)
        # A 4000 s pause hits both.
        long = cost.compute_cost(reqs, {1: 4000.0})
        self.assertAlmostEqual(long.scenarios["1h"].total_usd - base.scenarios["1h"].total_usd,
                               20_000 * (8.0 - 0.2) / 1e6, places=9)
        # Exactly at the threshold does not rebill.
        edge = cost.compute_cost(reqs, {1: 300.0})
        self.assertAlmostEqual(edge.scenarios["5m"].total_usd, base.scenarios["5m"].total_usd, places=12)

    def test_errors(self):
        reqs = cost.load_requests(self.main, self.sub_dir)
        with self.assertRaises(cost.CostError):
            cost.compute_cost(reqs, {5: 1000.0})
        with self.assertRaises(cost.CostError):
            cost.price_for("claude-mystery-9")
        bad = Path(self.tmp.name) / "bad.jsonl"
        bad.write_text("{not json\n", encoding="utf-8")
        with self.assertRaises(transcript.TranscriptError):
            cost.load_requests(bad)

    def test_sampling_is_seeded(self):
        gaps = [1.0, 10.0, 400.0, 5000.0]
        self.assertEqual(cost.sample_pauses([3, 7], gaps, 42), cost.sample_pauses([3, 7], gaps, 42))

    def test_behavior_counts(self):
        b = transcript.summarize_behavior([self.main], [self.sub_dir])
        self.assertEqual(b["compaction_events"], 1)
        self.assertEqual(b["subagent_spawns"], 1)
        self.assertEqual(b["subagent_spawns_by_type"], {"explorer": 1})
        self.assertEqual(b["tool_calls_main"], {"Agent": 1, "Bash": 1})


if __name__ == "__main__":
    unittest.main()
