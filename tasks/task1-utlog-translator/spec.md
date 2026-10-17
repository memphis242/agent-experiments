# Task: ut_assert test log translator

## Background

`make native_std.runtest` runs every unit test in this mission and writes one
log per test runner to `build-native_std/test-results/*.log`. The logs are in
the ut_assert text format (example below). Continuous-integration dashboards
and reviewers need these results as JUnit XML and as a readable Markdown
summary. Write a command-line tool that performs that translation.

## Deliverable

A Python 3.12 script at `tools/utlog2junit.py`, standard library only,
executable directly (`python3 tools/utlog2junit.py ...`), with a short
usage section added to `tools/README.md` (create the file if absent).

### Command line

```
utlog2junit.py [--junit OUT.xml] [--markdown OUT.md] [--strict] PATH [PATH ...]
```

- Each PATH is a `.log` file or a directory. For a directory, every file
  ending in `.log` directly inside it is processed, in sorted filename order.
  Files given explicitly are processed whatever their extension.
- At least one of `--junit` or `--markdown` must be given; otherwise print
  usage to stderr and exit 2.
- Exit status, regardless of which outputs were requested: 0 when every
  parsed test segment passed and every log was complete, 1 when any segment
  failed (FAIL, TSF or TTF) or any log was incomplete, 2 on usage errors,
  unreadable input, or (with `--strict`) any unrecognized line.
- An empty directory or a set of inputs yielding no logs is a usage error
  (exit 2) with a clear message naming the path.

### Input grammar

A log consists of lines. Lines are classified by their first bracketed tag
(column 0, exactly as printed by ut_assert):

- `[BEGIN] NN NAME` opens test segment number NN (two or more digits) named
  NAME (rest of line). The first segment of every log is `SETUP`.
- `[ PASS] NN.MMM FILE:LINE - TEXT`, `[ FAIL] ...`, `[  MIR] ...`,
  `[  TSF] ...`, `[  TTF] ...`, `[  N/A] ...` are assertion results inside
  the open segment. MIR, TSF, TTF and N/A are the ut_assert categories for
  manual inspection required, test setup failure, test teardown failure and
  not applicable. FILE:LINE may be absent on MIR lines.
- `[ INFO] TEXT` is informational and never affects results.
- `[  END] NN NAME TOTAL::t PASS::p FAIL::f MIR::m TSF::s TTF::x` closes the
  segment with its counts. `[  END] No test cases` closes an empty segment.
- `[  END] NN SUMMARY TOTAL::t PASS::p ...` is the whole-log summary.
- `COMPLETE: N tests Segment(s) executed` ends the log.
- The following are recognized and ignored, also under `--strict`: blank
  lines, the opening `[BEGIN] UNIT TEST` line, and lines starting with
  `**BUG**` (OSAL diagnostics).
- Any other line (for example OSAL startup chatter) is unrecognized: ignored
  without `--strict`, a usage error (exit 2) with `--strict`.

The SUMMARY line is optional; some runners omit it. A log without a
`COMPLETE:` line is incomplete (truncated run or crash). When two inputs
would produce the same suite name, use each path as given (without `.log`)
as the suite name instead.

### JUnit XML output

- One `<testsuite>` per log file, `name` = file name without `.log`,
  with `tests` (number of `<testcase>` elements in the suite, including
  skipped and error cases), `failures` (number of `<failure>` elements),
  `errors` (number of `<error>` elements), `skipped` (number of
  `<skipped/>` elements), and `time="0"` attributes.
- One `<testcase>` per segment, `classname` = suite name, `name` = NAME,
  excluding the SUMMARY segment and the `SETUP` segment when it has no
  assertions.
- A segment with FAIL > 0 (or any TSF or TTF) gets a `<failure>` whose
  `message` is the first failing line's TEXT and whose body lists every
  FAIL, TSF and TTF line verbatim.
- A segment closed by `No test cases` gets a `<skipped/>`.
- An incomplete log gets one additional `<testcase name="(log complete)">`
  with an `<error message="incomplete log: no COMPLETE line">`.
- MIR and N/A counts are recorded on the suite as
  `<properties><property name="mir" value="N"/><property name="na" value="N"/></properties>`.
- The root element is `<testsuites>` with aggregated attributes. Output must
  be well-formed XML and parse with `xml.etree.ElementTree`.

### Markdown output

A table with one row per log and these columns in this order: suite name,
segments (same count as the JUnit `tests` attribute), assertions total,
pass, fail, MIR, TSF, TTF, N/A, and status (`pass`, `FAIL`, `INCOMPLETE`).
Below it, a heading (any level) containing the word "Failures" followed by
one line per FAIL, TSF or TTF assertion as `suite / segment: FILE:LINE - TEXT`
and one line per incomplete log as `suite: incomplete log`, or the sentence
"No failures." when there are none. The Markdown must render in GitHub
without raw HTML.

### Example input

```
[BEGIN] UNIT TEST

[BEGIN] 01 SETUP
[  END] No test cases

[BEGIN] 01 SAMPLE_APP_ReportHousekeeping
[ PASS] 01.001 coveragetest_sample_app_cmds.c:70 - CallCount: CFE_SB_TransmitMsg() (1) == 1 (1)
[ INFO]                        ABORT::0     WARN::0     FLOW::0     DEBUG::0     N/A::0
[  END] 01 SAMPLE_APP_ReportHousekeeping TOTAL::1     PASS::1     FAIL::0     MIR::0     TSF::0     TTF::0

[ INFO]                        ABORT::0     WARN::0     FLOW::0     DEBUG::0     N/A::0
[  END] 01 SUMMARY              TOTAL::1     PASS::1     FAIL::0     MIR::0     TSF::0     TTF::0
COMPLETE: 1 tests Segment(s) executed
```

## Tests

Add unit tests under `tools/tests/test_utlog2junit.py` runnable with
`python3 -m unittest discover tools/tests`. Cover at least: a passing log,
a log with a failing assertion, a truncated log, MIR lines, an empty
directory, and `--strict` rejecting an unknown line.

## Acceptance

The tool is run over the real `build-native_std/test-results` directory and
over prepared logs containing failures, truncation and MIR lines. Exit codes,
XML well-formedness, per-suite counts and the failure listing are checked
against the rules above.
