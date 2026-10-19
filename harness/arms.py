#!/usr/bin/env python3
"""Generate the per-arm Claude Code configuration roots.

Each arm root is used as CLAUDE_CONFIG_DIR and holds only that arm's
settings.json, CLAUDE.md, and either agents/ (Claude Code subagents) or codex/
(headless Codex subagents started through the codex-agent command). Templates
live under harness/arms/templates/ so they can be reviewed as plain files.
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
DEFAULT_ARMS_DIR: Path = Path("/workspace/agent-experiments-arms")

OPUS_55 = "claude-opus-5-5"
SONNET_55 = "claude-sonnet-5-5"
SONNET_5 = "claude-sonnet-5"
SONNET_46 = "claude-sonnet-4-6"
HAIKU_55 = "claude-haiku-5-5"

GPT_61_SOL = "gpt-6.1-sol"

KNOWN_MODELS: frozenset[str] = frozenset({OPUS_55, SONNET_55, SONNET_5, SONNET_46, HAIKU_55})
# Models a headless Codex subagent may run on, with their display names.
CODEX_MODELS: Mapping[str, str] = MappingProxyType({GPT_61_SOL: "GPT-6.1 Sol"})
SUBAGENT_HARNESSES: tuple[str, ...] = ("claude", "codex")
EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

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

    def __post_init__(self) -> None:
        assert (self.subagent_model is None) == (self.delegation is None)
        assert self.delegation in (None, "eager", "conservative", "roles", "roles-debugger", "tiered")
        assert self.main_model in KNOWN_MODELS
        assert self.subagent_harness in SUBAGENT_HARNESSES
        if self.subagent_harness == "codex":
            assert self.subagent_model in CODEX_MODELS and not self.agent_models
        else:
            assert self.subagent_model is None or self.subagent_model in KNOWN_MODELS
        assert self.main_effort in EFFORT_LEVELS and self.subagent_effort in EFFORT_LEVELS
        assert set(self.agents) <= set(KNOWN_AGENT_NAMES) and len(set(self.agents)) == len(self.agents)
        overridden = [name for name, _ in self.agent_models]
        assert len(set(overridden)) == len(overridden) and set(overridden) <= set(self.agents)
        assert not self.agent_models or self.subagent_model is not None
        assert all(model in KNOWN_MODELS for _, model in self.agent_models)
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
    """The settings.json content for an arm."""
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
    """The CLAUDE.md section that tells the main agent how to start Codex subagents."""
    assert arm.codex and arm.subagent_model is not None
    template = _read_template("codex-subagents.md")
    roster = "\n".join(f"- {name}: {_split_agent_template(name)[0]['description']}" for name in arm.agents)
    text = (template.replace("{{MODEL_NAME}}", CODEX_MODELS[arm.subagent_model])
            .replace("{{EFFORT}}", arm.subagent_effort)
            .replace("{{COMMAND}}", CODEX_AGENT_COMMAND)
            .replace("{{EXAMPLE_AGENT}}", "implementer" if "implementer" in arm.agents else arm.agents[0])
            .replace("{{AGENTS}}", roster)
            .replace("{{TIMEOUT_MS}}", str(CODEX_BASH_MAX_TIMEOUT_MS)))
    assert "{{" not in text
    return text


def build_codex_files(arm: ArmSpec) -> dict[str, str]:
    """Map of path under codex/ to content for a Codex arm: config.toml, AGENTS.md (the
    working agreement every Codex subagent follows), agent.json (the codex-agent launcher's
    roster), and roles/<name>.md (each agent's Claude Code definition body, used as
    Codex developer instructions). The launcher itself is copied by generate_arm."""
    assert arm.codex and arm.subagent_model is not None
    config = (_read_template("codex/config.toml").replace("{{MODEL}}", arm.subagent_model)
              .replace("{{EFFORT}}", arm.subagent_effort))
    assert "{{" not in config
    roster = {name: {"read_only": codex_role_read_only(name)} for name in arm.agents}
    files = {"config.toml": config, "AGENTS.md": working_agreement(),
             "agent.json": json.dumps({"model": arm.subagent_model, "effort": arm.subagent_effort,
                                       "agents": roster}, indent=2) + "\n"}
    for name in arm.agents:
        files[f"roles/{name}.md"] = _split_agent_template(name)[1]
    return files


def build_agents(arm: ArmSpec) -> dict[str, str]:
    """Map of agent file name to content; empty for an arm without Claude Code subagents."""
    if arm.subagent_model is None or arm.codex:
        return {}
    agents: dict[str, str] = {}
    for name in arm.agents:
        text = _read_template(f"agents/{name}.md")
        assert "{{MODEL}}" in text
        agents[f"{name}.md"] = text.replace("{{MODEL}}", arm.model_for(name))
    return agents


def arm_root(arm_id: str, base_dir: Path = DEFAULT_ARMS_DIR) -> Path:
    return base_dir / get_arm(arm_id).arm_id


def generate_arm(arm_id: str, base_dir: Path = DEFAULT_ARMS_DIR) -> Path:
    """Write one arm root and return its path. Other content (projects/) is left alone."""
    arm = get_arm(arm_id)
    root = arm_root(arm_id, base_dir)
    root.mkdir(parents=True, exist_ok=True)
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
    if arm.codex:
        for relative, text in build_codex_files(arm).items():
            target = codex_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        launcher = codex_dir / "bin" / CODEX_AGENT_COMMAND
        launcher.parent.mkdir(parents=True)
        shutil.copyfile(CODEX_AGENT_SCRIPT, launcher)
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
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
