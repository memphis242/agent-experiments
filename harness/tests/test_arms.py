import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import arms

EAGER = ("Delegate aggressively. Hand every bounded piece of work (exploration, implementation of a "
         "component, writing tests, running builds, debugging a failure, review) to a subagent as soon as "
         "it can be stated clearly, and run independent subagents in parallel. Keep your own context for "
         "integration, decisions, and review of their results.")
CONSERVATIVE = ("Delegate sparingly. Do the work yourself by default and spawn a subagent only when a piece "
                "of work is large, clearly separable, and would otherwise flood your context (for example a "
                "broad codebase survey or a long independent test run). Never spawn more than one subagent "
                "at a time.")
EXPECTED = {"A1": None, "A2": "claude-opus-5-5", "A3": "claude-opus-5-5", "A4": "claude-sonnet-5-5",
            "A5": "claude-sonnet-5-5", "A6": "claude-sonnet-5", "A7": "claude-sonnet-5",
            "A8": "claude-sonnet-4-6", "A9": "claude-sonnet-4-6"}


class ArmsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        arms.generate_all(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_nine_roots(self):
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), sorted(EXPECTED))

    def test_settings_and_models(self):
        for arm, sub_model in EXPECTED.items():
            root = self.base / arm
            s = json.loads((root / "settings.json").read_text())
            self.assertEqual(s["model"], "claude-opus-5-5")
            self.assertEqual(s["effortLevel"], "high")
            self.assertEqual((s["promptCacheTtl"], s["subagentPromptCacheTtl"]), ("1h", "5m"))
            self.assertIs(s["autoMemoryEnabled"], False)
            if sub_model is None:
                self.assertEqual(s["permissions"]["deny"], ["Agent"])
                self.assertFalse((root / "agents").exists())
            else:
                self.assertNotIn("permissions", s)
                self.assertEqual(s["modelSettings"][sub_model]["effortLevel"], "high")
                names = sorted(p.stem for p in (root / "agents").glob("*.md"))
                self.assertEqual(names, sorted(arms.SUBAGENT_NAMES))
                self.assertEqual(set(arms.agent_models(root).values()), {sub_model})

    def test_claude_md_delegation(self):
        for arm in EXPECTED:
            text = (self.base / arm / "CLAUDE.md").read_text()
            self.assertNotIn("{{", text)
            self.assertIn("TASK.md", text)
            spec = arms.ARMS[arm]
            self.assertEqual(EAGER in text, spec.delegation == "eager")
            self.assertEqual(CONSERVATIVE in text, spec.delegation == "conservative")

    def test_readonly_agents_have_no_write_tools(self):
        for name in ("explorer", "reviewer"):
            text = (self.base / "A4" / "agents" / f"{name}.md").read_text()
            tools = [l for l in text.splitlines() if l.startswith("tools:")][0]
            for banned in ("Edit", "Write", "Bash"):
                self.assertNotIn(banned, tools)

    def test_unknown_arm(self):
        with self.assertRaises(arms.ArmError):
            arms.get_arm("A10")


if __name__ == "__main__":
    unittest.main()
