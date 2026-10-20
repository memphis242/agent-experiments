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
            "A8": "claude-sonnet-4-6", "A9": "claude-sonnet-4-6", "A10": None,
            "A11": "claude-haiku-5-5", "A12": None, "A13": None, "A14": "claude-sonnet-5-5",
            "A15": "claude-sonnet-5-5", "A16": None, "A17": None,
            "A18": None, "A19": None, "A20": None, "A21": "gpt-6.1-sol", "A22": "gpt-6.1-sol",
            "A23": "gpt-6.1-sol", "A24": "gpt-6-luna", "A25": "gpt-5.6-sol"}
CODEX_MAIN = {"A26": "gpt-6.1-sol", "A27": "gpt-6-luna", "A28": "gpt-5.6-sol", "A29": "gpt-5.6-luna"}
CODEX_AGENTS = {"A21": sorted(arms.SUBAGENT_NAMES), "A22": ["debugger", "implementer", "tester"],
                "A23": ["implementer", "small-implementer", "tester"],
                "A24": ["implementer", "small-implementer", "tester"],
                "A25": ["implementer", "small-implementer", "tester"]}
MAIN = {"A10": ("claude-sonnet-5-5", "xhigh"), "A11": ("claude-sonnet-5-5", "high"), "A12": ("claude-sonnet-5-5", "high"),
        "A13": ("claude-opus-5-5", "medium"), "A14": ("claude-opus-5-5", "low"),
        "A15": ("claude-opus-5-5", "medium"), "A16": ("claude-haiku-5-5", "high"),
        "A17": ("claude-haiku-5-5", "xhigh"), "A18": ("claude-sonnet-5", "medium"),
        "A19": ("claude-sonnet-5", "high"), "A20": ("claude-sonnet-4-6", "high"),
        "A23": ("claude-opus-5-5", "low"), "A24": ("claude-opus-5-5", "low"),
        "A25": ("claude-opus-5-5", "low")}
ROLES = ("Delegate selectively. Do exploration, planning, design decisions, debugging, review, and "
         "integration yourself.")
TIERED = "Delegate implementation, not thinking."


class ArmsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        arms.generate_all(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_roots(self):
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),
                         sorted(EXPECTED | CODEX_MAIN | {"A30": "", "A31": "", "A32": ""}))

    def test_settings_and_models(self):
        for arm, sub_model in EXPECTED.items():
            root = self.base / arm
            s = json.loads((root / "settings.json").read_text())
            main_model, main_effort = MAIN.get(arm, ("claude-opus-5-5", "high"))
            self.assertEqual(s["model"], main_model)
            self.assertEqual(s["effortLevel"], main_effort)
            self.assertEqual(s["modelSettings"][main_model], {"effortLevel": main_effort})
            self.assertEqual((s["promptCacheTtl"], s["subagentPromptCacheTtl"]), ("1h", "5m"))
            self.assertIs(s["autoMemoryEnabled"], False)
            if arm in ("A14", "A15"):
                self.assert_tiered(root, s)
                continue
            if arm in CODEX_AGENTS:
                self.assert_codex(arm, root, s)
                continue
            deny = s["permissions"]["deny"]
            for tool in arms.DENIED_TOOLS:
                self.assertIn(tool, deny)
            for kept in ("Skill", "WebSearch", "WebFetch"):
                self.assertNotIn(kept, deny)
            if sub_model is None:
                self.assertIn("Agent", deny)
                self.assertNotIn("env", s)
                self.assertFalse((root / "agents").exists())
            else:
                self.assertNotIn("Agent", deny)
                self.assertEqual(s["env"], {"CLAUDE_CODE_SUBAGENT_MODEL": sub_model,
                                            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"})
                self.assertEqual(s["modelSettings"][sub_model]["effortLevel"], "high")
                names = sorted(p.stem for p in (root / "agents").glob("*.md"))
                expected_names = ["implementer", "tester"] if arm == "A11" else sorted(arms.SUBAGENT_NAMES)
                self.assertEqual(names, expected_names)
                self.assertEqual(set(arms.agent_models(root).values()), {sub_model})

    def assert_codex(self, arm, root, s):
        self.assertIn("Agent", s["permissions"]["deny"])
        self.assertEqual(s["env"], {"BASH_MAX_TIMEOUT_MS": "7200000"})
        self.assertEqual(s["modelSettings"], {s["model"]: {"effortLevel": s["effortLevel"]}})
        self.assertFalse((root / "agents").exists())
        codex = root / "codex"
        self.assertEqual(sorted(p.stem for p in (codex / "roles").glob("*.md")), CODEX_AGENTS[arm])
        roster = json.loads((codex / "agent.json").read_text())
        self.assertEqual((roster["model"], roster["effort"]), (EXPECTED[arm], "high"))
        small = {"A23": "gpt-6-luna", "A25": "gpt-5.6-luna"}.get(arm, EXPECTED[arm])
        for name, agent in roster["agents"].items():
            self.assertEqual((agent["model"], agent["effort"]),
                             (small if name in ("small-implementer", "tester") else EXPECTED[arm], "high"))
        self.assertEqual({n for n, a in roster["agents"].items() if a["read_only"]},
                         {"explorer", "reviewer"} & set(CODEX_AGENTS[arm]))
        config = (codex / "config.toml").read_text()
        for line in (f'model = "{EXPECTED[arm]}"', 'model_reasoning_effort = "high"', 'web_search = "disabled"',
                     "multi_agent = false", "[agents]\nenabled = false"):
            self.assertIn(line, config)
        self.assertNotIn("{{", config)
        self.assertNotIn("Delegation", (codex / "AGENTS.md").read_text())
        launcher = codex / "bin" / "codex-agent"
        self.assertTrue(launcher.stat().st_mode & 0o111)
        self.assertEqual(launcher.read_bytes(), arms.CODEX_AGENT_SCRIPT.read_bytes())
        text = (root / "CLAUDE.md").read_text()
        self.assertIn("## Subagents", text)
        if arm in ("A23", "A25"):
            self.assertNotIn(" Sol at high", text)
            self.assertIn(f"- implementer ({arms.CODEX_MODELS[EXPECTED[arm]]}): ", text)
            self.assertIn(f"- tester ({arms.CODEX_MODELS[small]}): ", text)
            return
        self.assertIn(f"{arms.CODEX_MODELS[EXPECTED[arm]]} at high reasoning effort", text)
        for name in CODEX_AGENTS[arm]:
            self.assertIn(f"- {name}: ", text)

    def assert_tiered(self, root, s):
        deny = s["permissions"]["deny"]
        self.assertNotIn("env", s)
        self.assertNotIn("Agent", deny)
        for name in arms.BUILTIN_AGENT_TYPES:
            self.assertIn(f"Agent({name})", deny)
        self.assertEqual(arms.agent_models(root), {"implementer": "claude-sonnet-5-5",
                                                   "small-implementer": "claude-haiku-5-5",
                                                   "tester": "claude-haiku-5-5"})
        self.assertEqual(s["modelSettings"], {"claude-opus-5-5": {"effortLevel": s["effortLevel"]},
                                              "claude-sonnet-5-5": {"effortLevel": "high"},
                                              "claude-haiku-5-5": {"effortLevel": "high"}})

    def test_claude_md_delegation(self):
        for arm in EXPECTED:
            text = (self.base / arm / "CLAUDE.md").read_text()
            self.assertNotIn("{{", text)
            self.assertIn("TASK.md", text)
            spec = arms.ARMS[arm]
            self.assertEqual(EAGER in text, spec.delegation == "eager")
            self.assertEqual(CONSERVATIVE in text, spec.delegation == "conservative")
            self.assertEqual(ROLES in text, spec.delegation in ("roles", "roles-debugger"))
            self.assertEqual("## Subagents" in text, spec.codex)
            self.assertEqual(TIERED in text, spec.delegation == "tiered")

    def test_readonly_agents_have_no_write_tools(self):
        for name in ("explorer", "reviewer"):
            text = (self.base / "A4" / "agents" / f"{name}.md").read_text()
            tools = [l for l in text.splitlines() if l.startswith("tools:")][0]
            for banned in ("Edit", "Write", "Bash"):
                self.assertNotIn(banned, tools)

    def test_codex_arm_spec(self):
        self.assertEqual(dict(arms.effort_map(arms.ARMS["A21"])), {"claude-opus-5-5": "high"})
        self.assertEqual(arms.ARMS["A21"].claude_subagent_models, frozenset())
        with self.assertRaises(AssertionError):  # a Codex arm runs a Codex model
            arms.ArmSpec("X", arms.SONNET_55, "eager", subagent_harness="codex")
        with self.assertRaises(AssertionError):  # per-agent overrides on a Codex arm are Codex models too
            arms.ArmSpec("X", arms.GPT_61_SOL, "tiered", agents=arms.TIERED_AGENTS,
                         agent_models=arms.TIERED_MODELS, subagent_harness="codex")
        self.assertEqual(dict(arms.effort_map(arms.ARMS["A23"])), {"claude-opus-5-5": "low"})
        self.assertEqual(arms.ARMS["A23"].subagent_models, {arms.GPT_61_SOL, arms.GPT_6_LUNA})
        with self.assertRaises(AssertionError):  # and a Claude arm a Claude model
            arms.ArmSpec("X", arms.GPT_61_SOL, "eager")

    def test_codex_main_arm_roots(self):
        for arm, model in CODEX_MAIN.items():
            root = self.base / arm
            self.assertEqual([p.name for p in root.iterdir()], ["codex"])  # no Claude Code configuration
            self.assertEqual(sorted(p.name for p in (root / "codex").iterdir()), ["AGENTS.md", "config.toml"])
            config = (root / "codex" / "config.toml").read_text()
            for line in (f'model = "{model}"', 'model_reasoning_effort = "high"', 'web_search = "disabled"',
                         'approval_policy = "never"', "multi_agent = false", "[agents]\nenabled = false"):
                self.assertIn(line, config)
            self.assertNotIn("{{", config)
            # The same working agreement a Claude solo arm's CLAUDE.md holds.
            agreement = (root / "codex" / "AGENTS.md").read_text()
            self.assertEqual(agreement, (self.base / "A1" / "CLAUDE.md").read_text())
            self.assertNotIn("Delegation", agreement)

    def test_codex_main_arm_spec(self):
        spec = arms.ARMS["A26"]
        self.assertTrue(spec.codex_main and spec.uses_codex and not spec.codex)
        self.assertEqual(dict(arms.effort_map(spec)), {"gpt-6.1-sol": "high"})
        self.assertEqual(spec.subagent_models, frozenset())
        self.assertFalse(arms.ARMS["A1"].codex_main or arms.ARMS["A21"].codex_main)
        self.assertTrue(arms.ARMS["A21"].uses_codex and not arms.ARMS["A1"].uses_codex)
        with self.assertRaises(AssertionError):  # the main model must be a Codex model
            arms.ArmSpec("X", None, None, main_model=arms.OPUS_55, main_harness="codex")
        with self.assertRaises(AssertionError):  # and a Claude main agent a Claude model
            arms.ArmSpec("X", None, None, main_model=arms.GPT_6_LUNA)
        with self.assertRaises(AssertionError):  # no subagents, so no per-agent models
            arms.ArmSpec("X", None, None, main_model=arms.GPT_61_SOL, main_harness="codex",
                         agent_models=(("tester", arms.GPT_6_LUNA),))
        with self.assertRaises(AssertionError):
            arms.ArmSpec("X", None, None, main_model=arms.GPT_61_SOL, main_harness="gemini")
        with self.assertRaises(AssertionError):  # Claude-side files exist only for a Claude main agent
            arms.build_settings(spec)

    def test_codex_main_delegating_roots(self):
        """A Codex main agent's AGENTS.md is what the matching Claude main agent's CLAUDE.md is, up to
        the subagents section, which is written for Codex's shell tool."""
        for arm, claude_arm in (("A30", "A24"), ("A31", "A14"), ("A32", "A14")):
            text = (self.base / arm / "codex" / "AGENTS.md").read_text()
            head, _, section = text.partition("\n\n## Subagents\n")
            self.assertEqual(head.rstrip(), (self.base / claude_arm / "CLAUDE.md").read_text().split("\n\n## Subagents")[0].rstrip())
            self.assertIn("Delegate implementation, not thinking.", head)
            self.assertIn("`wait` for all of them in that same command", section)
            self.assertIn("no `pgrep`, `ps` or polling loop", section)
            for claude_only in ("run_in_background", "Bash timeout", "notified"):
                self.assertNotIn(claude_only, section)
            self.assertNotIn("{{", text)
            self.assertTrue(arms.ARMS[arm].codex_main and arms.ARMS[arm].command_subagents)
            self.assertEqual(arms.ARMS[arm].main_effort, "low")
            self.assertEqual(arms.ARMS[arm].subagent_effort, "high")
        self.assertIn("run_in_background", (self.base / "A24" / "CLAUDE.md").read_text())  # unchanged for A24

    def test_a30_codex_subagents_have_their_own_home_inputs(self):
        codex = self.base / "A30" / "codex"
        self.assertEqual(sorted(p.name for p in (self.base / "A30").iterdir()), ["codex"])
        self.assertTrue((codex / "bin" / "codex-agent").stat().st_mode & 0o111)
        self.assertEqual((codex / "bin" / "codex-agent").read_bytes(), arms.CODEX_AGENT_SCRIPT.read_bytes())
        self.assertEqual((codex / "subagent" / "AGENTS.md").read_text(), arms.working_agreement())
        self.assertIn('model = "gpt-6-luna"', (codex / "subagent" / "config.toml").read_text())
        main = (codex / "config.toml").read_text()
        self.assertIn('model = "gpt-6.1-sol"', main)
        self.assertIn('model_reasoning_effort = "low"', main)
        self.assertIn("background_terminal_max_timeout = 7200000", main)
        self.assertLess(main.index("background_terminal_max_timeout"), main.index("[agents]"))
        self.assertNotIn("background_terminal_max_timeout", (self.base / "A26" / "codex" / "config.toml").read_text())
        roster = json.loads((codex / "agent.json").read_text())
        self.assertEqual({n: (a["model"], a["effort"]) for n, a in roster["agents"].items()},
                         {n: ("gpt-6-luna", "high") for n in arms.TIERED_AGENTS})
        text = (codex / "AGENTS.md").read_text()
        self.assertIn("OpenAI's GPT-6 Luna at high reasoning effort", text)

    def test_claude_agent_arms(self):
        for arm, models in (("A31", {"implementer": "claude-opus-5-5", "small-implementer": "claude-haiku-5-5",
                                     "tester": "claude-haiku-5-5"}),
                            ("A32", {n: "claude-haiku-5-5" for n in arms.TIERED_AGENTS})):
            root = self.base / arm
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["claude", "codex"])
            self.assertEqual(sorted(p.name for p in (root / "codex").iterdir()), ["AGENTS.md", "config.toml"])
            claude = root / "claude"
            launcher = claude / "bin" / "claude-agent"
            self.assertTrue(launcher.stat().st_mode & 0o111)
            self.assertEqual(launcher.read_bytes(), arms.CLAUDE_AGENT_SCRIPT.read_bytes())
            roster = json.loads((claude / "agent.json").read_text())["agents"]
            self.assertEqual({n: a["model"] for n, a in roster.items()}, models)
            self.assertEqual({a["effort"] for a in roster.values()}, {"high"})
            self.assertEqual(roster["tester"]["tools"], ["Read", "Edit", "Write", "Grep", "Glob", "Bash"])
            settings = json.loads((claude / "settings.json").read_text())
            self.assertEqual(settings["modelSettings"], {m: {"effortLevel": "high"} for m in set(models.values())})
            self.assertEqual(settings["permissions"]["deny"], ["Agent", *arms.DENIED_TOOLS])
            self.assertEqual(settings["promptCacheTtl"], "5m")
            self.assertEqual((claude / "CLAUDE.md").read_text(), arms.working_agreement())
            for name in arms.TIERED_AGENTS:
                self.assertEqual((claude / "roles" / f"{name}.md").read_text(),
                                 arms._split_agent_template(name)[1])
            text = (root / "codex" / "AGENTS.md").read_text()
            self.assertIn("headless Claude Code sessions", text)
            self.assertIn("`claude-agent`", text)
        self.assertIn("Anthropic's Claude Haiku 5.5 at high effort", (self.base / "A32" / "codex" / "AGENTS.md").read_text())
        self.assertIn("- implementer (Claude Opus 5.5): ", (self.base / "A31" / "codex" / "AGENTS.md").read_text())

    def test_unknown_arm(self):
        with self.assertRaises(arms.ArmError):
            arms.get_arm("A99")

    def test_effort_map(self):
        self.assertEqual(dict(arms.effort_map(arms.ARMS["A1"])), {"claude-opus-5-5": "high"})
        self.assertEqual(dict(arms.effort_map(arms.ARMS["A10"])), {"claude-sonnet-5-5": "xhigh"})
        self.assertEqual(dict(arms.effort_map(arms.ARMS["A11"])),
                         {"claude-sonnet-5-5": "high", "claude-haiku-5-5": "high"})

        self.assertEqual(dict(arms.effort_map(arms.ARMS["A14"])),
                         {"claude-opus-5-5": "low", "claude-sonnet-5-5": "high", "claude-haiku-5-5": "high"})

    def test_per_agent_model_validation(self):
        tiered = arms.ARMS["A14"]
        self.assertEqual(tiered.subagent_models, {"claude-sonnet-5-5", "claude-haiku-5-5"})
        self.assertTrue(tiered.multi_model)
        self.assertFalse(arms.ARMS["A11"].multi_model)
        hash(tiered)
        with self.assertRaises(AssertionError):  # override for an agent the arm does not have
            arms.ArmSpec("X", arms.SONNET_55, "tiered", agents=("implementer",),
                         agent_models=(("tester", arms.HAIKU_55),))
        with self.assertRaises(AssertionError):  # a subagent on the main model must share its effort
            arms.ArmSpec("X", arms.SONNET_55, "tiered", agents=("implementer", "tester"),
                         agent_models=(("tester", arms.OPUS_55),), main_effort="low")


if __name__ == "__main__":
    unittest.main()
