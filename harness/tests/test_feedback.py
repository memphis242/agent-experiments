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
            fake = root / "fake-claude"
            fake.write_text(FAKE_CLAUDE)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            arms.generate_arm("A4", root / "arms")
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "x"
            rc = run.main(["--arm", "A4", "--task", "t1", "--rep", "1", "--template", str(template),
                           "--runs-dir", str(root / "runs"), "--arms-dir", str(root / "arms"),
                           "--tasks-dir", str(root / "tasks"), "--claude-bin", str(fake),
                           "--gaps", str(root / "missing.json")])
            self.assertEqual(rc, 0)
            (run_dir,) = (root / "runs").iterdir()
            rec = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(rec["status"], "completed")
            self.assertTrue(rec["passed"])
            self.assertEqual(rec["rounds_used"], 2)
            self.assertEqual(len(rec["session_ids"]), 1)
            self.assertEqual(rec["resume_request_indices"], [1])
            self.assertIn("The work is incomplete.", rec["rounds"][1]["message"])
            self.assertAlmostEqual(rec["totals"]["cli_cost_usd"], 3.0)
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
                           "--tasks-dir", str(root / "tasks"), "--claude-bin", "/nonexistent"])
            self.assertEqual(rc, 1)
            (run_dir,) = (root / "runs").iterdir()
            rec = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(rec["status"], "infra_error")
            self.assertEqual((rec["task"], rec["task_dir"]), ("task9", "task9-demo"))


class CommandTest(unittest.TestCase):
    def test_flags(self):
        _, cfg = run.parse_args(["--arm", "A1", "--task", "task1", "--rep", "1", "--autocompact", "100000"])
        self.assertEqual((cfg.task, cfg.task_dir), ("task1", "task1-utlog-translator"))
        cmd = run.build_claude_command(cfg, "RID", "hi", "sess", 10.0)
        self.assertEqual(cmd[:5], ["claude", "-p", "--resume", "sess", "hi"])
        self.assertIn("Run ID: RID", cmd)
        self.assertEqual(cmd[-2:], ["--disallowedTools", "Agent"])
        self.assertEqual(cmd[cmd.index("--autocompact") + 1], "100000")


if __name__ == "__main__":
    unittest.main()
