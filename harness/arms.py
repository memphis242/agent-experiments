#!/usr/bin/env python3
"""Generate the per-arm Claude Code configuration roots.

Each arm root is used as CLAUDE_CONFIG_DIR and holds only that arm's
settings.json, CLAUDE.md, and (except for A1) agents/. Templates live under
harness/arms/templates/ so they can be reviewed as plain files.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "arms" / "templates"
DEFAULT_ARMS_DIR: Path = Path("/workspace/agent-experiments-arms")

MAIN_MODEL = "claude-opus-5-5"
OPUS_55 = "claude-opus-5-5"
SONNET_55 = "claude-sonnet-5-5"
SONNET_5 = "claude-sonnet-5"
SONNET_46 = "claude-sonnet-4-6"

# Effort is pinned to high for every model, main agent and subagents alike.
EFFORT_BY_MODEL: Mapping[str, str] = MappingProxyType({
    OPUS_55: "high",
    SONNET_55: "high",
    SONNET_5: "high",
    SONNET_46: "high",
})

SUBAGENT_NAMES: tuple[str, ...] = ("explorer", "implementer", "tester", "debugger", "reviewer")
PROMPT_CACHE_TTL = "1h"
SUBAGENT_PROMPT_CACHE_TTL = "5m"


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    subagent_model: str | None  # None: the Agent tool is denied
    delegation: str | None      # "eager", "conservative", or None

    def __post_init__(self) -> None:
        assert (self.subagent_model is None) == (self.delegation is None)
        assert self.delegation in (None, "eager", "conservative")
        assert self.subagent_model is None or self.subagent_model in EFFORT_BY_MODEL


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
    model_settings: dict[str, dict[str, str]] = {MAIN_MODEL: {"effortLevel": EFFORT_BY_MODEL[MAIN_MODEL]}}
    if arm.subagent_model is not None:
        model_settings[arm.subagent_model] = {"effortLevel": EFFORT_BY_MODEL[arm.subagent_model]}
    settings: dict[str, object] = {
        "model": MAIN_MODEL,
        "effortLevel": EFFORT_BY_MODEL[MAIN_MODEL],
        "modelSettings": model_settings,
        "promptCacheTtl": PROMPT_CACHE_TTL,
        "subagentPromptCacheTtl": SUBAGENT_PROMPT_CACHE_TTL,
        "autoMemoryEnabled": False,
    }
    if arm.subagent_model is None:
        settings["permissions"] = {"deny": ["Agent"]}
    return settings


def build_claude_md(arm: ArmSpec) -> str:
    """CLAUDE.md text: the base text plus the arm's delegation paragraph."""
    base = _read_template("CLAUDE.base.md")
    assert "{{DELEGATION}}" in base
    if arm.delegation is None:
        # No subagents: the delegation section is dropped entirely.
        head = base.split("## Delegation")[0]
        return head.rstrip() + "\n"
    paragraph = _read_template(f"delegation-{arm.delegation}.txt").strip()
    return base.replace("{{DELEGATION}}", paragraph)


def build_agents(arm: ArmSpec) -> dict[str, str]:
    """Map of agent file name to content; empty for an arm without subagents."""
    if arm.subagent_model is None:
        return {}
    agents: dict[str, str] = {}
    for name in SUBAGENT_NAMES:
        text = _read_template(f"agents/{name}.md")
        assert "{{MODEL}}" in text
        agents[f"{name}.md"] = text.replace("{{MODEL}}", arm.subagent_model)
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
