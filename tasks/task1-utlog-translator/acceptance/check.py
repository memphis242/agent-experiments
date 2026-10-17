#!/usr/bin/env python3
"""Acceptance checks for tools/utlog2junit.py.

Usage: check.py <repo-path> <out.json>

Exit status: 0 every check passed, 1 at least one failed, 2 infrastructure
error (fixtures missing, bad arguments).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PACK = Path(__file__).resolve().parent
FIXTURES = PACK / "fixtures"
TOOL_TIMEOUT_S = 120
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class Fail(Exception):
    """A check did not hold; the message is the experimenter detail."""


class InfraError(Exception):
    """The pack itself cannot run."""


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    feedback: str
    fn: Callable[["Ctx"], None]


class Ctx:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.tool = repo / "tools" / "utlog2junit.py"
        self.expected: dict[str, dict[str, object]] = json.loads(
            (FIXTURES / "expected.json").read_text())

    # -- helpers -----------------------------------------------------------
    def need_tool(self) -> None:
        if not self.tool.is_file():
            raise Fail(f"{self.tool} does not exist")

    def run(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.need_tool()
        try:
            return subprocess.run(
                ["python3", str(self.tool), *args], cwd=cwd, text=True,
                capture_output=True, timeout=TOOL_TIMEOUT_S, errors="replace")
        except subprocess.TimeoutExpired:
            raise Fail(f"tool exceeded {TOOL_TIMEOUT_S} s") from None

    @staticmethod
    def fixture(rel: str) -> Path:
        p = FIXTURES / rel
        if not p.exists():
            raise InfraError(f"missing fixture {rel}")
        return p


def short(s: str, n: int = 600) -> str:
    return s if len(s) <= n else s[:n] + "..."


def expect_rc(proc: subprocess.CompletedProcess[str], rc: int) -> None:
    if proc.returncode != rc:
        raise Fail(f"exit {proc.returncode}, wanted {rc}; stderr: {short(proc.stderr)}")


def stage(dest: Path, rels: list[str]) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for rel in rels:
        src = Ctx.fixture(rel)
        shutil.copy2(src, dest / src.name)
        out.append(dest / src.name)
    return out


def load_xml(path: Path) -> ET.Element:
    if not path.is_file():
        raise Fail(f"{path.name} was not written")
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise Fail(f"XML not well-formed: {exc}") from None


def suites_of(root: ET.Element) -> dict[str, ET.Element]:
    if root.tag != "testsuites":
        raise Fail(f"root element is <{root.tag}>, wanted <testsuites>")
    return {s.get("name", ""): s for s in root.findall("testsuite")}


def junit_run(ctx: Ctx, tmp: Path, rels: list[str], extra: list[str] | None = None,
              ) -> tuple[subprocess.CompletedProcess[str], Path]:
    logs = stage(tmp / "logs", rels)
    out = tmp / "out.xml"
    proc = ctx.run(["--junit", str(out), *(extra or []), *map(str, logs)], tmp)
    return proc, out


def attrs_consistent(suite: ET.Element) -> None:
    """Spec: tests/failures/errors/skipped count the matching child elements."""
    got = {k: int(suite.get(k, "-1")) for k in ("tests", "failures", "errors", "skipped")}
    want = {"tests": len(suite.findall("testcase")),
            "failures": len(suite.findall("testcase/failure")),
            "errors": len(suite.findall("testcase/error")),
            "skipped": len(suite.findall("testcase/skipped"))}
    if got != want:
        raise Fail(f"suite {suite.get('name')}: attributes {got} != element counts {want}")


def prop_value(suite: ET.Element, name: str) -> str | None:
    for p in suite.findall("properties/property"):
        if p.get("name") == name:
            return p.get("value")
    return None


def case_named(suite: ET.Element, name: str) -> ET.Element:
    for tc in suite.findall("testcase"):
        if tc.get("name") == name:
            return tc
    names = [tc.get("name") for tc in suite.findall("testcase")]
    raise Fail(f"no testcase {name!r} in suite {suite.get('name')!r}; have {names}")


# -- checks ----------------------------------------------------------------

def c_tool_exists(ctx: Ctx) -> None:
    ctx.need_tool()


def c_usage(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (log,) = stage(tmp, ["real/coverage-shared-heap.log"])
        proc = ctx.run([str(log)], tmp)
        expect_rc(proc, 2)
        if not proc.stderr.strip():
            raise Fail("nothing printed to stderr")
        if proc.stdout.strip() and not proc.stderr.strip():
            raise Fail("usage went to stdout")


def c_empty_dir(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        empty = tmp / "empty_dir"
        shutil.copytree(Ctx.fixture("crafted/empty_dir"), empty)
        for p in empty.iterdir():
            p.unlink()
        assert not any(empty.iterdir())
        proc = ctx.run(["--junit", str(tmp / "o.xml"), str(empty)], tmp)
        expect_rc(proc, 2)
        if str(empty) not in proc.stderr:
            raise Fail(f"stderr does not name the path {empty}: {short(proc.stderr)}")


def real_names() -> list[str]:
    return sorted(p.name for p in (FIXTURES / "real").glob("*.log"))


def real_run(ctx: Ctx, tmp: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    logs = tmp / "logs"
    stage(logs, [f"real/{n}" for n in real_names()])
    out = tmp / "out.xml"
    return ctx.run(["--junit", str(out), str(logs)], tmp), out


def c_real_exit(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, _ = real_run(ctx, Path(t))
        expect_rc(proc, 0)


def c_real_wellformed(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = real_run(ctx, Path(t))
        expect_rc(proc, 0)
        root = load_xml(out)
        if root.tag != "testsuites":
            raise Fail(f"root is <{root.tag}>")
        order = [s.get("name") for s in root.findall("testsuite")]
        want = [n[:-4] for n in real_names()]
        if order != want:
            raise Fail(f"suite names/order {order} != {want}")
        sums = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for s in root.findall("testsuite"):
            for attr in ("tests", "failures", "errors", "skipped", "time"):
                if s.get(attr) is None:
                    raise Fail(f"suite {s.get('name')} lacks attribute {attr}")
            if s.get("time") != "0":
                raise Fail(f"suite {s.get('name')} time={s.get('time')!r}")
            for k in sums:
                sums[k] += int(s.get(k, "0"))
            attrs_consistent(s)
            for tc in s.findall("testcase"):
                if tc.get("classname") != s.get("name"):
                    raise Fail(f"testcase {tc.get('name')} classname {tc.get('classname')!r}")
        for k, v in sums.items():
            if root.get(k) is None or int(root.get(k, "-1")) != v:
                raise Fail(f"root {k}={root.get(k)!r}, suites sum to {v}")


def c_real_counts(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = real_run(ctx, Path(t))
        expect_rc(proc, 0)
        suites = suites_of(load_xml(out))
        bad = []
        for name in real_names():
            exp = ctx.expected[f"real/{name}"]
            s = suites.get(name[:-4])
            if s is None:
                bad.append(f"{name}: suite missing")
                continue
            got_names = sorted(tc.get("name", "") for tc in s.findall("testcase"))
            cases = exp["cases"]
            assert isinstance(cases, list)
            want_names = sorted(c["name"] for c in cases)
            got = (int(s.get("tests", "-1")), int(s.get("failures", "-1")),
                   int(s.get("errors", "-1")), int(s.get("skipped", "-1")))
            want = (exp["tests"], exp["failures"], 0, exp["skipped"])
            if got != want:
                bad.append(f"{name}: tests/failures/errors/skipped {got} != {want}")
            if got_names != want_names:
                bad.append(f"{name}: testcase names differ: {got_names[:6]} vs {want_names[:6]}")
            if len(s.findall("testcase")) != got[0]:
                bad.append(f"{name}: tests attribute != number of testcase elements")
        if bad:
            raise Fail("; ".join(bad))


def failing_run(ctx: Ctx, tmp: Path, rel: str, extra: list[str] | None = None,
                ) -> tuple[subprocess.CompletedProcess[str], ET.Element | None]:
    proc, out = junit_run(ctx, tmp, [rel], extra)
    return proc, (load_xml(out) if out.exists() else None)


def c_failing_exit(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, _ = junit_run(ctx, Path(t), ["crafted/failing.log"])
        expect_rc(proc, 1)


def c_failing_elements(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = junit_run(ctx, Path(t), ["crafted/failing.log"])
        suite = suites_of(load_xml(out)).get("failing")
        if suite is None:
            raise Fail("suite 'failing' missing")
        exp = ctx.expected["crafted/failing.log"]["cases"]
        assert isinstance(exp, list)
        names = [tc.get("name") for tc in suite.findall("testcase")]
        if names != [c["name"] for c in exp]:
            raise Fail(f"testcase names {names}")
        for c in exp:
            tc = case_named(suite, c["name"])
            fails = tc.findall("failure")
            if not c["failing"]:
                if fails or tc.findall("error"):
                    raise Fail(f"passing testcase {c['name']} has failure/error")
                continue
            if len(fails) != 1:
                raise Fail(f"testcase {c['name']} has {len(fails)} failure elements")
            body = "".join(fails[0].itertext())
            for line in c["fail_lines"]:
                if line not in body:
                    raise Fail(f"failure body lacks line {line!r}; body {short(body)!r}")
            first_text = c["fail_lines"][0].partition(" - ")[2]
            if fails[0].get("message") != first_text:
                raise Fail(f"message {fails[0].get('message')!r} != {first_text!r}")
        attrs_consistent(suite)
        if suite.get("failures") != "1":
            raise Fail(f"failures attribute {suite.get('failures')}")


def c_truncated_error(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = junit_run(ctx, Path(t), ["crafted/truncated.log"])
        suite = suites_of(load_xml(out)).get("truncated")
        if suite is None:
            raise Fail("suite 'truncated' missing")
        tc = case_named(suite, "(log complete)")
        errs = tc.findall("error")
        if len(errs) != 1:
            raise Fail(f"(log complete) has {len(errs)} error elements")
        if errs[0].get("message") != "incomplete log: no COMPLETE line":
            raise Fail(f"error message {errs[0].get('message')!r}")
        attrs_consistent(suite)
        exp_t = ctx.expected["crafted/truncated.log"]
        if (int(suite.get("tests", "-1")), int(suite.get("errors", "-1"))) != (exp_t["tests"], exp_t["errors"]):
            raise Fail(f"tests/errors {suite.get('tests')}/{suite.get('errors')}")
        exp = ctx.expected["crafted/truncated.log"]["cases"]
        assert isinstance(exp, list)
        got = [tc.get("name") for tc in suite.findall("testcase") if tc.get("name") != "(log complete)"]
        if got != [c["name"] for c in exp]:
            raise Fail(f"segment testcases {got}")


def c_truncated_exit(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, _ = junit_run(ctx, Path(t), ["crafted/truncated.log"])
        expect_rc(proc, 1)


def c_tsf(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = junit_run(ctx, Path(t), ["crafted/tsf.log"])
        expect_rc(proc, 1)
        suite = suites_of(load_xml(out)).get("tsf")
        if suite is None:
            raise Fail("suite 'tsf' missing")
        tc = case_named(suite, "GADGET_Startup")
        fails = tc.findall("failure")
        if len(fails) != 1:
            raise Fail(f"{len(fails)} failure elements")
        line = "[  TSF] 01.001 gadget_test.c:22 - Gadget_Open() (-1) == 0 (0)"
        if line not in "".join(fails[0].itertext()):
            raise Fail("TSF line missing from failure body")
        if int(suite.get("failures", "-1")) != 1:
            raise Fail(f"failures attribute {suite.get('failures')}")


def c_mir(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proc, out = junit_run(ctx, tmp, ["crafted/mir_only.log", "real/osal_core_UT.log"])
        expect_rc(proc, 0)
        suites = suites_of(load_xml(out))
        for name, rel in (("mir_only", "crafted/mir_only.log"), ("osal_core_UT", "real/osal_core_UT.log")):
            s = suites.get(name)
            if s is None:
                raise Fail(f"suite {name} missing")
            want = str(ctx.expected[rel]["mir"])
            if prop_value(s, "mir") != want:
                raise Fail(f"suite {name}: mir property {prop_value(s, "mir")!r}, wanted {want!r}")
            if s.findall("testcase/failure") or s.findall("testcase/error"):
                raise Fail(f"suite {name}: MIR produced failure/error")


def c_na_property(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = junit_run(ctx, Path(t), ["real/osal_core_UT.log", "real/coverage-shared-heap.log"])
        expect_rc(proc, 0)
        suites = suites_of(load_xml(out))
        for name, rel in (("osal_core_UT", "real/osal_core_UT.log"),
                          ("coverage-shared-heap", "real/coverage-shared-heap.log")):
            s = suites.get(name)
            if s is None:
                raise Fail(f"suite {name} missing")
            want = str(ctx.expected[rel]["na"])
            if prop_value(s, "na") != want:
                raise Fail(f"suite {name}: na property {prop_value(s, 'na')!r}, wanted {want!r}")
            if s.findall("testcase/failure") or s.findall("testcase/error"):
                raise Fail(f"suite {name}: N/A produced failure/error")


def c_no_cases(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = junit_run(ctx, Path(t), ["crafted/no_cases.log"])
        expect_rc(proc, 0)
        suite = suites_of(load_xml(out)).get("no_cases")
        if suite is None:
            raise Fail("suite 'no_cases' missing")
        tc = case_named(suite, "VALVE_Cleanup")
        if len(tc.findall("skipped")) != 1:
            raise Fail("no <skipped/> on the empty segment")
        for bad in ("SETUP", "SUMMARY"):
            if any(x.get("name") == bad for x in suite.findall("testcase")):
                raise Fail(f"unexpected testcase {bad}")
        attrs_consistent(suite)
        if suite.get("skipped") != "1" or suite.get("tests") != "1" or suite.get("errors") != "0":
            raise Fail(f"tests={suite.get('tests')} skipped={suite.get('skipped')} errors={suite.get('errors')}")


def c_strict_rejects(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, _ = junit_run(ctx, Path(t), ["crafted/unknown_line.log"], ["--strict"])
        expect_rc(proc, 2)


def c_nonstrict_ignores(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, out = junit_run(ctx, Path(t), ["crafted/unknown_line.log"])
        expect_rc(proc, 0)
        suite = suites_of(load_xml(out)).get("unknown_line")
        if suite is None or suite.get("tests") != "1" or suite.get("failures") != "0":
            raise Fail("suite unknown_line missing or wrong counts")


def c_strict_accepts(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, _ = junit_run(ctx, Path(t), ["crafted/clean.log"], ["--strict"])
        expect_rc(proc, 0)


def c_strict_real(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        proc, _ = junit_run(ctx, Path(t), ["crafted/real_no_chatter.log"], ["--strict"])
        expect_rc(proc, 0)


def c_dir_only_logs(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        d = tmp / "d"
        stage(d, ["real/coverage-shared-heap.log", "real/coverage-vxworks-no-shell.log"])
        (d / "notes.txt").write_text("[ FAIL] 01.001 x.c:1 - not a log\n")
        (d / "sub").mkdir()
        shutil.copy2(Ctx.fixture("crafted/failing.log"), d / "sub" / "failing.log")
        out = tmp / "o.xml"
        proc = ctx.run(["--junit", str(out), str(d)], tmp)
        expect_rc(proc, 0)
        names = sorted(suites_of(load_xml(out)))
        if names != ["coverage-shared-heap", "coverage-vxworks-no-shell"]:
            raise Fail(f"suites {names}")


def md_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            rows.append([c.strip().strip("*`").strip() for c in line.strip().strip("|").split("|")])
    return rows


def md_section(text: str, title: str) -> str | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s*(#+|\*\*)\s*{title}\b", line):
            body = []
            for nxt in lines[i + 1:]:
                if re.match(r"^\s*#+\s", nxt):
                    break
                body.append(nxt)
            return "\n".join(body)
    return None


def c_markdown(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # Failing run.
        logs = stage(tmp / "a", ["crafted/failing.log", "crafted/truncated.log",
                                 "real/coverage-vxworks-no-shell.log"])
        md = tmp / "a.md"
        proc = ctx.run(["--markdown", str(md), *map(str, logs)], tmp)
        expect_rc(proc, 1)
        if not md.is_file():
            raise Fail("markdown not written")
        text = md.read_text()
        if re.search(r"</?[A-Za-z][^>]*>", text):
            raise Fail("raw HTML in markdown")
        rows = md_rows(text)
        if len(rows) < 2 or not any(re.fullmatch(r":?-{3,}:?", c) for c in rows[1]):
            raise Fail(f"no table header/separator: {rows[:2]}")
        by = {r[0]: r for r in rows[2:] if r}
        for name, status in (("failing", "FAIL"), ("truncated", "INCOMPLETE"),
                             ("coverage-vxworks-no-shell", "pass")):
            r = by.get(name)
            if r is None:
                raise Fail(f"no table row for suite {name}; rows {list(by)}")
            if r[-1] != status:
                raise Fail(f"row {name}: status {r[-1]!r} != {status!r}")
        tot = ctx.expected["crafted/failing.log"]["totals"]
        assert isinstance(tot, dict)
        want = [str(tot[k]) for k in ("total", "pass", "fail", "mir", "tsf", "ttf", "na")]
        if by["failing"][2:9] != want:
            raise Fail(f"failing row numbers {by['failing'][2:9]} != {want}")
        for name, rel in (("failing", "crafted/failing.log"), ("truncated", "crafted/truncated.log"),
                          ("coverage-vxworks-no-shell", "real/coverage-vxworks-no-shell.log")):
            if by[name][1] != str(ctx.expected[rel]["tests"]):
                raise Fail(f"row {name}: segments {by[name][1]!r} != tests {ctx.expected[rel]['tests']}")
        if len(by) != 3:
            raise Fail(f"{len(by)} rows for 3 logs")
        sec = md_section(text, "Failures")
        if sec is None:
            raise Fail("no Failures section")
        for line in ("widget_test.c:41 - Widget_Compute(3) (7) == 9 (9)",
                     "widget_test.c:42 - Widget_Compute(4) (8) == 16 (16)"):
            if line not in sec:
                raise Fail(f"Failures section lacks {line!r}")
        if "WIDGET_Compute" not in sec or "failing" not in sec:
            raise Fail("Failures entries lack suite / segment")
        # Passing run.
        logs = stage(tmp / "b", ["crafted/clean.log", "crafted/mir_only.log"])
        md2 = tmp / "b.md"
        proc = ctx.run(["--markdown", str(md2), *map(str, logs)], tmp)
        expect_rc(proc, 0)
        text2 = md2.read_text()
        if "No failures." not in text2:
            raise Fail("passing run lacks 'No failures.'")
        if "FAIL" in {r[-1] for r in md_rows(text2)[2:]}:
            raise Fail("passing run has FAIL status row")


def c_markdown_na(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (log,) = stage(tmp, ["real/osal_core_UT.log"])
        md = tmp / "o.md"
        expect_rc(ctx.run(["--markdown", str(md), str(log)], tmp), 0)
        rows = md_rows(md.read_text())
        by = {r[0]: r for r in rows[2:] if r}
        r = by.get("osal_core_UT")
        tot = ctx.expected["real/osal_core_UT.log"]["totals"]
        assert isinstance(tot, dict)
        want = [str(tot[k]) for k in ("total", "pass", "fail", "mir", "tsf", "ttf", "na")]
        if r is None or r[2:9] != want:
            raise Fail(f"row {r} lacks numbers {want}")


def c_markdown_incomplete(ctx: Ctx) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (log,) = stage(tmp, ["crafted/truncated.log"])
        md = tmp / "o.md"
        expect_rc(ctx.run(["--markdown", str(md), str(log)], tmp), 1)
        sec = md_section(md.read_text(), "Failures")
        if sec is None:
            raise Fail("no Failures section")
        if "truncated: incomplete log" not in sec:
            raise Fail(f"Failures section: {short(sec)!r}")
        if "No failures." in sec:
            raise Fail("'No failures.' present for an incomplete log")


def c_readme(ctx: Ctx) -> None:
    p = ctx.repo / "tools" / "README.md"
    if not p.is_file():
        raise Fail("tools/README.md missing")
    s = p.read_text(errors="replace")
    for needle in ("utlog2junit", "--junit", "--markdown", "--strict"):
        if needle not in s:
            raise Fail(f"README lacks {needle}")


def c_unit_tests(ctx: Ctx) -> None:
    tests = ctx.repo / "tools" / "tests"
    if not tests.is_dir():
        raise Fail("tools/tests missing")
    try:
        proc = subprocess.run(["python3", "-m", "unittest", "discover", "tools/tests"],
                              cwd=ctx.repo, text=True, capture_output=True,
                              timeout=TOOL_TIMEOUT_S, errors="replace")
    except subprocess.TimeoutExpired:
        raise Fail("unittest timed out") from None
    m = re.search(r"^Ran (\d+) tests?", proc.stderr, re.M)
    if proc.returncode != 0:
        raise Fail(f"unittest exit {proc.returncode}: {short(proc.stderr)}")
    if m is None or int(m.group(1)) < 5:
        raise Fail(f"unittest ran {m.group(1) if m else '?'} tests")


CHECKS: list[CheckSpec] = [
    CheckSpec("tool-exists",
              "The script tools/utlog2junit.py must exist in the repository.", c_tool_exists),
    CheckSpec("usage-no-output-flag-exit-2",
              "When neither --junit nor --markdown is given, the tool must print usage to stderr and exit 2.",
              c_usage),
    CheckSpec("empty-dir-exit-2",
              "An empty directory (inputs yielding no logs) is a usage error: the tool must exit 2 with a clear message on stderr naming the path.",
              c_empty_dir),
    CheckSpec("real-dir-exit-0",
              "Run over a directory of logs in which every test segment passed, the tool must exit 0. Lines it does not recognize (blank lines, **BUG** diagnostics, startup chatter) are ignored without --strict.",
              c_real_exit),
    CheckSpec("real-dir-junit-wellformed",
              "The JUnit output must be well-formed XML that parses with xml.etree.ElementTree: a <testsuites> root with aggregated attributes, one <testsuite> per log file (in sorted filename order, name = file name without .log) carrying tests, failures, errors, skipped and time=\"0\", and testcases whose classname is the suite name.",
              c_real_wellformed),
    CheckSpec("real-dir-counts",
              "Per-suite JUnit counts must match the logs: one <testcase> per segment (excluding the SUMMARY segment and a SETUP segment without assertions, named by the segment NAME), a segment closed by 'No test cases' counted as skipped, and no failures or errors for passing logs.",
              c_real_counts),
    CheckSpec("failing-log-exit-1",
              "When any segment has a failing assertion the tool must exit 1.", c_failing_exit),
    CheckSpec("failing-log-failure-elements",
              "A segment with FAIL > 0 must get a <failure> element on its own testcase whose message is the first failing line's TEXT and whose body lists every FAIL line verbatim; passing segments get none, and the suite's failures attribute must match.",
              c_failing_elements),
    CheckSpec("truncated-log-error-case",
              "A log without a COMPLETE line must produce one additional testcase named (log complete) with an <error message=\"incomplete log: no COMPLETE line\"> element, in addition to the testcases of its segments.",
              c_truncated_error),
    CheckSpec("truncated-log-exit-1",
              "A log without a COMPLETE line is incomplete, and the tool must exit 1.", c_truncated_exit),
    CheckSpec("tsf-counted-as-failure",
              "A [  TSF] (test setup failure) line, like a TTF line, must count as a failure: the segment gets a <failure> whose body lists the TSF line verbatim, and the tool must exit 1.",
              c_tsf),
    CheckSpec("mir-property",
              "MIR (manual inspection required) counts must be recorded as a <properties> entry named mir on the suite, with the count as its value, and MIR lines must not make a segment fail.",
              c_mir),
    CheckSpec("no-cases-skipped",
              "A segment closed by 'No test cases' must get a <skipped/> element, and a SETUP segment without assertions must not appear as a testcase.",
              c_no_cases),
    CheckSpec("strict-rejects-unknown-exit-2",
              "With --strict, any line that is not recognized by the input grammar must make the tool exit 2.",
              c_strict_rejects),
    CheckSpec("nonstrict-ignores-unknown-exit-0",
              "Without --strict, unrecognized lines are ignored: a log that otherwise passed must still give exit 0 and correct counts.",
              c_nonstrict_ignores),
    CheckSpec("strict-accepts-recognized-lines",
              "With --strict, a log containing only lines recognized by the input grammar must still be accepted with exit 0.",
              c_strict_accepts),
    CheckSpec("strict-accepts-real-log",
              "With --strict, blank lines, the opening [BEGIN] UNIT TEST line and **BUG** diagnostic lines are recognized and ignored: a log made only of such lines and grammar lines must exit 0.",
              c_strict_real),
    CheckSpec("na-property",
              "N/A counts must be recorded on the suite as <property name=\"na\" value=\"N\"/> inside <properties>, and N/A lines must not make a segment fail.",
              c_na_property),
    CheckSpec("dir-ignores-non-log-entries",
              "For a directory PATH, only files ending in .log directly inside it are processed; other files and subdirectories are not.",
              c_dir_only_logs),
    CheckSpec("markdown-table-and-failures-section",
              "The Markdown output must be a table with one row per log (suite name, segments, assertions total, pass, fail, MIR, TSF, TTF, N/A, status as pass, FAIL or INCOMPLETE; segments equals the JUnit tests count), followed by a Failures section listing every failing assertion as 'suite / segment: FILE:LINE - TEXT', or the sentence 'No failures.'; it must use no raw HTML.",
              c_markdown),
    CheckSpec("markdown-na-column",
              "The Markdown table columns, in order, are suite, segments, assertions total, pass, fail, MIR, TSF, TTF, N/A and status; the N/A column must hold the log's N/A count.",
              c_markdown_na),
    CheckSpec("markdown-incomplete-log-line",
              "In the Failures section, each incomplete log must get one line 'suite: incomplete log' (so 'No failures.' must not be printed for it), and the tool must exit 1.",
              c_markdown_incomplete),
    CheckSpec("readme-usage-section",
              "tools/README.md must exist and contain a short usage section for utlog2junit.py covering its options.",
              c_readme),
    CheckSpec("unit-tests-present-and-pass",
              "Unit tests under tools/tests must run with 'python3 -m unittest discover tools/tests' from the repository root, pass (exit 0), and number at least five.",
              c_unit_tests),
]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check.py <repo-path> <out.json>", file=sys.stderr)
        return 2
    repo = Path(argv[1]).resolve()
    out = Path(argv[2])
    if not repo.is_dir():
        print(f"infrastructure error: repo {repo} is not a directory", file=sys.stderr)
        return 2
    ids = [c.check_id for c in CHECKS]
    assert len(set(ids)) == len(ids) and all(ID_RE.match(i) for i in ids)
    try:
        ctx = Ctx(repo)
        results = []
        for spec in CHECKS:
            try:
                spec.fn(ctx)
                results.append({"id": spec.check_id, "passed": True, "feedback": "", "detail": ""})
            except Fail as exc:
                results.append({"id": spec.check_id, "passed": False,
                                "feedback": spec.feedback, "detail": short(str(exc))})
    except (InfraError, OSError, json.JSONDecodeError) as exc:
        print(f"infrastructure error: {exc}", file=sys.stderr)
        return 2
    passed = all(r["passed"] for r in results)
    out.write_text(json.dumps({"passed": passed, "checks": results}, indent=1) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
