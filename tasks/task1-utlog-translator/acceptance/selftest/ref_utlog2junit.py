#!/usr/bin/env python3
"""Reference ut_assert log to JUnit XML / Markdown translator (selftest only)."""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

BEGIN_RE = re.compile(r"^\[BEGIN\] (\d{2,}) (.+?)\s*$")
RESULT_RE = re.compile(r"^\[( PASS| FAIL|  MIR|  TSF|  TTF|  N/A)\] (.*)$")
END_RE = re.compile(r"^\[  END\] (?:No test cases|\d{2,} \S+\s+TOTAL::\d+.*)$")
KEYS = ("PASS", "FAIL", "MIR", "TSF", "TTF", "N/A")


@dataclass
class Segment:
    name: str
    counts: dict[str, int] = field(default_factory=lambda: {k: 0 for k in KEYS})
    bad_lines: list[str] = field(default_factory=list)
    empty: bool = False


@dataclass
class Log:
    suite: str
    segments: list[Segment] = field(default_factory=list)
    complete: bool = False
    unknown: int = 0


def text_of(line: str) -> str:
    m = RESULT_RE.match(line)
    assert m is not None
    head, sep, tail = m.group(2).partition(" - ")
    return tail if sep else m.group(2)


def parse(path: Path) -> Log:
    log = Log(path.name[:-4] if path.name.endswith(".log") else path.name)
    cur: Segment | None = None
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.rstrip()
        m = BEGIN_RE.match(line)
        if m:
            cur = Segment(m.group(2))
            log.segments.append(cur)
            continue
        m = RESULT_RE.match(line)
        if m and cur is not None:
            key = m.group(1).strip()
            cur.counts[key] += 1
            if key in ("FAIL", "TSF", "TTF"):
                cur.bad_lines.append(line)
            continue
        if line.startswith("[ INFO]"):
            continue
        if line.startswith("[  END] No test cases"):
            if cur is not None:
                cur.empty = True
            continue
        if END_RE.match(line):
            continue
        if line.startswith("COMPLETE:"):
            log.complete = True
            continue
        if line.strip() and line != "[BEGIN] UNIT TEST" and not line.startswith("**BUG**"):
            log.unknown += 1
    return log


def cases_of(log: Log) -> list[Segment]:
    return [s for s in log.segments
            if s.name not in ("UNIT TEST", "SUMMARY")
            and not (s.name == "SETUP" and sum(s.counts.values()) == 0)]


def build_xml(logs: list[Log]) -> ET.Element:
    root = ET.Element("testsuites")
    tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for log in logs:
        suite = ET.SubElement(root, "testsuite", name=log.suite)
        cs = cases_of(log)
        fails = errors = skipped = 0
        props = ET.SubElement(suite, "properties")
        ET.SubElement(props, "property", name="mir",
                      value=str(sum(s.counts["MIR"] for s in log.segments)))
        ET.SubElement(props, "property", name="na",
                      value=str(sum(s.counts["N/A"] for s in log.segments)))
        for s in cs:
            tc = ET.SubElement(suite, "testcase", classname=log.suite, name=s.name)
            if s.bad_lines:
                f = ET.SubElement(tc, "failure", message=text_of(s.bad_lines[0]))
                f.text = "\n".join(s.bad_lines)
                fails += 1
            elif s.empty and sum(s.counts.values()) == 0:
                ET.SubElement(tc, "skipped")
                skipped += 1
        if not log.complete:
            tc = ET.SubElement(suite, "testcase", classname=log.suite, name="(log complete)")
            ET.SubElement(tc, "error", message="incomplete log: no COMPLETE line")
            errors += 1
        n = len(cs) + (0 if log.complete else 1)
        for k, v in (("tests", n), ("failures", fails), ("errors", errors),
                     ("skipped", skipped), ("time", 0)):
            suite.set(k, str(v))
            if k in tot:
                tot[k] += v
        assert len(suite.findall("testcase")) == n
    for k, v in tot.items():
        root.set(k, str(v))
    root.set("time", "0")
    return root


def build_md(logs: list[Log]) -> str:
    out = ["| Suite | Segments | Total | Pass | Fail | MIR | TSF | TTF | N/A | Status |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    fails: list[str] = []
    for log in logs:
        c = {k: sum(s.counts[k] for s in log.segments) for k in KEYS}
        total = sum(c.values())
        bad = c["FAIL"] + c["TSF"] + c["TTF"]
        status = "INCOMPLETE" if not log.complete else ("FAIL" if bad else "pass")
        out.append(f"| {log.suite} | {len(cases_of(log)) + (0 if log.complete else 1)} | {total} | {c['PASS']} | {c['FAIL']} "
                   f"| {c['MIR']} | {c['TSF']} | {c['TTF']} | {c['N/A']} | {status} |")
        for s in cases_of(log):
            for line in s.bad_lines:
                m = RESULT_RE.match(line)
                assert m is not None
                fails.append(f"- {log.suite} / {s.name}: {m.group(2)}")
        if not log.complete:
            fails.append(f"- {log.suite}: incomplete log")
    out += ["", "## Failures", ""]
    out += fails if fails else ["No failures."]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="utlog2junit.py")
    ap.add_argument("--junit")
    ap.add_argument("--markdown")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("paths", nargs="+")
    try:
        ns = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not ns.junit and not ns.markdown:
        ap.print_usage(sys.stderr)
        return 2
    files: list[Path] = []
    for p in map(Path, ns.paths):
        if p.is_dir():
            found = sorted(f for f in p.iterdir() if f.is_file() and f.name.endswith(".log"))
            if not found:
                print(f"error: no .log files in {p}", file=sys.stderr)
                return 2
            files += found
        elif p.is_file():
            files.append(p)
        else:
            print(f"error: cannot read {p}", file=sys.stderr)
            return 2
    try:
        logs = [parse(f) for f in files]
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for lg in logs:
        counts[lg.suite] = counts.get(lg.suite, 0) + 1
    for lg, f in zip(logs, files):
        if counts[lg.suite] > 1:
            raw = str(f)
            lg.suite = raw[:-4] if raw.endswith(".log") else raw
    if ns.strict and any(lg.unknown for lg in logs):
        print("error: unrecognized line in strict mode", file=sys.stderr)
        return 2
    if ns.junit:
        root = build_xml(logs)
        ET.indent(root)
        ET.ElementTree(root).write(ns.junit, encoding="utf-8", xml_declaration=True)
    if ns.markdown:
        Path(ns.markdown).write_text(build_md(logs))
    bad = any(not lg.complete or any(s.bad_lines for s in lg.segments) for lg in logs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
