"""Build and run the black-box STOR behaviour test inside the scratch copy of the mission tree."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from stor_live import Fail

# Test case names (as registered in the C file) -> what the specification requires there.
CASE_FEEDBACK: dict[str, str] = {
    "Startup and housekeeping":
        "at startup STOR must register events and the StorageTable (400 bytes), subscribe to its command, SEND_HK and "
        "WAKEUP message IDs and every used table entry, create both roots one directory level at a time, emit event 1, "
        "and answer SEND_HK with the specified housekeeping packet",
    "Startup error events":
        "initialization and pipe failures must be reported with events 7 (pipe), 8 (table registration), 9 (table "
        "load), 11 (subscription) and 12 (directory creation)",
    "Table validation":
        "the table validation function must reject empty or unterminated roots, zero quotas, MaxPacketFileBytes below "
        "1024 or above TelemetryQuotaBytes, a threshold outside 1..100, a used entry with Priority or Decimation 0 and "
        "duplicate MsgIds (event 10), and accept the boundary values",
    "Decimation and statistics":
        "an entry stores a packet only when (Received - 1) % Decimation is 0, counting the rest as Decimated; "
        "SEND_STATS reports Received, Stored, Decimated, Dropped, Priority and Decimation per entry; SET_DECIMATION and "
        "SET_PRIORITY take effect at run time with event 16",
    "Packet files and roll-over":
        "packet files are named tlm_<MMMM>_<NNNNNNNN>.bin under TelemetryRoot, start with a CFE_FS_Header_t of sub type "
        "0x5354 and description \"STOR packet file\", hold the packets exactly as received, roll over before a "
        "packet would exceed MaxPacketFileBytes (exactly filling it is allowed), and CLOSE_FILES closes them with event 27",
    "Startup scan and sequence":
        "at startup TelemetryBytesUsed and FileBytesUsed come from the sizes of the existing files and the packet file "
        "sequence continues from the highest existing sequence of each MID plus one",
    "Purge order":
        "when TelemetryBytesUsed exceeds the threshold, closed packet files are deleted one at a time, lowest priority "
        "first and lowest sequence first, never an open file, with FilesPurged and PurgeCycles counted",
    "Purge after priority change":
        "run-time SET_PRIORITY and SET_PURGE_THRESHOLD change which files are purged first",
    "Telemetry quota drops":
        "a packet that would take TelemetryBytesUsed over TelemetryQuotaBytes is dropped (Dropped, PacketsDropped) "
        "with event 13 once per entry until it stores again, and SET_PURGE_THRESHOLD frees space",
    "Parameter commands":
        "SET_PRIORITY, SET_DECIMATION (value 0 or unknown MsgId) and SET_PURGE_THRESHOLD (outside 1..100) are "
        "rejected with event 17 and a command error; valid values are accepted with event 16",
    "Command errors":
        "an invalid function code gives event 2, a wrong command length event 6, an unknown message ID event 5, each "
        "counted as a command error",
    "Command counters":
        "every accepted command (including SEND_STATS, LIST_DIR, LIST_ALL, CLOSE_FILES and the file commands) increments "
        "CommandCounter, and every rejected one increments CommandErrorCounter and nothing else",
    "Reset counters":
        "RESET_COUNTERS zeroes the command, packet, purge and file-add counters and all per-entry counters without "
        "touching byte accounting, FileCount, open files or run-time parameters",
    "File add and delete":
        "ADD_FILE copies the source into the file area creating directories (event 22) and rejects an existing "
        "destination (20), an unreadable source (19) and a quota overrun (21, FileAddsRejected, no partial file); "
        "DELETE_FILE removes a regular file (24) and reports a missing file or directory (23)",
    "Path rejection":
        "file area paths containing .. or starting with / are rejected with event 18 for ADD_FILE, DELETE_FILE and "
        "LIST_DIR without changing anything",
    "Listing packets":
        "LIST_DIR and LIST_ALL send STOR_ListTlm_t packets of at most four entries with TotalEntries, PacketIndex and "
        "EntriesInPacket, directories as name/ with size 0, LIST_ALL depth-first with relative paths, an empty "
        "listing as one packet, and event 25 for a missing directory",
    "Table update":
        "after a table update STOR subscribes to new entries, unsubscribes from removed ones and resets the run-time "
        "priority, decimation and threshold to the table values",
    "Idle close":
        "on WAKEUP a packet file not written for 10 seconds or more is closed, and not earlier",
    "File write failures":
        "a packet file that cannot be created or written is reported with event 15 and STOR keeps running",
}

BASE = ("STOR must behave as specified when driven through its entry point STOR_AppMain with software bus messages, "
        "using only the names, structures, message IDs, function codes and event IDs in the specification.")


def feedback_for(cases: list[str]) -> str:
    parts = [CASE_FEEDBACK[c] for c in cases if c in CASE_FEEDBACK]
    if not parts:
        return BASE
    return BASE + " Not yet met: " + "; ".join(parts) + "."


def run_hidden(ctx, pack: Path) -> None:  # noqa: ANN001 - ctx is check.Ctx (imported as __main__)
    stor = ctx.stor
    hidden = stor / "hidden_acceptance"
    if hidden.exists():
        shutil.rmtree(hidden)
    hidden.mkdir()
    for name in ("CMakeLists.txt", "coveragetest_stor_hidden.c"):
        shutil.copy2(pack / "hidden_tests" / name, hidden / name)
    cm = stor / "CMakeLists.txt"
    text = cm.read_text()
    if "hidden_acceptance" not in text:
        cm.write_text(text.rstrip("\n") + "\n\nif (ENABLE_UNIT_TESTS)\n  add_subdirectory(hidden_acceptance)\nendif (ENABLE_UNIT_TESTS)\n")
    prep = ctx.sh(["make", "native_std.prep"], 300)
    if prep.returncode != 0:
        raise Fail("the behaviour test could not be configured next to apps/stor: " + (prep.stdout + prep.stderr)[-900:],
                   BASE + " The STOR sources could not be compiled for a stub-based test; keep apps/stor/CMakeLists.txt "
                   "following the sample_app conventions (add_cfe_app, fsw/inc, config) so the application can be "
                   "built that way.")
    inst = ctx.sh(["make", "-j16", "native_std.install"], 600)
    if inst.returncode != 0:
        errs = [ln for ln in (inst.stdout + inst.stderr).splitlines() if "error" in ln.lower() or "undefined reference" in ln]
        raise Fail("the behaviour test does not build against apps/stor: " + "\n".join(errs[-15:]),
                   BASE + " The STOR sources must compile and link against the cFE and OSAL public APIs only (no "
                   "undefined references, no dependence on libraries other than the cFE core API).")
    runner = ctx.tree / "build-native_std" / "exe" / "cpu1" / "coverage-stor-hidden-testrunner"
    if not runner.is_file():
        raise Fail("coverage-stor-hidden-testrunner was not built", BASE)
    import subprocess
    try:
        proc = subprocess.run([str(runner)], cwd=runner.parent, text=True, errors="replace", capture_output=True,
                              timeout=ctx.timeout_for(300))
    except subprocess.TimeoutExpired:
        raise Fail("the behaviour test did not finish within 300 s (a hang)",
                   BASE + " STOR must keep answering messages; it stopped responding while the specified commands "
                   "and packets were delivered.") from None
    out = proc.stdout + proc.stderr
    failed_cases: list[str] = []
    current = ""
    fails: list[str] = []
    for line in out.splitlines():
        m = re.match(r"^\[BEGIN\] \d+ (.+?)\s*$", line)
        if m:
            current = m.group(1)
            continue
        if line.startswith("[ FAIL]"):
            fails.append(f"{current}: {line[8:]}")
            if current and current not in failed_cases and current != "SETUP":
                failed_cases.append(current)
    if proc.returncode < 0 or (proc.returncode != 0 and not fails):
        raise Fail(f"the behaviour test crashed (exit {proc.returncode}); tail: {out[-800:]}",
                   BASE + " STOR must not crash (or abort an assertion) when given the specified commands, packets, "
                   "tables and file operations.")
    if fails or proc.returncode != 0:
        raise Fail(f"{len(fails)} failed assertions; first: " + " | ".join(fails[:8]), feedback_for(failed_cases))
