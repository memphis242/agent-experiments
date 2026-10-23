#!/usr/bin/env python3
"""Acceptance checks for Task 5 (two-node flight system: STOR, CMDVAL/CMDI, TLMO, GW, SFI, CTF system tests).

Usage: check.py <repo-path> <out.json>

The repository is copied to scratch space and built there; the original is never modified. Checks are
graded per subsystem (S1..S8, N) and overall. Build, unit-test and static work
finish before live checks. Live groups run in nested namespaces one at a time
under a host-coordinated lock with explicit CPU reservation and scheduling evidence.
The result file has exactly the contract's keys; the per-subsystem summary is written next to it as
<out-stem>.subsystems.json and printed. Exit status: 0 every check passed, 1 at least one failed,
2 an infrastructure fault before the agent's code was used.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

PACK = Path(__file__).resolve().parent
sys.path.insert(0, str(PACK))

import ctf_checks as cc  # noqa: E402
import harness_checks as hc  # noqa: E402
import isolation  # noqa: E402
import live_cmdi as l3  # noqa: E402
import live_gw as l5  # noqa: E402
import live_nodes as ln  # noqa: E402
import live_sfi as l6  # noqa: E402
import live_mode as l7  # noqa: E402
import live_stor as l12  # noqa: E402
import live_tlmo as l4  # noqa: E402
import preflight  # noqa: E402
import timing  # noqa: E402
import static_checks as st  # noqa: E402
from common import EnvironmentFail, Fail, InfraError, PrereqFail  # noqa: E402
from hidden_glue import HIDDEN_CHECKS, HiddenTests, load_case_specs  # noqa: E402
from node import Node  # noqa: E402

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TOTAL_BUDGET_S = 180 * 60
JOBS = "-j16"
DEFAULT_GROUP_JOBS = 1
SUBSYSTEMS = {
    "S1": "S1 STOR base", "S2": "S2 STOR transfer", "S3": "S3 CMDVAL and CMDI", "S4": "S4 TLMO", "S5": "S5 GW",
    "S6": "S6 SFI", "S7": "S7 MODE", "S8": "S8 CTF system tests", "N": "N Non-functional and common conventions",
}


class BudgetExceeded(Exception):
    """The review's time budget ran out; remaining checks are reported as not run."""


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    sub: str
    term: str
    text: str
    fn: Callable[["Ctx"], None]
    lane: str
    live: bool = False

    @property
    def label(self) -> str:
        return f"{SUBSYSTEMS[self.sub]} - {self.term}"

    @property
    def feedback(self) -> str:
        return f"{self.label} [{self.check_id}]: {self.text}"


class Ctx:
    def __init__(self, repo: Path, work: Path, deadline: float) -> None:
        self.repo = repo
        self.work = work
        self.tree = work / "tree"
        self.deadline = deadline
        self.copy_lock = threading.Lock()
        self.build_lock = threading.Lock()
        self.runtest_lock = threading.Lock()
        self.tree_lock = threading.Lock()
        self.build_ok: bool | None = None
        self.build_log = ""
        self.warnings: list[str] = []
        self.runtest_proc: subprocess.CompletedProcess[str] | None = None
        self.coverage: dict = {}
        self.nodes: dict[int, Node] = {}
        self.session_no = 0
        self.cache: dict[str, object] = {}
        self.end_callbacks: list[Callable[[], None]] = []
        self.boot_failures: dict[int, list[str]] = {1: [], 2: []}
        self.boot_fail_dir: Path | None = None  # set when several isolated groups share the start-up record
        self.pristine_cache: dict[int, dict[str, bytes]] = {}
        self.hidden: HiddenTests | None = None
        self.ctf: cc.CtfEnv | None = None
        self.queue_preflight: dict[str, object] | None = None

    # -- time ----------------------------------------------------------------
    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def timeout_for(self, want: float) -> float:
        left = self.remaining() - 15
        if left < 20:
            raise BudgetExceeded("the review's time budget ran out")
        return min(want, left)

    # -- tree and build ----------------------------------------------------------
    def copy_tree(self) -> None:
        with self.copy_lock:
            if self.tree.exists():
                return
            skip = shutil.ignore_patterns(".git", "build-native_std", "__pycache__", "*.pyc")
            shutil.copytree(self.repo, self.tree, ignore=skip, symlinks=True)
            for d in ("CTF", "venv", "results"):
                shutil.rmtree(self.tree / "ctf" / d, ignore_errors=True)

    def sh(self, cmd: list[str], timeout: float, env: dict[str, str] | None = None,
           cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(cmd, cwd=cwd or self.tree, text=True, errors="replace", capture_output=True,
                                  timeout=self.timeout_for(timeout), env=env)
        except subprocess.TimeoutExpired as exc:
            tail = str(exc.stdout or "")[-600:]
            raise Fail(f"`{' '.join(cmd)}` did not finish within {timeout:.0f} s; output tail: {tail}") from None

    def build(self) -> None:
        with self.build_lock:
            if self.build_ok is None:
                self.copy_tree()
                prep = self.sh(["make", "native_std.prep"], 400)
                log = prep.stdout + prep.stderr
                ok = prep.returncode == 0
                if ok:
                    inst = self.sh(["make", JOBS, "native_std.install"], 1200)
                    log += inst.stdout + inst.stderr
                    ok = inst.returncode == 0
                self.warnings = [ln_ for ln_ in log.splitlines() if "warning:" in ln_]
                if not ok:
                    lines = [ln_ for ln_ in log.splitlines() if "error" in ln_.lower()]
                    log = "\n".join(lines[-25:]) or log[-1500:]
                self.build_log = log
                self.build_ok = ok
        if not self.build_ok:
            raise PrereqFail("the mission does not configure and build: " + self.build_log[-1200:])

    def runtest(self) -> subprocess.CompletedProcess[str]:
        self.build()
        with self.runtest_lock:
            if self.runtest_proc is None:
                self.runtest_proc = self.sh(["make", JOBS, "native_std.runtest"], 900)
        return self.runtest_proc

    def pristine(self, node: int) -> dict[str, bytes]:
        if node not in self.pristine_cache:
            self.build()
            imgs = {}
            for app, (fname, _name, _size) in st.spec.TABLES.items():
                f = self.tree / "build-native_std" / "exe" / f"cpu{node}" / "cf" / fname
                if f.is_file():
                    imgs[app] = f.read_bytes()
            self.pristine_cache[node] = imgs  # a missing image is reported by the check that needs it
        return self.pristine_cache[node]

    # -- sessions -------------------------------------------------------------------
    @contextlib.contextmanager
    def boot_record(self) -> Iterator[dict[int, list[str]]]:
        """Node start-up failures since each node's last successful start, shared by every isolated group when
        boot_fail_dir is set (a file under an exclusive lock), else this process's own record."""
        if self.boot_fail_dir is None:
            yield self.boot_failures
            return
        self.boot_fail_dir.mkdir(parents=True, exist_ok=True)
        state = self.boot_fail_dir / "boot-failures.json"
        with open(self.boot_fail_dir / "lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = {1: [], 2: []}
            if state.is_file():
                record.update({int(k): v for k, v in json.loads(state.read_text()).items()})
            yield record
            tmp = state.with_suffix(".tmp")
            tmp.write_text(json.dumps(record))
            tmp.replace(state)

    def on_session_end(self, fn: Callable[[], None]) -> None:
        self.end_callbacks.append(fn)

    def shutdown_session(self) -> None:
        for fn in reversed(self.end_callbacks):
            try:
                fn()
            except Exception:  # noqa: BLE001 - clean-up must continue
                traceback.print_exc()
        self.end_callbacks = []
        self.cache = {}
        for n in self.nodes.values():
            n.stop()
        self.nodes = {}

    def session(self, nodes: tuple[int, ...], seed: dict[int, dict[str, bytes]] | None = None,
                startup_edit: Callable[[str], str] | None = None) -> dict[int, Node]:
        self.build()
        self.shutdown_session()
        if self.queue_preflight is None:
            self.queue_preflight = preflight.namespace_preflight()
        with self.boot_record() as record:
            for nid in nodes:
                if len(record[nid]) >= 2:
                    raise PrereqFail(f"cpu{nid} could not be started earlier in this review: {record[nid][-1]}")
        self.timeout_for(90)
        self.session_no += 1
        started: dict[int, Node] = {}
        try:
            for nid in nodes:
                n = Node(self.tree, self.work / "nodes" / f"s{self.session_no}-cpu{nid}", nid, self.pristine(nid))
                n.prepare(startup_edit=startup_edit, seed=(seed or {}).get(nid))
                try:
                    n.start()
                except PrereqFail as exc:
                    with self.boot_record() as record:
                        record[nid].append(str(exc))
                    raise
                with self.boot_record() as record:
                    record[nid] = []
                started[nid] = n
                self.nodes[nid] = n
            inventory = preflight.verify_nodes(tuple((n.cpu, n.pids()[0]) for n in started.values()))
            if "inventory" not in self.queue_preflight:
                # Learn the actual OSAL inventory, then stop the entire node
                # lifetime before probing it as the final non-root identity.
                for n in started.values():
                    n.stop()
                probes = tuple(preflight.Queue(q.name, q.depth, q.message_size) for q in inventory)
                if len(nodes) == 1:
                    probes += probes
                preflight.allocation_probe(probes)
                self.queue_preflight["inventory"] = tuple((q.name, q.depth, q.message_size) for q in probes)
                for nid, n in started.items():
                    n.prepare(startup_edit=startup_edit, seed=(seed or {}).get(nid))
                    n.start()
                preflight.verify_nodes(tuple((n.cpu, n.pids()[0]) for n in started.values()))
            (self.work / "queue-preflight.json").write_text(json.dumps(self.queue_preflight, indent=2) + "\n")
        except BaseException:
            for n in started.values():
                n.stop()
            self.nodes = {}
            raise
        return started


# ============================================================================ checks

HIDDEN_SPECS = load_case_specs(PACK)
GENERIC_HIDDEN = ("The component must behave as specified when driven only through its entry point with software bus "
                  "messages, tables and I/O, using only the names, structures, message IDs, function codes and event IDs of "
                  "the specification.")


def hidden_text(check_id: str) -> str:
    spec_ = HIDDEN_SPECS.get(check_id)
    return spec_.base if spec_ is not None else GENERIC_HIDDEN


def hidden(check_id: str) -> Callable[[Ctx], None]:
    def run(ctx: Ctx) -> None:
        ctx.build()
        ctx.runtest()  # coverage data is measured before the tests are wired in
        if ctx.hidden is None:
            ctx.hidden = HiddenTests(ctx.tree, PACK, ctx.sh)
        spec_ = BY_ID[check_id]
        with ctx.tree_lock:
            ctx.hidden.run_all(ctx.timeout_for)
        ctx.hidden.verdict(check_id, spec_.label, ctx.timeout_for)
    return run


def script(name: str) -> Callable[[Ctx], None]:
    return getattr(cc, "c_" + name.removesuffix(".json").removeprefix("st_"))


def table(app: str) -> Callable[[Ctx], None]:
    return lambda ctx: st.c_default_tables(ctx, app)


C = CheckSpec
CHECKS: list[CheckSpec] = [
    # ---------------------------------------------------------------- N
    C("n-build-werror", "N", "N Build", "make native_std.prep and make -j16 native_std.install must succeed with no compiler "
      "warnings, with -Werror kept in sample_defs/arch_build_custom.cmake.", st.c_build_werror, "build"),
    C("n-component-layout", "N", "C.2 Application structure", "Each of apps/stor, apps/cmdi, apps/tlmo, apps/gw and apps/sfi "
      "must have fsw/src, fsw/inc, fsw/tables, config, unit-test and DESIGN.md, the headers <app>_topicids.h, <app>_eventids.h, "
      "<app>_fcncodes.h, default_<app>_msgids.h, default_<app>_fcncode_values.h, default_<app>_tbldefs.h, the table source "
      "fsw/tables/<app>_tbl.c and the entry point <APP>_AppMain, with no EDS variants; SFI_ComputeClusters lives in "
      "apps/sfi/fsw/src/sfi_cluster.c and sfi_cluster.h; libs/cmdval_lib has fsw/src, fsw/public_inc/cmdval_lib.h, unit-test, "
      "ut-stubs, DESIGN.md and CMDVAL_LibInit.", st.c_layout, "misc"),
    C("n-targets", "N", "C.6 Mission integration (targets.cmake, default_osconfig.cmake)", "cmdval_lib stor cmdi tlmo gw sfi must "
      "be in MISSION_GLOBAL_APPLIST, cpu1_APPLIST and cpu2_APPLIST must each be sch_lab only, and OSAL_CONFIG_MAX_FILE_NAME "
      "must be 32.", st.c_targets, "misc"),
    C("n-startup-scripts", "N", "C.6 Mission integration (generate_startup.cmake)", "The generated cf/cfe_es_startup.scr of both "
      "nodes must name neither ci_lab nor to_lab, list cmdval_lib right after sample_lib, cmdi (priority 40, stack 32768) and "
      "tlmo (45, 32768) in the lab apps' slots, and stor, gw and sfi (70, 131072) with CFS_APP_STARTUP_ORDER "
      "lc cf ds fm hk hs sc md cs stor gw sfi sbn.", st.c_startup_scripts, "misc"),
    C("n-install-custom", "N", "C.6 Mission integration (install_custom.cmake)", "sample_defs/cpu1/install_custom.cmake and "
      "cpu2/install_custom.cmake must drop ci_lab and to_lab from APP_INCLUDE_TABLE_LIST, add the new apps as hk is, drop the "
      "to_lab.table lines and add_cfe_tables(to_lab to_lab_sub_alt.c), and give tlmo.table, stor.table and sfi.table the "
      "APP_INCLUDE_TABLE_LIST include directories and APP_DEFINITION_TABLE_LIST definitions.", st.c_install_custom, "misc"),
    C("n-schedule", "N", "C.6 Mission integration (sch_lab_table.c)", "sample_defs/tables/sch_lab_table.c must remove the "
      "CI_LAB and TO_LAB SEND_HK entries and includes and schedule STOR SEND_HK 49 and WAKEUP 10, CMDI SEND_HK 50 and WAKEUP 1, "
      "TLMO SEND_HK 51 and WAKEUP 1, GW SEND_HK 53 and WAKEUP 1, SFI SEND_HK 55 and WAKEUP 10, within 32 entries.",
      st.c_schedule, "misc"),
    C("n-readme", "N", "C.6 Mission integration (README.md)", "The mission README.md must name CMDI and TLMO on their ports and "
      "the framed commands in its Ground interface section, and TLMO's table instead of to_lab_sub.c in Registering an app.",
      st.c_readme, "misc"),
    C("n-change-scope", "N", "N Build (files changed)", "Existing apps, including apps/ci_lab and apps/to_lab, must be unchanged; "
      "outside the new apps and library only the files of C.6, the run scripts of C.1, tools/udp_harness.py, the CTF files "
      "of S7 and the reports may change.", st.c_change_scope, "misc"),
    C("n-unit-tests", "N", "N Build (unit tests)", "make -j16 native_std.runtest must pass, with CI_LAB's and TO_LAB's own unit "
      "tests no longer built or run.", st.c_unit_tests, "cpu"),
    C("n-coverage-tests", "N", "N Unit tests", "Every source module of stor, cmdi, tlmo, gw, sfi and cmdval_lib must have "
      "unit-test/coveragetest/coveragetest_<module>.c, built and run by make native_std.runtest with passing assertions only.",
      st.c_coverage_tests_present, "cpu"),
    C("n-coverage", "N", "N Coverage", "Line coverage of every source file of stor, cmdi, tlmo, gw, sfi and cmdval_lib, "
      "including the code CMDI and TLMO inherited from CI_LAB and TO_LAB, must be at least 95 percent, measured from the "
      "unit-test build's gcov data.", st.c_coverage, "cpu"),
    C("n-design-notes", "N", "N Design notes", "DESIGN.md in each of apps/stor, apps/cmdi, apps/tlmo, apps/gw, apps/sfi and "
      "libs/cmdval_lib must be at most 80 lines and cover purpose, data flow, main data structures, timing (what runs on which "
      "wakeup), error handling and the decisions made.", st.c_design_notes, "misc"),
    C("n-system-report", "N", "N Report", "docs/reports/system.md must have the sections Design summary, Interfaces, Unit tests "
      "(counts per app and library), Coverage (per source file), System tests (referring to ctf/run_system_tests.sh, with the "
      "CTF plugins and patches) and Limitations, and record how the nodes' RAM disks are kept apart and their clocks kept "
      "within the age window.", st.c_system_report, "misc"),
    C("n-run-scripts", "N", "C.1 Nodes (run scripts)", "CFS_CPU=cpu2 tools/run_cfs.sh must start core-cpu2 from exe/cpu2 with "
      "its log and PID in build-native_std/run/cpu2/, cpu1 must keep build-native_std/run/cfs.log, and tools/stop_cfs.sh "
      "with the same CFS_CPU must stop the node.", ln.c_run_scripts, "isolated", live=True),
    C("n-nodes-operational", "N", "C.1 Nodes and C.6 Mission integration", "cpu1 and cpu2 started with tools/run_cfs.sh "
      "(CFS_CPU=cpu2) must run at the same time and reach OPERATIONAL with the initialization events of STOR, CMDI, TLMO, GW "
      "and SFI and without CI_LAB or TO_LAB.", ln.c_nodes_operational, "isolated", live=True),
    C("n-ram-disks-distinct", "N", "C.1 Nodes (RAM disks)", "cpu1 and cpu2 must not share one OSAL RAM disk directory while "
      "both run (on this host both default to /dev/shm/osal:RAM); make each node's RAM disk distinct.",
      ln.c_ram_disks_distinct, "isolated", live=True),
    C("n-node-clocks", "N", "S5.2 Egress framing (node clocks)", "The onboard clocks of cpu1 and cpu2 started with "
      "tools/run_cfs.sh must agree within the table's MaxAgeSeconds (30 s) so that the two gateways accept each other's frames.",
      ln.c_node_clocks, "isolated", live=True),
    # ---------------------------------------------------------------- S1
    C("s1-default-table", "S1", "S1.1 Table", "The installed StorageTable (/cf/stor_tbl.tbl, 400 bytes) of each node must default "
      "to roots /cf/stor/tlm and /cf/stor/files, quotas 262144 and 1048576, MaxPacketFileBytes 16384, threshold 80 and the "
      "entries CFE_EVS_HK_TLM_MID (10, 1), CFE_ES_HK_TLM_MID (20, 1), SAMPLE_APP_HK_TLM_MID (30, 2), CFE_TIME_HK_TLM_MID (40, 1) "
      "with that node's MIDs.", table("STOR"), "misc"),
    C("s1-scheduled-housekeeping", "S1", "S1.5 Housekeeping", "STOR housekeeping (0x08C4) must reach UDP 2234 through TLMO "
      "without any request, scheduled every 4.9 s.", l12.c_scheduled_housekeeping, "isolated", live=True),
    C("s1-startup-and-hk", "S1", "S1.5 Housekeeping and events", "STOR must emit its initialization event (1) once and answer a "
      "framed SEND_HK with STOR_HkTlm_t in the specified field order (72-byte payload) reporting the default quotas, threshold 80, "
      "ActiveEntries 4 and idle transfer fields; NOOP increments CommandCounter with event 3; the default entries are stored.",
      l12.c_startup_and_hk, "isolated", live=True),
    C("s1-decimation-and-stats", "S1", "S1.2 Telemetry storage (decimation) and S1.4 SEND_STATS", "STOR must store a packet only "
      "when (Received - 1) % Decimation == 0, count the others as Decimated, report per entry MsgId, Priority, Decimation, "
      "Received, Stored, Decimated and Dropped in STOR_StatsTlm_t, and apply SET_DECIMATION and SET_PRIORITY at run time; tables "
      "naming reserved MIDs are loaded through cFE Table Services.", l12.c_decimation_and_stats, "isolated", live=True),
    C("s1-packet-file-naming", "S1", "S1.2 Telemetry storage (packet files)", "Stored packets must be appended unchanged to "
      "<TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin starting with a CFE_FS_Header_t of sub type 0x5354 and description "
      "\"STOR packet file\"; CLOSE_FILES closes them with event 27 and TelemetryBytesUsed equals the bytes on disk.",
      l12.c_packet_file_naming, "isolated", live=True),
    C("s1-rollover", "S1", "S1.2 Telemetry storage (roll-over)", "STOR must close a packet file and open the next sequence before "
      "a packet would make it exceed MaxPacketFileBytes (exactly filling it is allowed), with sequences per MID from 0.",
      l12.c_rollover, "isolated", live=True),
    C("s1-idle-file-close", "S1", "S1.2 WAKEUP (idle close)", "On every 1 Hz WAKEUP STOR must close packet files not written for "
      "10 s or more, and not earlier.", l12.c_idle_file_close, "isolated", live=True),
    C("s1-purge-order", "S1", "S1.2 Purge", "While TelemetryBytesUsed exceeds PurgeThresholdPercent of TelemetryQuotaBytes, after "
      "every store and on every WAKEUP, STOR must delete one closed packet file at a time, lowest current priority first and "
      "lowest sequence first, counting FilesPurged and PurgeCycles; run-time SET_PRIORITY changes the order.",
      l12.c_purge_order, "isolated", live=True),
    C("s1-purge-archived-first", "S1", "S1.2 Purge (archived files)", "Archived (.arc) packet files count as packet files and must "
      "be purged before any .bin file, whatever their MID's priority.", l12.c_purge_archived_first, "isolated", live=True),
    C("s1-quota-drop", "S1", "S1.2 Quota", "A packet that would take TelemetryBytesUsed over TelemetryQuotaBytes (a new file's "
      "header included) must be dropped (Dropped, PacketsDropped) with STOR_TLM_QUOTA_ERR_EID once per entry until it stores "
      "again; SET_PURGE_THRESHOLD triggers a purge check.", l12.c_quota_drop, "isolated", live=True),
    C("s1-restart-scan", "S1", "S1.2 Startup", "At start-up STOR must delete *.part files in the file area, initialize "
      "TelemetryBytesUsed from the packet files (.bin and .arc) and FileBytesUsed and FileCount from the file area, and continue "
      "each MID's sequence after the highest existing one.", l12.c_restart_scan, "isolated", live=True),
    C("s1-set-param-errors", "S1", "S1.4 Commands (SET_*)", "SET_PRIORITY and SET_DECIMATION must reject value 0 or an unknown "
      "MsgId and SET_PURGE_THRESHOLD values outside 1..100 with STOR_SET_PARAM_ERR_EID and a command error; valid values give "
      "STOR_SET_PARAM_INF_EID; an unknown function code and a wrong length give events 2 and 6.",
      l12.c_set_param_errors, "isolated", live=True),
    C("s1-file-add-list-delete", "S1", "S1.3 File area", "ADD_FILE, DELETE_FILE, LIST_DIR and LIST_ALL must behave as specified "
      "(events 22, 24, 25, 26, 23; FileCount and FileBytesUsed; listings of at most four entries, directories with a trailing / "
      "and size 0, LIST_ALL depth-first relative to FileRoot, an empty listing as one packet).",
      l12.c_file_add_list_delete, "isolated", live=True),
    C("s1-file-path-rejection", "S1", "S1.3 File area (paths)", "A path containing .. or starting with / must be rejected with "
      "STOR_PATH_ERR_EID for ADD_FILE, DELETE_FILE and LIST_DIR without touching anything outside FileRoot.",
      l12.c_file_path_rejection, "isolated", live=True),
    C("s1-file-exists-rejection", "S1", "S1.3 ADD_FILE", "ADD_FILE must reject an existing destination with "
      "STOR_FILE_EXISTS_ERR_EID and an unopenable source with STOR_FILE_SRC_ERR_EID.", l12.c_file_exists_rejection, "isolated",
      live=True),
    C("s1-file-quota-rejection", "S1", "S1.3 ADD_FILE (quota)", "ADD_FILE must reject a file that would take FileBytesUsed over "
      "FileQuotaBytes with STOR_FILE_QUOTA_ERR_EID, FileAddsRejected and no partial file, and accept one that exactly fills it.",
      l12.c_file_quota_rejection, "isolated", live=True),
    C("s1-reset-counters", "S1", "S1.4 RESET_COUNTERS", "RESET_COUNTERS must zero the command, per-entry, packet, purge, file-add "
      "and transfer counters without changing byte accounting, file counts, open files or run-time parameters.",
      l12.c_reset_counters, "isolated", live=True),
    C("s1-table-validation-live", "S1", "C.4 Tables (StorageTable)", "A StorageTable that fails validation (for example threshold "
      "0 or duplicate MsgIds) loaded through cFE Table Services must be rejected and leave the previous table active.",
      l12.c_table_validation_live, "isolated", live=True),
    C("s1-hidden-stor", "S1", "S1 Acceptance (coverage tests)", hidden_text("s1-hidden-stor"), hidden("s1-hidden-stor"), "cpu"),
    # ---------------------------------------------------------------- S2
    C("s2-ground-transfer", "S2", "S2.3 Sending (ground) and S2.4 DELETE", "A TRANSFER of a file-area file to the ground must "
      "send STOR_XferTlm_t segments on STOR_XFER_TLM_MID (forwarded by TLMO) with the S2.1 fields (TransferId, nodes, Area, "
      "SegmentIndex, SegmentCount, FileSize, FileCrc as CRC-32/ISO-HDLC, Offset, DataLength, DestPath, zeroed unused bytes), "
      "complete with STOR_XFER_DONE_INF_EID and apply DELETE with the accounting of DELETE_FILE.", l12.c_ground_transfer,
      "isolated", live=True),
    C("s2-tlm-transfer-archive", "S2", "S2.4 MARK_ARCHIVED", "A TRANSFER of a closed packet file to the ground with MARK_ARCHIVED "
      "must send its exact contents and rename tlm_MMMM_NNNNNNNN.bin to .arc without changing byte accounting.",
      l12.c_tlm_transfer_archive, "isolated", live=True),
    C("s2-peer-transfer", "S2", "S2.3 Sending (peer) and S2.5 Receiving", "A TRANSFER from cpu1 to node 2, with segments and "
      "acknowledgements carried as CMDI frames, must complete on cpu1 (XfersCompleted, DELETE applied) and leave a "
      "byte-identical file under cf/stor/files/<DestPath> on cpu2 (FilesReceived, FileCount, no .part).", l12.c_peer_transfer,
      "isolated", live=True),
    C("s2-peer-exists-nak", "S2", "S2.5 Receiving (EXISTS)", "A transfer to a destination that already exists on the receiver must "
      "fail on the sender with status EXISTS (XfersFailed, XferLastStatus 1, STOR_XFER_FAIL_ERR_EID), leave the source unchanged "
      "and count RxErrors with STOR_XFER_RX_ERR_EID on the receiver.", l12.c_peer_exists_nak, "isolated", live=True),
    C("s2-abort-and-rejections", "S2", "S2.2 TRANSFER and S2.3 ABORT_TRANSFER", "TRANSFER must be rejected with "
      "STOR_XFER_ERR_EID for every S2.2 reason (active transfer, Area or SuccessAction out of range, own node, path rules, TLM "
      "names, missing or directory source, open packet file, empty peer DestPath); ABORT_TRANSFER fails an active transfer with "
      "ABORTED and STOR_XFER_ABORT_INF_EID and is rejected with none active.", l12.c_transfer_abort_and_rejections, "isolated",
      live=True),
    C("s2-hidden-stor-transfer", "S2", "S2 Acceptance (coverage tests)", hidden_text("s2-hidden-stor-transfer"), hidden("s2-hidden-stor-transfer"), "cpu"),
    # ---------------------------------------------------------------- S3
    C("s3-default-table", "S3", "S3.3 CMDI table", "The installed ValTable (/cf/cmdi_tbl.tbl, CMDI.ValTable, 56 bytes) must "
      "default to the key cFS-cmdval-test-key-0123456789ab, sources 0x0001 and 0x0002, MaxAgeSeconds 30 and EnableMask 0x0F.",
      table("CMDI"), "misc"),
    C("s3-cmdval-self-contained", "S3", "S3.2 Library API", "cmdval_lib must be self-contained C with no external crypto "
      "library and no dynamic allocation.", st.c_cmdval_self_contained, "misc"),
    C("s3-hidden-cmdval-lib", "S3", "S3 Acceptance (cmdval_lib unit test)", hidden_text("s3-hidden-cmdval-lib"), hidden("s3-hidden-cmdval-lib"), "cpu"),
    C("s3-hidden-cmdi", "S3", "S3 Acceptance (coverage tests)", hidden_text("s3-hidden-cmdi"), hidden("s3-hidden-cmdi"), "cpu"),
    C("s3-valid-frame-executes", "S3", "S3.4 CMDI behaviour", "A valid CMDI frame carrying SAMPLE_APP NOOP sent to UDP 1234 must "
      "execute, counted in FramesReceived and FramesAccepted.", l3.c_valid_frame_executes, "isolated", live=True),
    C("s3-unframed-format", "S3", "S3.4 CMDI behaviour (unframed packets)", "An unframed SAMPLE_APP NOOP sent to UDP 1234 must be "
      "a FORMAT rejection (FormatErrors, CMDI_REJECT_ERR_EID naming FORMAT) and not execute.", l3.c_unframed_format, "isolated",
      live=True),
    C("s3-rejection-reasons", "S3", "S3.1 Frame format and S3.4 rejections", "Frames with a bad CRC, a bad MAC, an unlisted "
      "source, a timestamp outside the window or a malformed header (magic, version, flags, length, over 1074 bytes) must each be "
      "rejected in the S3.2 order, counted under their reason with one CMDI_REJECT_ERR_EID naming the reason and, except for "
      "FORMAT, the source ID, and not execute.", l3.c_rejection_reasons, "isolated", live=True),
    C("s3-set-enable-mask", "S3", "S3.4 SET_ENABLE_MASK", "SET_ENABLE_MASK sent as a valid frame must change the checks from the "
      "next frame (a cleared HMAC bit lets a bad MAC through), reject masks above 0x0F with CMDI_SET_PARAM_ERR_EID, and an "
      "unframed or bad-MAC SET_ENABLE_MASK must change nothing.", l3.c_set_enable_mask, "isolated", live=True),
    C("s3-set-max-age", "S3", "S3.4 SET_MAX_AGE", "SET_MAX_AGE sent as a valid frame must widen the age window (1..86400), "
      "values 0 and 86401 must be rejected, and an unframed or bad-MAC SET_MAX_AGE must change nothing.", l3.c_set_max_age,
      "isolated", live=True),
    C("s3-cmdi-commands", "S3", "S3.4 Commands and C.4", "CMDI NOOP, RESET_COUNTERS (command and frame counters), an unknown "
      "function code and a wrong length must behave as in C.4.", l3.c_cmdi_commands, "isolated", live=True),
    C("s3-harness-functions", "S3", "S3.5 Frame builder", "tools/udp_harness.py must provide CMDI_DEFAULT_KEY, "
      "CMDI_DEFAULT_SOURCE_ID, CMDI_BASE_PORT, TLMO_BASE_PORT, node_ports, crc16_ccitt_false, build_frame, onboard_seconds, "
      "send_frame and send_framed_command with the pinned signatures, building byte-identical S3.1 frames, raising HarnessError "
      "for out-of-range inputs and unknown nodes, reading onboard seconds from the first telemetry packet, and numbering "
      "sequence None from 1 per process.", hc.c_harness_functions, "misc"),
    C("s3-harness-cli", "S3", "S3.5 Frame builder (CLI)", "udp_harness.py send-framed must send one framed command to node N's "
      "CMDI port (default 1) with --payload-hex, --key-hex, --source-id, --time-seconds and --sequence, reading onboard time from "
      "node N's TLMO port otherwise, and print the frame length and sequence; recv --node N listens on node N's TLMO port; "
      "errors give exit status 1 and a one-line message.", hc.c_harness_cli, "isolated"),
    C("s3-harness-live", "S3", "S3 Acceptance (harness frame)", "A frame built and sent with tools/udp_harness.py send-framed "
      "must be accepted by cpu1 and execute.", l3.c_harness_live, "isolated", live=True),
    C("s3-cpu2-frame", "S3", "S3 Acceptance (cpu2)", "On cpu2 a valid frame carrying cpu2's SAMPLE_APP NOOP sent to UDP 1235 "
      "must execute there.", l3.c_cpu2_frame, "isolated", live=True),
    # ---------------------------------------------------------------- S4
    C("s4-default-table", "S4", "S4.1 Table", "The installed PriorityTable (/cf/tlmo_tbl.tbl, 516 bytes) of each node must "
      "default to budget 16, capacity 64 and the 48 streams of S4.1 in their classes, with that node's MIDs.", table("TLMO"),
      "misc"),
    C("s4-hidden-tlmo", "S4", "S4 Acceptance (coverage tests)", hidden_text("s4-hidden-tlmo"), hidden("s4-hidden-tlmo"), "cpu"),
    C("s4-default-telemetry", "S4", "S4.2 Behaviour (always on)", "With no enable command, housekeeping of cFE ES, EVS, SB, TBL "
      "and TIME, SAMPLE_APP, STOR, CMDI, TLMO, GW and SFI and event messages must arrive on UDP 2234 within one housekeeping "
      "period.", l4.c_default_telemetry, "isolated", live=True),
    C("s4-hold-drop-arithmetic", "S4", "S4.2 Enqueue (eviction)", "With the budget at 0 and the queue full, an arriving packet "
      "must evict the oldest packet of the lowest-priority class present when that class is lower than its own, else be dropped "
      "itself; DroppedByClass, SentByClass and PacketsReceived must count exactly that.", l4.c_hold_drop_arithmetic, "isolated",
      live=True),
    C("s4-drain-order", "S4", "S4.2 Drain", "After SET_BUDGET to a small value, held datagrams must arrive class 0 first, then "
      "1, 2 and 3, each class in arrival order, at most the budget per 10 Hz WAKEUP.", l4.c_drain_order, "isolated", live=True),
    C("s4-set-class", "S4", "S4.3 SET_CLASS", "SET_CLASS must move a used stream to another class for packets enqueued "
      "afterwards (TLMO_SET_CLASS_INF_EID) and reject unused streams and classes above 3 (TLMO_SET_CLASS_ERR_EID).",
      l4.c_set_class, "isolated", live=True),
    C("s4-set-budget", "S4", "S4.3 SET_BUDGET", "SET_BUDGET must accept 0..256 (TLMO_SET_BUDGET_INF_EID, BudgetPacketsPerCycle) "
      "and reject larger values (TLMO_SET_BUDGET_ERR_EID); a budget of 1 sends one packet per WAKEUP.", l4.c_set_budget,
      "isolated", live=True),
    C("s4-add-remove-stream", "S4", "S4.3 ADD_STREAM and REMOVE_STREAM", "ADD_STREAM must fill the lowest unused slot and make "
      "the stream's packets arrive, rejecting used streams, classes above 3 and invalid MIDs; REMOVE_STREAM must stop a stream "
      "(also a table stream) and reject unused ones, with events 20 to 23.", l4.c_add_remove_stream, "isolated", live=True),
    C("s4-table-update", "S4", "S4.2 Table update", "A PriorityTable update through cFE Table Services must replace the runtime "
      "list (dropping added streams), reset classes and budget to the table, send TLMO_TBL_UPDATE_INF_EID, and a table with a "
      "class above 3 must be rejected.", l4.c_table_update, "isolated", live=True),
    C("s4-class-map", "S4", "S4.3 SEND_CLASS_MAP", "SEND_CLASS_MAP must send one TLMO_ClassTlm_t with the budget, StreamCount and "
      "the runtime list in slot order (TLMO_CLASS_MAP_INF_EID).", l4.c_class_map, "isolated", live=True),
    C("s4-tlmo-commands", "S4", "S4.3 Commands and telemetry", "TLMO housekeeping must report QueueCapacity, ActiveStreams, "
      "budget, PacketsReceived and SentByClass; NOOP, RESET_COUNTERS, an unknown function code and a wrong length must behave "
      "as in C.4.", l4.c_tlmo_commands, "isolated", live=True),
    C("s4-cpu2-telemetry", "S4", "S4 Acceptance (cpu2)", "cpu2's telemetry (its own MIDs) must arrive on UDP 2235 without any "
      "enable command.", l4.c_cpu2_telemetry, "isolated", live=True),
    C("s4-pipe-depth-probe", "S4", "S4.2 Behaviour (pipe depth 48)", "TLMO's pipe of depth 48 with a message limit of 32 must take "
      "the 48 default streams and bursts of verification packets without software bus pipe overflows or message-limit errors.",
      l4.c_pipe_depth_probe, "isolated", live=True),
    # ---------------------------------------------------------------- S5
    C("s5-default-table", "S5", "S5.1 Table", "The installed ConnTable (/cf/gw_tbl.tbl, 1216 bytes) must have every slot unused.",
      table("GW"), "misc"),
    C("s5-hidden-gw", "S5", "S5 Acceptance (coverage tests)", hidden_text("s5-hidden-gw"), hidden("s5-hidden-gw"), "cpu"),
    C("s5-udp-egress", "S5", "S5.2 Outbound and egress framing", "A UDP connection added by ADD_CONN must egress each subscribed "
      "packet as a valid CMDI frame under the table key with SourceId the node ID, onboard TimeSeconds and Sequence from 1, "
      "counted in PacketsEgressed and the connection's TxPackets, with ValTableShared 1.", l5.c_udp_egress, "isolated", live=True),
    C("s5-udp-ingress", "S5", "S5.2 Inbound", "A valid frame carrying SAMPLE_APP NOOP arriving on a GW UDP connection must be "
      "published and execute (PacketsIngressed, FramesReceived); an unframed packet must not.", l5.c_udp_ingress, "isolated",
      live=True),
    C("s5-ingress-rejections", "S5", "S5.2 Ingress validation", "Frames on a GW connection with a bad CRC, a bad MAC, an unlisted "
      "source, a timestamp outside the window or a malformed header or size must each be counted under their reason and in the "
      "connection's RxErrors with one GW_FRAME_REJECT_ERR_EID naming the connection, the reason and, except FORMAT, the source "
      "ID, and not execute.", l5.c_ingress_rejections, "isolated", live=True),
    C("s5-loop-drop", "S5", "S5.2 Inbound (loop rule)", "An accepted frame embedding a command whose MID is in the Subs of an "
      "enabled connection must be dropped (LoopDrops) and not published.", l5.c_loop_drop, "isolated", live=True),
    C("s5-valtable-key-change", "S5", "S5.2 Validation table", "After a CMDI.ValTable update with a new key, GW must reject "
      "old-key frames (HmacErrors), accept new-key frames and frame its egress under the new key.", l5.c_valtable_key_change,
      "isolated", live=True),
    C("s5-tcp", "S5", "S5.2 TCP", "A TCP connection must stay CONNECTING until a listener appears, then reach UP "
      "(GW_CONN_UP_INF_EID, ConnectionsUp) within one attempt period, carry marker-framed CMDI frames both ways (split frames, "
      "garbage before a marker, an out-of-range length as a FORMAT rejection, a bad MAC rejected) and begin a new connect cycle "
      "with an attempt at once when the peer closes.",
      l5.c_tcp, "isolated", live=True),
    C("s5-tcp-down", "S5", "S5.2 TCP connect cycle", "Against a peer that never listens, a TCP connection must make four "
      "attempts 5 s apart (a first attempt and GW_TCP_MAX_RETRIES retries, each bounded by GW_TCP_ATTEMPT_SEC) without "
      "blocking GW, then enter DOWN (3) with one GW_CONN_TIMEOUT_ERR_EID naming the connection, RemoteAddr and RemotePort "
      "about 20 s after it was opened, make no further attempt (egress counts as errors), and begin a new cycle with an "
      "attempt at once on DISABLE_CONN then ENABLE_CONN.", l5.c_tcp_down, "isolated", live=True),
    C("s5-serial", "S5", "S5.2 SERIAL", "A SERIAL connection on a PTY slave set to raw mode by the other side must carry "
      "marker-framed CMDI frames both ways, including split frames, garbage before a marker, a bad MAC and an out-of-range "
      "length.", l5.c_pty, "isolated", live=True),
    C("s5-pty-idle-probe", "S5", "S5.2 SERIAL (non-blocking reads)", "With a SERIAL connection open and nothing pending on the PTY, "
      "GW must keep answering housekeeping requests at once and keep its heartbeat (no SFI miss): reads of the PTY must never "
      "block the main loop.", l5.c_pty_idle_probe, "isolated", live=True),
    C("s5-restart-serial-probe", "S5", "S5.2 SERIAL (restart)", "GW stopped with ES STOP_APP and started with ES START_APP "
      "(/cf/gw.so, GW_AppMain, 70, 131072) after a SERIAL connection was used must initialize without errors and carry traffic "
      "both ways over a new SERIAL connection on a fresh PTY.", l5.c_restart_serial_probe, "isolated", live=True),
    C("s5-conn-commands", "S5", "S5.3 Commands and telemetry", "ADD_CONN must reject every invalid definition and slot "
      "(GW_CONN_ADD_ERR_EID); DISABLE_CONN, ENABLE_CONN, REMOVE_CONN and RELOAD and their error cases must behave as specified, "
      "with Subscriptions, ConnectionsConfigured, the connection status packet and RESET_COUNTERS.", l5.c_conn_commands,
      "isolated", live=True),
    C("s5-valtable-share-probe", "S5", "S5.2 Validation table (share and retry)", "GW must share CMDI.ValTable when it starts "
      "before CMDI registers it, retrying on WAKEUP, and share it again after CMDI is restarted, so that ingress works in both "
      "cases.", l5.c_valtable_share_probe, "isolated", live=True),
    # ---------------------------------------------------------------- S6
    C("s6-default-table", "S6", "S6.1 Table", "The installed SfiTable (/cf/sfi_tbl.tbl, 520 bytes) of each node must default to "
      "N 3, W 5, H 60, the apps STOR, CMDI, TLMO, GW, SFI_T1, SFI_T2, SFI_T3 and the six watches of S6.1 with that node's MIDs.",
      table("SFI"), "misc"),
    C("s6-hidden-sfi-clusters", "S6", "S6.3 Clustering (SFI_ComputeClusters)", hidden_text("s6-hidden-sfi-clusters"), hidden("s6-hidden-sfi-clusters"), "cpu"),
    C("s6-hidden-sfi", "S6", "S6 Acceptance (coverage tests)", hidden_text("s6-hidden-sfi"), hidden("s6-hidden-sfi"), "cpu"),
    C("s6-apps-ok-after-startup", "S6", "S6.2 Heartbeats (start-up)", "After start-up STOR, CMDI, TLMO and GW must be OK in the "
      "status packet with no misses, heartbeats counted in TotalHeartbeats.", l6.c_apps_ok_after_startup, "isolated", live=True),
    C("s6-gw-stop-miss", "S6", "S6.2 Heartbeats (miss and restore)", "Stopping GW with ES STOP_APP must give one SFI_HB_MISS_ERR_EID "
      "naming GW within N + 2 seconds, AppsMissing 1 and an onset in the status packet and history; ES START_APP restores it "
      "with SFI_HB_RESTORED_INF_EID.", l6.c_gw_stop_miss, "isolated", live=True),
    C("s6-cluster-and-clear", "S6", "S6.3 Clustering", "Heartbeats of SFI_T1 and SFI_T2 stopping at once must give a cluster of "
      "size 2 (CLUSTERED, SFI_CLUSTER_ERR_EID); after CLEAR_HISTORY two misses more than W apart must give UNCORRELATED.",
      l6.c_cluster_and_clear, "isolated", live=True),
    C("s6-stall-rate", "S6", "S6.4 Counter watches", "Verification packets on reserved MIDs 0xEC and 0xED must raise and clear the "
      "STALL and RATE faults as specified (including a counter reset), with SFI_STALL_ERR_EID, SFI_RATE_ERR_EID and "
      "SFI_WATCH_CLEAR_INF_EID once per transition and StallFaults, RateFaults and WatchesInFault.", l6.c_stall_rate, "isolated",
      live=True),
    C("s6-sfi-commands", "S6", "S6.5 Commands and telemetry", "SET_MISS_LIMIT (1..60), SET_CLUSTER_WINDOW (1..600), SEND_STATUS, "
      "RESET_COUNTERS and the C.4 error cases must behave as specified, reflected in SFI housekeeping.", l6.c_sfi_commands,
      "isolated", live=True),
    # ---------------------------------------------------------------- S7
    C("s8-setup-fresh", "S8", "S8.1 Where CTF code goes (setup_ctf.sh)", "ctf/setup_ctf.sh must create a fresh CTF checkout and "
      "venv, apply every patch in ctf/shims/patches/, and be idempotent (a second run succeeds and leaves the same patched tree).",
      cc.c_setup_fresh, "misc"),
    C("s8-plugin-layout", "S8", "S8.1 and S8.2 Plugins and two targets", "ctf/plugins must be a package with cmdi_plugin "
      "(CmdiPlugin), tlm_order_plugin (TlmOrderPlugin), link_plugin (LinkPlugin), es_app_plugin (EsAppPlugin) and "
      "node_file_plugin (NodeFilePlugin), loaded through additional_plugins_path ${CTF_PLUGINS_DIR}; mission.ini must have the "
      "[cpu1] and [cpu2] sections of S8.2; run_ctf.sh must export CTF_PLUGINS_DIR and CFS_RUN_DIR_CPU2 and check UDP 1235; "
      "ctf/README.md must list every plugin and patch; no requirement may be added.", cc.c_plugin_layout, "misc"),
    C("s8-plugin-info", "S8", "S8.3 Required instructions", "ctf --pluginInfo must list every pinned instruction: "
      + ", ".join(cc.INSTRUCTIONS) + ".", cc.c_plugin_info, "misc"),
    C("s8-ccdd-layout", "S8", "S8.4 CCDD definitions", "ctf/ccdd/json must define every command and telemetry message of STOR, "
      "CMDI, TLMO, GW and SFI with all function codes, the SEND_HK, WAKEUP, heartbeat and transfer MIDs, ES START_APP (4) and "
      "STOP_APP (5), and set2 with every MID's cpu2 value; layout_probe.c must probe them all and "
      "probe_layout.sh | check_layout.py must pass.", cc.c_ccdd_layout, "misc"),
    C("s8-run-system-tests", "S8", "S8.5 Run order", "ctf/run_system_tests.sh must run the two smoke scripts and then the eight "
      "system test scripts of S8.5 in order with run_ctf.sh.", cc.c_run_system_tests_script, "misc"),
    C("s8-listener-probe", "S8", "S8.3 tlm_order_plugin (CTF telemetry listener)", "CTF's telemetry listener must keep parsing "
      "telemetry while command-type and short datagrams arrive on the telemetry port.", cc.c_listener_probe, "isolated"),
] + [
    C(f"s8-{name.removesuffix('.json').replace('_', '-')}", "S8", f"S8.5 {name}",
      f"ctf/tests/{name} must exist, send commands only through the CMDI frame plugins (no SendCfsCommand"
      f"{', no TO_LAB enable step' if name in cc.SMOKE else ''}), register and shut down its own nodes, and pass when run with "
      f"ctf/run_ctf.sh against scratch copies of both installed nodes within 300 seconds.", script(name), "isolated", live=True)
    for name in cc.SMOKE + cc.SYSTEM
]


# Expanded requirements have separate verdicts so feedback identifies the failing behaviour.
CHECKS += [
    C("n-queue-capacity", "N", "C.6 IPC queue prerequisites and effective pipe depths", "msg_max must be >=512 and the final non-root node limits must support the actual two-node inventory including kernel overhead; mq_getattr must prove TLMO/MODE >=128 and SFI >=256 without truncation.", ln.c_queue_capacity, "isolated", live=True),
    C("n-mid-inventory", "N", "C.3 Native classic MID inventory", "Every pinned full MID must match the mission and bundled apps, with command/telemetry types distinct and verification topics reserved.", st.c_mid_inventory, "misc"),
    C("n-mode-integration", "N", "C.6 MODE integration and queue depths", "MODE must be installed on both CPUs after SFI with its table, heartbeat and all 32 actual scheduler entries; queue depth ceiling is 512.", st.c_mode_integration, "misc"),
    C("n-sb-pool-sizing", "N", "C.6/C.7 Native software bus pool sizing", "Each CPU's SB pool must cover allocation-rounded descriptors and buckets for the declared verification burst and a full large request; live background sufficiency requires zero allocator errors.", st.c_sb_pool_sizing, "misc"),
    C("s1-quota-file-info", "S1", "S1.6 Quotas and FILE_INFO", "Authenticated quota activation must preserve accounting; FILE_INFO must report unchanged size and ISO-HDLC CRC on the default stream.", l12.c_quota_file_info, "isolated", live=True),
    C("s3-replay-window", "S3", "S3.6 Replay window and source statistics", "Serials 100 and unseen 37 execute, 36 and duplicate 100 reject; sources/nodes and wrap are independent, with exact source and aggregate counters.", l3.c_replay_window, "isolated", live=True),
    C("s3-key-rotation", "S3", "S3.6 Staged key rotation", "Authenticated NEXT staging/switch must retire ACTIVE immediately, retain replay history, update GW and require switch HMAC even with the ordinary HMAC mask disabled.", l3.c_key_rotation, "isolated", live=True),
    C("s3-same-wakeup-order", "S3", "S3.6 Datagram dispatch ordering", "Queued key/mask/age changes must affect subsequent datagrams in the same wakeup, with source-separated observations.", l3.c_same_wakeup_order, "isolated", live=True),
    C("s4-stream-profiles", "S4", "S4.4 Stream profiles and statistics", "Held output must preserve decimation/ownership/class/order through a table reorder, capture reserved statistics after budget restoration, and apply all profiles.", l4.c_stream_profiles, "isolated", live=True),
    C("s4-stream-reorder", "S4", "S4.4 Retained queue ownership on table reorder", "Held packets must follow their MID to the new table slot with unchanged queued class; counters and phases reset while Queued is recomputed.", l4.c_stream_reorder, "isolated", live=True),
    C("s5-link-health", "S5", "S5.4 Keep-alive health", "Valid probes refresh health, malformed probes do not; a silent cut causes exactly one DOWN/report within [5,8] s and valid probes recover it.", l5.c_link_health, "isolated", live=True),
    C("s5-route-failover", "S5", "S5.4 Routing and failback", "Dual transparent relays must deliver unique packets only over backup after primary loss, then fail back after [10,12] s of continuous primary health.", l5.c_route_failover, "isolated", live=True),
    C("s6-limit-latch-enable", "S6", "S6.6 LIMIT, latch and enables", "EF samples 9,9,10,9,9,9,10 must enforce persistence, paired reports, severity, latch/ACK guards and enable baseline resets.", l6.c_limit_latch_enable, "isolated", live=True),
    C("s6-event-rate", "S6", "S6.7 ERROR event rate", "Six GW ERROR events in 10 s trigger one EVENT_RATE onset and expiry clear; non-ERROR events cannot trigger it.", l6.c_event_rate, "isolated", live=True),
    C("s6-mixed-cluster", "S6", "S6.3 Mixed fault clustering", "Heartbeat and watch onsets must share clustering history and produce the specified masks, correlation and tie selection.", l6.c_mixed_cluster, "isolated", live=True),
    C("s6-peer-health", "S6", "S6.8 First-contact peer health", "No absent-startup peer timer arms; authenticated contact arms loss detection and relay cuts cause NODE_LOSS on the specified deadline, clearing on contact.", l6.c_peer_health, "isolated", live=True),
    C("s6-log-ground", "S6", "S6.9 Binary fault log", "A STOR KEEP transfer must reconstruct the independently parsed oldest-first fault log, matching report bytes and FILE_INFO size/CRC without local dump reads.", l6.c_log_ground, "isolated", live=True),
    C("s7-default-table", "S7", "S7.1 MODE table", "Both installed ModeTable images must have every pinned profile, restart target and response default.", table("MODE"), "misc"),
    C("s7-hidden-mode", "S7", "S7 MODE component behaviour", hidden_text("s7-hidden-mode"), hidden("s7-hidden-mode"), "cpu"),
    C("s7-fault-response", "S7", "S7.2 Fault mode and restart", "Stopped GW must trigger DEGRADED and one automatic restart, restore heartbeat, apply target profiles and return only after [20,25] s continuous health.", l7.c_fault_response, "isolated", live=True),
    C("s7-cooldown-cap", "S7", "S7.3 Persistent cooldown and cap", "Restart attempts must remain >=30 s apart with cap 3, no fourth START, one exhaustion event and guarded SAFE reset.", l7.c_cooldown_cap, "isolated", live=True),
    C("s7-response-actions", "S7", "S7.3 Response action payloads and lifetime", "STOR priority/KEEP and GW preference/release must use exact targets; WAIT_STOP table activation must retain the attempt snapshot and cooldown.", l7.c_response_actions, "isolated", live=True),
    C("s7-peer-mode", "S7", "S7.4 Independent peer-mode timer", "MODE_PEER-only loss must cause DEGRADED at its independent 10 s deadline without NODE_LOSS; primary relay loss and recovery must match both-node profiles.", l7.c_peer_mode, "isolated", live=True),
    C("s8-script-content", "S8", "S8.6 System script review", "All fifteen system scenarios must contain their required fault, replay, key, route, profile and binary-log assertions, with authenticated commands and bounded execution.", cc.c_script_content, "misc"),
    C("s8-plugin-negative", "S8", "S8.3 Plugin errors and timeouts", "Pinned instructions must reject malformed transfers/logs, raw-counter/tap errors and invalid key-image operations with actionable bounded failures; native activation and shared command serials must follow S8.3.", cc.c_plugin_adversarial, "isolated", live=True),
]

BY_ID = {c.check_id: c for c in CHECKS}
assert len(BY_ID) == len(CHECKS), "duplicate check ids"
assert all(ID_RE.match(c.check_id) for c in CHECKS)
assert all(c.check_id.split("-", 1)[0].upper() == c.sub for c in CHECKS), "check ids are prefixed by their subsystem"
assert set(HIDDEN_CHECKS) <= set(BY_ID)
assert all(c.lane in ("build", "isolated", "cpu", "misc") for c in CHECKS)

# Execution order within each lane (the build lane runs first; the others in parallel after it starts).
RUN_ORDER = {
    "build": ["n-build-werror"],
    "misc": ["n-component-layout", "n-targets", "n-install-custom", "n-schedule", "n-readme", "n-change-scope",
             "n-design-notes", "n-system-report", "s8-plugin-layout", "s8-run-system-tests", "s3-harness-functions",
             "n-startup-scripts", "s1-default-table", "s3-default-table", "s4-default-table", "s5-default-table",
             "s6-default-table", "s3-cmdval-self-contained", "s8-ccdd-layout", "s8-setup-fresh", "s8-plugin-info"],
    "cpu": ["n-unit-tests", "n-coverage-tests", "n-coverage", "s3-hidden-cmdval-lib", "s3-hidden-cmdi", "s4-hidden-tlmo",
            "s5-hidden-gw", "s6-hidden-sfi-clusters", "s6-hidden-sfi", "s1-hidden-stor", "s2-hidden-stor-transfer"],
    "isolated": ["s3-harness-cli", "n-run-scripts", "n-nodes-operational", "n-ram-disks-distinct", "n-node-clocks",
              "s4-default-telemetry", "s3-valid-frame-executes", "s3-unframed-format", "s3-rejection-reasons",
              "s3-set-enable-mask", "s3-set-max-age", "s3-cmdi-commands", "s3-harness-live", "s3-cpu2-frame",
              "s4-hold-drop-arithmetic", "s4-drain-order", "s4-set-class", "s4-set-budget", "s4-add-remove-stream",
              "s4-table-update", "s4-class-map", "s4-tlmo-commands", "s4-cpu2-telemetry", "s4-pipe-depth-probe",
              "s1-scheduled-housekeeping", "s1-startup-and-hk", "s1-decimation-and-stats", "s1-packet-file-naming",
              "s1-rollover", "s1-idle-file-close", "s1-purge-order", "s1-purge-archived-first", "s1-quota-drop",
              "s1-restart-scan", "s1-set-param-errors", "s1-file-add-list-delete", "s1-file-path-rejection",
              "s1-file-exists-rejection", "s1-file-quota-rejection", "s1-reset-counters", "s1-table-validation-live",
              "s2-ground-transfer", "s2-tlm-transfer-archive", "s2-peer-transfer", "s2-peer-exists-nak",
              "s2-abort-and-rejections",
              "s5-udp-egress", "s5-udp-ingress", "s5-ingress-rejections", "s5-loop-drop", "s5-valtable-key-change",
              "s5-tcp", "s5-tcp-down", "s5-serial", "s5-pty-idle-probe", "s5-restart-serial-probe", "s5-conn-commands",
              "s5-valtable-share-probe",
              "s6-apps-ok-after-startup", "s6-gw-stop-miss", "s6-cluster-and-clear", "s6-stall-rate", "s6-sfi-commands",
              "s8-listener-probe"] + [f"s8-{n.removesuffix('.json').replace('_', '-')}" for n in cc.SMOKE + cc.SYSTEM],
}
_ordered = {cid for ids in RUN_ORDER.values() for cid in ids}
for _check in CHECKS:
    if _check.check_id not in _ordered:
        RUN_ORDER[_check.lane].append(_check.check_id)
assert sorted(sum(RUN_ORDER.values(), [])) == sorted(BY_ID), "RUN_ORDER must list every check exactly once"
for _lane, _ids in RUN_ORDER.items():
    assert all(BY_ID[i].lane == _lane for i in _ids), f"lane mismatch in {_lane}"

# Isolated groups: checks that share nodes through Ctx.cache run in one group, in order. The CTF scripts share
# the one set-up ctf/ tree (its venv and patched checkout), so they run one after another in one group as well.
# Every other isolated check is a group of its own. Groups are started longest first.
SHARED_GROUPS = {
    "ctf": ["s8-listener-probe"] + [f"s8-{n.removesuffix('.json').replace('_', '-')}" for n in cc.SMOKE + cc.SYSTEM] + ["s8-plugin-negative"],
    "tlmo-hold": ["s4-hold-drop-arithmetic", "s4-drain-order", "s4-set-class", "s4-set-budget"],
    "gw-udp": ["s5-udp-egress", "s5-udp-ingress", "s5-ingress-rejections", "s5-loop-drop"],
    "gw-serial": ["s5-serial", "s5-pty-idle-probe", "s5-restart-serial-probe"],
    "stor-peer": ["s2-peer-transfer", "s2-peer-exists-nak"],
    "two-nodes": ["n-nodes-operational", "n-ram-disks-distinct", "n-node-clocks", "n-queue-capacity"],
}
FIRST_SINGLES = ["s5-tcp-down", "s5-tcp", "s6-stall-rate", "s6-cluster-and-clear", "s6-gw-stop-miss", "s1-idle-file-close",
                 "s1-rollover", "s1-purge-order", "s1-quota-drop", "n-run-scripts"]


def isolated_groups() -> list[tuple[str, list[str]]]:
    grouped = {cid for ids in SHARED_GROUPS.values() for cid in ids}
    order = RUN_ORDER["isolated"]
    groups = [(name, [cid for cid in order if cid in ids]) for name, ids in SHARED_GROUPS.items()]
    singles = FIRST_SINGLES + [cid for cid in order if cid not in grouped and cid not in FIRST_SINGLES]
    groups += [(cid, [cid]) for cid in singles]
    return groups


_GROUPS = isolated_groups()
assert sorted(c for _n, ids in _GROUPS for c in ids) == sorted(RUN_ORDER["isolated"]), "every isolated check in one group"
assert all(BY_ID[c].lane == "isolated" for ids in SHARED_GROUPS.values() for c in ids)
assert not set(FIRST_SINGLES) & {c for ids in SHARED_GROUPS.values() for c in ids}, "a check in two groups"


# ============================================================================ running

class Results:
    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.items: dict[str, dict[str, object]] = {}
        self.ran: set[str] = set()

    def put(self, cid: str, passed: bool, feedback: str, detail: str, ran: bool = True, echo: bool = True) -> None:
        assert cid in BY_ID
        with self.lock:
            self.items[cid] = {"id": cid, "passed": passed, "feedback": "" if passed else feedback,
                               "detail": detail if detail.endswith(" [PRELIMINARY]") else detail + " [PRELIMINARY]"}
            if ran:
                self.ran.add(cid)
            else:
                self.ran.discard(cid)
            self.lock.notify_all()
        if echo:
            print(f"[{cid}] {'ok' if passed else 'FAILED'}: {detail[:300]}", flush=True)

    def wait_for(self, cid: str, timeout: float) -> bool:
        """Wait until `cid` has a result; False when `timeout` passed first."""
        with self.lock:
            return self.lock.wait_for(lambda: cid in self.items, timeout=max(timeout, 0.0))


def run_one(ctx: Ctx, spec_: CheckSpec, results: Results) -> None:
    t0 = time.monotonic()

    def attempt() -> None:
        if spec_.lane != "isolated":
            spec_.fn(ctx)
            return
        with timing.host_lane(live=spec_.live):
            for number in (1, 2):
                failure: BaseException | None = None
                with timing.Watchdog() as watchdog:
                    try:
                        spec_.fn(ctx)
                        for node in ctx.nodes.values():
                            if node.alive():
                                node.verify_observation_integrity()
                    except BaseException as exc:
                        failure = exc
                evidence = watchdog.evidence
                timing.save_evidence(ctx.work / f"timing-{spec_.check_id}.jsonl", number, evidence)
                if evidence["stalls_s"]:
                    ctx.shutdown_session()
                    if number == 2:
                        raise EnvironmentFail(f"ENVIRONMENT {spec_.check_id}: scheduler stalled on both serialized "
                                              f"attempts; observed gaps {evidence['stalls_s']}; implementation ungraded")
                    print(f"[{spec_.check_id}] scheduling stall invalidated attempt; one clean serialized rerun", flush=True)
                    continue
                if failure is not None:
                    raise failure
                return

    try:
        attempt()
        results.put(spec_.check_id, True, "", f"{time.monotonic() - t0:.0f} s")
    except BudgetExceeded as exc:
        results.put(spec_.check_id, False, spec_.feedback, f"not run: {exc}", ran=False)
        raise
    except EnvironmentFail as exc:
        results.put(spec_.check_id, False, f"{spec_.feedback} [{spec_.check_id}] ENVIRONMENT prerequisite failed: {exc}",
                    f"ENVIRONMENT: {exc}", ran=False)
    except Fail as exc:
        observed = ("the named component requirements were not met" if spec_.check_id in HIDDEN_CHECKS else str(exc))
        results.put(spec_.check_id, False, f"{exc.feedback or spec_.feedback} [{spec_.check_id}] Expected: {spec_.term}. "
                    f"Observed: {observed}", f"{exc} ({time.monotonic() - t0:.0f} s)")
    except InfraError as exc:
        print(f"INFRASTRUCTURE PROBLEM in {spec_.check_id}: {exc}", file=sys.stderr, flush=True)
        results.put(spec_.check_id, False, spec_.feedback, f"infrastructure problem: {exc}")
    except Exception as exc:  # noqa: BLE001 - a check must never take the review down
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        results.put(spec_.check_id, False, spec_.feedback, f"unexpected {type(exc).__name__}: {exc}\n{tb[-800:]}")


def run_lane(ctx: Ctx, lane: str, results: Results, only: set[str]) -> None:
    ids = RUN_ORDER[lane]
    for i, cid in enumerate(ids):
        if only and cid not in only:
            results.put(cid, False, BY_ID[cid].feedback, "not run (selected subset)", ran=False)
            continue
        try:
            run_one(ctx, BY_ID[cid], results)
        except BudgetExceeded:
            for rest in ids[i + 1:]:
                results.put(rest, False, BY_ID[rest].feedback, "not run: the review's time budget ran out", ran=False)
            break
    if lane == "isolated":
        ctx.shutdown_session()


class GroupProcesses:
    """The isolated groups running now, so that every one of them can be killed when the review ends early."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.procs: set[subprocess.Popen[str]] = set()
        self.closed = False

    def start(self, argv: list[str]) -> subprocess.Popen[str] | None:
        with self.lock:
            if self.closed:
                return None
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1)
            self.procs.add(proc)
            return proc

    def done(self, proc: subprocess.Popen[str]) -> None:
        with self.lock:
            self.procs.discard(proc)

    def kill_all(self) -> None:
        with self.lock:
            self.closed = True
            for proc in self.procs:
                with contextlib.suppress(OSError):
                    proc.kill()


GROUP_PROCS = GroupProcesses()


def run_group(ctx: Ctx, name: str, ids: list[str], results: Results) -> None:
    """Run one group of isolated checks in live_worker.py inside its own namespaces and record their results."""
    assert ids and all(BY_ID[c].lane == "isolated" for c in ids)
    gdir = ctx.work / "groups" / name
    gdir.mkdir(parents=True)
    spec_: dict[str, object] = {"repo": str(ctx.repo), "tree": str(ctx.tree), "work": str(gdir / "work"),
                                "deadline_wall": time.time() + ctx.remaining(), "checks": ids,
                                "boot_fail_dir": str(ctx.work / "boot-failures")}
    if any(BY_ID[c].sub == "S8" for c in ids):
        # The fresh set-up and its idempotence re-run (s8-setup-fresh) must be over before a script runs.
        if not results.wait_for("s8-setup-fresh", ctx.remaining() - 30):
            raise BudgetExceeded("the review's time budget ran out")
        env = cc.ctf_env(ctx)
        try:
            env.setup()
        except PrereqFail:
            pass  # recorded in env; every script in the group reports it
        spec_.update(ctf_root=str(env.root), ctf_setup_ok=env.setup_ok, ctf_setup_detail=env.setup_detail)
    spec_file, result_file = gdir / "spec.json", gdir / "result.json"
    spec_file.write_text(json.dumps(spec_))
    argv = isolation.wrap([sys.executable, "-I", "-B", str(PACK / "live_worker.py"), str(spec_file), str(result_file)])
    proc = GROUP_PROCS.start(argv)
    if proc is None:
        raise BudgetExceeded("the review is ending")
    tail: list[str] = []

    def relay() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip("\n"))
            del tail[:-40]
            print(f"<{name}> {line.rstrip()}", flush=True)

    reader = threading.Thread(target=relay, name=f"relay-{name}", daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=max(ctx.remaining() - 5, 1))
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    finally:
        GROUP_PROCS.done(proc)
    reader.join(timeout=10)
    got: dict[str, dict[str, object]] = {}
    ran: set[str] = set()
    if result_file.is_file():
        data = json.loads(result_file.read_text())
        got, ran = data["items"], set(data["ran"])
    for cid in ids:
        if cid in got:
            item = got[cid]
            results.put(cid, bool(item["passed"]), str(item["feedback"]), str(item["detail"]), ran=cid in ran, echo=False)
        elif timed_out or ctx.remaining() < 20:
            results.put(cid, False, BY_ID[cid].feedback, "not run: the review's time budget ran out", ran=False)
        else:
            detail = (f"ENVIRONMENT: the isolated group {name} ended with status {proc.returncode} before "
                      f"this check finished; output tail: " + " | ".join(tail[-8:]))
            print(f"INFRASTRUCTURE PROBLEM in {cid}: {detail}", file=sys.stderr, flush=True)
            results.put(cid, False, BY_ID[cid].feedback + f" [{cid}] {detail}", detail, ran=False)


def group_jobs() -> int:
    """T5_PACK_JOBS (isolated groups run at once), validated up front; ValueError names the problem."""
    raw = os.environ.get("T5_PACK_JOBS", str(DEFAULT_GROUP_JOBS))
    if not raw.isdigit() or not 1 <= int(raw) <= 64:
        raise ValueError(f"T5_PACK_JOBS must be a whole number from 1 to 64, not {raw!r}")
    return int(raw)


def run_isolated(ctx: Ctx, results: Results, only: set[str], jobs: int) -> None:
    """The isolated lane: every group in its own namespaces, `jobs` at a time. When the build failed, or nested
    namespaces cannot be made here, the lane runs in this process one check after another (nothing then runs
    two nodes on the same ports at once)."""
    if not ctx.build_ok:
        run_lane(ctx, "isolated", results, only)
        return
    ok, why = isolation.available()
    if not ok:
        for cid in RUN_ORDER["isolated"]:
            results.put(cid, False, BY_ID[cid].feedback + f" [{cid}] ENVIRONMENT: namespace prerequisites unavailable: {why}",
                        f"ENVIRONMENT: nested IPC/network namespace setup failed: {why}", ran=False)
        return
    ctx.boot_fail_dir = ctx.work / "boot-failures"
    groups = []
    for name, ids in _GROUPS:
        for cid in ids:
            if only and cid not in only:
                results.put(cid, False, BY_ID[cid].feedback, "not run (selected subset)", ran=False)
        mine = [cid for cid in ids if not only or cid in only]
        if mine:
            groups.append((name, mine))

    def one(name: str, ids: list[str]) -> None:
        try:
            if ctx.remaining() < 35:
                raise BudgetExceeded("the review's time budget ran out")
            run_group(ctx, name, ids, results)
        except BudgetExceeded as exc:
            for cid in ids:
                if cid not in results.items:
                    results.put(cid, False, BY_ID[cid].feedback, f"not run: {exc}", ran=False)
        except Exception as exc:  # noqa: BLE001 - one group must never take the review down
            tb = traceback.format_exc()
            print(tb, file=sys.stderr, flush=True)
            for cid in ids:
                if cid not in results.items:
                    results.put(cid, False, BY_ID[cid].feedback,
                                f"infrastructure problem: group {name}: {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="group") as pool:
        for fut in [pool.submit(one, name, ids) for name, ids in groups]:
            fut.result()


def summarize(checks: list[dict[str, object]], ran: set[str]) -> dict[str, object]:
    subs: dict[str, dict[str, object]] = {}
    for key in SUBSYSTEMS:
        mine = [c for c in checks if BY_ID[str(c["id"])].sub == key]
        run = [c for c in mine if c["id"] in ran]
        passed = sum(1 for c in mine if c["passed"])
        environment = sum(1 for c in mine if str(c["detail"]).startswith("ENVIRONMENT"))
        graded = len(mine) - environment
        subs[key] = {"label": SUBSYSTEMS[key], "checks": len(mine), "run": len(run), "passed": passed,
                     "environment": environment, "graded": graded,
                     "failed": graded - passed, "fraction": round(passed / graded, 3) if graded else 0.0,
                     "passed_all": passed == len(mine)}
    total = len(checks)
    passed = sum(1 for c in checks if c["passed"])
    environment = sum(1 for c in checks if str(c["detail"]).startswith("ENVIRONMENT"))
    graded = total - environment
    return {"preliminary": True, "subsystems": subs,
            "overall": {"checks": total, "run": len(ran), "passed": passed, "environment": environment,
                        "graded": graded, "failed": graded - passed,
                        "fraction": round(passed / graded, 3) if graded else 0.0, "passed_all": passed == total}}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check.py <repo-path> <out.json>", file=sys.stderr)
        return 2
    repo = Path(argv[1]).resolve()
    out = Path(argv[2]).resolve()
    if not repo.is_dir():
        print(f"repository {repo} not found", file=sys.stderr)
        return 2
    if not (repo / "Makefile").is_file() or not (repo / "tools" / "run_cfs.sh").is_file():
        print(f"{repo} is not a cFS mission tree", file=sys.stderr)
        return 2
    for tool in ("make", "cmake", "gcc", "gcov", "nm", "git"):
        if shutil.which(tool) is None:
            print(f"required tool {tool} not found", file=sys.stderr)
            return 2
    try:
        jobs = group_jobs()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    deadline = time.monotonic() + float(os.environ.get("T5_PACK_BUDGET_S", TOTAL_BUDGET_S))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(2))
    base = os.environ.get("T5_PACK_TMP") or "/workspace/tmp-acceptance"
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="t5-pack-", dir=base))
    except OSError as exc:
        print(f"infrastructure error: cannot create scratch space under {base}: {exc}", file=sys.stderr)
        return 2
    ctx = Ctx(repo, work, deadline)
    results = Results()
    only = {x for x in os.environ.get("T5_PACK_ONLY", "").split(",") if x}
    t0 = time.monotonic()
    try:
        with timing.host_lane():
            run_lane(ctx, "build", results, only)
            misc = threading.Thread(target=run_lane, args=(ctx, "misc", results, only), name="lane-misc")
            cpu = threading.Thread(target=run_lane, args=(ctx, "cpu", results, only), name="lane-cpu")
            misc.start()
            cpu.start()
            misc.join()
            cpu.join()
        run_isolated(ctx, results, only, jobs)
    finally:
        GROUP_PROCS.kill_all()
        ctx.shutdown_session()
        evidence = {str(p.relative_to(work)): p.read_text() for p in work.rglob("timing-*.jsonl")}
        evidence.update({str(p.relative_to(work)): p.read_text() for p in work.rglob("queue-preflight.json")})
        evidence.update({str(p.relative_to(work)): p.read_text() for p in (work / "native-pool-probe").glob("cpu*.json")})
        evidence.update({str(p.relative_to(work)): p.read_text() for p in (work / "native-mid-inventory").glob("cpu*.txt")})
        out.with_name(out.stem + ".evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        if os.environ.get("T5_PACK_KEEP") != "1":
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"scratch kept at {work}", file=sys.stderr)
    checks = [results.items[c.check_id] for c in CHECKS]
    passed = all(c["passed"] for c in checks)
    out.write_text(json.dumps({"passed": passed, "checks": checks}, indent=2) + "\n")
    summary = summarize(checks, results.ran)
    summary["elapsed_s"] = round(time.monotonic() - t0)
    sidecar = out.with_name(out.stem + ".subsystems.json")
    sidecar.write_text(json.dumps(summary, indent=2) + "\n")
    for key, s in summary["subsystems"].items():  # type: ignore[union-attr]
        print(f"subsystem {key}: {s['passed']}/{s['checks']} passed ({s['run']} run)", flush=True)
    print(f"overall: {summary['overall']}  elapsed {summary['elapsed_s']} s", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
