import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import analysis
import arms
import claude_calls
import codex
import cost
import run
from test_codex import THREAD, make_world, rollout_lines

SECRET = "tok-secret-value"
# A fake `claude -p`: records how it was started, writes the session transcript Claude Code would,
# and prints the --output-format json result. The task LIMIT makes it fail as at a usage limit.
FAKE_CLAUDE = textwrap.dedent(r"""
    #!/usr/bin/env python3
    import json, os, re, sys, uuid
    from pathlib import Path
    if sys.argv[1:] == ["--version"]:
        print("2.1.fake (Claude Code)")
        sys.exit(0)
    task = sys.stdin.read()
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    with (cfg / "fake-claude.jsonl").open("a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "task": task, "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")
    if "LIMIT" in task:
        print(json.dumps({"type": "result", "is_error": True, "session_id": "s0", "result": "Claude AI usage limit reached|1792000000"}))
        sys.exit(1)
    model = sys.argv[sys.argv.index("--model") + 1]
    sid = str(uuid.uuid4())
    project = cfg / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    project.mkdir(parents=True, exist_ok=True)
    usage = {"input_tokens": 150000, "output_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    (project / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-10-17T10:00:00Z", "effort": "high",
        "message": {"id": "msg_" + sid, "model": model, "usage": usage,
                    "content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}}]}}) + "\n")
    print(json.dumps({"type": "result", "session_id": sid, "is_error": False, "result": "report from " + model,
                      "total_cost_usd": 0.1, "num_turns": 2, "duration_ms": 5,
                      "modelUsage": {model: {"inputTokens": 150000, "outputTokens": 300, "cacheReadInputTokens": 0,
                                             "cacheCreationInputTokens": 0}}}))
    """).lstrip()


def install_fake_claude(root: Path) -> Path:
    fake = root / "fake-claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


class ClaudeAgentLauncherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        arms.generate_arm("A31", self.base / "arms")
        self.claude = self.base / "claude"
        run.copy_arm_root(self.base / "arms" / "A31", self.base / "root", claude_files=False)
        self.claude = self.base / "root" / "claude"
        fake = install_fake_claude(self.base)
        (self.claude / "agent-runtime.json").write_text(json.dumps({"claude_bin": str(fake)}))
        (self.base / "login").write_text(SECRET + "\n")
        (self.base / "repo").mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
                    "CLAUDE_AGENT_CREDENTIALS_FILE": str(self.base / "login"), "CODEX_HOME": "/codex-home",
                    "CODEX_API_KEY": "dropped"}

    def launch(self, *args, task="build it", env=None):
        return subprocess.run([sys.executable, str(self.claude / "bin" / "claude-agent"), *args], input=task,
                              text=True, capture_output=True, env=env or self.env, cwd=self.base / "repo", timeout=60)

    def test_command_line_environment_and_call_record(self):
        done = self.launch("implementer")
        self.assertEqual((done.returncode, done.stdout), (0, "report from claude-opus-5-5\n"), done.stderr)
        [call] = claude_calls.call_records(self.claude)
        self.assertEqual((call["agent"], call["model"], call["effort"], call["exit_code"], call["failed"],
                          call["usage_limit"]), ("implementer", "claude-opus-5-5", "high", 0, False, False))
        self.assertTrue(call["session_id"] and call["ended_epoch"] >= call["started_epoch"])
        self.assertEqual(call["model_usage"]["claude-opus-5-5"]["outputTokens"], 300)
        self.assertNotIn(SECRET, json.dumps(call))
        seen = json.loads((self.claude / "fake-claude.jsonl").read_text())
        argv = seen["argv"]
        self.assertEqual((seen["task"], seen["cwd"]), ("build it", str((self.base / "repo").resolve())))
        self.assertEqual(argv[:3], ["-p", "--model", "claude-opus-5-5"])
        for flag, value in (("--permission-mode", "bypassPermissions"), ("--output-format", "json"),
                            ("--disallowedTools", "Agent"), ("--tools", "Read,Edit,Write,Grep,Glob,Bash"),
                            ("--append-system-prompt", arms._split_agent_template("implementer")[1])):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"], SECRET)
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"], str(self.claude))
        self.assertEqual(seen["env"]["CODEX_AGENT_DEPTH"], "1")
        self.assertFalse([k for k in seen["env"] if k.startswith("CODEX")] != ["CODEX_AGENT_DEPTH"])
        # The call is a Claude Code session whose transcript is priced as subagent requests: the Haiku
        # and Opus cards apply per request, and a transcript is found through the call record.
        [request] = claude_calls.load_requests(self.claude)
        self.assertEqual((request.source, request.model, request.input_tokens), ("subagent", "claude-opus-5-5", 150000))
        self.assertAlmostEqual(cost.request_cost(request, "billed"), (150000 * 4.00 + 100 * 20.00) / 1e6)

    def test_small_agent_runs_on_haiku_and_is_priced_at_the_long_prompt_card(self):
        self.assertEqual(self.launch("tester").returncode, 0)
        [request] = claude_calls.load_requests(self.claude)
        self.assertEqual(request.model, "claude-haiku-5-5")
        self.assertAlmostEqual(cost.request_cost(request, "billed"), (150000 * 0.50 + 100 * 2.50) / 1e6)

    def test_refusals(self):
        self.assertEqual(self.launch("planner").returncode, 2)
        self.assertEqual(self.launch("implementer", task="  \n").returncode, 2)
        self.assertEqual(self.launch("implementer", env={**self.env, "CODEX_AGENT_DEPTH": "1"}).returncode, 2)
        no_login = {k: v for k, v in self.env.items() if k != "CLAUDE_AGENT_CREDENTIALS_FILE"}
        done = self.launch("implementer", env=no_login)
        self.assertEqual(done.returncode, 1)
        self.assertIn("CLAUDE_AGENT_CREDENTIALS_FILE", done.stderr)
        self.assertFalse((self.claude / "fake-claude.jsonl").exists())

    def test_usage_limit_failure(self):
        done = self.launch("tester", task="LIMIT now")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Claude usage limit reached", done.stderr)
        [call] = claude_calls.call_records(self.claude)
        self.assertTrue(call["failed"] and call["usage_limit"])
        summary = claude_calls.summarize(self.claude)
        self.assertEqual(claude_calls.run_outcome(summary)[0], "usage_limit")
        call_json = next((self.claude / "agent-calls").iterdir()) / "call.json"
        call_json.write_text(json.dumps({**call, "usage_limit": False}))
        self.assertEqual(claude_calls.run_outcome(claude_calls.summarize(self.claude))[0], "claude_error")

    def test_validation_flags_wrong_model_effort_and_denied_tools(self):
        self.assertEqual(self.launch("implementer").returncode, 0)
        summary = claude_calls.summarize(self.claude)
        ok = claude_calls.validate(self.claude, summary, {"implementer": "claude-opus-5-5"}, "high", ["Agent"])
        self.assertEqual(ok, [])
        wrong_model = claude_calls.validate(self.claude, summary, {"implementer": "claude-haiku-5-5"}, "high", [])
        self.assertEqual([v["kind"] for v in wrong_model], ["model"])
        bad = claude_calls.validate(self.claude, summary, {"implementer": "claude-opus-5-5"}, "low", ["Bash"])
        self.assertEqual(sorted(v["kind"] for v in bad), ["effort", "forbidden_tool"])
        self.assertEqual({v["source"] for v in bad}, {"subagent"})


class ClaudeAgentRunTest(unittest.TestCase):
    def run_cell(self, prompt):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {run.TOKEN_ENV: SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)
        fake = install_fake_claude(root)
        rc = run.main(make_world(self, root, prompt, arm="A31", claude_bin=fake))
        (run_dir,) = [p for p in (root / "runs").iterdir() if p.is_dir()]
        return rc, run_dir, json.loads((run_dir / "run.json").read_text())

    def test_codex_main_with_claude_subagents(self):
        rc, run_dir, rec = self.run_cell("DELEGATE the work.")
        self.assertEqual(rc, 0)
        self.assertEqual((rec["status"], rec["passed"], rec["rounds_used"]), ("completed", True, 2))
        report = (run_dir / "repo" / "agent-report.txt").read_text()
        self.assertTrue(report.startswith("0|report from claude-opus-5-5\n|"), report)
        main_env = json.loads((codex.home_for(run_dir) / "fake-invocations.jsonl").read_text().splitlines()[0])["env"]
        self.assertEqual(main_env["PATH"].split(":")[0], str(run_dir / "config" / "claude" / "bin"))
        self.assertNotIn(run.TOKEN_ENV, main_env)  # the login is not in the Codex main agent's environment
        self.assertNotIn(SECRET, json.dumps(main_env))
        login_file = main_env[run.CREDENTIALS_FILE_ENV]
        self.assertFalse(Path(login_file).exists())  # removed when the round ended
        self.assertNotIn(SECRET, (run_dir / "run.json").read_text())
        claude_dir = run_dir / "config" / "claude"
        self.assertEqual(json.loads((claude_dir / "agent-runtime.json").read_text())["claude_bin"],
                         rec["claude_agents"]["claude_bin"])
        # Costs: the Codex main is source main, each Claude call a subagent, per model.
        billed = rec["cost"]["scenarios"]["billed"]
        self.assertEqual(set(billed["by_model"]), {"gpt-6.1-sol", "claude-opus-5-5"})
        self.assertGreater(billed["by_source"]["main"], 0)
        # 150000 input and 100 output at Opus prices, plus 200 output tokens the CLI counted beyond the transcript.
        self.assertAlmostEqual(billed["by_model"]["claude-opus-5-5"], (150000 * 4.00 + 300 * 20.00) / 1e6, places=6)
        self.assertAlmostEqual(billed["by_source"]["subagent"], billed["by_model"]["claude-opus-5-5"], places=6)
        self.assertEqual(rec["cost"]["billed_basis"], "codex_rollout+claude_model_usage")
        self.assertEqual(rec["cost"]["untranscribed"]["claude-opus-5-5"]["tokens"]["output"], 200)
        behavior = rec["behavior"]
        self.assertEqual((behavior["subagent_spawns"], behavior["subagent_spawns_by_type"]), (1, {"implementer": 1}))
        self.assertEqual(behavior["max_context_subagent"], 150000)
        self.assertEqual(behavior["claude_agents"]["tool_calls"], {"Bash": 1})
        self.assertEqual(rec["totals"]["turns"], 2)  # the main agent's requests only
        self.assertEqual(sum(r["delegation"]["spawns"] for r in rec["rounds"]), 1)
        self.assertTrue(rec["validation"]["ok"], rec["validation"])
        self.assertIsNone(rec["analysis_error"])

    def test_claude_usage_limit_ends_the_run_as_usage_limit(self):
        rc, run_dir, rec = self.run_cell("DELEGATE LIMIT work.")
        self.assertEqual(rc, run.EXIT_USAGE_LIMIT)
        self.assertEqual(rec["status"], "usage_limit")
        self.assertIn("Claude usage limit", rec["error"])
        self.assertEqual(rec["rounds_used"], 1)  # stopped before acceptance
        self.assertFalse((run_dir / "results").exists())


class NamespaceCredentialsTest(unittest.TestCase):
    def test_login_goes_to_a_private_file_and_not_into_the_agent_environment(self):
        plain = run.wrap_netns(["true"], hide_run_dir=Path("/runs/x"))
        self.assertNotIn(run.CREDENTIALS_SOURCE_ENV, " ".join(plain))
        wrapped = run.wrap_netns(["true"], hide_run_dir=Path("/runs/x"), credentials=True)
        script = wrapped[-1]
        self.assertIn(f'( umask 077 && printf %s "${run.CREDENTIALS_SOURCE_ENV}" > {run.CREDENTIALS_FILE} )', script)
        self.assertIn(f"exec env -u {run.CREDENTIALS_SOURCE_ENV} unshare -U", script)
        self.assertLess(script.index("mount -t tmpfs tmpfs /dev/shm"), script.index(run.CREDENTIALS_FILE))

    def test_codex_main_environment(self):
        base = {"PATH": "/usr/bin", "HOME": "/h", "CLAUDE_CODE_OAUTH_TOKEN": SECRET, "OPENAI_API_KEY": "k"}
        env = run.codex_main_env(Path("/run/codex-home"), base, [Path("/run/config/claude/bin")], "/dev/shm/login")
        self.assertEqual(env, {"PATH": "/run/config/claude/bin:/usr/bin", "HOME": "/h",
                               "CODEX_HOME": "/run/codex-home", "CLAUDE_AGENT_CREDENTIALS_FILE": "/dev/shm/login"})


class SeparatedCodexSessionsTest(unittest.TestCase):
    def write_log(self, home, lines, name):
        path = home / "sessions" / "2026" / "10" / "19" / f"rollout-2026-10-19T08-49-16-{name}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(l) + "\n" for l in lines))

    def test_main_and_subagent_homes_are_costed_and_validated_apart(self):
        sub_thread = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sub = codex.subagent_home_for(home)
            self.write_log(home, rollout_lines(model="gpt-6.1-sol", effort="low"), THREAD)
            lines = [dict(l, payload={**l["payload"], "id": sub_thread}) if l["type"] == "session_meta" else l
                     for l in rollout_lines(model="gpt-6-luna", effort="high")]
            self.write_log(sub, lines, sub_thread)
            call = sub / "agent-calls" / "c1-tester"
            call.mkdir(parents=True)
            (call / "call.json").write_text(json.dumps({
                "agent": "tester", "started_epoch": 100.0, "ended_epoch": 160.0, "exit_code": 0, "failed": False,
                "thread_id": sub_thread, "usage_limit": False}))
            record = {"arm": "A30", "session_ids": [THREAD], "resume_request_indices": [], "rounds": [
                {"round": 1, "started_epoch": 90.0, "wall_time_s": 80.0}]}
            analysis.update_record(record, [], [], [], Path("/missing"), 0, {}, home, sub, None)
            self.assertIsNone(record["analysis_error"])
            billed = record["cost"]["scenarios"]["billed"]
            self.assertEqual(set(billed["by_model"]), {"gpt-6.1-sol", "gpt-6-luna"})
            self.assertGreater(billed["by_source"]["subagent"], 0)
            self.assertAlmostEqual(billed["by_source"]["main"] + billed["by_source"]["subagent"], billed["total_usd"], places=5)
            self.assertAlmostEqual(billed["by_model"]["gpt-6-luna"], billed["by_source"]["subagent"], places=5)
            behavior = record["behavior"]
            self.assertEqual((behavior["subagent_spawns"], behavior["subagent_spawns_by_type"]), (1, {"tester": 1}))
            self.assertEqual(record["totals"]["turns"], 2)  # the main thread's requests only
            self.assertEqual((behavior["max_context_main"], behavior["max_context_subagent"]), (300_000, 300_000))
            self.assertEqual(record["rounds"][0]["delegation"]["spawns"], 1)
            self.assertTrue(record["validation"]["ok"], record["validation"])
            # A subagent session on the main agent's model or effort is flagged.
            [path] = codex.rollout_files(sub)
            path.write_text(path.read_text().replace("gpt-6-luna", "gpt-6.1-sol").replace('"high"', '"low"'))
            analysis.update_record(record, [], [], [], Path("/missing"), 0, {}, home, sub, None)
            self.assertEqual(sorted((v["kind"], v["observed"]) for v in record["validation"]["violations"]),
                             [("effort", "low"), ("model", "gpt-6.1-sol")])


if __name__ == "__main__":
    unittest.main()
