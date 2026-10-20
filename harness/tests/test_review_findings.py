import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import aggregate
import analysis
import arms
import claude_calls
import codex
import run
from test_codex import THREAD, rollout_lines
from test_cost import asst, write_jsonl


class ReviewTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def runner(self, arm="A26", netns=False):
        cfg = run.RunConfig(arm, "task1", 1, None, self.root / "template", self.root,
                            self.root / "arms", self.root / "tasks", self.root / "gaps", 1,
                            60.0, 60.0, 0, "unused", "task1", netns)
        runner = run.Runner(cfg, "review", self.root / "run")
        for path in (runner.repo, runner.arm_root, runner.codex_home,
                     cfg.tasks_dir / "task1" / "acceptance"):
            path.mkdir(parents=True, exist_ok=True)
        runner.codex_bin = "/fake-codex"
        runner.record = {"arm": arm, "rounds": [], "session_ids": runner.session_ids,
                         "status": "running", "passed": False}
        return runner

    def log(self, home, model="gpt-6.1-sol", extra=()):
        path = home / "sessions" / "rollout-test.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in [*rollout_lines(model=model), *extra]))

    def call(self, **updates):
        directory = self.root / "claude" / "agent-calls" / "c1"
        directory.mkdir(parents=True, exist_ok=True)
        record = {"agent": "implementer", "model": "claude-opus-5-5", "cwd": str(self.root),
                  "session_id": "s1", "started_epoch": 1, "ended_epoch": 2, "exit_code": 0,
                  "failed": False, **updates}
        (directory / "call.json").write_text(json.dumps(record))
        return directory.parent.parent

    def claude_log(self, home, session="s1", model="claude-opus-5-5", inp=100):
        path = home / "projects" / "project" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(path, [asst("msg1", model, "2026-10-17T10:00:00Z", inp, 100, 0)])
        return path

    def test_positive_reconciliation_gap_blocks_completion_and_aggregate_total(self):
        runner = self.runner()
        self.log(runner.codex_home)
        result = {"session_id": THREAD, "usage": {"input_tokens": 400000}, "turns_completed": 1,
                  "is_error": False, "_exit_code": 0, "result": "done"}
        with mock.patch.object(runner, "invoke", return_value=result), mock.patch.object(run.acceptance, "run_pack") as pack:
            runner.run("task")
        self.assertNotEqual(runner.record["status"], "completed")
        self.assertTrue(runner.record["accounting_incomplete"])
        self.assertIsNone(runner.record["totals"]["billed_usd"])
        self.assertGreater(runner.record["cost"]["billed_transcript_usd"], 0)
        self.assertIsNone(aggregate.scenario_cost(runner.record, "billed"))
        pack.assert_not_called()
        runner.record.update(_dir="incomplete", task="task1", passed=True, status="completed")
        good = {"_dir": "good", "arm": "A26", "task": "task1", "status": "completed", "passed": True,
                "cost": {"scenarios": {"billed": {"total_usd": 1.0}}}}
        with mock.patch.object(aggregate, "hash_warnings", return_value=[]):
            text = aggregate.render([runner.record, good], "billed")
        self.assertIn("| 1/2 | - |", text)
        self.assertIn("accounting incomplete", text)

    def test_unpriced_wrong_model_is_validated_before_pricing_and_blocks_acceptance(self):
        runner = self.runner()
        self.log(runner.codex_home, model="gpt-6.1-astra")
        result = {"session_id": THREAD, "usage": {}, "turns_completed": 1,
                  "is_error": False, "_exit_code": 0, "result": "done"}
        with mock.patch.object(runner, "invoke", return_value=result), mock.patch.object(run.acceptance, "run_pack") as pack:
            runner.run("task")
        self.assertNotEqual(runner.record["status"], "completed")
        self.assertIn("no price", runner.record["analysis_error"])
        self.assertFalse(runner.record["validation"]["ok"])
        self.assertIn("model", [v["kind"] for v in runner.record["validation"]["violations"]])
        pack.assert_not_called()

    def test_failed_resume_retains_last_available_cumulative_usage(self):
        good = {"session_id": THREAD, "usage": {"input_tokens": 100, "output_tokens": 10}}
        failed = codex.main_round_result(json.dumps({"type": "thread.started", "thread_id": THREAD}),
                                         "failed", 1, self.root / "absent")
        self.assertIsNone(failed["usage"])
        rec = {"rounds": [{"codex_cli": good}, {"codex_cli": failed}]}
        reconciled = analysis.exec_usage_reconciliation(rec, ())
        self.assertEqual(reconciled["exec_usage"]["input_tokens"], 100)
        self.assertEqual(reconciled["diff"]["output_tokens"], 10)
        failed.update(usage={key: 0 for key in codex.USAGE_KEYS}, turns_completed=0)
        self.assertEqual(analysis.exec_usage_reconciliation(rec, ())["exec_usage"]["input_tokens"], 100)

    def test_incomplete_stream_and_missing_empty_unreadable_output_are_errors(self):
        started = json.dumps({"type": "thread.started", "thread_id": THREAD})
        completed = started + "\n" + json.dumps({"type": "turn.completed", "usage": {}})
        last = self.root / "last"
        for stream, value in ((started, "done"), (completed, None), (completed, " \n")):
            with self.subTest(stream=stream, value=value):
                last.unlink(missing_ok=True)
                if value is not None:
                    last.write_text(value)
                result = codex.main_round_result(stream, "", 0, last)
                self.assertTrue(result["is_error"])
                self.assertTrue(result["result"].strip())
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertTrue(codex.main_round_result(completed, "", 0, last)["is_error"])

    def test_web_search_response_item_is_a_forbidden_main_tool(self):
        home = self.root / "home"
        self.log(home, extra=[{"type": "response_item", "payload": {
            "type": "web_search_call", "id": "ws_1", "status": "completed",
            "action": {"type": "search", "query": "answer"}}}])
        summary = codex.summarize(home)
        self.assertEqual(summary["tool_calls"]["web_search"], 1)
        violations = codex.validate_main(summary, codex.read_rollouts(home), "gpt-6.1-sol", "high", [THREAD])
        self.assertIn("web_search", [v["observed"] for v in violations])

    def test_credential_write_and_close_failures_unlink_owned_file(self):
        runner = self.runner("A31")
        for operation in ("write", "close"):
            with self.subTest(operation=operation):
                handle = mock.MagicMock()
                handle.__enter__.return_value = handle
                if operation == "write":
                    handle.write.side_effect = OSError("full")
                else:
                    handle.__exit__.side_effect = OSError("close failed")
                with mock.patch.dict(os.environ, {run.TOKEN_ENV: "ephemeral-secret"}), \
                        mock.patch.object(run.os, "open", return_value=42), \
                        mock.patch.object(run.os, "fdopen", return_value=handle), \
                        mock.patch.object(Path, "unlink") as unlink, \
                        mock.patch.object(runner, "execute") as execute:
                    with self.assertRaises(OSError):
                        runner.invoke_codex("task", None, 1)
                unlink.assert_called_once_with(missing_ok=True)
                execute.assert_not_called()

    def test_credential_creation_collision_does_not_unlink_foreign_file(self):
        runner = self.runner("A31")
        with mock.patch.dict(os.environ, {run.TOKEN_ENV: "ephemeral-secret"}), \
                mock.patch.object(run.os, "open", side_effect=FileExistsError()), \
                mock.patch.object(Path, "unlink") as unlink:
            with self.assertRaises(FileExistsError):
                runner.invoke_codex("task", None, 1)
        unlink.assert_not_called()

    def test_default_namespace_output_path_is_preserved_and_archived(self):
        runner = self.runner(netns=True)
        # Inspect the default /workspace layout without starting namespaces.
        runner.run_dir = Path("/workspace/agent-experiments-runs/review")
        runner.codex_home = runner.run_dir / codex.HOME_DIR
        seen = []

        def execute(cmd, env, *args, **kwargs):
            output = Path(cmd[cmd.index("-o") + 1])
            self.assertTrue(output.is_relative_to(runner.codex_home))
            seen.append(output)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"type": "thread.started", "thread_id": THREAD})
                                               + '\n{"type":"turn.completed","usage":{}}', "")

        with mock.patch.object(runner, "execute", side_effect=execute), \
                mock.patch.object(Path, "read_text", return_value="done"), \
                mock.patch.object(Path, "write_text"), mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(run.shutil, "copy2") as archive:
            runner.invoke_codex("task", None, 1)
        archive.assert_called_once_with(seen[0], runner.run_dir / "codex-main-round1.last.txt")

    def test_finished_missing_transcript_keeps_model_usage_spend(self):
        home = self.call(model_usage={"claude-opus-5-5": {"inputTokens": 1000, "outputTokens": 100}})
        gap = claude_calls.untranscribed(home)
        self.assertAlmostEqual(gap["claude-opus-5-5"]["usd"], 0.006)

    def test_missing_claude_transcript_prevents_clean_completion_with_known_spend(self):
        runner = self.runner("A31")
        runner.claude_dir = self.call(model_usage={"claude-opus-5-5": {"inputTokens": 1000, "outputTokens": 100}})
        self.log(runner.codex_home, model="gpt-6.1-sol")
        # A31's main agent runs at low effort.
        [path] = codex.rollout_files(runner.codex_home)
        path.write_text(path.read_text().replace('"high"', '"low"'))
        result = {"session_id": THREAD, "usage": {"input_tokens": 307452, "cached_input_tokens": 7296,
                                                "output_tokens": 124},
                  "turns_completed": 1, "is_error": False, "_exit_code": 0, "result": "done"}
        with mock.patch.object(runner, "invoke", return_value=result), mock.patch.object(run.acceptance, "run_pack") as pack:
            runner.run("task")
        self.assertEqual(runner.record["status"], "validation_error")
        self.assertAlmostEqual(runner.record["cost"]["scenarios"]["billed"]["by_model"]["claude-opus-5-5"], 0.006)
        pack.assert_not_called()

    def test_interrupted_claude_call_marks_integrated_accounting_incomplete(self):
        runner = self.runner("A31")
        runner.claude_dir = self.call(ended_epoch=None)
        self.claude_log(runner.claude_dir)
        self.log(runner.codex_home)
        result = {"session_id": THREAD, "usage": {}, "turns_completed": 1,
                  "is_error": False, "_exit_code": 0, "result": "done"}
        with mock.patch.object(runner, "invoke", return_value=result), mock.patch.object(run.acceptance, "run_pack") as pack:
            runner.run("task")
        self.assertEqual(runner.record["status"], "claude_error")
        self.assertTrue(runner.record["accounting_incomplete"])
        self.assertIsNone(runner.record["totals"]["billed_usd"])
        self.assertGreater(runner.record["cost"]["billed_transcript_usd"], 0)
        pack.assert_not_called()

    def test_short_haiku_helper_gap_uses_provable_short_tier(self):
        home = self.call(model_usage={"claude-haiku-5-5": {"inputTokens": 1000, "outputTokens": 100}})
        gap = claude_calls.untranscribed(home)
        self.assertAlmostEqual(gap["claude-haiku-5-5"]["usd"], 0.00015)

    def test_untranscribed_cache_creation_uses_subagent_5m_policy(self):
        home = self.call(model_usage={"claude-opus-5-5": {
            "inputTokens": 100, "outputTokens": 100, "cacheCreationInputTokens": 1000}})
        self.claude_log(home)
        self.assertAlmostEqual(claude_calls.untranscribed(home)["claude-opus-5-5"]["usd"], 0.005)

    def test_unfinished_call_is_claude_error_and_spend_is_incomplete(self):
        home = self.call(ended_epoch=None)
        self.assertEqual(claude_calls.run_outcome(claude_calls.summarize(home))[0], "claude_error")
        self.assertTrue(claude_calls.accounting_issues(home))

    def test_ambiguous_haiku_gap_is_incomplete(self):
        home = self.call(model_usage={"claude-haiku-5-5": {"inputTokens": 150000, "outputTokens": 100}})
        self.assertTrue(claude_calls.accounting_issues(home))
        self.assertEqual(claude_calls.untranscribed(home)["claude-haiku-5-5"]["usd"], 0)

    def test_authoritative_haiku_attribution_prices_ambiguous_tiers(self):
        home = self.call(model_usage={"claude-haiku-5-5": {
            "inputTokens": 150000, "outputTokens": 100, "costUSD": 0.025}})
        self.assertFalse(claude_calls.accounting_issues(home))
        self.assertEqual(claude_calls.untranscribed(home)["claude-haiku-5-5"]["usd"], 0.025)

    def test_legacy_orphan_transcript_is_recovered_once_with_incomplete_spend(self):
        home = self.call(session_id=None, ended_epoch=None)
        self.claude_log(home, session="orphan")
        [request] = claude_calls.load_requests(home)
        self.assertEqual(request.input_tokens, 100)
        self.assertTrue(claude_calls.accounting_issues(home))
        self.call(session_id="orphan", ended_epoch=None)
        self.assertEqual(len(claude_calls.load_requests(home)), 1)

    def test_scan_failure_marks_previous_cost_incomplete(self):
        runner = self.runner()
        runner.record.update(cost={"scenarios": {"billed": {"total_usd": 1.0}}}, totals={"billed_usd": 1.0})
        with mock.patch.object(runner, "scan_leaks", side_effect=OSError("unreadable log")):
            runner.analyze(1)
        self.assertTrue(runner.record["accounting_incomplete"])
        self.assertIsNone(runner.record["totals"]["billed_usd"])
        self.assertIsNone(aggregate.scenario_cost(runner.record, "billed"))


if __name__ == "__main__":
    unittest.main()
