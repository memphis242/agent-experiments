"""Build and run the black-box STOR behaviour test inside the scratch copy of the mission tree.

Every assertion of the C test names the behaviour it checks as a tag in braces ("{tag} message"). The feedback
lists the distinct failing behaviours, one sentence each, in the order they first failed; the detail keeps every
failing assertion message.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from stor_live import Fail

# Behaviour tag -> the specified behaviour, in the language of the specification.
BEHAVIOUR: dict[str, str] = {
    "startup-run":
        "With a valid table, STOR_AppMain must complete its initialization and then wait for messages on its pipe "
        "(CFE_SB_ReceiveBuffer); it must not end its main loop or hang during startup.",
    "startup-roots":
        "At startup STOR must create TelemetryRoot and FileRoot when they are missing, one level at a time with OS_mkdir; "
        "an already existing directory (for which OS_mkdir returns success) is not an error.",
    "startup-init-event":
        "When initialization succeeds STOR must send STOR_INIT_INF_EID (1, INFORMATION) exactly once.",
    "startup-evs-register":
        "STOR must register with Event Services (CFE_EVS_Register) during initialization, before it sends its first "
        "event.",
    "startup-table-register":
        "STOR must register its table as \"StorageTable\" with the size of STOR_StorageTable_t (400 bytes) and a table "
        "validation function.",
    "startup-subscribe-cmds":
        "STOR must subscribe its pipe to STOR_CMD_MID, STOR_SEND_HK_MID and STOR_WAKEUP_MID.",
    "table-subscribe-entries":
        "STOR must subscribe to the MsgId of every used entry of the loaded table.",
    "startup-err-tblreg":
        "A table registration failure at startup must be reported with STOR_TBL_REG_ERR_EID (8, ERROR).",
    "startup-err-tblload":
        "A table load failure at startup must be reported with STOR_TBL_LOAD_ERR_EID (9, ERROR).",
    "startup-err-subscribe":
        "A subscription failure at startup must be reported with STOR_SUB_ERR_EID (11, ERROR).",
    "startup-err-mkdir":
        "A failure to create a root directory at startup must be reported with STOR_DIR_CREATE_ERR_EID (12, ERROR).",
    "startup-err-pipe":
        "A software bus pipe read error must be reported with STOR_PIPE_ERR_EID (7, ERROR).",
    "tbl-reject-roots":
        "The table validation function must reject an empty or unterminated TelemetryRoot or FileRoot.",
    "tbl-reject-quota":
        "The table validation function must reject TelemetryQuotaBytes 0 and FileQuotaBytes 0.",
    "tbl-reject-maxfile":
        "The table validation function must reject MaxPacketFileBytes below 1024 or above TelemetryQuotaBytes.",
    "tbl-reject-threshold":
        "The table validation function must reject PurgeThresholdPercent outside 1..100.",
    "tbl-reject-entry":
        "The table validation function must reject a used entry (MsgId not CFE_SB_INVALID_MSG_ID) with Priority 0 or "
        "Decimation 0.",
    "tbl-reject-duplicate":
        "The table validation function must reject a table in which two used entries have the same MsgId, wherever "
        "they are in the table.",
    "tbl-reject-event":
        "A rejected table must be reported with STOR_TBL_VAL_ERR_EID (10, ERROR).",
    "tbl-accept":
        "The table validation function must accept every table the specification allows, including the boundaries: "
        "MaxPacketFileBytes equal to TelemetryQuotaBytes (also both 1024), thresholds 1 and 100, any non-zero "
        "FileQuotaBytes, roots of up to 63 characters, unused entries whatever their Priority and Decimation, 32 used "
        "entries with values 255, and no used entry at all.",
    "decimation":
        "For each used entry Received counts every packet; a packet is stored only when (Received - 1) % Decimation "
        "== 0, and every other packet counts in the entry's Decimated and in PacketsDecimated.",
    "stats-packet":
        "SEND_STATS must transmit one STOR_StatsTlm_t on STOR_STATS_TLM_MID (16-byte telemetry header plus 32 elements "
        "of 24 bytes) with one element per used entry giving its MsgId, current Priority and Decimation, Received, "
        "Stored, Decimated and Dropped; the remaining elements are all zero.",
    "stats-event":
        "SEND_STATS must send STOR_STATS_INF_EID (28, INFORMATION) once after transmitting the statistics packet.",
    "hk-packet":
        "SEND_HK must transmit exactly one STOR_HkTlm_t on STOR_HK_TLM_MID: the 16-byte telemetry header followed by "
        "the 48-byte payload in the specified field order.",
    "hk-table-values":
        "Housekeeping must report TelemetryQuotaBytes, FileQuotaBytes and the threshold of the active table.",
    "hk-active-entries":
        "ActiveEntries must be the number of used entries in the active table.",
    "hk-initial":
        "Right after startup with empty roots every housekeeping counter, byte total, FileCount and OpenPacketFiles "
        "must be 0.",
    "hk-packet-totals":
        "PacketsStored and PacketsDecimated must count every stored and every decimated packet of all entries.",
    "hk-open-files":
        "STOR keeps at most one packet file open per entry, and OpenPacketFiles must equal the number of packet files "
        "currently open.",
    "cmd-hk-wakeup-not-counted":
        "SEND_HK and WAKEUP are not commands: they must never change CommandCounter or CommandErrorCounter.",
    "cmd-hk-wakeup-length":
        "A SEND_HK or WAKEUP whose length is not sizeof(CFE_MSG_CommandHeader_t) must be ignored (no housekeeping "
        "packet, no wakeup processing) with STOR_CMD_LEN_ERR_EID and CommandErrorCounter incremented.",
    "cmd-noop":
        "NOOP must send STOR_NOOP_INF_EID (3, INFORMATION) and increment CommandCounter.",
    "pkt-file-naming":
        "Packet files must be named <TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin: MMMM the upper-case four-digit hex MID, "
        "NNNNNNNN the eight-digit decimal sequence, counted per MID from 0 in an empty directory.",
    "pkt-file-content":
        "A packet file must contain the 64-byte cFE file header followed by each stored packet appended exactly as "
        "received (headers and payload), and nothing else.",
    "pkt-file-header":
        "Each packet file must start with a CFE_FS_Header_t written with CFE_FS_WriteHeader (which sets the 'cFE1' "
        "content type and writes the header big-endian), with SubType 0x5354 and Description \"STOR packet file\".",
    "pkt-rollover":
        "Before appending a packet that would make the open file exceed MaxPacketFileBytes, STOR must close the file and "
        "put the packet in a new file with the next sequence number; a packet that makes the file exactly "
        "MaxPacketFileBytes stays in the current file.",
    "pkt-no-reopen":
        "A closed packet file (roll-over, CLOSE_FILES, idle close, table update or write failure) is never reopened: the "
        "next stored packet of that MID starts a new file with the next sequence number.",
    "pkt-too-large":
        "A packet that cannot fit even in an empty file (64-byte header plus packet larger than MaxPacketFileBytes) must "
        "be dropped without closing the open file: STOR_FILE_WRITE_ERR_EID, the entry's Dropped and PacketsDropped "
        "incremented, and no file created.",
    "tlm-bytes-used":
        "TelemetryBytesUsed must always equal the total size on disk of the packet files in TelemetryRoot, file headers "
        "included.",
    "tlm-root-clean":
        "STOR must create nothing in TelemetryRoot except packet files (no index, temporary or state files, no "
        "subdirectories).",
    "cmd-close-files":
        "CLOSE_FILES must close every open packet file, increment CommandCounter and send STOR_CLOSE_INF_EID (27, "
        "INFORMATION) once.",
    "scan-tlm":
        "At startup TelemetryBytesUsed must be the total size of the packet files directly in TelemetryRoot (names "
        "matching tlm_<MMMM>_<NNNNNNNN>.bin exactly); other files and subdirectories there are ignored.",
    "scan-files":
        "At startup FileBytesUsed and FileCount must be the total size and the number of all regular files under "
        "FileRoot, recursively (directories themselves do not count).",
    "scan-sequence":
        "The sequence of each MID must continue from the highest sequence of that MID found directly in TelemetryRoot at "
        "startup, plus one.",
    "purge-threshold":
        "After every store, and on every other specified purge trigger, while TelemetryBytesUsed exceeds "
        "TelemetryQuotaBytes * PurgeThresholdPercent / 100 STOR must delete closed packet files one at a time; nothing "
        "is deleted below that limit.",
    "purge-order":
        "Purge must delete closed files in ascending order of the current priority of the file's MID, then of the "
        "sequence number: lowest priority and oldest sequence first.",
    "purge-tie-break":
        "Among closed files with equal priority and equal sequence number, purge must delete the one with the lower MID "
        "value first.",
    "purge-unused-mid":
        "A packet file whose MID is not a used table entry has priority 0 for purging, so it is purged first.",
    "purge-runtime-priority":
        "Purge order must use the current run-time priorities set by SET_PRIORITY.",
    "purge-open-files":
        "Open packet files are never purged, even above the threshold.",
    "purge-counters":
        "Each purged file must increment FilesPurged and subtract its size from TelemetryBytesUsed.",
    "purge-event":
        "Each purge pass that deletes at least one file must increment PurgeCycles and send exactly one "
        "STOR_PURGE_INF_EID (14, DEBUG).",
    "purge-set-threshold":
        "SET_PURGE_THRESHOLD must apply the new threshold at once and run a purge pass.",
    "purge-close":
        "CLOSE_FILES must run a purge pass after closing the files, so they are purged at once when over the threshold.",
    "purge-wakeup":
        "Every WAKEUP must run a purge pass, so files closed since the last pass are purged when over the threshold.",
    "purge-not-at-init":
        "No purge runs during initialization: existing packet files above the threshold stay until the first purge "
        "trigger (for example a WAKEUP).",
    "purge-non-packet":
        "Purge must only delete packet files directly in TelemetryRoot; other files there, and files in its "
        "subdirectories, are never deleted.",
    "quota-drop":
        "A packet due to be stored must be dropped when TelemetryBytesUsed, as it stands when the packet arrives, plus "
        "its size would exceed TelemetryQuotaBytes; a drop increments the entry's Dropped and PacketsDropped and stores "
        "nothing, and storing resumes as soon as there is room.",
    "quota-header":
        "The quota check must include the 64-byte file header when the packet would start a new file (first packet, "
        "after a close or on roll-over).",
    "quota-no-purge":
        "The quota check never deletes files (no purging to make room); space is freed only by the threshold purge.",
    "quota-event":
        "STOR_TLM_QUOTA_ERR_EID (13, ERROR) must be sent on an entry's first drop and not again for that entry until a "
        "packet of that entry has been stored.",
    "cmd-set-param-ok":
        "SET_PRIORITY and SET_DECIMATION for a used MsgId with a value 1..255 must change that entry's run-time value "
        "immediately, increment CommandCounter and send STOR_SET_PARAM_INF_EID (16, INFORMATION).",
    "cmd-set-param-reject":
        "SET_PRIORITY and SET_DECIMATION with value 0 or a MsgId that is not a used entry must be rejected with "
        "STOR_SET_PARAM_ERR_EID (17, ERROR) and CommandErrorCounter incremented, changing nothing else.",
    "cmd-set-threshold-ok":
        "SET_PURGE_THRESHOLD 1..100 must set the run-time threshold reported in housekeeping, increment CommandCounter "
        "and send STOR_SET_PARAM_INF_EID (16, INFORMATION).",
    "cmd-set-threshold-reject":
        "SET_PURGE_THRESHOLD outside 1..100 must be rejected with STOR_SET_PARAM_ERR_EID (17) and CommandErrorCounter "
        "incremented, keeping the threshold.",
    "cmd-bad-cc":
        "An unknown function code on STOR_CMD_MID must send STOR_CC_ERR_EID (2, ERROR) and increment "
        "CommandErrorCounter (unlike sample_app).",
    "cmd-bad-length":
        "A STOR_CMD_MID command whose length differs from its command structure must send STOR_CMD_LEN_ERR_EID (6, "
        "ERROR), increment CommandErrorCounter and not be executed.",
    "bad-mid":
        "A message on a MID that is neither a STOR command MID nor a used table entry must send STOR_MID_ERR_EID (5, "
        "ERROR) and change no counter.",
    "cmd-counter":
        "Every accepted STOR_CMD_MID command (NOOP, SET_*, ADD_FILE, DELETE_FILE, LIST_DIR, LIST_ALL, SEND_STATS, "
        "CLOSE_FILES) must increment CommandCounter exactly once and leave CommandErrorCounter unchanged.",
    "cmd-error-counter":
        "Every rejected STOR_CMD_MID command must increment CommandErrorCounter exactly once and leave CommandCounter "
        "unchanged.",
    "cmd-reset":
        "RESET_COUNTERS must zero CommandCounter, CommandErrorCounter, PacketsStored, PacketsDecimated, PacketsDropped, "
        "FilesPurged, PurgeCycles, FileAddsRejected and every entry's Received, Stored, Decimated and Dropped, send "
        "STOR_RESET_INF_EID (4, INFORMATION), and leave both command counters at 0.",
    "cmd-reset-keeps":
        "RESET_COUNTERS must not change TelemetryBytesUsed, FileBytesUsed, FileCount, open files, ActiveEntries or the "
        "run-time priority, decimation and threshold.",
    "file-add":
        "ADD_FILE must copy the source byte for byte to <FileRoot>/<DestPath>, creating missing intermediate "
        "directories, then increment FileCount and CommandCounter, add the size to FileBytesUsed and send "
        "STOR_FILE_ADD_INF_EID (22, INFORMATION).",
    "file-add-exists":
        "ADD_FILE to an existing destination must be rejected with STOR_FILE_EXISTS_ERR_EID (20, ERROR).",
    "file-add-source":
        "ADD_FILE whose source cannot be opened or is a directory must be rejected with STOR_FILE_SRC_ERR_EID (19, "
        "ERROR), creating nothing.",
    "file-add-quota":
        "ADD_FILE must be rejected with STOR_FILE_QUOTA_ERR_EID (21, ERROR) and FileAddsRejected incremented when "
        "FileBytesUsed plus the source size exceeds FileQuotaBytes (exactly reaching it is accepted), leaving no file or "
        "directory behind: directories are created only after every check has passed.",
    "file-add-errors":
        "Every rejected ADD_FILE must increment CommandErrorCounter; FileAddsRejected counts only quota rejections, and "
        "FileCount and FileBytesUsed stay unchanged.",
    "file-delete":
        "DELETE_FILE of a regular file must remove it, decrement FileCount, subtract its size from FileBytesUsed, "
        "increment CommandCounter and send STOR_FILE_DEL_INF_EID (24, INFORMATION).",
    "file-delete-errors":
        "DELETE_FILE of a missing path or of any directory, empty or not, must be rejected with STOR_FILE_DEL_ERR_EID "
        "(23, ERROR) and leave the directory in place.",
    "path-reject":
        "A command path that contains .., starts with / or is empty (except for LIST_DIR) must be rejected with "
        "STOR_PATH_ERR_EID (18, ERROR) and CommandErrorCounter incremented before anything else happens (no file or "
        "directory created, deleted or listed).",
    "path-unterminated":
        "A path field, including ADD_FILE's SourcePath, with no NUL within its 64 bytes must be rejected with "
        "STOR_PATH_ERR_EID.",
    "list-packet":
        "Every listing packet must be the full sizeof(STOR_ListTlm_t) (16 + 8 + 4 x 72 bytes), with the entries beyond "
        "EntriesInPacket zero-filled.",
    "list-empty":
        "An empty listing must be exactly one packet with TotalEntries 0, PacketIndex 0 and EntriesInPacket 0.",
    "list-split":
        "A listing must be split into packets of at most four entries, each carrying the TotalEntries of the whole "
        "listing, its 0-based PacketIndex and its EntriesInPacket.",
    "list-all":
        "LIST_ALL must list every regular file under FileRoot with its size, named by its path relative to FileRoot, "
        "depth-first in OS_DirectoryRead order, descending into each subdirectory at its position (directories "
        "themselves are not listed).",
    "list-dir":
        "LIST_DIR must list the immediate entries of <FileRoot>/<Path> (an empty path means FileRoot) in "
        "OS_DirectoryRead order with bare names: files with their size, directories as name/ with size 0.",
    "list-dots":
        "Directory traversals (startup scan, LIST_DIR, LIST_ALL) must skip the . and .. entries that OS_DirectoryRead "
        "returns.",
    "list-error":
        "LIST_DIR of a missing path or of a path that is not a directory must send STOR_LIST_ERR_EID (25, ERROR) and no "
        "listing packet.",
    "list-event":
        "Each successful listing command must send exactly one STOR_LIST_INF_EID (26, INFORMATION) with the entry count, "
        "however many packets the listing takes.",
    "table-update-check":
        "STOR must check for a table update at least once per SEND_HK cycle (CFE_TBL_Manage, then CFE_TBL_GetAddress) "
        "and release the table address with CFE_TBL_ReleaseAddress after each use, because table services cannot "
        "activate a new table while the address is held.",
    "table-update-subscriptions":
        "On a table update STOR must subscribe to the MsgIds of new entries and unsubscribe from MsgIds no longer in the "
        "table.",
    "table-update-runtime":
        "A table update must take effect no later than the second SEND_HK after activation and reset the run-time "
        "priority, decimation and threshold to the new table's values.",
    "table-update-entries":
        "After a table update an entry still present keeps its counters, open file and sequence; a removed entry's open "
        "file is closed and the entry disappears from statistics and ActiveEntries; new entries start at 0.",
    "idle-close":
        "On WAKEUP STOR must close every packet file whose last write was 10 seconds or more ago (measured with cFE "
        "time), and keep more recently written files open.",
    "write-fail-create":
        "When a packet file cannot be created or its header cannot be written, STOR must send STOR_FILE_WRITE_ERR_EID "
        "(15, ERROR), remove any partly created file, not consume the sequence number, and count the packet as neither "
        "stored nor dropped.",
    "write-fail-packet":
        "When OS_write of a packet returns anything other than the packet size, STOR must send STOR_FILE_WRITE_ERR_EID "
        "(15, ERROR), close the file, count the packet as neither stored nor dropped, and include the bytes that did "
        "reach the file in TelemetryBytesUsed.",
    "responsive":
        "STOR must handle every message and return to CFE_SB_ReceiveBuffer promptly (no hang or endless loop).",
    "exit-on-stop":
        "STOR's main loop must end, and the application exit through CFE_ES_ExitApp, as soon as CFE_ES_RunLoop returns "
        "false.",
    "exit-unexpected":
        "After a successful startup STOR must keep running its main loop while CFE_ES_RunLoop returns true; it must not "
        "leave the loop on its own.",
    "harness":
        "STOR created far more files or sent far more telemetry than the specified behaviour calls for in a short "
        "scenario.",
}

# Tags handled specially: "meta" lines summarize a test case, "crash" and "hang" name the scenario, "unmodelled"
# names the call, "skipped" marks scenarios not run after STOR stopped responding twice.
SPECIAL_TAGS = {"meta", "crash", "hang", "unmodelled", "skipped"}

# Startup failures that prevent every other behaviour from being checked.
CRITICAL_TAGS = ("startup-run", "startup-roots")

# Test case names (as registered in the C file) -> what STOR was doing, for crash and hang feedback.
SCENARIO: dict[str, str] = {
    "Startup and housekeeping": "startup, SEND_HK and NOOP",
    "Startup error: table registration": "startup with a failing table registration",
    "Startup error: table load": "startup with a failing table load",
    "Startup error: subscription": "startup with a failing subscription",
    "Startup error: directory creation": "startup with a failing OS_mkdir",
    "Startup error: pipe read": "a software bus pipe read error",
    "Table validation": "the table validation function",
    "Decimation and statistics": "storing decimated packets and SEND_STATS",
    "Packet files and roll-over": "packet file creation, roll-over and CLOSE_FILES",
    "Startup scan and sequence": "the startup scan of existing files",
    "Purge order": "purging closed packet files",
    "Purge of existing files": "purging packet files found at startup",
    "Purge after priority change": "purging after SET_PRIORITY",
    "Telemetry quota drops": "dropping packets at the telemetry quota",
    "Parameter commands": "SET_PRIORITY, SET_DECIMATION and SET_PURGE_THRESHOLD",
    "Command errors": "invalid function codes, lengths and message IDs",
    "Command counters": "accepted and rejected commands",
    "Reset counters": "RESET_COUNTERS",
    "File add and delete": "ADD_FILE and DELETE_FILE",
    "Path rejection": "invalid command paths",
    "Listing packets": "LIST_DIR and LIST_ALL",
    "Table update": "a table update",
    "Idle close": "the idle close on WAKEUP",
    "File write failures": "packet file write failures",
}

BASE = ("STOR must behave as specified when driven through its entry point STOR_AppMain with software bus messages, "
        "using only the names, structures, message IDs, function codes and event IDs in the specification.")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_RESULT_RE = re.compile(r"^\[\s*(FAIL|TSF|TTF|ABORT)\]\s*(?:\d+\.\d+\s+\S+:\d+\s+-\s+)?(.*)$")
_TAG_RE = re.compile(r"^\{([a-z0-9-]+)\}\s*(.*)$")
_SOURCE_TAG_RE = re.compile(r'\b(?:EXPECT|FAIL|SETUP_FAILED)\("([a-z0-9-]+)"')


def check_tag_coverage(pack: Path) -> list[str]:
    """Consistency of the C test and the sentences: every tag used has a sentence, every sentence is used, and every
    test case has a scenario description."""
    source = (pack / "hidden_tests" / "coveragetest_stor_hidden.c").read_text()
    used = set(_SOURCE_TAG_RE.findall(source))
    problems = [f"tag {t!r} has no feedback sentence" for t in sorted(used - set(BEHAVIOUR) - SPECIAL_TAGS)]
    problems += [f"feedback sentence {t!r} is not used by any assertion" for t in sorted(set(BEHAVIOUR) - used)]
    cases = set(re.findall(r'UtTest_Add\(\w+, \w+, \w+, "([^"]+)"\)', source))
    problems += [f"test case {c!r} has no scenario description" for c in sorted(cases - set(SCENARIO))]
    problems += [f"scenario {c!r} is not a test case" for c in sorted(set(SCENARIO) - cases)]
    return problems


def parse_output(out: str) -> list[tuple[str, str, str, str]]:
    """Failing results of the runner output as (test case, kind, tag, message), in output order."""
    results: list[tuple[str, str, str, str]] = []
    case = ""
    for raw in out.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        m = re.match(r"^\[BEGIN\] \d+ (.+?)\s*$", line)
        if m:
            case = m.group(1)
            continue
        m = _RESULT_RE.match(line)
        if not m:
            continue
        kind, text = m.group(1), m.group(2)
        t = _TAG_RE.match(text)
        tag, msg = (t.group(1), t.group(2)) if t else ("", text)
        results.append((case, kind, tag, msg))
    return results


def _scenarios(cases: list[str]) -> str:
    seen: list[str] = []
    for c in cases:
        s = SCENARIO.get(c, "the specified behaviour")
        if s not in seen:
            seen.append(s)
    return "; ".join(seen)


def compose(results: list[tuple[str, str, str, str]]) -> tuple[str, str]:
    """(feedback, detail) for the failing results: one sentence per distinct failing behaviour."""
    behaviours: list[str] = []
    crashed: list[str] = []
    hung: list[str] = []
    unmodelled: list[str] = []
    unexplained = False
    skipped = False
    unresponsive: list[str] = []
    for case, kind, tag, msg in results:
        if tag == "meta":
            continue
        if tag == "skipped":
            skipped = True
            continue
        if tag == "crash":
            crashed.append(case)
        elif tag == "hang":
            hung.append(case)
        elif tag == "unmodelled":
            name = msg.rsplit(" ", 1)[-1]
            if name not in unmodelled:
                unmodelled.append(name)
        elif tag in BEHAVIOUR:
            if tag == "responsive":
                unresponsive.append(case)
            if tag not in behaviours:
                behaviours.append(tag)
        else:
            unexplained = True
    critical = [t for t in behaviours if t in CRITICAL_TAGS]
    items = [BEHAVIOUR[t] for t in (critical or behaviours)]
    if unresponsive and not critical:
        i = behaviours.index("responsive")
        items[i] += f" It stopped responding while handling {_scenarios(unresponsive)}."
    if crashed:
        items.append("STOR must not crash, call exit or abort: it ended the process while handling "
                     f"{_scenarios(crashed)}.")
    if hung:
        items.append("STOR must keep responding: it did not return to its pipe within the time limit while handling "
                     f"{_scenarios(hung)}.")
    if unmodelled:
        items.append("STOR called " + ", ".join(unmodelled) + ", which the specification rules out (all processing in "
                     "the main task with the OSAL file and directory calls and the cFE message, table, event and time "
                     "calls); use those calls instead.")
    if unexplained and not items:
        items.append("STOR must not abort the cFE or OSAL calls it makes; an unexpected failure ended a scenario.")
    feedback = BASE
    if items:
        feedback += " Not yet met:\n" + "\n".join(f"- {s}" for s in items)
    if critical:
        feedback += "\nSTOR did not start correctly, so the rest of its behaviour could not be checked yet."
    elif skipped:
        feedback += "\nSTOR stopped responding repeatedly, so some of its behaviour could not be checked yet."
    lines = [f"{c}: [{k}] " + (f"{{{t}}} " if t else "") + m for c, k, t, m in results if t != "meta"]
    detail = f"{len(lines)} failed assertions"
    if unmodelled:
        detail += "; UNMODELLED CALLS: " + ", ".join(unmodelled)
    detail += ":\n" + "\n".join(lines)
    return feedback, detail


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
    # STOR_PACK_JOBS lets pack self-tests on a shared machine build with fewer jobs; the harness never sets it.
    inst = ctx.sh(["make", f"-j{os.environ.get('STOR_PACK_JOBS') or 16}", "native_std.install"], 600)
    if inst.returncode != 0:
        errs = [ln for ln in (inst.stdout + inst.stderr).splitlines() if "error" in ln.lower() or "undefined reference" in ln]
        raise Fail("the behaviour test does not build against apps/stor: " + "\n".join(errs[-15:]),
                   BASE + " The STOR sources must compile and link against the cFE and OSAL public APIs only (no "
                   "undefined references, no dependence on libraries other than the cFE core API).")
    runner = ctx.tree / "build-native_std" / "exe" / "cpu1" / "coverage-stor-hidden-testrunner"
    if not runner.is_file():
        raise Fail("coverage-stor-hidden-testrunner was not built", BASE)
    try:
        proc = subprocess.run([str(runner)], cwd=runner.parent, text=True, errors="replace", capture_output=True,
                              timeout=ctx.timeout_for(420))
    except subprocess.TimeoutExpired:
        raise Fail("the behaviour test did not finish within 420 s",
                   BASE + " STOR must keep answering messages; it stopped responding while the specified commands "
                   "and packets were delivered.") from None
    out = proc.stdout + proc.stderr
    results = parse_output(out)
    if proc.returncode < 0 or (proc.returncode != 0 and not results):
        raise Fail(f"the behaviour test runner itself ended abnormally (exit {proc.returncode}); tail: {out[-1500:]}",
                   BASE + " STOR must not crash, call exit or abort the process when given the specified commands, "
                   "packets, tables and file operations.")
    if results or proc.returncode != 0:
        feedback, detail = compose(results)
        raise Fail(detail, feedback)
