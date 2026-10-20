import json
import os
import subprocess
import sys
import tempfile
import textwrap
import base64
import signal
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import arms
import codex
import cost
import run

THREAD = "01a1546c-748c-73f2-9d3d-f5bc46873fd0"
FAKE_CODEX = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    from pathlib import Path
    argv = sys.argv[1:]
    task = sys.stdin.read()
    home = Path(os.environ["CODEX_HOME"])
    (home / "fake-invocation.json").write_text(json.dumps({"argv": argv, "task": task, "env": dict(os.environ)}))
    if "SLEEP" in task:
        import time; time.sleep(30)
    if "FAIL" in task:
        print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}))
        sys.exit(1)
    print(json.dumps({"type": "thread.started", "thread_id": "%s"}))
    Path(argv[argv.index("-o") + 1]).write_text("REPORT for " + task.strip())
    ''' % THREAD)


SOL_ONLY = {"implementer": "gpt-6.1-sol", "tester": "gpt-6.1-sol"}
TIERED = {"implementer": "gpt-6.1-sol", "tester": "gpt-6-luna"}


def rollout_lines(model="gpt-6.1-sol", effort="high"):
    def usage(inp, cached, out):
        return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                "output_tokens": out, "reasoning_output_tokens": 3, "total_tokens": inp + out}
    return [
        {"timestamp": "2026-10-19T13:49:16.832Z", "type": "session_meta", "payload": {"id": THREAD}},
        {"timestamp": "2026-10-19T13:49:18.823Z", "type": "turn_context", "payload": {"model": model, "effort": effort}},
        {"timestamp": "2026-10-19T13:49:21.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec"}},
        {"timestamp": "2026-10-19T13:49:22.113Z", "type": "token_usage_record",
         "payload": {"response_id": "resp_1", "usage": usage(7452, 0, 110)}},
        {"timestamp": "2026-10-19T13:49:22.114Z", "type": "token_usage_record",
         "payload": {"response_id": "resp_1", "usage": usage(7452, 0, 110)}},  # duplicate record
        {"timestamp": "2026-10-19T13:49:22.129Z", "type": "event_msg",
         "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 8.0}}}},
        {"timestamp": "2026-10-19T13:49:23.898Z", "type": "token_usage_record",
         "payload": {"response_id": "resp_2", "usage": usage(300_000, 7296, 14)}},
    ]


class CodexAgentLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        arms.generate_arm("A21", base / "arms")
        self.launcher = base / "arms" / "A21" / "codex" / "bin" / "codex-agent"
        self.home = base / "home"
        self.home.mkdir()
        (self.home / "config.toml").write_text("")
        fake_bin = base / "bin"
        fake_bin.mkdir()
        (fake_bin / "codex").write_text(FAKE_CODEX)
        (fake_bin / "codex").chmod(0o755)
        (self.home / "agent-runtime.json").write_text(json.dumps({"codex_bin": str(fake_bin / "codex")}))
        self.env = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "CODEX_HOME": str(self.home),
                    "CLAUDE_CODE_OAUTH_TOKEN": "secret", "CLAUDECODE": "1", "HOME": os.environ.get("HOME", "/")}

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, *args, task="build it", env=None):
        return subprocess.run([sys.executable, str(self.launcher), *args], input=task, text=True,
                              capture_output=True, env=env or self.env, cwd=self.tmp.name, timeout=60)

    def invocation(self):
        return json.loads((self.home / "fake-invocation.json").read_text())

    def test_report_and_call_record(self):
        done = self.launch("implementer")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "REPORT for build it\n")
        seen = self.invocation()
        self.assertEqual(seen["task"], "build it")
        self.assertNotIn("--sandbox", seen["argv"])
        self.assertEqual(seen["argv"][:3], ["exec", "--json", "--strict-config"])
        role = (self.launcher.parent.parent / "roles" / "implementer.md").read_text()
        self.assertIn(f"developer_instructions={json.dumps(role)}", seen["argv"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen["env"])
        self.assertNotIn("CLAUDECODE", seen["env"])
        self.assertEqual(seen["env"]["CODEX_AGENT_DEPTH"], "1")
        [call] = codex.call_records(self.home)
        self.assertEqual((call["agent"], call["exit_code"], call["thread_id"], call["failed"]),
                         ("implementer", 0, THREAD, False))

    def test_agent_runs_on_its_own_model(self):
        tiered = Path(self.tmp.name) / "tiered"
        arms.generate_arm("A23", tiered)
        launcher = tiered / "A23" / "codex" / "bin" / "codex-agent"
        done = subprocess.run([sys.executable, str(launcher), "tester"], input="t", text=True, capture_output=True,
                              env=self.env, cwd=self.tmp.name, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        argv = self.invocation()["argv"]
        self.assertIn('model="gpt-6-luna"', argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        [call] = codex.call_records(self.home)
        self.assertEqual(call["model"], "gpt-6-luna")

    def test_read_only_agents_get_the_read_only_sandbox(self):
        self.assertEqual(self.launch("reviewer").returncode, 0)
        argv = self.invocation()["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")

    def test_refusals(self):
        self.assertEqual(self.launch("planner").returncode, 2)
        self.assertEqual(self.launch("implementer", task="  \n").returncode, 2)
        self.assertEqual(self.launch("implementer", env={**self.env, "CODEX_AGENT_DEPTH": "1"}).returncode, 2)
        self.assertFalse((self.home / "fake-invocation.json").exists())

    def test_usage_limit_failure(self):
        done = self.launch("tester", task="FAIL now")
        self.assertEqual(done.returncode, 1)
        self.assertIn("usage limit", done.stderr)
        [call] = codex.call_records(self.home)
        self.assertTrue(call["failed"] and call["usage_limit"])
        summary = codex.summarize(self.home)
        self.assertTrue(summary["usage_limit"])
        kinds = [v["kind"] for v in codex.validate(self.home, summary, SOL_ONLY, "high")]
        self.assertEqual(kinds, ["codex_call_failed"])


    def test_signal_stop_is_not_a_failure(self):
        proc = subprocess.Popen([sys.executable, str(self.launcher), "tester"], stdin=subprocess.PIPE, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, cwd=self.tmp.name)
        proc.stdin.write("SLEEP please")
        proc.stdin.close()
        deadline = time.time() + 20
        while not (self.home / "fake-invocation.json").exists() and time.time() < deadline:
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
        summary = codex.summarize(self.home)
        [call] = summary["per_call"]
        self.assertEqual(call["stopped_by_signal"], "SIGTERM")
        self.assertFalse(call["failed"])
        self.assertIsNone(codex.run_outcome(summary))

    def test_outcome_of_a_usage_limit_and_a_failure(self):
        self.launch("tester", task="FAIL now")
        status, error = codex.run_outcome(codex.summarize(self.home))
        self.assertEqual(status, "usage_limit")
        self.assertIn("Codex usage limit", error)
        call = next((self.home / "agent-calls").iterdir()) / "call.json"
        record = json.loads(call.read_text())
        call.write_text(json.dumps({**record, "usage_limit": False}))
        self.assertEqual(codex.run_outcome(codex.summarize(self.home))[0], "codex_error")


class CodexLogTest(unittest.TestCase):
    def write_home(self, lines, agent="implementer"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        path = home / "sessions" / "2026" / "10" / "19" / f"rollout-2026-10-19T08-49-16-{THREAD}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(l) + "\n" for l in lines))
        call = home / "agent-calls" / f"c1-{agent}"
        call.mkdir(parents=True)
        (call / "call.json").write_text(json.dumps({"agent": agent, "started_epoch": 100.0,
                                                    "ended_epoch": 160.0, "exit_code": 0, "failed": False,
                                                    "thread_id": THREAD, "usage_limit": False}))
        return home

    def test_requests_are_deduplicated_and_priced(self):
        home = self.write_home(rollout_lines())
        first, second = codex.load_requests(home)
        self.assertEqual((first.input_tokens, first.cache_read_tokens, first.output_tokens), (7452, 0, 110))
        self.assertEqual((second.input_tokens, second.cache_read_tokens), (300_000 - 7296, 7296))
        self.assertEqual((first.source, first.model, first.thread), ("subagent", "gpt-6.1-sol", f"codex-{THREAD}"))
        self.assertAlmostEqual(cost.request_cost(first, "billed"), (7452 * 2.00 + 110 * 10.00) / 1e6)
        long_price = cost.price_for("gpt-6.1-sol", 300_000)  # above 272K: the long-context card
        self.assertEqual((long_price.input, long_price.output), (4.00, 15.00))
        self.assertEqual(cost.context_peaks([first, second])["subagent"], 300_000)

    def test_summary_and_validation(self):
        home = self.write_home(rollout_lines())
        summary = codex.summarize(home)
        self.assertEqual((summary["calls"], summary["calls_by_agent"]), (1, {"implementer": 1}))
        self.assertEqual(summary["tool_calls"], {"exec": 1})
        self.assertEqual(summary["per_call"][0]["wall_s"], 60.0)
        self.assertEqual(summary["per_call"][0]["first_input_tokens"], 7452)
        self.assertEqual(summary["rate_limit_used_percent_max"], 8.0)
        self.assertEqual(codex.validate(home, summary, SOL_ONLY, "high"), [])
        [spawn] = codex.spawn_calls(summary)
        self.assertEqual((spawn["subagent_type"], spawn["timestamp"]), ("implementer", 100.0))

    def test_wrong_model_or_effort_is_a_violation(self):
        home = self.write_home(rollout_lines(model="gpt-6.1-astra", effort="medium"))
        kinds = sorted(v["kind"] for v in codex.validate(home, codex.summarize(home), SOL_ONLY, "high"))
        self.assertEqual(kinds, ["effort", "model"])


    def test_each_turn_is_checked_against_its_agents_model(self):
        luna = self.write_home(rollout_lines(model="gpt-6-luna"), agent="tester")
        self.assertEqual(codex.validate(luna, codex.summarize(luna), TIERED, "high"), [])
        wrong = self.write_home(rollout_lines(model="gpt-6-luna"), agent="implementer")
        [violation] = codex.validate(wrong, codex.summarize(wrong), TIERED, "high")
        self.assertEqual((violation["kind"], violation["expected"], violation["observed"]),
                         ("model", "gpt-6.1-sol", "gpt-6-luna"))

    def test_truncated_last_line_is_tolerated_but_not_elsewhere(self):
        home = self.write_home(rollout_lines())
        [path] = codex.rollout_files(home)
        path.write_text(path.read_text() + '{"timestamp": "2026-10-19T13:49:30Z", "type": "tok')
        summary = codex.summarize(home)
        self.assertEqual(summary["truncated_logs"], [path.name])
        self.assertEqual(len(codex.load_requests(home)), 2)
        path.write_text('{"cut\n' + path.read_text())
        with self.assertRaises(codex.TranscriptError):
            codex.load_requests(home)

    def test_session_without_a_call_and_compaction_count(self):
        home = self.write_home(rollout_lines() + [{"type": "compacted", "payload": {}},
                                                  {"type": "event_msg", "payload": {"type": "context_compacted"}}])
        (home / "agent-calls" / "c1-implementer" / "call.json").unlink()
        (home / "agent-calls" / "c1-implementer").rmdir()
        summary = codex.summarize(home)
        self.assertEqual(summary["compaction_events"], 1)
        kinds = [v["kind"] for v in codex.validate(home, summary, SOL_ONLY, "high")]
        self.assertEqual(kinds, ["codex_session_without_call"])


def fake_auth(last_refresh, exp):
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp.timestamp()}).encode()).decode().rstrip("=")
    return json.dumps({"auth_mode": "chatgpt", "last_refresh": last_refresh,
                       "tokens": {"access_token": f"h.{claims}.s", "refresh_token": "r"}})


class CodexLoginTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.user_auth = self.base / "user" / "auth.json"
        self.user_auth.parent.mkdir()
        for name, value in (("USER_AUTH", self.user_auth), ("AUTH_LOCK", self.base / "user" / ".lock")):
            patcher = mock.patch.object(codex, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = datetime.now(timezone.utc)

    def run_copy(self, name, auth, owner=None):
        home = self.base / "runs" / name / codex.HOME_DIR
        home.mkdir(parents=True)
        (home / codex.AUTH_FILE).write_text(auth)
        if owner is not None:
            (home / codex.OWNER_FILE).write_text(json.dumps(owner))
        return home / codex.AUTH_FILE

    def test_refresh_point_is_the_earlier_of_age_and_expiry(self):
        self.user_auth.write_text(fake_auth("2026-10-15T17:00:48.869140414Z", datetime(2026, 10, 25, 17, tzinfo=timezone.utc)))
        self.assertEqual(codex.login_refresh_point(), datetime(2026, 10, 23, 17, 0, 48, 869140, tzinfo=timezone.utc))
        self.user_auth.write_text(fake_auth("", datetime(2026, 10, 20, tzinfo=timezone.utc)))
        self.assertEqual(codex.login_refresh_point(), datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.user_auth.write_text("[]")
        self.assertIsNone(codex.login_refresh_point())

    def test_release_writes_back_a_newer_login_and_always_removes_the_copy(self):
        self.user_auth.write_text(fake_auth("2026-10-15T00:00:00Z", self.now))
        copy = self.run_copy("r1", fake_auth("2026-10-19T00:00:00Z", self.now))
        info = codex.release_home(copy.parent.parent, "installed-sha")
        self.assertEqual((info["auth_refreshed"], info["auth_written_back"]), (True, True))
        self.assertFalse(copy.exists())
        self.assertIn("2026-10-19", self.user_auth.read_text())
        copy = self.run_copy("r2", fake_auth("2026-10-20T00:00:00Z", self.now))
        with mock.patch.object(codex.shutil, "copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                codex.release_home(copy.parent.parent, "installed-sha")
        self.assertFalse(copy.exists())

    def test_sweep_removes_only_copies_of_dead_runs(self):
        self.user_auth.write_text(fake_auth("2026-10-15T00:00:00Z", self.now))
        dead = self.run_copy("dead", fake_auth("2026-10-19T00:00:00Z", self.now), {"pid": 2 ** 22 + 7, "start": "1"})
        live = self.run_copy("live", fake_auth("2026-10-15T00:00:00Z", self.now),
                             {"pid": os.getpid(), "start": codex._proc_start(os.getpid())})
        swept = codex.sweep_stale_logins(self.base / "runs")
        self.assertEqual(swept, [str(dead.parent.parent)])
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())
        self.assertIn("2026-10-19", self.user_auth.read_text())


    def fake_refreshing_codex(self, refresh):
        script = self.base / "fake-codex"
        script.write_text(textwrap.dedent(f'''\
            #!/usr/bin/env python3
            import json, os, sys
            from pathlib import Path
            sys.stdin.read()
            auth = Path(os.environ["CODEX_HOME"]) / "auth.json"
            data = json.loads(auth.read_text())
            if {refresh!r} and data["last_refresh"] < "2026-10-16":
                data["last_refresh"] = "2026-10-19T12:00:00Z"
                data["tokens"]["refresh_token"] = "rotated"
                auth.write_text(json.dumps(data))
            print("OK")
            '''))
        script.chmod(0o755)
        return mock.patch.object(codex, "resolve_codex_bin", return_value=script)

    def refresh(self):
        config = self.base / "cfg"
        config.mkdir(exist_ok=True)
        (config / "config.toml").write_text("")
        (self.base / "runs").mkdir(exist_ok=True)
        return codex.refresh_login(self.base / "runs", config)

    def test_refresh_login_forces_a_refresh_and_writes_it_back(self):
        self.user_auth.write_text(fake_auth("2026-10-15T17:00:48Z", self.now + timedelta(days=5)))
        with self.fake_refreshing_codex(True):
            outcome = self.refresh()
        self.assertTrue(outcome["forced"] and outcome["refreshed"] and outcome["written_back"])
        self.assertIn("rotated", self.user_auth.read_text())
        self.assertEqual(list((self.base / "runs").iterdir()), [])  # scratch home removed

    def test_refresh_login_keeps_the_login_when_codex_did_not_refresh(self):
        self.user_auth.write_text(fake_auth("2026-10-15T17:00:48Z", self.now + timedelta(days=5)))
        before = self.user_auth.read_text()
        with self.fake_refreshing_codex(False):
            outcome = self.refresh()
        self.assertFalse(outcome["refreshed"] or outcome["written_back"])
        self.assertEqual(self.user_auth.read_text(), before)

    def test_refresh_login_refuses_while_a_run_holds_a_copy(self):
        self.user_auth.write_text(fake_auth("2026-10-15T17:00:48Z", self.now + timedelta(days=5)))
        self.run_copy("live", self.user_auth.read_text(), {"pid": os.getpid(), "start": codex._proc_start(os.getpid())})
        with self.fake_refreshing_codex(True), self.assertRaises(codex.CodexError):
            self.refresh()


class CodexRunWiringTest(unittest.TestCase):
    def test_child_env_for_a_codex_arm(self):
        env = run.child_env(Path("/run/config"), {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "o"},
                            codex_home=Path("/run/codex-home"))
        self.assertEqual(env["CODEX_HOME"], "/run/codex-home")
        self.assertEqual(env["PATH"], "/run/config/codex/bin:/bin")
        self.assertNotIn("CODEX_HOME", run.child_env(Path("/run/config"), {"PATH": "/bin"}, include_token=False))

    def test_codex_home_is_bound_back_and_user_codex_hidden(self):
        steps = "\n".join(run.hiding_steps(Path("/runs/x"), run.VISIBLE_RUN_DIRS + (codex.HOME_DIR,)))
        self.assertIn("mount --bind /dev/shm/.stage/codex-home /runs/x/codex-home", steps)
        self.assertIn(os.path.expanduser("~/.codex"), steps)
        self.assertNotIn("codex-home", "\n".join(run.hiding_steps(Path("/runs/x"))))
        packages = codex.HOST_VISIBLE[0]
        with_host = run.hiding_steps(Path("/runs/x"), run.VISIBLE_RUN_DIRS, codex.HOST_VISIBLE)
        hide = next(i for i, step in enumerate(with_host) if "/.codex;" in step)
        self.assertLess(with_host.index(f"mount --bind {packages} /dev/shm/.stage/host0"), hide)
        self.assertGreater(with_host.index(f"mount -o remount,bind,ro {packages}"), hide)

    def test_dns_forwarding_only_when_asked(self):
        plain = run.wrap_netns(["true"])
        self.assertNotIn("--dns-forward", plain)
        forwarded = run.wrap_netns(["true"], dns_forward=True)
        self.assertEqual(forwarded[forwarded.index("--dns-forward") + 1], run.DNS_FORWARD_ADDR)
        self.assertLess(forwarded.index("--dns-forward"), forwarded.index("--"))
        self.assertIn("mount --bind /dev/shm/.resolv.conf /etc/resolv.conf", forwarded[-1])

    def test_codex_arm_disallows_the_agent_tool(self):
        cfg = run.RunConfig("A21", "task1", 1, None, Path("/t"), Path("/r"), Path("/a"), Path("/k"), Path("/g"),
                            5, 60.0, None, 0, "claude")
        self.assertEqual(run.build_claude_command(cfg, "id", "go", None)[-2:], ["--disallowedTools", "Agent"])


if __name__ == "__main__":
    unittest.main()
