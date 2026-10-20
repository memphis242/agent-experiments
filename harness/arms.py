#!/usr/bin/env python3
"""Generate the per-arm configuration roots.

An arm whose main agent is Claude Code uses its root as CLAUDE_CONFIG_DIR; it holds
only that arm's settings.json, CLAUDE.md, and either agents/ (Claude Code subagents)
or codex/ (headless Codex subagents started through the codex-agent command). An arm
whose main agent is a headless Codex session (solo, no subagents) holds only codex/:
the Codex configuration and the working agreement as AGENTS.md. Templates live under
harness/arms/templates/ so they can be reviewed as plain files.
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "arms" / "templates"
CODEX_AGENT_SCRIPT: Path = Path(__file__).resolve().parent / "codex_agent.py"
CLAUDE_AGENT_SCRIPT: Path = Path(__file__).resolve().parent / "claude_agent.py"
DEFAULT_ARMS_DIR: Path = Path("/workspace/agent-experiments-arms")

OPUS_55 = "claude-opus-5-5"
SONNET_55 = "claude-sonnet-5-5"
SONNET_5 = "claude-sonnet-5"
SONNET_46 = "claude-sonnet-4-6"
HAIKU_55 = "claude-haiku-5-5"

GPT_61_SOL = "gpt-6.1-sol"
GPT_6_LUNA = "gpt-6-luna"
GPT_56_SOL = "gpt-5.6-sol"
GPT_56_LUNA = "gpt-5.6-luna"

KNOWN_MODELS: frozenset[str] = frozenset({OPUS_55, SONNET_55, SONNET_5, SONNET_46, HAIKU_55})
# Models a headless Codex session (subagent or main agent) may run on, with their display names.
CODEX_MODELS: Mapping[str, str] = MappingProxyType({GPT_61_SOL: "GPT-6.1 Sol", GPT_6_LUNA: "GPT-6 Luna",
                                                     GPT_56_SOL: "GPT-5.6 Sol", GPT_56_LUNA: "GPT-5.6 Luna"})
SUBAGENT_HARNESSES: tuple[str, ...] = ("claude", "codex")
MAIN_HARNESSES: tuple[str, ...] = ("claude", "codex")
EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

# Display names of the Claude models, for the roster a Codex main agent reads.
CLAUDE_MODEL_NAMES: Mapping[str, str] = MappingProxyType({
    OPUS_55: "Claude Opus 5.5", SONNET_55: "Claude Sonnet 5.5", SONNET_5: "Claude Sonnet 5",
    SONNET_46: "Claude Sonnet 4.6", HAIKU_55: "Claude Haiku 5.5"})

SUBAGENT_NAMES: tuple[str, ...] = ("explorer", "implementer", "tester", "debugger", "reviewer")
# Every agent template that exists; SUBAGENT_NAMES is the default roster.
KNOWN_AGENT_NAMES: tuple[str, ...] = (*SUBAGENT_NAMES, "small-implementer")
# Agent types Claude Code 2.1.302 ships with. A multi-model arm denies each so that
# only the arm's own agents can be spawned.
BUILTIN_AGENT_TYPES: tuple[str, ...] = ("Explore", "Plan", "general-purpose", "statusline-setup", "claude-code-guide")
# Tools denied in every arm. Agent, Skill, WebSearch, and WebFetch stay available;
# A1 additionally denies Agent. These either spawn or message other agents
# outside the Agent tool, schedule work, or change the working tree location,
# which would break the one-factor-per-arm design or the run's isolation.
DENIED_TOOLS: tuple[str, ...] = (
    "Workflow", "SendMessage", "ListAgents", "RemoteTrigger", "CronCreate", "CronDelete",
    "CronList", "ScheduleWakeup", "EnterWorktree", "ExitWorktree", "PushNotification",
    "EndConversation",
)
PROMPT_CACHE_TTL = "1h"
SUBAGENT_PROMPT_CACHE_TTL = "5m"
# The Bash tool's ceiling on a command's timeout in arms whose subagents run as
# codex-agent commands, so that the main agent can wait on one in the foreground.
CODEX_BASH_MAX_TIMEOUT_MS = 2 * 60 * 60 * 1000
# The codex/ entries of a Codex arm root: Codex configuration, the instructions
# every Codex subagent gets, one role prompt per agent, and the launcher.
CODEX_DIR = "codex"
CODEX_AGENT_COMMAND = "codex-agent"
# The codex/ entry holding a Codex main agent's subagents' own CODEX_HOME inputs (config.toml, AGENTS.md).
CODEX_SUBAGENT_DIR = "subagent"
# The claude/ entries of a Codex-main arm whose subagents are headless Claude Code sessions: the
# CLAUDE_CONFIG_DIR inputs (settings.json, CLAUDE.md), the roster, one role prompt per agent, and the
# claude-agent launcher.
CLAUDE_DIR = "claude"
CLAUDE_AGENT_COMMAND = "claude-agent"
# Raises the Codex shell tool's wait on a running command so that a foreground subagent call can
# last up to CODEX_BASH_MAX_TIMEOUT_MS (Codex's default wait is five minutes).
CODEX_WAIT_KEY = "background_terminal_max_timeout"


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    subagent_model: str | None  # None: the Agent tool is denied
    delegation: str | None      # "eager", "conservative", "roles", "roles-debugger", "tiered", or None
    main_model: str = OPUS_55
    main_effort: str = "high"
    subagent_effort: str = "high"
    agents: tuple[str, ...] = SUBAGENT_NAMES  # subagent definitions written for the arm
    # (agent name, model) pairs overriding subagent_model for those agents.
    agent_models: tuple[tuple[str, str], ...] = ()
    # "claude": Claude Code subagents through the Agent tool; "codex": headless
    # Codex sessions started with the codex-agent command, the Agent tool denied.
    subagent_harness: str = "claude"
    # "claude": the main agent is Claude Code (main_model a Claude model); "codex": a
    # headless Codex session (main_model a Codex model), solo and without subagents.
    main_harness: str = "claude"

    def __post_init__(self) -> None:
        assert (self.subagent_model is None) == (self.delegation is None)
        assert self.delegation in (None, "eager", "conservative", "roles", "roles-debugger", "tiered")
        assert self.main_harness in MAIN_HARNESSES
        if self.main_harness == "codex":
            assert self.main_model in CODEX_MODELS
            assert self.subagent_model is not None or self.subagent_harness == "claude"
        else:
            assert self.main_model in KNOWN_MODELS
        assert self.subagent_harness in SUBAGENT_HARNESSES
        if self.subagent_harness == "codex":
            assert self.subagent_model in CODEX_MODELS
        else:
            assert self.subagent_model is None or self.subagent_model in KNOWN_MODELS
        assert self.main_effort in EFFORT_LEVELS and self.subagent_effort in EFFORT_LEVELS
        assert set(self.agents) <= set(KNOWN_AGENT_NAMES) and len(set(self.agents)) == len(self.agents)
        overridden = [name for name, _ in self.agent_models]
        assert len(set(overridden)) == len(overridden) and set(overridden) <= set(self.agents)
        assert not self.agent_models or self.subagent_model is not None
        known = CODEX_MODELS if self.subagent_harness == "codex" else KNOWN_MODELS
        assert all(model in known for _, model in self.agent_models)
        assert all(model != self.main_model or self.subagent_effort == self.main_effort
                   for model in self.subagent_models)

    def model_for(self, agent: str) -> str:
        """The model the named subagent runs on."""
        assert self.subagent_model is not None and agent in self.agents
        return dict(self.agent_models).get(agent, self.subagent_model)

    @property
    def subagent_models(self) -> frozenset[str]:
        """Every model a subagent of this arm runs on; empty without subagents."""
        if self.subagent_model is None:
            return frozenset()
        return frozenset(self.model_for(name) for name in self.agents)

    @property
    def multi_model(self) -> bool:
        """Whether subagents run on more than one model."""
        return len(self.subagent_models) > 1

    @property
    def codex(self) -> bool:
        """Whether subagents are headless Codex sessions rather than Claude Code subagents."""
        return self.subagent_harness == "codex"

    @property
    def codex_main(self) -> bool:
        """Whether the main agent is a headless Codex session rather than Claude Code."""
        return self.main_harness == "codex"

    @property
    def uses_codex(self) -> bool:
        """Whether any agent of this arm is a headless Codex session."""
        return self.codex or self.codex_main

    @property
    def claude_agents(self) -> bool:
        """Whether a Codex main agent's subagents are headless Claude Code sessions (claude-agent)."""
        return self.codex_main and self.subagent_model is not None and self.subagent_harness == "claude"

    @property
    def command_subagents(self) -> bool:
        """Whether subagents are started with a shell command (codex-agent or claude-agent) rather
        than Claude Code's Agent tool."""
        return self.codex or self.claude_agents

    @property
    def claude_subagent_models(self) -> frozenset[str]:
        """Models Claude Code subagents of this arm may run on; empty for a Codex arm."""
        return frozenset() if self.codex else self.subagent_models


def effort_map(arm: ArmSpec) -> Mapping[str, str]:
    """Model to effort level for the arm's main agent and, when present, its Claude Code subagents."""
    efforts = {arm.main_model: arm.main_effort}
    for model in arm.claude_subagent_models:
        efforts[model] = arm.subagent_effort
    return MappingProxyType(efforts)


TIERED_AGENTS: tuple[str, ...] = ("implementer", "small-implementer", "tester")
TIERED_MODELS: tuple[tuple[str, str], ...] = (("small-implementer", HAIKU_55), ("tester", HAIKU_55))
CODEX_ROLES_AGENTS: tuple[str, ...] = ("implementer", "tester", "debugger")
CODEX_TIERED_MODELS: tuple[tuple[str, str], ...] = (("small-implementer", GPT_6_LUNA), ("tester", GPT_6_LUNA))
CODEX_56_TIERED_MODELS: tuple[tuple[str, str], ...] = (("small-implementer", GPT_56_LUNA), ("tester", GPT_56_LUNA))

ARMS: Mapping[str, ArmSpec] = MappingProxyType({
    a.arm_id: a for a in (
        ArmSpec("A1", None, None),
        ArmSpec("A2", OPUS_55, "eager"),
        ArmSpec("A3", OPUS_55, "conservative"),
        ArmSpec("A4", SONNET_55, "eager"),
        ArmSpec("A5", SONNET_55, "conservative"),
        ArmSpec("A6", SONNET_5, "eager"),
        ArmSpec("A7", SONNET_5, "conservative"),
        ArmSpec("A8", SONNET_46, "eager"),
        ArmSpec("A9", SONNET_46, "conservative"),
        ArmSpec("A10", None, None, main_model=SONNET_55, main_effort="xhigh"),
        ArmSpec("A11", HAIKU_55, "roles", main_model=SONNET_55, agents=("implementer", "tester")),
        ArmSpec("A12", None, None, main_model=SONNET_55, main_effort="high"),
        ArmSpec("A13", None, None, main_effort="medium"),
        ArmSpec("A14", SONNET_55, "tiered", main_effort="low", agents=TIERED_AGENTS, agent_models=TIERED_MODELS),
        ArmSpec("A15", SONNET_55, "tiered", main_effort="medium", agents=TIERED_AGENTS, agent_models=TIERED_MODELS),
        ArmSpec("A16", None, None, main_model=HAIKU_55, main_effort="high"),
        ArmSpec("A17", None, None, main_model=HAIKU_55, main_effort="xhigh"),
        ArmSpec("A18", None, None, main_model=SONNET_5, main_effort="medium"),
        ArmSpec("A19", None, None, main_model=SONNET_5, main_effort="high"),
        ArmSpec("A20", None, None, main_model=SONNET_46, main_effort="high"),
        ArmSpec("A21", GPT_61_SOL, "eager", subagent_harness="codex"),
        ArmSpec("A22", GPT_61_SOL, "roles-debugger", agents=CODEX_ROLES_AGENTS, subagent_harness="codex"),
        ArmSpec("A23", GPT_61_SOL, "tiered", main_effort="low", agents=TIERED_AGENTS,
                agent_models=CODEX_TIERED_MODELS, subagent_harness="codex"),
        ArmSpec("A24", GPT_6_LUNA, "tiered", main_effort="low", agents=TIERED_AGENTS, subagent_harness="codex"),
        ArmSpec("A25", GPT_56_SOL, "tiered", main_effort="low", agents=TIERED_AGENTS,
                agent_models=CODEX_56_TIERED_MODELS, subagent_harness="codex"),
        ArmSpec("A26", None, None, main_model=GPT_61_SOL, main_harness="codex"),
        ArmSpec("A27", None, None, main_model=GPT_6_LUNA, main_harness="codex"),
        ArmSpec("A28", None, None, main_model=GPT_56_SOL, main_harness="codex"),
        ArmSpec("A29", None, None, main_model=GPT_56_LUNA, main_harness="codex"),
        ArmSpec("A30", GPT_6_LUNA, "tiered", main_model=GPT_61_SOL, main_effort="low", main_harness="codex",
                agents=TIERED_AGENTS, subagent_harness="codex"),
        ArmSpec("A31", OPUS_55, "tiered", main_model=GPT_61_SOL, main_effort="low", main_harness="codex",
                agents=TIERED_AGENTS, agent_models=TIERED_MODELS),
        ArmSpec("A32", HAIKU_55, "tiered", main_model=GPT_61_SOL, main_effort="low", main_harness="codex",
                agents=TIERED_AGENTS),
    )
})


class ArmError(Exception):
    """Raised for an unknown arm or a missing template."""


def get_arm(arm_id: str) -> ArmSpec:
    try:
        return ARMS[arm_id]
    except KeyError:
        raise ArmError(f"unknown arm {arm_id!r}; known arms: {', '.join(ARMS)}") from None


def _read_template(relative: str) -> str:
    path = TEMPLATES_DIR / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArmError(f"cannot read template {path}: {exc}") from exc


def build_settings(arm: ArmSpec) -> dict[str, object]:
    """The settings.json content for an arm whose main agent is Claude Code."""
    assert not arm.codex_main
    model_settings = {model: {"effortLevel": effort} for model, effort in effort_map(arm).items()}
    settings: dict[str, object] = {
        "model": arm.main_model,
        "effortLevel": arm.main_effort,
        "modelSettings": model_settings,
        "promptCacheTtl": PROMPT_CACHE_TTL,
        "subagentPromptCacheTtl": SUBAGENT_PROMPT_CACHE_TTL,
        "autoMemoryEnabled": False,
    }
    if arm.subagent_model is None:
        settings["permissions"] = {"deny": ["Agent", *DENIED_TOOLS]}
    elif arm.codex:
        # Subagents are codex-agent commands run through the Bash tool, so the Agent tool is denied.
        settings["permissions"] = {"deny": ["Agent", *DENIED_TOOLS]}
        settings["env"] = {"BASH_MAX_TIMEOUT_MS": str(CODEX_BASH_MAX_TIMEOUT_MS)}
    elif arm.multi_model:
        # FORCE would override the per-agent frontmatter models, so the built-in
        # agent types are denied and only the arm's own agents can be spawned.
        deny = [*DENIED_TOOLS, *(f"Agent({name})" for name in BUILTIN_AGENT_TYPES)]
        settings["permissions"] = {"deny": deny}
    else:
        settings["permissions"] = {"deny": list(DENIED_TOOLS)}
        # Built-in agent types (Explore, Plan, general-purpose) would otherwise
        # inherit the main model; FORCE makes this model win over per-call overrides.
        settings["env"] = {
            "CLAUDE_CODE_SUBAGENT_MODEL": arm.subagent_model,
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
        }
    return settings


def working_agreement() -> str:
    """The base CLAUDE.md text without its delegation section."""
    base = _read_template("CLAUDE.base.md")
    assert "## Delegation" in base
    return base.split("## Delegation")[0].rstrip() + "\n"


def build_claude_md(arm: ArmSpec) -> str:
    """CLAUDE.md text: the base text plus the arm's delegation paragraph and, for a Codex
    arm, the section on starting subagents with codex-agent."""
    assert not arm.codex_main
    base = _read_template("CLAUDE.base.md")
    assert "{{DELEGATION}}" in base
    if arm.delegation is None:
        # No subagents: the delegation section is dropped entirely.
        return working_agreement()
    paragraph = _read_template(f"delegation-{arm.delegation}.txt").strip()
    text = base.replace("{{DELEGATION}}", paragraph)
    if arm.codex:
        text = text.rstrip() + "\n\n" + build_codex_section(arm)
    return text


def _split_agent_template(name: str) -> tuple[dict[str, str], str]:
    """(frontmatter fields, body) of agents/<name>.md."""
    text = _read_template(f"agents/{name}.md")
    head, sep, body = text.partition("\n---\n")
    assert text.startswith("---\n") and sep, f"agents/{name}.md lacks frontmatter"
    fields = dict(line.split(":", 1) for line in head.splitlines()[1:] if ":" in line)
    return {k.strip(): v.strip() for k, v in fields.items()}, body.strip() + "\n"


def codex_role_read_only(name: str) -> bool:
    """A role whose Claude Code definition grants no editing tool runs in Codex's read-only sandbox."""
    tools = {t.strip() for t in _split_agent_template(name)[0]["tools"].split(",")}
    return not tools & {"Edit", "Write", "Bash"}


def build_codex_section(arm: ArmSpec) -> str:
    """The instructions section that tells the main agent how to start subagents with a command:
    Codex subagents for a Claude Code main agent (CLAUDE.md), or Codex or Claude Code subagents
    for a Codex main agent (AGENTS.md), where waiting is described for Codex's shell tool."""
    assert arm.command_subagents and arm.subagent_model is not None
    names = CODEX_MODELS if arm.codex else CLAUDE_MODEL_NAMES
    vendor = "OpenAI" if arm.codex else "Anthropic"
    template = _read_template("codex-main-codex-subagents.md" if arm.codex and arm.codex_main
                              else "codex-main-claude-subagents.md" if arm.codex_main
                              else "codex-subagents.md")

    def roster_line(name: str) -> str:
        label = f" ({names[arm.model_for(name)]})" if arm.multi_model else ""
        return f"- {name}{label}: {_split_agent_template(name)[0]['description']}"

    roster = "\n".join(roster_line(name) for name in arm.agents)
    runtime = (f"{vendor} models, each agent on the model named in its entry below," if arm.multi_model
               else f"{vendor}'s {names[arm.subagent_model]}")
    text = (template.replace("{{RUNTIME}}", runtime)
            .replace("{{EFFORT}}", arm.subagent_effort)
            .replace("{{COMMAND}}", CODEX_AGENT_COMMAND if arm.codex else CLAUDE_AGENT_COMMAND)
            .replace("{{EXAMPLE_AGENT}}", "implementer" if "implementer" in arm.agents else arm.agents[0])
            .replace("{{AGENTS}}", roster)
            .replace("{{TIMEOUT_MS}}", str(CODEX_BASH_MAX_TIMEOUT_MS)))
    assert "{{" not in text
    return text


def _codex_config(model: str, effort: str) -> str:
    """The config.toml of a Codex session running `model` at `effort`."""
    assert model in CODEX_MODELS and effort in EFFORT_LEVELS
    config = _read_template("codex/config.toml").replace("{{MODEL}}", model).replace("{{EFFORT}}", effort)
    assert "{{" not in config
    return config


def build_main_instructions(arm: ArmSpec) -> str:
    """The AGENTS.md text of an arm whose main agent is Codex: what a Claude main agent of the
    matching arm gets as CLAUDE.md (the working agreement and, with subagents, the arm's delegation
    paragraph and the section on starting them), with the section written for Codex's shell tool."""
    assert arm.codex_main
    if arm.delegation is None:
        return working_agreement()
    base = _read_template("CLAUDE.base.md")
    assert "{{DELEGATION}}" in base
    text = base.replace("{{DELEGATION}}", _read_template(f"delegation-{arm.delegation}.txt").strip())
    return text.rstrip() + "\n\n" + build_codex_section(arm)


def build_codex_main_files(arm: ArmSpec) -> dict[str, str]:
    """Map of path under codex/ to content for an arm whose main agent is Codex: config.toml,
    and AGENTS.md (see build_main_instructions). With Codex subagents it adds what build_codex_files
    gives them (agent.json, roles/<name>.md) and subagent/config.toml and subagent/AGENTS.md, the
    inputs of the subagents' own CODEX_HOME (which, unlike the main agent's, holds the plain working
    agreement). The launcher itself is copied by generate_arm."""
    assert arm.codex_main
    config = _codex_config(arm.main_model, arm.main_effort)
    if arm.subagent_model is not None:
        # Top-level keys precede the first table; the wait applies to the main agent's shell tool.
        anchor = "\n# Subagents cannot start subagents of their own.\n[agents]"
        assert config.count(anchor) == 1
        config = config.replace(anchor, f"\n# Foreground subagent calls can last up to two hours.\n"
                                f"{CODEX_WAIT_KEY} = {CODEX_BASH_MAX_TIMEOUT_MS}\n{anchor}")
    files = {"config.toml": config, "AGENTS.md": build_main_instructions(arm)}
    if arm.codex:
        for relative, text in build_codex_files(arm).items():
            if relative in ("config.toml", "AGENTS.md"):
                relative = f"{CODEX_SUBAGENT_DIR}/{relative}"
            files[relative] = text
    return files


def _tools_of(name: str) -> list[str]:
    return [t.strip() for t in _split_agent_template(name)[0]["tools"].split(",")]


def build_claude_agent_files(arm: ArmSpec) -> dict[str, str]:
    """Map of path under claude/ to content for an arm whose Codex main agent has Claude Code
    subagents: settings.json (each subagent model's effort, the 5m cache TTL of a Claude Code
    subagent, the Agent and DENIED_TOOLS denied), CLAUDE.md (the working agreement), agent.json (the
    claude-agent launcher's roster), and roles/<name>.md (each agent's definition body, appended to
    the system prompt). The launcher itself is copied by generate_arm."""
    assert arm.claude_agents and arm.subagent_model is not None
    settings = {
        "effortLevel": arm.subagent_effort,
        "modelSettings": {m: {"effortLevel": arm.subagent_effort} for m in sorted(arm.subagent_models)},
        "promptCacheTtl": SUBAGENT_PROMPT_CACHE_TTL,
        "subagentPromptCacheTtl": SUBAGENT_PROMPT_CACHE_TTL,
        "autoMemoryEnabled": False,
        "permissions": {"deny": ["Agent", *DENIED_TOOLS]},
    }
    roster = {name: {"model": arm.model_for(name), "effort": arm.subagent_effort, "tools": _tools_of(name)}
              for name in arm.agents}
    files = {"settings.json": json.dumps(settings, indent=2) + "\n", "CLAUDE.md": working_agreement(),
             "agent.json": json.dumps({"effort": arm.subagent_effort, "agents": roster}, indent=2) + "\n"}
    for name in arm.agents:
        files[f"roles/{name}.md"] = _split_agent_template(name)[1]
    return files


def build_codex_files(arm: ArmSpec) -> dict[str, str]:
    """Map of path under codex/ to content for a Codex arm: config.toml, AGENTS.md (the
    working agreement every Codex subagent follows), agent.json (the codex-agent launcher's
    roster), and roles/<name>.md (each agent's Claude Code definition body, used as
    Codex developer instructions). The launcher itself is copied by generate_arm."""
    assert arm.codex and arm.subagent_model is not None
    config = _codex_config(arm.subagent_model, arm.subagent_effort)
    roster = {name: {"read_only": codex_role_read_only(name), "model": arm.model_for(name),
                     "effort": arm.subagent_effort} for name in arm.agents}
    files = {"config.toml": config, "AGENTS.md": working_agreement(),
             "agent.json": json.dumps({"model": arm.subagent_model, "effort": arm.subagent_effort,
                                       "agents": roster}, indent=2) + "\n"}
    for name in arm.agents:
        files[f"roles/{name}.md"] = _split_agent_template(name)[1]
    return files


def build_agents(arm: ArmSpec) -> dict[str, str]:
    """Map of agent file name to content; empty for an arm without Claude Code subagents."""
    if arm.subagent_model is None or arm.command_subagents:
        return {}
    agents: dict[str, str] = {}
    for name in arm.agents:
        text = _read_template(f"agents/{name}.md")
        assert "{{MODEL}}" in text
        agents[f"{name}.md"] = text.replace("{{MODEL}}", arm.model_for(name))
    return agents


def arm_root(arm_id: str, base_dir: Path = DEFAULT_ARMS_DIR) -> Path:
    return base_dir / get_arm(arm_id).arm_id


def _install_launcher(script: Path, launcher: Path) -> None:
    """Copy a launcher script to its bin/ path and make it executable."""
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(script, launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def generate_arm(arm_id: str, base_dir: Path = DEFAULT_ARMS_DIR) -> Path:
    """Write one arm root and return its path. Other content (projects/) is left alone."""
    arm = get_arm(arm_id)
    root = arm_root(arm_id, base_dir)
    root.mkdir(parents=True, exist_ok=True)
    if not arm.codex_main:
        (root / "settings.json").write_text(json.dumps(build_settings(arm), indent=2) + "\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text(build_claude_md(arm), encoding="utf-8")
    agents_dir = root / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
    agents = build_agents(arm)
    if agents:
        agents_dir.mkdir()
        for filename, text in agents.items():
            (agents_dir / filename).write_text(text, encoding="utf-8")
    codex_dir = root / CODEX_DIR
    if codex_dir.exists():
        shutil.rmtree(codex_dir)
    if arm.codex_main or arm.codex:
        for relative, text in (build_codex_main_files(arm) if arm.codex_main else build_codex_files(arm)).items():
            target = codex_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    if arm.codex:
        _install_launcher(CODEX_AGENT_SCRIPT, codex_dir / "bin" / CODEX_AGENT_COMMAND)
    claude_dir = root / CLAUDE_DIR
    if claude_dir.exists():
        shutil.rmtree(claude_dir)
    if arm.claude_agents:
        for relative, text in build_claude_agent_files(arm).items():
            target = claude_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        _install_launcher(CLAUDE_AGENT_SCRIPT, claude_dir / "bin" / CLAUDE_AGENT_COMMAND)
    return root


def generate_all(base_dir: Path = DEFAULT_ARMS_DIR) -> tuple[Path, ...]:
    return tuple(generate_arm(a, base_dir) for a in ARMS)


def agent_models(root: Path) -> dict[str, str]:
    """Map subagent name to its frontmatter model for a generated arm root."""
    models: dict[str, str] = {}
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return models
    for path in sorted(agents_dir.glob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            if line.startswith("model:"):
                models[path.stem] = line.split(":", 1)[1].strip()
                break
            if line.strip() == "---":
                break
    return models


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the experiment arm config roots.")
    parser.add_argument("--arms-dir", type=Path, default=DEFAULT_ARMS_DIR)
    parser.add_argument("--arm", action="append", choices=list(ARMS), help="limit to these arms (default: all)")
    args = parser.parse_args(argv)
    try:
        for arm_id in args.arm or list(ARMS):
            print(f"wrote {generate_arm(arm_id, args.arms_dir)}")
    except (ArmError, OSError) as exc:
        print(f"arms: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
