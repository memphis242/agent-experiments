import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import _support  # noqa: F401
import acceptance
import arms
import run


def check(cid, passed, fb="Do the thing."):
    return acceptance.Check(cid, passed, fb, "detail")


class FeedbackTest(unittest.TestCase):
    def test_deterministic_and_sorted(self):
        failures = [check("zeta", False, "Z."), check("alpha", False, "A."), check("mid", False, "M.")]
        texts = {acceptance.compose_feedback(list(p)) for p in itertools.permutations(failures)}
        self.assertEqual(len(texts), 1)
        text = texts.pop()
        self.assertLess(text.index("A."), text.index("M."))
        self.assertLess(text.index("M."), text.index("Z."))
        self.assertTrue(text.startswith(acceptance.FEEDBACK_PREAMBLE))

    def test_contract_violations(self):
        good = {"passed": False, "checks": [
            {"id": "a-b", "passed": False, "feedback": "Fix it.", "detail": ""}]}
        self.assertFalse(acceptance.parse_result(good, 1).passed)
        cases = [
            (good, 0),                                           # exit disagrees
            ({**good, "passed": True}, 0),                       # passed disagrees with checks
            ({"passed": False, "checks": []}, 1),                # empty checks
            ({"passed": False, "checks": [{**good["checks"][0], "id": "Bad_Id"}]}, 1),
            ({"passed": False, "checks": [{**good["checks"][0], "feedback": " "}]}, 1),
            ({"passed": False, "checks": [{**good["checks"][0], "feedback": "see test_foo.py"}]}, 1),
            ({"passed": False, "checks": [good["checks"][0]] * 2}, 1),  # duplicate id
        ]
        for data, code in cases:
            with self.assertRaises(acceptance.ContractError, msg=str(data)):
                acceptance.parse_result(data, code)
        with self.assertRaises(acceptance.ContractError):
            acceptance.parse_result(good, 7)


FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, re, sys, uuid
    from pathlib import Path
    args = sys.argv[1:]
    resume = args[args.index("--resume") + 1] if "--resume" in args else None
    sid = resume or str(uuid.uuid4())
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    proj = cfg / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    proj.mkdir(parents=True, exist_ok=True)
    n = len(list(proj.glob("*.jsonl"))) and sum(1 for _ in (proj / f"{sid}.jsonl").open()) if (proj / f"{sid}.jsonl").exists() else 0
    with (proj / f"{sid}.jsonl").open("a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-17T10:00:00Z", "message": {
            "id": f"msg_{sid}_{n}", "model": "claude-opus-5-5", "content": [],
            "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
                      "cache_creation_input_tokens": 500,
                      "cache_creation": {"ephemeral_1h_input_tokens": 500, "ephemeral_5m_input_tokens": 0}}}}) + "\\n")
    if resume:
        Path("done").write_text("x")
    print(json.dumps({"type": "result", "session_id": sid, "total_cost_usd": 1.5, "num_turns": 3,
                      "duration_ms": 10, "duration_api_ms": 5, "is_error": False, "result": "ok",
                      "usage": {}, "modelUsage": {}}))
''')

PACK = textwrap.dedent('''\
    #!/usr/bin/env bash
    [ "$ACCEPT_NO_NAMESPACE" = 1 ] || { echo "ACCEPT_NO_NAMESPACE missing" >&2; exit 2; }
    [ -e "$(dirname "$0")/selftest" ] && { echo "selftest was copied" >&2; exit 2; }
    if [ -f "$1/done" ]; then
      echo '{"passed": true, "checks": [{"id": "done-file", "passed": true, "feedback": "", "detail": ""}]}' > "$2"; exit 0
    fi
    echo '{"passed": false, "checks": [{"id": "done-file", "passed": false, "feedback": "The work is incomplete.", "detail": "no file"}]}' > "$2"; exit 1
''')


class EndToEndTest(unittest.TestCase):
    def test_fake_claude_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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
            (tasks / "prompt.md").write_text("Do the task.")
            (tasks / "acceptance" / "run.sh").write_text(PACK)
            (tasks / "acceptance" / "selftest").mkdir()
            (tasks / "acceptance" / "selftest" / "ref.txt").write_text("answer")
            fake = root / "fake-claude"
            fake.write_text(FAKE_CLAUDE)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            arms.generate_arm("A4", root / "arms")
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "x"
            rc = run.main(["--arm", "A4", "--task", "t1", "--rep", "1", "--template", str(template),
                           "--runs-dir", str(root / "runs"), "--arms-dir", str(root / "arms"),
                           "--tasks-dir", str(root / "tasks"), "--claude-bin", str(fake),
                           "--gaps", str(root / "missing.json"), "--no-netns"])
            self.assertEqual(rc, 0)
            (run_dir,) = [p for p in (root / "runs").iterdir() if p.is_dir()]
            rec = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(rec["status"], "completed")
            self.assertTrue(rec["passed"])
            self.assertEqual(rec["rounds_used"], 2)
            self.assertEqual(len(rec["session_ids"]), 1)
            self.assertEqual(rec["resume_request_indices"], [1])
            self.assertIn("The work is incomplete.", rec["rounds"][1]["message"])
            self.assertEqual(rec["totals"]["cli_reported_by_round_usd"], [1.5, 1.5])
            self.assertEqual(rec["totals"]["cli_reported_cumulative_usd"], 1.5)
            self.assertIn("delegation", rec["rounds"][0])
            self.assertAlmostEqual(rec["totals"]["billed_usd"], 2 * 0.00624, places=9)
            self.assertEqual(rec["cost"]["primary_scenario"], "billed")
            self.assertEqual(len(rec["cost"]["reconciliation"]["rounds"]), 2)
            self.assertRegex(rec["run_id"], r"^[0-9a-f]{8}$")
            self.assertEqual(run_dir.name, rec["run_id"])
            self.assertIn("harness_sha", rec["provenance"])
            self.assertEqual(len(rec["provenance"]["pack_sha256"]), 64)
            self.assertFalse((run_dir / "acceptance").exists())  # pack copy is removed after each run
            self.assertTrue((run_dir / "results" / "acceptance-result-round1.json").is_file())
            self.assertIn(rec["run_id"], (root / "runs" / "runs.tsv").read_text())
            self.assertEqual(rec["leak_check"], [])
            self.assertTrue(rec["validation"]["ok"])
            self.assertEqual(rec["cost"]["requests"], 2)
            self.assertTrue(rec["cost"]["warnings"])
            self.assertTrue((run_dir / "repo" / "TASK.md").is_file())
            self.assertFalse((run_dir / "repo" / "acceptance").exists())
            self.assertEqual(len(rec["config_sha256"]), 64)
            self.assertEqual(rec["config_sha256"], run.hash_config_root(root / "arms" / "A4"))
            self.assertTrue((run_dir / "config" / "projects").is_dir())  # transcripts land in the run dir
            self.assertFalse((root / "arms" / "A4" / "projects").exists())

    def test_ctf_setup_failure_is_infra_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "template"
            (template / "ctf").mkdir(parents=True)
            (template / "ctf" / "setup_ctf.sh").write_text("exit 3\n")
            subprocess.run(["git", "init", "-q", str(template)], check=True)
            subprocess.run(["git", "-C", str(template), "add", "."], check=True)
            subprocess.run(["git", "-C", str(template), "-c", "user.name=t", "-c", "user.email=t@t",
                            "commit", "-qm", "init"], check=True)
            task = root / "tasks" / "task9-demo"
            (task / "acceptance").mkdir(parents=True)
            (task / "spec.md").write_text("s")
            (task / "prompt.md").write_text("p")
            arms.generate_arm("A1", root / "arms")
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "x"
            rc = run.main(["--arm", "A1", "--task", "task9", "--rep", "1", "--template", str(template),
                           "--runs-dir", str(root / "runs"), "--arms-dir", str(root / "arms"),
                           "--tasks-dir", str(root / "tasks"), "--claude-bin", "/nonexistent", "--no-netns"])
            self.assertEqual(rc, 1)
            (run_dir,) = [p for p in (root / "runs").iterdir() if p.is_dir()]
            rec = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(rec["status"], "infra_error")
            self.assertEqual((rec["task"], rec["task_dir"]), ("task9", "task9-demo"))


class NetnsTest(unittest.TestCase):
    CMD = ["claude", "-p", "it's a \"test\" $HOME; `rm -rf /` \\ x", "--x", "a b"]

    def peel(self, wrapped):
        """Peel pasta -> unshare (mount/ipc/pid) -> unshare -U -> original argv; returns (steps, argv)."""
        import shlex
        self.assertEqual(wrapped[:12], ["pasta", "--config-net", "--quiet", "-t", "none", "-u", "none",
                                        "-T", "none", "-U", "none", "--"])
        self.assertEqual(wrapped[12:14], ["sh", "-c"])
        self.assertEqual(len(wrapped), 15)
        ns = shlex.split(wrapped[14])
        self.assertEqual(ns[:8], ["unshare", "-m", "-i", "-p", "-f", "--mount-proc", "--kill-child", "sh"])
        self.assertEqual(ns[8], "-c")
        steps = ns[9].split(" && ")
        last = shlex.split(steps[-1])
        self.assertEqual(last[:2], ["unshare", "-U"])
        self.assertEqual(last[2:4], [f"--map-user={os.getuid()}", f"--map-group={os.getgid()}"])
        self.assertEqual(last[4:6], ["sh", "-c"])
        return steps[:-1], shlex.split(last[6])

    def test_wrap_quotes_through_all_layers(self):
        steps, argv = self.peel(run.wrap_netns(self.CMD))
        self.assertEqual(steps, ["mount -t tmpfs tmpfs /dev/shm"])
        self.assertEqual(argv, self.CMD)

    def test_wrap_with_hiding(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps, argv = self.peel(run.wrap_netns(self.CMD, hide_run_dir=Path(tmp)))
            self.assertEqual(argv, self.CMD)
            text = "\n".join(steps)
            for template, reason in run.HIDDEN_PATHS:
                self.assertIn(os.path.expanduser(template), text)
                self.assertTrue(reason)
            self.assertLess(text.index("--bind"), text.index("/workspace"))      # stage before hiding
            self.assertLess(text.index("mount -t tmpfs tmpfs /tmp"), text.rindex("--bind"))  # rebind after hiding
            self.assertTrue(steps[-1].startswith("cd "))
            self.assertTrue(steps[-1].endswith("repo"))

    def test_child_env_allowlist(self):
        base = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CODE_OAUTH_TOKEN": "o",
                "CLAUDE_CODE_SUBAGENT_MODEL": "x", "CLAUDE_CODE_EFFORT_LEVEL": "low", "PATH": "/bin", "HOME": "/h"}
        env = run.child_env(Path("/cfg"), base)
        self.assertEqual(set(env), {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
                                    "DISABLE_AUTOUPDATER", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"})
        self.assertEqual(env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "7200000")
        self.assertEqual((env["CLAUDE_CODE_OAUTH_TOKEN"], env["CLAUDE_CONFIG_DIR"]), ("o", "/cfg"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", run.child_env(Path("/cfg"), base, include_token=False))
        self.assertIn("ANTHROPIC_API_KEY", base)  # input is not mutated

    def test_copy_arm_root_rejects_extras(self):
        with tempfile.TemporaryDirectory() as tmp:
            arms.generate_arm("A4", Path(tmp) / "arms")
            src = Path(tmp) / "arms" / "A4"
            run.copy_arm_root(src, Path(tmp) / "copy")
            self.assertEqual(run.hash_config_root(src), run.hash_config_root(Path(tmp) / "copy"))
            (src / "projects").mkdir()
            with self.assertRaises(run.RunError):
                run.copy_arm_root(src, Path(tmp) / "copy2")

    def test_kill_strays_ignores_other_names_and_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run.kill_stray_processes(Path(tmp)), [])


def make_world(root, pack_text, fake_text):
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
    (tasks / "prompt.md").write_text("Do the task.")
    (tasks / "acceptance" / "run.sh").write_text(pack_text)
    fake = root / "fake-claude"
    fake.write_text(fake_text)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    arms.generate_arm("A1", root / "arms")
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "x"
    return ["--arm", "A1", "--task", "t1", "--rep", "1", "--template", str(template),
            "--runs-dir", str(root / "runs"), "--arms-dir", str(root / "arms"),
            "--tasks-dir", str(root / "tasks"), "--claude-bin", str(fake), "--no-netns",
            "--gaps", str(root / "missing.json")]


LIMIT_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, sys
    if "--version" in sys.argv:
        print("0.0.0"); sys.exit(0)
    print(json.dumps({"type": "result", "session_id": "s1", "is_error": True, "total_cost_usd": 0.5,
                      "result": "Claude usage limit reached", "num_turns": 1}))
    sys.exit(1)
''')

SLOW_PACK = textwrap.dedent('''\
    #!/usr/bin/env bash
    if [ -f "$1/done" ]; then
      echo '{"passed": true, "checks": [{"id": "done-file", "passed": true, "feedback": "", "detail": ""}]}' > "$2"; exit 0
    fi
    sleep 30
''')


class OutcomeTest(unittest.TestCase):
    def one_run(self, pack, fake, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = make_world(root, pack, fake)
            rc = run.main([*args, *extra])
            (run_dir,) = [p for p in (root / "runs").iterdir() if p.is_dir()]
            return rc, json.loads((run_dir / "run.json").read_text())

    def test_usage_limit_is_recorded_with_its_own_status(self):
        rc, rec = self.one_run(PACK, LIMIT_CLAUDE)
        self.assertEqual(rc, run.EXIT_USAGE_LIMIT)
        self.assertEqual(rec["status"], "usage_limit")

    def test_usage_limit_pattern(self):
        for text in ("You've hit your session limit", "You have hit your weekly limit", "usage limit reached"):
            self.assertTrue(run.USAGE_LIMIT_RE.search(text), text)
        for text in ("error: unknown option --foo", "I hit the target; limit of 3 retries is fine"):
            self.assertFalse(run.USAGE_LIMIT_RE.search(text), text)

    def test_acceptance_timeout_is_a_failed_round(self):
        rc, rec = self.one_run(SLOW_PACK, FAKE_CLAUDE, ["--acceptance-timeout", "2"])
        self.assertEqual(rc, 0)
        self.assertEqual(rec["status"], "completed")
        self.assertTrue(rec["passed"])
        self.assertTrue(rec["rounds"][0]["acceptance"]["timed_out"])
        self.assertEqual(rec["rounds"][1]["message"], acceptance.GENERIC_TIMEOUT_FEEDBACK)


class CommandTest(unittest.TestCase):
    def test_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "task1-utlog-translator").mkdir()
            _, cfg = run.parse_args(["--arm", "A1", "--task", "task1", "--rep", "1", "--autocompact", "100000",
                                     "--tasks-dir", tmp])
        self.assertEqual((cfg.task, cfg.task_dir), ("task1", "task1-utlog-translator"))
        cmd = run.build_claude_command(cfg, "RID", "hi", "sess")
        self.assertEqual(cmd[:5], ["claude", "-p", "--resume", "sess", "hi"])
        self.assertIn("Run ID: RID", cmd)
        self.assertNotIn("--max-budget-usd", cmd)
        self.assertEqual(cmd[-2:], ["--disallowedTools", "Agent"])
        self.assertEqual(cmd[cmd.index("--autocompact") + 1], "100000")


class AcceptanceTimeoutTest(unittest.TestCase):
    def parse(self, timeout_file, *flags):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "task1-utlog-translator" / "acceptance"
            pack.mkdir(parents=True)
            if timeout_file is not None:
                (pack / "timeout_s").write_bytes(timeout_file)
            _, cfg = run.parse_args(["--arm", "A1", "--task", "task1", "--rep", "1", "--tasks-dir", tmp, *flags])
        return cfg.acceptance_timeout_s

    def test_no_file_gives_default(self):
        self.assertEqual(self.parse(None), 2400.0)
        self.assertEqual(self.parse(None, "--acceptance-timeout", "30"), 30.0)

    def test_file_gives_its_value(self):
        self.assertEqual(self.parse(b"2760"), 2760.0)
        self.assertEqual(self.parse(b"2760\n"), 2760.0)

    def test_explicit_flag_at_or_above_file_wins(self):
        self.assertEqual(self.parse(b"2760\n", "--acceptance-timeout", "3000"), 3000.0)
        self.assertEqual(self.parse(b"2760\n", "--acceptance-timeout", "2760"), 2760.0)

    def test_explicit_flag_below_file_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.parse(b"2760\n", "--acceptance-timeout", "2400")

    def test_malformed_file_is_an_error(self):
        for content in (b"", b"\n", b"abc", b"0", b"-5", b"27.5", b"2760 s", b"2760\n\n", b" 2760", b"\xff"):
            with self.subTest(content=content), self.assertRaises(SystemExit):
                self.parse(content)

    def test_unreadable_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "timeout_s").mkdir()
            with self.assertRaises(run.RunError):
                run.pack_timeout_s(Path(tmp))


if __name__ == "__main__":
    unittest.main()
