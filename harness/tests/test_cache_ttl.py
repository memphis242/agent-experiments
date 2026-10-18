import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import analysis
import arms
import cost
from test_cost import OPUS, SONNET46, asst, write_jsonl

T0, T1, T2 = "2026-10-17T10:00:00Z", "2026-10-17T10:01:00Z", "2026-10-17T10:02:00Z"


def report(main_entries, sub_entries=()):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "s.jsonl"
        write_jsonl(main, main_entries)
        sub_dir = root / "s" / "subagents"
        if sub_entries:
            write_jsonl(sub_dir / "agent-a1.jsonl", sub_entries)
        requests = cost.load_requests([main], [sub_dir])
    return analysis.cache_ttl_report(requests, arms.PROMPT_CACHE_TTL, arms.SUBAGENT_PROMPT_CACHE_TTL)


class CacheTtlTest(unittest.TestCase):
    def test_expected_ttls_come_from_arms(self):
        self.assertEqual((arms.PROMPT_CACHE_TTL, arms.SUBAGENT_PROMPT_CACHE_TTL), ("1h", "5m"))

    def test_main_1h_only_has_no_violation(self):
        summary, violations = report([asst("m1", OPUS, T0, 1, 1, 0, w1h=500), asst("m2", OPUS, T1, 1, 1, 0, w1h=300)],
                                     [asst("s1", SONNET46, T0, 1, 1, 0, w5m=100)])
        self.assertEqual(violations, [])
        self.assertEqual(summary["main"]["write_1h_tokens"], 800)
        self.assertEqual(summary["main"]["write_5m_tokens"], 0)
        self.assertEqual(summary["main"]["mismatched_requests"], 0)
        self.assertEqual(summary["subagent"]["write_5m_tokens"], 100)

    def test_main_5m_write_is_flagged_with_count_and_first_timestamp(self):
        summary, violations = report([asst("m1", OPUS, T0, 1, 1, 0, w1h=500), asst("m2", OPUS, T1, 1, 1, 0, w5m=40),
                                      asst("m3", OPUS, T2, 1, 1, 0, w1h=10, w5m=5)])
        self.assertEqual(summary["main"]["requests_with_5m_write"], 2)
        self.assertEqual(summary["main"]["mismatched_requests"], 2)
        self.assertEqual(summary["main"]["first_mismatch_timestamp"], "2026-10-17T10:01:00Z")
        self.assertEqual(len(violations), 1)
        self.assertEqual((violations[0]["kind"], violations[0]["source"], violations[0]["count"]),
                         ("cache_ttl", "main", 2))

    def test_subagent_1h_write_is_reported(self):
        summary, violations = report([asst("m1", OPUS, T0, 1, 1, 0, w1h=500)],
                                     [asst("s1", SONNET46, T1, 1, 1, 0, w1h=70)])
        self.assertEqual(summary["subagent"]["write_1h_tokens"], 70)
        self.assertEqual([(v["kind"], v["source"], v["expected"], v["observed"], v["count"]) for v in violations],
                         [("cache_ttl", "subagent", "5m", "1h", 1)])

    def test_unsplit_main_write_is_not_judged(self):
        entry = asst("m1", OPUS, T0, 1, 1, 0, w5m=40)
        entry["message"]["usage"].pop("cache_creation", None)
        summary, violations = report([entry])
        self.assertEqual(violations, [])
        self.assertEqual((summary["main"]["unsplit_requests"], summary["main"]["mismatched_requests"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
