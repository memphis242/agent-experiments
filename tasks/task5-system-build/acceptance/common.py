"""Exceptions and small helpers shared by every module of the pack."""
from __future__ import annotations

from dataclasses import dataclass, field


class Fail(Exception):
    """A check did not hold. The message is the experimenter detail; `feedback`, when given, replaces the
    check's standard feedback (it must still be written in the language of the specification)."""

    def __init__(self, message: str = "", feedback: str | None = None) -> None:
        super().__init__(message)
        self.feedback = feedback


class PrereqFail(Fail):
    """A failure of something many checks depend on (build, node start-up, telemetry). It is cached and
    reported by every dependent check without retrying."""


class InfraError(Exception):
    """The pack itself cannot run (tooling missing, pack defect). Only raised before the agent's code is used."""


@dataclass
class Problems:
    """Collects deviations so that one check can report all of them."""
    items: list[str] = field(default_factory=list)

    def check(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.items.append(msg)
        return cond

    def raise_if(self, feedback: str | None = None) -> None:
        if self.items:
            raise Fail("; ".join(self.items), feedback)


def short(text: str, n: int = 900) -> str:
    text = text.strip()
    return text if len(text) <= n else "..." + text[-n:]
