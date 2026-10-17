#!/usr/bin/env python3
"""Regenerates fixtures/expected.json from the fixture logs.

The parser here counts assertion lines directly and cross-checks them against
the END lines printed by ut_assert, so the expectations do not depend on any
tool under test.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESULT_RE = re.compile(r"^\[( PASS| FAIL|  MIR|  TSF|  TTF|  N/A)\] (\S+)(?: (.*))?$")
BEGIN_RE = re.compile(r"^\[BEGIN\] (\d{2,}) (.+?)\s*$")
END_RE = re.compile(r"^\[  END\] (\d{2,}) (\S+)\s+TOTAL::(\d+)\s+PASS::(\d+)\s+FAIL::(\d+)"
                    r"\s+MIR::(\d+)\s+TSF::(\d+)\s+TTF::(\d+)")
KEYS = ("PASS", "FAIL", "MIR", "TSF", "TTF", "N/A")


def text_of(rest: str) -> str:
    # "FILE:LINE - TEXT" or, for MIR without location, plain text.
    head, sep, tail = rest.partition(" - ")
    return tail if sep else rest


def parse(path: Path) -> dict[str, object]:
    segments: list[dict[str, object]] = []
    cur: dict[str, object] | None = None
    complete = False
    totals = {k: 0 for k in KEYS}
    for raw in path.read_text(errors="replace").splitlines():
        m = BEGIN_RE.match(raw)
        if m:
            cur = {"name": m.group(2), "counts": {k: 0 for k in KEYS},
                   "fail_lines": [], "closed_empty": False}
            segments.append(cur)
            continue
        m = RESULT_RE.match(raw)
        if m and cur is not None:
            key = m.group(1).strip()
            counts = cur["counts"]
            assert isinstance(counts, dict)
            counts[key] += 1
            totals[key] += 1
            if key in ("FAIL", "TSF", "TTF"):
                fl = cur["fail_lines"]
                assert isinstance(fl, list)
                fl.append(raw.rstrip())
            continue
        if raw.startswith("[  END] No test cases"):
            assert cur is not None
            cur["closed_empty"] = True
            continue
        m = END_RE.match(raw)
        if m and m.group(2) != "SUMMARY":
            assert cur is not None and cur["name"] == m.group(2), (path, raw)
            counts = cur["counts"]
            assert isinstance(counts, dict)
            printed = [int(g) for g in m.groups()[3:8]]
            got = [counts[k] for k in ("PASS", "FAIL", "MIR", "TSF", "TTF")]
            assert printed == got, (path, raw, printed, got)
            assert int(m.group(3)) == sum(counts.values()), (path, raw)
            continue
        if raw.startswith("COMPLETE:"):
            complete = True
    cases = []
    for seg in segments:
        counts = seg["counts"]
        assert isinstance(counts, dict)
        if seg["name"] == "UNIT TEST":
            continue
        assertions = sum(counts.values())
        if seg["name"] == "SETUP" and assertions == 0:
            continue
        failing = counts["FAIL"] + counts["TSF"] + counts["TTF"] > 0
        cases.append({"name": seg["name"], "failing": failing,
                      "skipped": bool(seg["closed_empty"]) and assertions == 0,
                      "fail_lines": seg["fail_lines"]})
    return {
        "complete": complete,
        "tests": len(cases) + (0 if complete else 1),
        "segments": len(cases),
        "failures": sum(1 for c in cases if c["failing"]),
        "skipped": sum(1 for c in cases if c["skipped"]),
        "errors": 0 if complete else 1,
        "mir": totals["MIR"],
        "na": totals["N/A"],
        "totals": {"total": sum(totals.values()), "pass": totals["PASS"],
                   "fail": totals["FAIL"], "mir": totals["MIR"],
                   "tsf": totals["TSF"], "ttf": totals["TTF"], "na": totals["N/A"]},
        "cases": cases,
    }


def main() -> int:
    out: dict[str, object] = {}
    for sub in ("real", "crafted"):
        for p in sorted((FIXTURES / sub).glob("*.log")):
            out[f"{sub}/{p.name}"] = parse(p)
    (FIXTURES / "expected.json").write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {len(out)} fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
