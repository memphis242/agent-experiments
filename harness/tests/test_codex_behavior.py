"""Offline regression coverage through the archived-run reanalysis entry point."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import arms
import claude_calls
import reanalyze
from test_builtin_agents import child_lines
from test_codex import THREAD, rollout_lines


class CodexBehaviorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_jsonl(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def make_run(self, arm_id):
        directory = self.root / arm_id
        directory.mkdir()
        arm = arms.get_arm(arm_id)
        record = {"arm": arm_id, "main_harness": "codex", "status": "completed",
                  "session_ids": [THREAD], "rounds": [
                      {"round": 1, "started_epoch": 1.0, "wall_time_s": 5.0}]}
        (directory / "run.json").write_text(json.dumps(record))
        self.write_jsonl(directory / "codex-home" / "sessions" / "rollout-main.jsonl",
                         rollout_lines(arm.main_model, arm.main_effort))
        return directory

    def reanalyze(self, directory):
        with mock.patch.object(reanalyze.run, "harness_git", return_value=("fixture", False)):
            reanalyze.reanalyze_run(directory, self.root / "missing-gaps")
        record = json.loads((directory / "run.json").read_text())
        self.assertIsNone(record["analysis_error"])
        self.assertEqual(record["status"], "completed")
        return record["behavior"]

    def add_claude_call(self, directory, call_id, agent, model, entries=None):
        claude_dir = directory / "config" / "claude"
        path = claude_dir / "agent-calls" / call_id / "call.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"agent": agent, "session_id": call_id,
                                    "model": "configured-model-is-not-evidence",
                                    "started_epoch": 2.0, "ended_epoch": 3.0}))
        if entries is None:
            entry = {"type": "assistant", "effort": "high", "message": {
                "id": call_id, "model": model, "usage": {
                    "input_tokens": 100, "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 300, "output_tokens": 10},
                "content": [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {}}]}}
            entries = [entry, entry]
        self.write_jsonl(claude_dir / "projects" / "archived" / f"{call_id}.jsonl", entries)
        return claude_dir

    def test_main_tools_for_every_codex_main_arm(self):
        for number in range(26, 33):
            with self.subTest(arm=number):
                behavior = self.reanalyze(self.make_run(f"A{number}"))
                self.assertEqual(behavior["tool_calls_main"], {"exec": 1})
                self.assertEqual(behavior["tool_calls_subagent"], {})
                self.assertEqual(behavior["tool_calls_total"], 1)
                self.assertEqual(behavior["subagent_spawns_by_observed_model"], {})
                self.assertIsNone(behavior["max_context_subagent"])

    def test_builtin_children_tools_models_and_context_exclude_unrelated_threads(self):
        directory = self.make_run("A30")
        main = rollout_lines(effort="low")
        for tool in ("spawn_agent", "wait_agent"):
            main.append({"type": "response_item", "payload": {"type": "function_call", "name": tool}})
        self.write_jsonl(directory / "codex-home" / "sessions" / "rollout-main.jsonl", main)
        children = child_lines()
        children.insert(-1, {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "high"}})
        children.insert(-1, {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "high"}})
        self.write_jsonl(directory / "codex-home" / "sessions" / "rollout-child.jsonl", children)
        self.write_jsonl(directory / "codex-home" / "sessions" / "rollout-orphan.jsonl",
                         child_lines("orphan", "missing"))
        behavior = self.reanalyze(directory)
        self.assertEqual(behavior["tool_calls_main"], {"exec": 1, "spawn_agent": 1, "wait_agent": 1})
        self.assertEqual(behavior["tool_calls_subagent"], {"exec": 1})
        self.assertEqual(behavior["tool_calls_total"], 4)
        self.assertEqual(behavior["subagent_spawns"], 1)
        self.assertEqual(behavior["subagent_spawn_calls"][0]["observed_models"],
                         ["gpt-6-luna", "gpt-6.1-sol"])
        self.assertEqual(behavior["subagent_spawns_by_observed_model"], {"gpt-6-luna": 1, "gpt-6.1-sol": 1})
        self.assertEqual(behavior["max_context_subagent"], 300000)

    def test_claude_call_tools_models_and_context_for_both_arms(self):
        for arm_id in ("A31", "A32"):
            with self.subTest(arm=arm_id):
                directory = self.make_run(arm_id)
                arm = arms.get_arm(arm_id)
                models = [arm.model_for(agent) for agent in ("implementer", "tester")]
                for index, (agent, model) in enumerate(zip(("implementer", "tester"), models)):
                    self.add_claude_call(directory, f"call-{index}", agent, model)
                behavior = self.reanalyze(directory)
                self.assertEqual(behavior["tool_calls_main"], {"exec": 1})
                self.assertEqual(behavior["tool_calls_subagent"], {"Bash": 2})
                self.assertEqual(behavior["tool_calls_total"], 3)
                self.assertEqual([call["observed_models"] for call in behavior["subagent_spawn_calls"]],
                                 [[model] for model in models])
                self.assertEqual(behavior["subagent_spawns_by_observed_model"],
                                 {model: models.count(model) for model in models})
                self.assertEqual(behavior["max_context_subagent"], 600)

    def test_usage_free_models_synthetic_messages_and_missing_transcripts(self):
        directory = self.make_run("A32")
        entries = [{"type": "assistant", "message": {"id": str(index), "model": model}}
                   for index, model in enumerate(("claude-opus-5-5", "claude-haiku-5-5",
                                                  "claude-haiku-5-5", "<synthetic>"))]
        claude_dir = self.add_claude_call(directory, "observed", "tester", None, entries)
        self.add_claude_call(directory, "missing", "tester", None, [])
        (claude_dir / "projects" / "archived" / "missing.jsonl").unlink()
        summary = claude_calls.summarize(claude_dir)
        observed = {call["call_id"]: call["observed_models"] for call in summary["per_call"]}
        self.assertEqual(observed, {"observed": ["claude-haiku-5-5", "claude-opus-5-5"], "missing": []})
        behavior = self.reanalyze(directory)
        self.assertEqual(behavior["subagent_spawns_by_observed_model"],
                         {"(no transcript)": 1, "claude-haiku-5-5": 1, "claude-opus-5-5": 1})


if __name__ == "__main__":
    unittest.main()
