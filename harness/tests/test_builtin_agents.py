"""Offline fixtures for Codex V2's thread-linked child rollouts."""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import analysis
import arms
import codex
import cost
import run
from test_codex import THREAD, rollout_lines

CHILD = "11111111-2222-3333-4444-555555555555"
MODELS = {name: "gpt-6-luna" for name in arms.TIERED_AGENTS}


def child_lines(thread=CHILD, parent=THREAD, role="tester", model="gpt-6-luna", effort="high"):
    lines = copy.deepcopy(rollout_lines(model, effort))
    lines[0]["payload"] = {
        "id": thread, "parent_thread_id": parent, "session_id": "shared-root-session",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": parent, "depth": 1, "agent_role": role, "agent_path": "/root/tests"}}}}
    lines.append({"type": "event_msg", "payload": {"type": "task_complete"}})
    return lines


class BuiltinAgentsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex-home"
        self.write_log(THREAD, rollout_lines(effort="low"))
        self.write_log(CHILD, child_lines())

    def write_log(self, thread, lines, suffix=""):
        path = self.home / "sessions" / f"rollout-{thread}{suffix}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
        return path

    def analyze(self):
        record = {"arm": "A30", "session_ids": [THREAD], "rounds": [
            {"round": 1, "started_epoch": 1.0, "wall_time_s": 5.0, "codex_cli": {
                "session_id": THREAD, "turns_completed": 1, "usage": {
                    "input_tokens": 307452, "cached_input_tokens": 7296,
                    "output_tokens": 124, "reasoning_output_tokens": 6}}}]}
        analysis.update_record(record, [], [], [], Path("/missing"), 0, {}, self.home)
        return record

    def test_main_and_children_costed_by_thread_and_model(self):
        record = self.analyze()
        self.assertIsNone(record["analysis_error"])
        self.assertTrue(record["validation"]["ok"], record["validation"])
        billed = record["cost"]["scenarios"]["billed"]
        self.assertEqual(set(billed["by_model"]), {"gpt-6.1-sol", "gpt-6-luna"})
        self.assertGreater(billed["by_source"]["subagent"], 0)
        self.assertAlmostEqual(billed["by_source"]["subagent"], billed["by_model"]["gpt-6-luna"], places=6)
        self.assertAlmostEqual(sum(billed["by_source"].values()), billed["total_usd"], places=5)
        behavior = record["behavior"]
        self.assertEqual((behavior["subagent_spawns"], behavior["subagent_spawns_by_type"]), (1, {"tester": 1}))
        self.assertEqual(record["totals"]["turns"], 2)
        self.assertEqual((behavior["max_context_main"], behavior["max_context_subagent"]), (300000, 300000))
        self.assertEqual(record["rounds"][0]["delegation"]["spawns"], 1)
        self.assertEqual(record["cost"]["exec_usage_reconciliation"]["diff"],
                         dict.fromkeys(codex.USAGE_KEYS[:3], 0))
        self.assertFalse(record["accounting_incomplete"])
        main = codex.load_requests(self.home, "main", root_threads=[THREAD])
        children = codex.load_requests(self.home, root_threads=[THREAD])
        self.assertEqual(len(main), 2)
        self.assertEqual(len(children), 2)  # response ids can coincide across threads
        self.assertTrue(all(r.source == "subagent" and r.main_index is None for r in children))
        self.assertAlmostEqual(cost.compute_cost(main + children).scenarios["billed"].total_usd,
                               billed["total_usd"], places=6)

    def test_descendant_chain_duplicates_orphans_and_cycles(self):
        self.write_log("grandchild", child_lines("grandchild", CHILD, "small-implementer"))
        self.write_log(CHILD, child_lines(), "-duplicate")
        self.write_log("orphan", child_lines("orphan", "missing"))
        self.write_log("cycle-a", child_lines("cycle-a", "cycle-b"))
        self.write_log("cycle-b", child_lines("cycle-b", "cycle-a"))
        main, children, unexpected = codex.split_threads(codex.read_rollouts(self.home), [THREAD])
        self.assertEqual([r.thread for r in main], [THREAD])
        self.assertEqual({r.thread for r in children}, {CHILD, "grandchild"})
        self.assertEqual({r.thread for r in unexpected}, {"orphan", "cycle-a", "cycle-b"})
        self.assertEqual(len(codex.load_requests(self.home, root_threads=[THREAD])), 4)
        summary = codex.summarize_children(self.home, children)
        self.assertEqual(summary["calls_by_agent"], {"small-implementer": 1, "tester": 1})
        record = self.analyze()
        self.assertFalse(record["validation"]["ok"])
        self.assertIn("codex_unexpected_session", [v["kind"] for v in record["validation"]["violations"]])
        self.assertTrue(record["accounting_incomplete"])

    def test_spawn_metadata_parent_fallback_and_top_level_role(self):
        lines = child_lines()
        del lines[0]["payload"]["parent_thread_id"]
        lines[0]["payload"]["agent_role"] = "implementer"
        del lines[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_role"]
        path = self.write_log(CHILD, lines)
        child = codex.read_rollout(path)
        self.assertEqual((child.parent_thread, child.role), (THREAD, "implementer"))

    def test_child_role_model_and_each_turn_effort_are_validated(self):
        for role, model, effort, expected in (
            ("tester", "gpt-6.1-sol", "low", {"model", "effort"}),
            ("unconfigured", "gpt-6-luna", "high", {"codex_unknown_role", "model"}),
            (None, "gpt-6-luna", "high", {"codex_unknown_role", "model"}),
            ("implementer", "unknown-model", "high", {"model"}),
        ):
            with self.subTest(role=role, model=model, effort=effort):
                self.write_log(CHILD, child_lines(role=role, model=model, effort=effort))
                record = self.analyze()
                self.assertEqual({v["kind"] for v in record["validation"]["violations"]}, expected)
                self.assertEqual({v["source"] for v in record["validation"]["violations"]}, {"subagent"})
        lines = child_lines()
        lines.insert(-1, {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "low"}})
        self.write_log(CHILD, lines)
        self.assertEqual([v["kind"] for v in self.analyze()["validation"]["violations"]], ["effort"])

    def test_only_builtin_mains_can_use_collaboration_tools(self):
        lines = rollout_lines(effort="low")
        lines.append({"type": "response_item", "payload": {
            "type": "function_call", "name": "collaboration.spawn_agent"}})
        self.write_log(THREAD, lines)
        self.assertTrue(self.analyze()["validation"]["ok"])
        summary = codex.summarize(self.home)
        self.assertIn("forbidden_tool", [v["kind"] for v in codex.validate_main(
            summary, codex.read_rollouts(self.home), "gpt-6.1-sol", "low", [THREAD])])
        lines[-1]["payload"]["name"] = "web_search"
        self.write_log(THREAD, lines)
        self.assertIn("forbidden_tool", [v["kind"] for v in self.analyze()["validation"]["violations"]])

    def test_children_may_report_with_send_message_but_not_spawn(self):
        lines = child_lines()
        for name in ("send_message", "list_agents", "wait_agent"):
            lines.insert(-1, {"type": "response_item", "payload": {
                "type": "function_call", "name": "collaboration." + name}})
        self.write_log(CHILD, lines)
        self.assertTrue(self.analyze()["validation"]["ok"])
        lines[-2]["payload"]["name"] = "collaboration.spawn_agent"
        self.write_log(CHILD, lines)
        self.assertEqual([v["kind"] for v in self.analyze()["validation"]["violations"]], ["forbidden_tool"])

    def test_unicode_line_separators_inside_strings_do_not_split_records(self):
        lines = child_lines()
        lines.insert(-1, {"type": "event_msg", "payload": {"type": "item_completed",
                                                         "text": "a\u2028b\u2029c\x0cd"}})
        self.write_log(CHILD, lines)
        self.assertTrue(self.analyze()["validation"]["ok"])

    def write_aborted_child(self, reason, stamp):
        self.write_log(THREAD, rollout_lines(effort="low") + [{
            "timestamp": "2026-10-19T13:50:00.000Z", "type": "event_msg",
            "payload": {"type": "task_complete"}}])
        return self.write_log(CHILD, child_lines()[:-1] + [{
            "timestamp": stamp, "type": "event_msg",
            "payload": {"type": "turn_aborted", "reason": reason}}])

    def test_child_interrupted_at_or_after_root_completion_is_cancelled(self):
        for stamp in ("2026-10-19T13:50:00.000Z", "2026-10-19T13:50:00.022Z"):
            with self.subTest(stamp=stamp):
                self.write_aborted_child("interrupted", stamp)
                record = self.analyze()
                summary = record["behavior"]["codex_subagents"]
                self.assertIsNone(codex.run_outcome(summary))
                self.assertTrue(record["validation"]["ok"], record["validation"])
                call, = summary["per_call"]
                self.assertTrue(call["cancelled"])
                self.assertFalse(call["failed"] or call["unfinished"] or call["errors"])

    def test_child_interrupted_before_root_completion_fails(self):
        self.write_aborted_child("interrupted", "2026-10-19T13:49:59.999Z")
        record = self.analyze()
        summary = record["behavior"]["codex_subagents"]
        self.assertEqual(codex.run_outcome(summary)[0], "codex_error")
        self.assertEqual({v["kind"] for v in record["validation"]["violations"]},
                         {"codex_call_failed", "codex_call_unfinished"})

    def test_other_child_abort_reasons_fail_after_root_completion(self):
        for reason in ("replaced", "shutdown", None):
            with self.subTest(reason=reason):
                self.write_aborted_child(reason, "2026-10-19T13:50:00.022Z")
                record = self.analyze()
                summary = record["behavior"]["codex_subagents"]
                self.assertEqual(codex.run_outcome(summary)[0], "codex_error")
                self.assertEqual({v["kind"] for v in record["validation"]["violations"]},
                                 {"codex_call_failed", "codex_call_unfinished"})

    def test_cancellation_uses_ancestor_root_completion(self):
        self.write_aborted_child("interrupted", "2026-10-19T13:50:00.022Z")
        lines = child_lines("grandchild", CHILD)[:-1] + [{
            "timestamp": "2026-10-19T13:50:00.023Z", "type": "event_msg",
            "payload": {"type": "turn_aborted", "reason": "interrupted"}}]
        self.write_log("grandchild", lines)
        self.write_log("other-root", [{"type": "session_meta", "payload": {"id": "other-root"}}, {
            "timestamp": "2026-10-19T13:51:00Z", "type": "event_msg", "payload": {"type": "task_complete"}}])
        logs = codex.read_rollouts(self.home)
        _, children, _ = codex.split_threads(logs, [THREAD, "other-root"])
        self.assertEqual({child.thread for child in children}, {CHILD, "grandchild"})
        self.assertTrue(all(child.cancelled for child in children))
        self.assertFalse(codex.validate_children(children, MODELS, "high"))
        self.assertIsNone(codex.run_outcome(codex.summarize_children(self.home, children)))
        self.assertFalse(any(log.cancelled for log in logs))

    def test_cancellation_requires_completion_and_abort_timestamps(self):
        for ending in (
            [],
            [{"type": "event_msg", "payload": {"type": "task_complete"}}],
            [{"timestamp": "2026-10-19T13:50:00Z", "type": "event_msg",
              "payload": {"type": "task_complete"}},
             {"type": "event_msg", "payload": {"type": "task_started"}}],
        ):
            with self.subTest(ending=ending):
                self.write_aborted_child("interrupted", "2026-10-19T13:50:00.022Z")
                self.write_log(THREAD, rollout_lines(effort="low") + ending)
                record = self.analyze()
                self.assertEqual(codex.run_outcome(record["behavior"]["codex_subagents"])[0], "codex_error")
                self.assertFalse(record["validation"]["ok"])
        path = self.write_aborted_child("interrupted", "2026-10-19T13:50:00.022Z")
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        del lines[-1]["timestamp"]
        self.write_log(CHILD, lines)
        self.assertFalse(self.analyze()["validation"]["ok"])

    def test_cancellation_preserves_errors_usage_limits_and_truncation(self):
        cases = (
            ([{"type": "event_msg", "payload": {"type": "error", "message": "child failed"}}], "", "codex_error"),
            ([{"type": "event_msg", "payload": {
                "type": "error", "message": "You've hit your usage limit."}}], "", "usage_limit"),
            ([{"timestamp": "2026-10-19T13:50:00.001Z", "type": "event_msg", "payload": {
                "type": "turn_aborted", "reason": "interrupted", "codex_error_info": "usage_limit_reached"}}],
             "", "usage_limit"),
            ([], '{"type":', "codex_error"),
        )
        for errors, tail, status in cases:
            with self.subTest(errors=errors, tail=tail):
                path = self.write_aborted_child("interrupted", "2026-10-19T13:50:00.022Z")
                lines = [json.loads(line) for line in path.read_text().splitlines()]
                self.write_log(CHILD, lines[:-1] + errors + lines[-1:])
                with path.open("a") as log:
                    log.write(tail)
                record = self.analyze()
                self.assertEqual(codex.run_outcome(record["behavior"]["codex_subagents"])[0], status)
                self.assertFalse(record["validation"]["ok"])

    def test_child_started_after_cancellation_is_unfinished(self):
        path = self.write_aborted_child("interrupted", "2026-10-19T13:50:00.022Z")
        with path.open("a") as log:
            log.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        record = self.analyze()
        self.assertEqual(codex.run_outcome(record["behavior"]["codex_subagents"])[0], "codex_error")
        self.assertEqual({v["kind"] for v in record["validation"]["violations"]}, {"codex_call_unfinished"})

    def test_child_errors_and_incomplete_rollouts_stop_before_acceptance(self):
        cfg = run.RunConfig("A30", "t1", 1, None, self.root / "template", self.root, self.root / "arms",
                            self.root / "tasks", self.root / "missing", 1, 60.0, 60.0, 0, "unused", "t1", False)
        pack = cfg.tasks_dir / "t1" / "acceptance"
        pack.mkdir(parents=True)
        cases = [
            (child_lines()[:-1], "codex_error"),
            (child_lines() + [{"type": "event_msg", "payload": {"type": "task_started"}}], "codex_error"),
            (child_lines() + [{"type": "turn_context", "payload": {
                "model": "gpt-6-luna", "effort": "high"}}], "codex_error"),
            (child_lines() + [{"type": "event_msg", "payload": {"type": "error", "message": "child failed"}}], "codex_error"),
            (child_lines() + [{"type": "event_msg", "payload": {
                "type": "error", "message": "You've hit your usage limit."}}], "usage_limit"),
            (child_lines() + [{"type": "event_msg", "payload": {
                "type": "error", "message": "Request refused", "codex_error_info": "usage_limit_reached"}}],
             "usage_limit"),
        ]
        for lines, status in cases:
            with self.subTest(status=status, ending=lines[-1]):
                self.write_log(CHILD, lines)
                runner = run.Runner(cfg, "fixture", self.root)
                runner.record = {"arm": "A30", "rounds": [], "session_ids": runner.session_ids, "passed": False}
                result = {"session_id": THREAD, "usage": {}, "turns_completed": 1,
                          "is_error": False, "_exit_code": 0, "result": "done"}
                with mock.patch.object(runner, "invoke", return_value=result), \
                        mock.patch.object(run.acceptance, "run_pack") as acceptance:
                    runner.run("task")
                self.assertEqual(runner.record["status"], status, runner.record.get("error"))
                self.assertIn(CHILD, runner.record["error"])
                acceptance.assert_not_called()

    def test_truncated_child_is_unfinished_and_accounting_incomplete(self):
        path = self.write_log(CHILD, child_lines())
        path.write_text(path.read_text() + '{"type":')
        record = self.analyze()
        self.assertTrue(record["accounting_incomplete"])
        self.assertEqual(codex.run_outcome(record["behavior"]["codex_subagents"])[0], "codex_error")

    def test_resume_reconciliation_uses_cumulative_main_only(self):
        record = self.analyze()
        root_requests = codex.load_requests(self.home, "main", root_threads=[THREAD])
        record["rounds"].insert(0, {"codex_cli": {"session_id": THREAD, "turns_completed": 1,
                                                   "usage": {"input_tokens": 7452, "output_tokens": 110}}})
        self.assertEqual(analysis.exec_usage_reconciliation(record, root_requests)["diff"],
                         dict.fromkeys(codex.USAGE_KEYS[:3], 0))
        runner = run.Runner(run.RunConfig("A30", "t1", 1, None, self.root, self.root, self.root,
                                         self.root, self.root, 1, 60.0, 60.0, 0, "unused", "t1", False),
                            "fixture", self.root)
        runner.session_ids.append(THREAD)
        self.assertEqual(runner.main_request_count(), 2)

    def test_every_arm_hides_secrets_without_rebinding_them(self):
        secret_dir = Path.home() / ".secrets"
        for arm in arms.ARMS.values():
            visible = run.VISIBLE_RUN_DIRS + ((codex.HOME_DIR,) if arm.uses_codex else ())
            steps = run.hiding_steps(self.root, visible, codex.HOST_VISIBLE if arm.uses_codex else ())
            hide = f"{{ [ ! -d {secret_dir} ] || mount -t tmpfs tmpfs {secret_dir}; }}"
            self.assertIn(hide, steps)
            self.assertFalse(any(str(secret_dir) in step and "--bind" in step for step in steps))
        with mock.patch.dict(os.environ, {run.TOKEN_ENV: "synthetic-credential"}):
            self.assertEqual(run.child_env(self.root)[run.TOKEN_ENV], "synthetic-credential")
            env = run.codex_main_env(self.home, credentials_file=run.CREDENTIALS_FILE)
            self.assertNotIn(run.TOKEN_ENV, env)
            script = run.wrap_netns(["true"], hide_run_dir=self.root, credentials=True)[-1]
            self.assertIn(str(secret_dir), script)
            self.assertIn(run.CREDENTIALS_SOURCE_ENV, script)

    def test_namespace_hides_synthetic_secrets_after_harness_read(self):
        if not shutil.which("unshare"):
            self.skipTest("unshare is unavailable")
        probe = subprocess.run(["unshare", "-Urnm", "true"], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest("user/mount/network namespaces unavailable: " + probe.stderr.strip())
        secrets_dir = self.root / "owner" / ".secrets"
        secrets_dir.mkdir(parents=True)
        token = secrets_dir / "synthetic-token"
        token.write_text("fixture-value")
        self.assertEqual(token.read_text(), "fixture-value")  # harness-side credential load
        run_dir = self.root / "run"
        for name in run.VISIBLE_RUN_DIRS:
            (run_dir / name).mkdir(parents=True)
        command = ["python3", "-c", (
            "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
            "assert p.is_dir(); assert not list(p.iterdir()); assert not (p/'synthetic-token').exists()"
        ), str(secrets_dir)]
        with mock.patch.object(run, "HIDDEN_PATHS", ((str(secrets_dir), "synthetic owner secrets"),)), \
                mock.patch.object(run, "PASTA_PREFIX", ("unshare", "-Urn", "--")):
            wrapped = run.wrap_netns(command, hide_run_dir=run_dir)
        done = subprocess.run(wrapped, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(token.read_text(), "fixture-value")


class BuiltinHomeCheckTest(unittest.TestCase):
    def build_home(self, arm, *, wrong_feature=None, extra_prompt=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = arms.generate_arm(arm, root / "arms") / "codex"
        home, repo = root / "home", root / "repo"
        home.mkdir()
        repo.mkdir()
        config = tomllib.loads((source / "config.toml").read_text())
        states = {name: value.get("enabled", True) if isinstance(value, dict) else value
                  for name, value in config["features"].items()}
        if wrong_feature:
            states[wrong_feature] = not states[wrong_feature]

        def capture(command, cwd, env):
            if command[1:] == ["features", "list"]:
                return "\n".join(f"{key} stable {str(on).lower()}" for key, on in states.items())
            text = (source / "AGENTS.md").read_text() + f"<cwd>{repo}</cwd>" + extra_prompt
            return json.dumps([{"role": "developer", "content": [{"text": text}]}])

        with mock.patch.object(codex, "_capture", side_effect=capture), \
                mock.patch.object(codex, "check_strict_config") as strict:
            codex._build_home(home, source, root, repo, {}, "/fake-codex", codex.PROMPT_INPUT_FILE)
            strict.assert_called_once()
        return home

    def test_nested_feature_settings_and_builtin_role_copy(self):
        home = self.build_home("A30", extra_prompt="<multi_agent_role><multi_agent_mode>")
        for name in arms.TIERED_AGENTS:
            role = home / "roles" / f"{name}.toml"
            self.assertTrue(role.is_file())
        for arm in ("A31", "A32"):
            self.build_home(arm)

    def test_wrong_nested_feature_states_are_rejected(self):
        for arm, feature in (("A30", "multi_agent_v2"), ("A31", "code_mode"), ("A32", "code_mode")):
            with self.subTest(arm=arm), self.assertRaises(codex.CodexError):
                self.build_home(arm, wrong_feature=feature)

    def test_collaboration_markers_allowed_only_for_builtin_arms(self):
        with self.assertRaises(codex.CodexError):
            self.build_home("A26", extra_prompt="<multi_agent_role>")
        with self.assertRaises(codex.CodexError):
            self.build_home("A30", extra_prompt="<plugins_instructions>")


if __name__ == "__main__":
    unittest.main()
