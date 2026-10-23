import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import base64
import signal
import shutil
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import analysis
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


# A fake `codex exec` / `codex exec resume` for a Codex main agent: it logs each invocation, writes the
# session log Codex would, prints the --json events, and leaves done in the repo on a resumed round.
FAKE_CODEX_MAIN = textwrap.dedent(r"""
    #!/usr/bin/env python3
    import json, os, sys, tomllib
    from pathlib import Path
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("fake-codex 1")
        sys.exit(0)
    home = Path(os.environ["CODEX_HOME"])
    if argv == ["features", "list"]:
        config = tomllib.loads((home / "config.toml").read_text())
        for name, enabled in config.get("features", {}).items():
            print(name, "stable", str(enabled.get("enabled", True) if isinstance(enabled, dict) else enabled).lower())
        sys.exit(0)
    if argv == ["debug", "prompt-input"]:
        config = tomllib.loads((home / "config.toml").read_text())
        markers = "<multi_agent_role><multi_agent_mode>" if config["features"]["multi_agent"] else ""
        print(json.dumps([{"role": "developer", "content": [{"text":
            (home / "AGENTS.md").read_text() + markers + f"<cwd>{Path.cwd()}</cwd>"}]}]))
        sys.exit(0)
    task = sys.stdin.read()
    if not task:
        print("No prompt provided")
        sys.exit(0)
    resume = argv[1] == "resume"
    thread = argv[argv.index("-") - 1] if resume else "@THREAD@"
    if os.environ.get("CODEX_AGENT_DEPTH"):
        thread = "11111111-2222-3333-4444-555555555555"
    with (home / "fake-invocations.jsonl").open("a") as f:
        f.write(json.dumps({"argv": argv, "task": task, "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")
    if "FAIL" in task:
        print(json.dumps({"type": "thread.started", "thread_id": thread}))
        print(json.dumps({"type": "error", "message": "You've hit your usage limit."}))
        print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}))
        sys.exit(1)
    def setting(key):
        return next(a.split('"')[1] for a in argv if a.startswith(key + "="))
    log = home / "sessions" / "2026" / "10" / "19" / f"rollout-2026-10-19T08-49-16-{thread}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    n = sum(1 for l in log.open() if "token_usage_record" in l) if log.exists() else 0
    entries = [] if resume else [{"timestamp": "2026-10-19T13:49:16.832Z", "type": "session_meta",
                                  "payload": {"id": thread}}]
    entries += [{"timestamp": "2026-10-19T13:49:18.823Z", "type": "turn_context",
                 "payload": {"model": setting("model"), "effort": setting("model_reasoning_effort")}},
                {"timestamp": "2026-10-19T13:49:22.113Z", "type": "token_usage_record",
                 "payload": {"response_id": f"resp_{n}", "usage": {
                     "input_tokens": 1000 * (n + 1), "cached_input_tokens": 500 * n, "cache_write_input_tokens": 0,
                     "output_tokens": 100, "reasoning_output_tokens": 10, "total_tokens": 1100 * (n + 1)}}}]
    with log.open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in entries))
    if "DELEGATE" in task:  # persist the built-in child's rollout or run a shell-launched Claude child
        import subprocess
        if "CODEX" in task:
            config = tomllib.loads((home / "config.toml").read_text())
            role = tomllib.loads((home / config["agents"]["tester"]["config_file"]).read_text())
            child = "11111111-2222-3333-4444-555555555555"
            child_log = log.with_name("rollout-child-" + child + ".jsonl")
            child_log.write_text("\n".join(json.dumps(e) for e in [
                {"timestamp": "2026-10-19T13:49:19Z", "type": "session_meta", "payload": {
                    "id": child, "parent_thread_id": thread, "source": {"subagent": {"thread_spawn": {
                        "parent_thread_id": thread, "agent_role": "tester", "depth": 1, "agent_path": "/root/test"}}}}},
                {"type": "turn_context", "payload": {"model": role["model"], "effort": role["model_reasoning_effort"]}},
                {"timestamp": "2026-10-19T13:49:22Z", "type": "token_usage_record", "payload": {
                    "response_id": "resp_0", "usage": {"input_tokens": 1000, "output_tokens": 100}}},
                {"type": "event_msg", "payload": {"type": "task_complete"}}]) + "\n")
            sub = subprocess.CompletedProcess([], 0, "builtin report", "")
        else:
            sub = subprocess.run(["claude-agent", "implementer"], input="LIMIT" if "LIMIT" in task else "build it",
                                 text=True, capture_output=True)
        Path("agent-report.txt").write_text(f"{sub.returncode}|{sub.stdout}|{sub.stderr}")
    if resume:
        Path("done").write_text("x")
    print(json.dumps({"type": "thread.started", "thread_id": thread}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": sum(1000 * (i + 1) for i in range(n + 1)), "cached_input_tokens": sum(500 * i for i in range(n + 1)),
        "output_tokens": 100 * (n + 1), "reasoning_output_tokens": 10 * (n + 1)}}))
    Path(argv[argv.index("-o") + 1]).write_text("final for " + task.strip()[:20])
    """).lstrip().replace("@THREAD@", THREAD)

FAKE_PACK = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ -f "$1/done" ]; then
      echo '{"passed": true, "checks": [{"id": "done-file", "passed": true, "feedback": "", "detail": ""}]}' > "$2"; exit 0
    fi
    echo '{"passed": false, "checks": [{"id": "done-file", "passed": false, "feedback": "The work is incomplete.", "detail": "no file"}]}' > "$2"; exit 1
    """)

MAIN_MODEL, MAIN_EFFORT = "gpt-6.1-sol", "high"


def make_world(test, root, prompt, arm="A26", claude_bin=None):
    """A template, task, acceptance pack, fake codex, and generated `arm` root under `root`, with
    codex.prepare_home replaced by one that only creates the homes; returns run.py's arguments."""
    template = root / "template"
    template.mkdir()
    subprocess.run(["git", "init", "-q", str(template)], check=True)
    (template / "README").write_text("x")
    subprocess.run(["git", "-C", str(template), "add", "."], check=True)
    subprocess.run(["git", "-C", str(template), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], check=True)
    tasks = root / "tasks" / "t1"
    (tasks / "acceptance").mkdir(parents=True)
    (tasks / "spec.md").write_text("spec")
    (tasks / "prompt.md").write_text(prompt)
    (tasks / "acceptance" / "run.sh").write_text(FAKE_PACK)
    fake = root / "fake-codex"
    fake.write_text(FAKE_CODEX_MAIN)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    arms.generate_arm(arm, root / "arms")

    def fake_prepare(run_dir, codex_config, repo, base_env):
        test.assertTrue(arms.get_arm(arm).codex_main)
        test.assertNotIn("settings.json", {p.name for p in codex_config.parent.iterdir()})  # no Claude Code configuration
        codex.home_for(run_dir).mkdir()
        return {"codex_bin": str(fake), "codex_version": "fake"}

    patcher = mock.patch.object(codex, "prepare_home", fake_prepare)
    patcher.start()
    test.addCleanup(patcher.stop)
    argv = ["--arm", arm, "--task", "t1", "--rep", "1", "--template", str(template),
            "--runs-dir", str(root / "runs"), "--arms-dir", str(root / "arms"),
            "--tasks-dir", str(root / "tasks"), "--gaps", str(root / "missing.json"), "--no-netns"]
    return argv + (["--claude-bin", str(claude_bin)] if claude_bin else [])


class CodexMainTest(unittest.TestCase):
    def write_log(self, home, lines):
        path = home / "sessions" / "2026" / "10" / "19" / f"rollout-2026-10-19T08-49-16-{THREAD}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(l) + "\n" for l in lines))
        return path

    def test_initial_and_resume_command_lines(self):
        last, repo = Path("/run/codex-main-round1.last.txt"), Path("/run/repo")
        first = codex.main_command("/bin/codex", MAIN_MODEL, MAIN_EFFORT, "Run ID: ab12", last, repo, None)
        self.assertEqual(first, ["/bin/codex", "exec", "--json", "--strict-config", "-C", "/run/repo",
                                 "-o", str(last), "-c", 'model="gpt-6.1-sol"', "-c", 'model_reasoning_effort="high"',
                                 "-c", 'developer_instructions="Run ID: ab12"', "-"])
        resumed = codex.main_command("/bin/codex", MAIN_MODEL, MAIN_EFFORT, "Run ID: ab12", last, repo, THREAD)
        self.assertEqual(resumed[:5], ["/bin/codex", "exec", "resume", "--json", "--strict-config"])
        self.assertNotIn("-C", resumed)  # resume runs in the caller's working directory
        self.assertNotIn("--ephemeral", first + resumed)
        self.assertEqual(resumed[-2:], [THREAD, "-"])
        self.assertEqual(run.codex_developer_instructions("ab12"), "Run ID: ab12")

    def test_event_stream_becomes_a_round_record(self):
        events = "\n".join(json.dumps(e) for e in [
            {"type": "thread.started", "thread_id": THREAD}, {"type": "turn.started"},
            {"type": "error", "message": "model metadata not found"},  # a warning, not a failure
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                 "output_tokens": 7, "reasoning_output_tokens": 3}}]) + "\nnot json\n"
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp) / "last.txt"
            last.write_text("all done")
            result = codex.main_round_result(events, "", 0, last)
            self.assertEqual((result["session_id"], result["is_error"], result["result"], result["_exit_code"]),
                             (THREAD, False, "all done", 0))
            self.assertEqual(result["usage"]["cached_input_tokens"], 40)
            self.assertEqual((result["turns_completed"], result["errors"]), (1, ["model metadata not found"]))
            limited = codex.main_round_result(
                '{"type": "thread.started", "thread_id": "t"}\n'
                '{"type": "turn.failed", "error": {"message": "You\'ve hit your usage limit."}}\n', "", 1, last)
            self.assertTrue(limited["is_error"] and limited["_usage_limit"])
            self.assertIn("usage limit", limited["result"])
            other = codex.main_round_result('{"type": "thread.started", "thread_id": "t"}\n', "boom", 2, last)
            self.assertTrue(other["is_error"] and not other["_usage_limit"])
            self.assertEqual(other["result"], "boom")
            self.assertIsNone(codex.main_round_result("", "no network", 1, last)["session_id"])

    def test_cost_and_analysis_from_a_main_session_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_log(home, rollout_lines())
            requests = codex.load_requests(home, "main")
            self.assertEqual([(r.source, r.main_index) for r in requests], [("main", 0), ("main", 1)])
            record = {"arm": "A26", "session_ids": [THREAD], "resume_request_indices": [1], "rounds": [
                {"round": 1, "started_epoch": 1.0, "wall_time_s": 60.0, "codex_cli": {
                    "session_id": THREAD, "usage": {"input_tokens": 307_452, "cached_input_tokens": 7296,
                                                    "output_tokens": 124, "reasoning_output_tokens": 6}}}]}
            analysis.update_record(record, [], [], [1], Path("/missing"), 0, {}, home)
            self.assertIsNone(record["analysis_error"])
            billed = record["cost"]["scenarios"]["billed"]
            self.assertEqual(set(billed["by_model"]), {"gpt-6.1-sol"})
            self.assertEqual(billed["by_source"]["subagent"], 0.0)
            self.assertAlmostEqual(billed["by_source"]["main"], billed["total_usd"])
            self.assertAlmostEqual(billed["total_usd"], (7452 * 2.00 + 110 * 10.00) / 1e6
                                   + (292_704 * 4.00 + 7296 * 0.20 + 14 * 15.00) / 1e6, places=6)
            self.assertEqual(record["cost"]["billed_basis"], "codex_rollout")
            self.assertEqual(record["cost"]["exec_usage_reconciliation"]["diff"],
                             {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
            self.assertEqual(record["behavior"]["max_context_main"], 300_000)
            self.assertIsNone(record["behavior"]["max_context_subagent"])
            self.assertEqual(record["totals"]["turns"], 2)
            self.assertEqual(record["totals"]["billed_usd"], billed["total_usd"])
            self.assertTrue(record["validation"]["ok"], record["validation"])

    def test_validation_flags_a_wrong_model_a_forbidden_tool_and_a_foreign_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_log(home, rollout_lines(model="gpt-6-luna", effort="low")
                           + [{"type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent"}}])
            kinds = sorted(v["kind"] for v in codex.validate_main(
                codex.summarize(home), codex.read_rollouts(home), MAIN_MODEL, MAIN_EFFORT, ["another-thread"]))
            self.assertEqual(kinds, ["codex_session_log_missing", "codex_unexpected_session", "effort",
                                     "forbidden_tool", "model"])

    def test_session_logs_are_leak_scanned_and_compactions_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self.write_log(home, rollout_lines() + [{"type": "compacted", "payload": {}}])
            self.assertEqual(codex.leak_scan_files(home), [path])
            self.assertEqual(codex.summarize(home)["compaction_events"], 1)

    def make_world(self, root, prompt):
        return make_world(self, root, prompt)

    def invocations(self, run_dir):
        lines = (codex.home_for(run_dir) / "fake-invocations.jsonl").read_text().splitlines()
        return [json.loads(l) for l in lines]

    def test_fake_codex_main_loop(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):
            os.environ.pop(run.TOKEN_ENV, None)  # a Codex main agent needs no Claude token
            root = Path(tmp)
            self.assertEqual(run.main(self.make_world(root, "Do the task.")), 0)
            (run_dir,) = [p for p in (root / "runs").iterdir() if p.is_dir()]
            rec = json.loads((run_dir / "run.json").read_text())
            self.assertEqual((rec["status"], rec["passed"], rec["rounds_used"]), ("completed", True, 2))
            self.assertEqual((rec["main_harness"], rec["session_ids"], rec["resume_request_indices"]),
                             ("codex", [THREAD], [1]))
            self.assertEqual(rec["settings"]["model"], MAIN_MODEL)
            self.assertEqual([r["codex_cli"]["session_id"] for r in rec["rounds"]], [THREAD, THREAD])
            self.assertNotIn("cli", rec["rounds"][0])
            self.assertIn("The work is incomplete.", rec["rounds"][1]["message"])
            first, second = self.invocations(run_dir)
            self.assertEqual((first["task"], first["argv"][:2]), ("Do the task.", ["exec", "--json"]))
            self.assertEqual(second["argv"][:2], ["exec", "resume"])
            self.assertEqual(second["argv"][-2:], [THREAD, "-"])
            self.assertIn("The work is incomplete.", second["task"])
            self.assertEqual(second["cwd"], str((run_dir / "repo").resolve()))
            self.assertEqual(second["env"]["CODEX_HOME"], str(codex.home_for(run_dir)))
            self.assertFalse([k for k in second["env"] if k.startswith(("CLAUDE", "ANTHROPIC"))])
            self.assertIn(f"Run ID: {rec['run_id']}", json.dumps(second["argv"]))
            self.assertTrue((run_dir / "codex-main-round2.events.jsonl").is_file())
            self.assertEqual((run_dir / "codex-main-round1.last.txt").read_text(), "final for Do the task.")
            self.assertIsNone(rec["analysis_error"])
            billed = rec["cost"]["scenarios"]["billed"]
            self.assertEqual((set(billed["by_model"]), {k for k, v in billed["by_source"].items() if v}),
                             ({MAIN_MODEL}, {"main"}))
            self.assertEqual((rec["totals"]["turns"], rec["behavior"]["max_context_main"]), (2, 2000))
            self.assertEqual(rec["cost"]["exec_usage_reconciliation"]["diff"]["input_tokens"], 0)
            self.assertTrue(rec["validation"]["ok"], rec["validation"])
            self.assertFalse((run_dir / "transcripts").exists())

    def test_compact_percentage_sets_codex_limit_and_arm_identity(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):
            os.environ.pop(run.TOKEN_ENV, None)
            root = Path(tmp)
            argv = self.make_world(root, "Do the task.")
            self.assertEqual(run.main([*argv, "--compact-pct", "50"]), 0)
            (run_dir,) = [p for p in (root / "runs").iterdir() if p.is_dir()]
            rec = json.loads((run_dir / "run.json").read_text())
            self.assertEqual((rec["arm"], rec["compact_pct"]), ("A26@c50", 50))
            first, _ = self.invocations(run_dir)
            self.assertIn(["-c", "model_auto_compact_token_limit=237500"],
                          [first["argv"][i:i + 2] for i in range(len(first["argv"]) - 1)])

    def delegation_world(self, root, netns=False):
        arm = arms.generate_arm("A30", root / "arms")
        run_dir = root / "runs" / "review"
        repo = run_dir / "repo"
        repo.mkdir(parents=True)
        run.copy_arm_root(arm, run_dir / "config", claude_files=False)
        fake = run_dir / "config" / "fake-codex"
        fake.write_text(FAKE_CODEX_MAIN)
        fake.chmod(0o755)
        pack = root / "tasks" / "t1" / "acceptance"
        pack.mkdir(parents=True)
        (pack / "run.sh").write_text(FAKE_PACK)
        cfg = run.RunConfig("A30", "t1", 1, None, root / "template", root / "runs", root / "arms",
                            root / "tasks", root / "missing", 2, 60.0, 60.0, 0, "unused", "t1", netns)
        runner = run.Runner(cfg, "review", run_dir)
        runner.record = {"arm": "A30", "rounds": [], "session_ids": [], "passed": False}
        auth = root / "synthetic-auth.json"
        auth.write_text(fake_auth(datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc) + timedelta(days=10)))
        for name, value in (("USER_AUTH", auth), ("AUTH_LOCK", root / ".auth-lock")):
            patcher = mock.patch.object(codex, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(codex, "resolve_codex_bin", return_value=fake):
            runner.prepare_codex()
        self.addCleanup(runner.release_codex)
        return runner

    def check_delegation(self, runner):
        runner.run("DELEGATE CODEX the work.")
        rec = runner.record
        self.assertEqual(rec["status"], "completed", rec.get("error"))
        self.assertEqual(rec["behavior"]["codex_subagents"]["calls"], 1)
        self.assertEqual(rec["behavior"]["subagent_spawns"], 1)
        self.assertTrue(rec["validation"]["ok"], rec["validation"])
        self.assertGreater(rec["cost"]["scenarios"]["billed"]["by_model"]["gpt-6-luna"], 0)
        self.assertIsNone(runner.subagent_home)
        self.assertNotIn("shell_environment_policy", (runner.codex_home / "config.toml").read_text())
        self.assertEqual(len(codex.read_rollouts(runner.codex_home)), 2)
        self.assertTrue((runner.codex_home / "roles" / "tester.toml").is_file())
        self.assertTrue((runner.repo / "agent-report.txt").read_text().startswith("0|builtin report"))
        for round_no in (1, 2):
            self.assertTrue((runner.run_dir / f"codex-main-round{round_no}.last.txt").read_text().strip())

    def test_codex_main_with_builtin_subagents_shares_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check_delegation(self.delegation_world(Path(tmp)))

    def test_namespace_codex_delegation_and_final_output_smoke(self):
        if not shutil.which("unshare"):
            self.skipTest("unshare is unavailable")
        probe = subprocess.run(["unshare", "-Urnm", "true"], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest("user/mount/network namespaces unavailable: " + probe.stderr.strip())
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.delegation_world(Path(tmp), netns=True)
            wrap = run.wrap_netns

            def offline_wrap(cmd, **kwargs):
                return wrap(cmd, **{**kwargs, "dns_forward": False})

            # This smoke test creates isolated namespaces without network forwarding or DNS traffic.
            with mock.patch.object(run, "PASTA_PREFIX", ("unshare", "-Urn", "--")), \
                    mock.patch.object(codex, "HOST_VISIBLE", ()), \
                    mock.patch.object(run, "wrap_netns", side_effect=offline_wrap):
                self.check_delegation(runner)

    def test_codex_usage_limit_is_a_usage_limit_run(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):
            os.environ.pop(run.TOKEN_ENV, None)
            root = Path(tmp)
            self.assertEqual(run.main(self.make_world(root, "FAIL right away")), run.EXIT_USAGE_LIMIT)
            (run_dir,) = [p for p in (root / "runs").iterdir() if p.is_dir()]
            rec = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(rec["status"], "usage_limit")
            self.assertIn("usage limit", rec["error"])
            self.assertEqual(rec["rounds_used"], 1)


if __name__ == "__main__":
    unittest.main()
