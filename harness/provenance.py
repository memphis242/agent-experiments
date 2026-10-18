"""Identify the harness code that shaped a run.

A finished run is reusable as long as the files that drive a run (the run pipeline,
acceptance loop, process wrapper and arm generation) are unchanged. Analysis,
aggregation, driver and documentation changes do not invalidate it. The run-code
hash is a SHA-256 over `git ls-tree` of those paths at a commit, so it can be
derived for any recorded commit, not only the current checkout.
"""
from __future__ import annotations

import functools
import hashlib
from typing import Callable, Sequence

import run

RUN_CODE_PATHS: tuple[str, ...] = (
    "harness/run.py", "harness/acceptance.py", "harness/proc.py", "harness/arms.py", "harness/arms",
)
assert "harness/provenance.py" not in RUN_CODE_PATHS, "the hash must not depend on its own definition"


# Run-code hashes of earlier commits whose differences from the current run pipeline were
# reviewed as behaviour-preserving, so runs recorded under them stay reusable and comparable.
EQUIVALENT_RUN_CODES: frozenset[str] = frozenset({
    # 43e08d18cc5c (Task 1 wave): post-run analysis was later moved out of run.py.
    "9e55e24b8e88f87aa9b33708ee6126b141d9d3d8d7e9d358327e9175647e7a3e",
    # 68ce5b2 (Tasks 2 to 4 waves): the current change parameterises the main model per arm;
    # for the arms that existed then it yields the same model, effort and records.
    "36554411e09c7994e2238f6ac7f095e084f894be058043c9110211bd92da03db",
    # 84adedf (A10 to A12 waves and the full Task 4 wave): the current change adds per-agent
    # subagent models and tiered arms; A1 to A12 config roots and validation results are unchanged.
    "6614d502a0031f6b92920ea6754a322c30c6882a48cff7fca34d72a2b4151fb6",
    # 4122116 (A13 to A15 waves): the current change only lets a pack declare its acceptance
    # timeout; packs without the declaration keep 2400 s.
    "72a53602f78e97f9692bc56aab09601efd2a2a553c49076a738bc1bf6cb06792",
})


def same_run_code(recorded: str | None, current: str) -> bool:
    """True when a recorded run-code hash is the current one or a reviewed equivalent."""
    return recorded is not None and (recorded == current or recorded in EQUIVALENT_RUN_CODES)


@functools.cache
def run_code_sha(commit: str) -> str | None:
    """Hash of the run-relevant harness files at `commit` (a sha or 'HEAD'), or None when
    the commit is unknown to this repository."""
    if not commit:
        return None
    listing = run._capture(["git", "-C", str(run.REPO_ROOT), "ls-tree", "-r", commit, "--", *RUN_CODE_PATHS])
    if listing.startswith("unavailable") or not listing:
        return None
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def run_code_dirty() -> bool:
    """True when any run-relevant file has uncommitted changes (the recorded commit would then
    not describe the code that actually ran)."""
    status = run._capture(["git", "-C", str(run.REPO_ROOT), "status", "--porcelain", "--", *RUN_CODE_PATHS])
    return status.startswith("unavailable") or bool(status)


Resolver = Callable[[str], str | None]


def recorded_run_code(attempt: dict, resolve: Resolver = run_code_sha) -> str | None:
    """Run-code hash of a recorded attempt, derived from its provenance commit."""
    return resolve((attempt.get("provenance") or {}).get("harness_sha") or "")
