"""Build and run the black-box component tests inside the scratch copy of the mission tree.

Each component's test is wired in next to its own unit tests (as Task 4 did for STOR), built as a
separate target so that one component that does not compile cannot hide the others, and run with a
time limit. A check's feedback names the requirements whose cases failed, in specification terms.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from common import Fail, short

# check id -> (component, runner unit name)
HIDDEN_CHECKS: dict[str, tuple[str, str]] = {
    "s1-hidden-stor": ("stor", "hidden"),
    "s2-hidden-stor-transfer": ("stor", "hidden"),
    "s3-hidden-cmdval-lib": ("cmdval_lib", "hidden"),
    "s3-hidden-cmdi": ("cmdi", "hidden"),
    "s4-hidden-tlmo": ("tlmo", "hidden"),
    "s5-hidden-gw": ("gw", "hidden"),
    "s6-hidden-sfi-clusters": ("sfi", "hidden_clusters"),
    "s6-hidden-sfi": ("sfi", "hidden"),
}
COMPONENT_DIRS = {"stor": "apps/stor", "cmdi": "apps/cmdi", "tlmo": "apps/tlmo", "gw": "apps/gw", "sfi": "apps/sfi",
                  "cmdval_lib": "libs/cmdval_lib"}
ENTRY = {"stor": "STOR_AppMain", "cmdi": "CMDI_AppMain", "tlmo": "TLMO_AppMain", "gw": "GW_AppMain",
         "sfi": "SFI_AppMain", "cmdval_lib": "CMDVAL_ValidateFrame"}
RUN_TIMEOUT_S = 300
BEGIN_RE = re.compile(r"^\[BEGIN\] \d+ (.+?)\s*$")
UNMODELLED_RE = re.compile(r"unmodelled call: (\w+)")


@dataclass(frozen=True)
class CaseSpec:
    runner: str
    prefix: str
    base: str
    cases: dict[str, str]


def load_case_specs(pack: Path) -> dict[str, CaseSpec]:
    """check id -> its runner, case-name prefix, base sentence and per-case requirement phrases."""
    out: dict[str, CaseSpec] = {}
    for f in sorted((pack / "hidden_tests").glob("*/cases*.json")):
        doc = json.loads(f.read_text())
        for cid, d in doc.items():
            assert cid in HIDDEN_CHECKS, f"{f}: unknown check id {cid}"
            assert cid not in out, f"{cid} defined twice"
            out[cid] = CaseSpec(d["runner"], d.get("prefix", ""), d["base"], dict(d["cases"]))
            assert out[cid].runner == HIDDEN_CHECKS[cid][1], f"{cid}: runner {out[cid].runner}"
    return out


@dataclass
class RunnerResult:
    built: bool = False
    build_detail: str = ""
    ran: bool = False
    returncode: int | None = None
    output: str = ""
    failed_cases: dict[str, list[str]] = field(default_factory=dict)  # case -> failing assertion lines
    seen_cases: list[str] = field(default_factory=list)
    timed_out: bool = False


def parse_output(out: str) -> tuple[list[str], dict[str, list[str]]]:
    seen: list[str] = []
    failed: dict[str, list[str]] = {}
    current = ""
    for line in out.splitlines():
        m = BEGIN_RE.match(line)
        if m:
            current = m.group(1)
            seen.append(current)
            continue
        if line.startswith("[ FAIL]"):
            failed.setdefault(current or "SETUP", []).append(line[8:].strip())
    return seen, failed


def feedback_for(spec: CaseSpec, failed: list[str], label: str) -> str:
    parts = [spec.cases[c] for c in failed if c in spec.cases]
    unknown = [c for c in failed if c not in spec.cases]
    text = f"{label}: {spec.base}"
    if parts:
        text += " Not yet met: " + "; ".join(parts) + "."
    elif unknown:
        text += " Its behaviour deviates from the specification when driven through its entry point."
    return text


class HiddenTests:
    """Wires, builds and runs every component's black-box test once; checks read the results."""

    def __init__(self, tree: Path, pack: Path, run_sh) -> None:  # noqa: ANN001 - run_sh is Ctx.sh
        self.tree = tree
        self.pack = pack
        self.sh = run_sh
        self.specs = load_case_specs(pack)
        self.lock = threading.Lock()
        self.done = False
        self.prep_ok = False
        self.prep_detail = ""
        self.results: dict[tuple[str, str], RunnerResult] = {}
        self.present: dict[str, bool] = {}

    # -- wiring ---------------------------------------------------------------
    def wire(self) -> None:
        for comp, rel in COMPONENT_DIRS.items():
            d = self.tree / rel
            srcs = list((d / "fsw" / "src").glob("*.c")) if (d / "fsw" / "src").is_dir() else []
            cm = d / "CMakeLists.txt"
            self.present[comp] = bool(srcs) and cm.is_file()
            if not self.present[comp]:
                continue
            hd = d / "hidden_acceptance"
            if hd.exists():
                shutil.rmtree(hd)
            hd.mkdir()
            for f in (self.pack / "hidden_tests" / comp).iterdir():
                if f.suffix in (".c", ".txt", ".h"):
                    shutil.copy2(f, hd / f.name)
            shutil.copy2(self.pack / "hidden_tests" / "t5_model.h", hd / "t5_model.h")
            text = cm.read_text(errors="replace")
            if "hidden_acceptance" not in text:
                cm.write_text(text.rstrip("\n") + "\n\nif (ENABLE_UNIT_TESTS)\n  add_subdirectory(hidden_acceptance)\n"
                              "endif (ENABLE_UNIT_TESTS)\n")

    def runners(self) -> list[tuple[str, str]]:
        return sorted({v for v in HIDDEN_CHECKS.values()})

    def run_all(self, deadline_fn) -> None:  # noqa: ANN001 - deadline_fn(want) -> allowed timeout
        with self.lock:
            if self.done:
                return
            self._run_all(deadline_fn)
            self.done = True

    def _run_all(self, timeout_for) -> None:
        self.wire()
        if not any(self.present.values()):
            self.prep_ok = False
            self.prep_detail = "no component sources exist"
            return
        prep = self.sh(["make", "native_std.prep"], 300)
        self.prep_ok = prep.returncode == 0
        if not self.prep_ok:
            self.prep_detail = short(prep.stdout + prep.stderr, 1500)
            return
        bdir = self.tree / "build-native_std" / "native" / "default_cpu1"
        for comp, unit in self.runners():
            res = RunnerResult()
            self.results[(comp, unit)] = res
            if not self.present.get(comp):
                res.build_detail = f"{COMPONENT_DIRS[comp]} has no sources"
                continue
            target = f"coverage-{comp}-{unit}-testrunner"
            proc = self.sh(["make", "-C", str(bdir), "-j16", target], 600)
            exe = next((p for p in bdir.rglob(target) if p.is_file()), None)
            if proc.returncode != 0 or exe is None:
                errs = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                        if "error" in ln.lower() or "undefined reference" in ln]
                res.build_detail = "\n".join(errs[-15:]) or short(proc.stdout + proc.stderr, 1200)
                continue
            res.built = True
        threads = []
        for (comp, unit), res in self.results.items():
            if res.built:
                t = threading.Thread(target=self._run_one, args=(comp, unit, res, timeout_for(RUN_TIMEOUT_S)))
                t.start()
                threads.append(t)
        for t in threads:
            t.join()

    def _run_one(self, comp: str, unit: str, res: RunnerResult, timeout: float) -> None:
        bdir = self.tree / "build-native_std" / "native" / "default_cpu1"
        exe = next(p for p in bdir.rglob(f"coverage-{comp}-{unit}-testrunner") if p.is_file())
        try:
            proc = subprocess.run([str(exe)], cwd=exe.parent, text=True, errors="replace", capture_output=True,
                                  timeout=timeout)
            res.returncode = proc.returncode
            res.output = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            res.timed_out = True
            out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
            res.output = out or ""
        res.ran = True
        res.seen_cases, res.failed_cases = parse_output(res.output)

    # -- verdicts -------------------------------------------------------------
    def verdict(self, check_id: str, label: str, timeout_for) -> None:  # noqa: ANN001
        """Raise Fail for `check_id` unless every case of its runner (with its prefix) passed."""
        comp, unit = HIDDEN_CHECKS[check_id]
        spec = self.specs.get(check_id)
        if spec is None:
            raise AssertionError(f"no case table for {check_id}")
        rel = COMPONENT_DIRS[comp]
        if not self.present.get(comp, False) and not self.done:
            self.run_all(timeout_for)
        if not self.present.get(comp, False):
            raise Fail(f"{rel} has no sources or no CMakeLists.txt",
                       f"{label}: {spec.base} The component does not exist in {rel} with fsw/src sources.")
        self.run_all(timeout_for)
        if not self.prep_ok:
            raise Fail("the tree could not be configured with the behaviour tests wired in: " + self.prep_detail,
                       f"{label}: {spec.base} Keep {rel}/CMakeLists.txt following the sample_app conventions "
                       f"(add_cfe_app, fsw/inc, config) so that the component can be built for a stub-based test.")
        res = self.results[(comp, unit)]
        if not res.built:
            raise Fail("the behaviour test does not build: " + res.build_detail,
                       f"{label}: {spec.base} The sources of {rel} must compile and link against the cFE and OSAL "
                       f"public APIs and the names pinned by the specification (entry point {ENTRY[comp]}), with no "
                       f"undefined references.")
        mine = [c for c in res.seen_cases if c.startswith(spec.prefix)] if spec.prefix else list(res.seen_cases)
        failed = [c for c in res.failed_cases if (c.startswith(spec.prefix) if spec.prefix else True)]
        if spec.prefix and "SETUP" in res.failed_cases:
            failed.append("SETUP")
        unmodelled = sorted({m.group(1) for c in failed for ln in res.failed_cases.get(c, [])
                             for m in UNMODELLED_RE.finditer(ln)})
        detail_fails = [f"{c}: {ln}" for c in failed for ln in res.failed_cases.get(c, [])[:3]]
        if res.timed_out:
            raise Fail(f"the behaviour test did not finish within {RUN_TIMEOUT_S} s; last cases {mine[-3:]}",
                       f"{label}: {spec.base} The component stopped responding while the specified commands and "
                       f"packets were delivered (it must return to its software bus pipe after each message).")
        crashed = res.returncode is not None and (res.returncode < 0 or (res.returncode != 0 and not res.failed_cases))
        missing = [c for c in spec.cases if c not in res.seen_cases]
        if crashed or missing:
            last = mine[-1] if mine else "(start-up)"
            raise Fail(f"the behaviour test ended abnormally (exit {res.returncode}) during {last!r}; "
                       f"cases not reached {missing[:5]}; tail: {short(res.output, 700)}",
                       f"{label}: {spec.base} The component must not crash or abort when given the specified "
                       f"commands, packets, tables and I/O.")
        if failed:
            fb = feedback_for(spec, sorted(set(failed) - {"SETUP"}) or ["SETUP"], label)
            if unmodelled:
                fb += (" It called " + ", ".join(unmodelled) + ", which is outside the OSAL and cFE calls the "
                       "specification's design needs; use the standard OSAL file, socket and select calls and the "
                       "cFE message, table, event and time calls instead.")
            detail = f"{len(detail_fails)} failing assertions shown; " + " | ".join(detail_fails[:10])
            if unmodelled:
                detail = "UNMODELLED CALLS: " + ", ".join(unmodelled) + "; " + detail
            raise Fail(detail, fb)
        if not mine:
            raise Fail(f"no case with prefix {spec.prefix!r} ran", f"{label}: {spec.base}")

