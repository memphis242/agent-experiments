"""Acceptance pack execution, result validation, and feedback composition.

The contract is documented in ACCEPTANCE_CONTRACT.md and enforced here.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

EXIT_PASS, EXIT_FAIL, EXIT_INFRA = 0, 1, 2
CHECK_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Feedback must be in spec language: no pack paths and no test file names.
_LEAK_RE = re.compile(r"acceptance/|\btest_[A-Za-z0-9_]*\.py\b|\b[A-Za-z0-9_]*_test\.py\b|\brun\.sh\b")

FEEDBACK_PREAMBLE = (
    "Your work was reviewed against the specification in TASK.md and the following "
    "requirements are not yet met. Address each one, then rebuild, retest, and commit.")


class ContractError(Exception):
    """The acceptance pack violated its contract (an infrastructure fault, not an agent failure)."""


class AcceptanceInfraError(ContractError):
    """The pack reported exit status 2, or could not run."""


@dataclass(frozen=True)
class Check:
    check_id: str
    passed: bool
    feedback: str
    detail: str


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    checks: tuple[Check, ...]

    def failures(self) -> tuple[Check, ...]:
        return tuple(sorted((c for c in self.checks if not c.passed), key=lambda c: c.check_id))

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": [
            {"id": c.check_id, "passed": c.passed, "feedback": c.feedback, "detail": c.detail}
            for c in self.checks]}


def parse_result(data: object, exit_code: int) -> AcceptanceResult:
    """Validate the result JSON against the contract and the process exit status."""
    if exit_code not in (EXIT_PASS, EXIT_FAIL):
        raise ContractError(f"run.sh exit status {exit_code} is not 0, 1, or 2")
    if not isinstance(data, dict) or set(data) != {"passed", "checks"}:
        raise ContractError("result JSON must be an object with exactly the keys 'passed' and 'checks'")
    if not isinstance(data["passed"], bool) or not isinstance(data["checks"], list) or not data["checks"]:
        raise ContractError("'passed' must be a bool and 'checks' a non-empty list")
    checks: list[Check] = []
    seen: set[str] = set()
    for i, raw in enumerate(data["checks"]):
        if not isinstance(raw, dict) or set(raw) != {"id", "passed", "feedback", "detail"}:
            raise ContractError(f"checks[{i}] must be an object with keys id, passed, feedback, detail")
        cid, passed, feedback, detail = raw["id"], raw["passed"], raw["feedback"], raw["detail"]
        if not isinstance(cid, str) or not CHECK_ID_RE.match(cid):
            raise ContractError(f"checks[{i}].id must be a stable kebab-case string, got {cid!r}")
        if cid in seen:
            raise ContractError(f"duplicate check id {cid!r}")
        seen.add(cid)
        if not isinstance(passed, bool) or not isinstance(feedback, str) or not isinstance(detail, str):
            raise ContractError(f"checks[{i}] has wrongly typed fields")
        if not passed and not feedback.strip():
            raise ContractError(f"failing check {cid!r} has empty feedback")
        if _LEAK_RE.search(feedback):
            raise ContractError(f"feedback of check {cid!r} mentions the acceptance pack or a test file")
        checks.append(Check(cid, passed, feedback, detail))
    overall = all(c.passed for c in checks)
    if data["passed"] != overall:
        raise ContractError("'passed' disagrees with the individual checks")
    if (exit_code == EXIT_PASS) != overall:
        raise ContractError(f"exit status {exit_code} disagrees with passed={data['passed']}")
    return AcceptanceResult(overall, tuple(checks))


def compose_feedback(failures: Sequence[Check]) -> str:
    """Deterministic feedback: failures sorted by id, each check's feedback verbatim."""
    assert failures, "feedback is only composed when a check failed"
    ordered = sorted(failures, key=lambda c: c.check_id)
    items = [f"{n}. {c.feedback.strip()}" for n, c in enumerate(ordered, 1)]
    return FEEDBACK_PREAMBLE + "\n\n" + "\n".join(items) + "\n"


def run_pack(pack_src: Path, run_dir: Path, repo: Path, round_no: int, timeout_s: float) -> AcceptanceResult:
    """Copy the pack outside the repo, execute it, and validate its result file."""
    pack = run_dir / "acceptance"
    if pack.exists():
        shutil.rmtree(pack)
    try:
        shutil.copytree(pack_src, pack)
    except OSError as exc:
        raise AcceptanceInfraError(f"cannot copy acceptance pack {pack_src}: {exc}") from exc
    script = pack / "run.sh"
    if not script.is_file():
        raise AcceptanceInfraError(f"acceptance pack {pack_src} has no run.sh")
    out_json = run_dir / f"acceptance-result-round{round_no}.json"
    out_json.unlink(missing_ok=True)
    try:
        proc = subprocess.run(["bash", str(script), str(repo), str(out_json)], cwd=pack,
                              capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise AcceptanceInfraError(f"acceptance run.sh exceeded {timeout_s:.0f} s") from None
    (run_dir / f"acceptance-log-round{round_no}.txt").write_text(
        f"exit {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}", encoding="utf-8")
    if proc.returncode == EXIT_INFRA:
        raise AcceptanceInfraError("acceptance run.sh reported an infrastructure error (exit 2); see its log")
    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"acceptance run.sh (exit {proc.returncode}) wrote no valid result JSON: {exc}") from exc
    return parse_result(data, proc.returncode)
