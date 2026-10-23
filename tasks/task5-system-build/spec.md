# Task: two-node flight system build (STOR, CMDVAL, TLMO, GW, SFI, MODE)

## Background

The mission is growing from a single lab processor into a two-node system.
Operations needs onboard storage with file transfer between nodes and to the
ground, authenticated command ingest, prioritised telemetry output under a
bandwidth budget, a gateway that moves software-bus traffic over UDP, TCP
and serial links, and a system fault identifier that watches every
application and reports system-level health, and a mode manager that applies
fault responses and synchronises system mode between nodes. Build these as cFS
applications and one new library, following the conventions of
`apps/sample_app` and `libs/sample_lib` (v7 `config/` layout, dispatch,
events, coverage tests), register them on both processors, deliver unit
tests with coverage and a set of system-level tests written for the cFS
Test Framework (CTF), and document the design.

The command ingest app CMDI and the telemetry output app TLMO replace the
lab apps CI_LAB and TO_LAB on both processors. They are clone-and-own: you
copy the CI_LAB and TO_LAB sources into the mission as the new apps, rename
them, own them from then on and extend them to the interfaces pinned here,
and CI_LAB and TO_LAB are no longer built or started. CMDI and TLMO take
over the lab apps' UDP ports, so ground tools keep their addresses, but
from now on every command into a node arrives as an authenticated CMDI
frame. NASA's DS, FM, CF, HS and SBN apps in this tree solve related
problems; you may study them, but STOR, CMDVAL, GW, SFI and MODE are new code,
and every interface below is pinned here.

## How this specification is organised

The work is split into subsystems. Each ends in an "Acceptance" paragraph
that is checked independently of the others, so a subsystem can be
delivered and verified on its own. Sections C (common conventions) and N
(non-functional requirements) apply to all of them. Feedback on a failed
check names the subsystem id and the term of this document it concerns.

| Id | Subsystem | Location | Depends on |
|----|-----------|----------|------------|
| S1 | STOR storage manager (base) | `apps/stor` | C |
| S2 | STOR transfer | `apps/stor` | S1 |
| S3 | CMDVAL library and CMDI command ingest app (replaces CI_LAB), frame builder in `tools/udp_harness.py` | `libs/cmdval_lib`, `apps/cmdi`, `tools/udp_harness.py` | C |
| S4 | TLMO prioritised telemetry output app (replaces TO_LAB) | `apps/tlmo` | C |
| S5 | GW gateway app | `apps/gw` | C |
| S6 | SFI system fault identifier app | `apps/sfi` | C (its heartbeat definitions are used by S1, S3, S4, S5, S7) |
| S7 | MODE mode manager and fault response | `apps/mode` | S1-S6 |
| S8 | System-level tests in CTF, CTF plugins and CCDD definitions | `ctf/` | all |

Because CMDI is the only command path into a node, every live check of
every subsystem sends its commands as CMDI frames (S3.1), and S3 and S4
are needed by the live part of every other subsystem's acceptance.

## C. Common conventions

### C.1 Nodes

The mission builds two processors, `cpu1` (processor ID 1) and `cpu2`
(processor ID 2). Both are already in `sample_defs/targets.cmake`, both are
built natively by `make native_std.install` into `build-native_std/exe/cpu1`
and `exe/cpu2`, and both can run at the same time on one host. Everything in
this task is installed and started on both. A "node" is a running processor;
its node ID is its processor ID (`CFE_PSP_GetProcessorId()`).

| | cpu1 | cpu2 |
|---|---|---|
| Executable, `/cf` | `exe/cpu1/core-cpu1`, `exe/cpu1/cf` | `exe/cpu2/core-cpu2`, `exe/cpu2/cf` |
| Local command / telemetry MID base | 0x1800 / 0x0800 | 0x1900 / 0x0900 |
| CMDI command frames (S3), TLMO telemetry (S4) | UDP 1234, 2234 | UDP 1235, 2235 |

These are the ports CI_LAB and TO_LAB used; CMDI and TLMO take them over.
No other app in a node accepts ground commands: CI_LAB and TO_LAB are not
started on either node (C.6).

MIDs below are given for cpu1. Local MIDs are built from topic IDs with
`CFE_PLATFORM_CMD_TOPICID_TO_MIDV` / `CFE_PLATFORM_TLM_TOPICID_TO_MIDV`, so
the same topic yields 0x19xx / 0x09xx on cpu2. Global MIDs (S2) use
`CFE_GLOBAL_CMD_TOPICID_TO_MIDV` and are identical on both nodes. This task uses the native classic MID mapping, not the EDS topic allocator;
no EDS build is required. All network traffic is on loopback. CMDI's and TLMO's ports are their base
port (1234, 2234) plus the node ID minus 1, as CI_LAB and TO_LAB computed
theirs.

Extend `tools/run_cfs.sh` and `tools/stop_cfs.sh` so that `CFS_CPU`
(default `cpu1`) selects the processor: `CFS_CPU=cpu2 tools/run_cfs.sh`
starts `core-cpu2` from `exe/cpu2` with its log and PID in
`build-native_std/run/cpu2/`. Behaviour and paths for `cpu1` stay as they are
(`build-native_std/run/cfs.log`); `CFS_RUN_DIR` still overrides the run
directory. The two nodes must be able to run at the same time without
disturbing each other, including their OSAL RAM disks (on this host both
default to `/dev/shm/osal:RAM`); if they collide, make each node's RAM disk
distinct and record how in `docs/reports/system.md`.

### C.2 Application structure

| App | Directory | Prefix | Entry point | ES name | Table name, file |
|-----|-----------|--------|-------------|---------|------------------|
| STOR | `apps/stor` | `STOR_` | `STOR_AppMain` | `STOR` | `StorageTable`, `/cf/stor_tbl.tbl` |
| CMDI | `apps/cmdi` | `CMDI_` | `CMDI_AppMain` | `CMDI` | `ValTable`, `/cf/cmdi_tbl.tbl` |
| TLMO | `apps/tlmo` | `TLMO_` | `TLMO_AppMain` | `TLMO` | `PriorityTable`, `/cf/tlmo_tbl.tbl` |
| GW | `apps/gw` | `GW_` | `GW_AppMain` | `GW` | `ConnTable`, `/cf/gw_tbl.tbl` |
| SFI | `apps/sfi` | `SFI_` | `SFI_AppMain` | `SFI` | `SfiTable`, `/cf/sfi_tbl.tbl` |
| MODE | `apps/mode` | `MODE_` | `MODE_AppMain` | `MODE` | `ModeTable`, `/cf/mode_tbl.tbl` |

Each app has `fsw/src`, `fsw/inc`, `fsw/tables`, `config/`, `unit-test/` and
a `DESIGN.md`. CMDI starts as a copy of `apps/ci_lab` and TLMO as a copy of
`apps/to_lab`, renamed throughout (directory, prefix, entry point, ES name,
topic IDs, MIDs, function codes, event IDs and tables are the ones pinned
here, not the lab apps'); where the lab code and this document disagree,
this document wins. The lab apps' EDS variants (`*_eds_*.c`,
`eds_*.h`) are not carried over. Following sample_app: topic IDs in `fsw/inc/<app>_topicids.h`,
MIDs in `config/default_<app>_msgids.h`, function codes in
`config/default_<app>_fcncode_values.h` and `fsw/inc/<app>_fcncodes.h`
(`<APP>_<NAME>_CC`), event IDs in `fsw/inc/<app>_eventids.h`
(`<APP>_<NAME>_EID`), table types in `config/default_<app>_tbldefs.h`, the
default table source in `fsw/tables/<app>_tbl.c`. Command types are
`<APP>_<Name>Cmd_t` in CamelCase with payload `<APP>_<Name>_Payload_t`;
housekeeping is `<APP>_HkTlm_t` / `<APP>_HkTlm_Payload_t`; other telemetry
`<APP>_<Name>Tlm_t` / `<APP>_<Name>_Payload_t`. Other internal names are
yours unless pinned. The library lives in `libs/cmdval_lib` (`fsw/src`,
`fsw/public_inc`, `unit-test/`, `ut-stubs/`, `DESIGN.md`), prefix `CMDVAL_`,
init function `CMDVAL_LibInit`, ES name `CMDVAL_LIB`.

### C.3 Message IDs

Topic macros are `<APP>_MISSION_<NAME>_TOPICID`, MID macros
`<APP>_<NAME>_MID`. Values (topic / cpu1 MID):

| App | CMD | SEND_HK | WAKEUP (rate) | HK_TLM | Other |
|-----|-----|---------|---------------|--------|-------|
| STOR | 0xC4 / 0x18C4 | 0xC5 / 0x18C5 | 0xC6 / 0x18C6 (1 Hz) | 0xC4 / 0x08C4 | `LIST_TLM` 0xC5 / 0x08C5, `STATS_TLM` 0xC6 / 0x08C6, `XFER_TLM` 0xC7 / 0x08C7, `FILE_INFO_TLM` 0xC8 / 0x08C8; global `XFER_SEG` 0x18 / 0x1878, global `XFER_ACK` 0x19 / 0x1879 |
| CMDI | 0xD0 / 0x18D0 | 0xD1 / 0x18D1 | 0xD2 / 0x18D2 (10 Hz) | 0xD0 / 0x08D0 | `SOURCE_TLM` 0xD1 / 0x08D1 |
| TLMO | 0xD4 / 0x18D4 | 0xD5 / 0x18D5 | 0xD6 / 0x18D6 (10 Hz) | 0xD4 / 0x08D4 | `CLASS_TLM` 0xD5 / 0x08D5, `STREAM_TLM` 0xD6 / 0x08D6 |
| GW | 0xD8 / 0x18D8 | 0xD9 / 0x18D9 | 0xDA / 0x18DA (10 Hz) | 0xD8 / 0x08D8 | `CONN_TLM` 0xD9 / 0x08D9 |
| SFI | 0xDC / 0x18DC | 0xDD / 0x18DD | 0xDE / 0x18DE (1 Hz) | 0xDC / 0x08DC | `STATUS_TLM` 0xDD / 0x08DD, `HEARTBEAT` (command) 0xDF / 0x18DF, `FAULT` (command) 0xF3 / 0x18F3, `FAULT_TLM` 0xDE / 0x08DE; global `HEALTH` (command) 0x1A / 0x187A |
| MODE | 0xF0 / 0x18F0 | 0xF1 / 0x18F1 | 0xF2 / 0x18F2 (1 Hz) | 0xF0 / 0x08F0 | `STATUS_TLM` 0xF1 / 0x08F1; global `PEER` (command) 0x1B / 0x187B |

GW also owns global command `GW_KEEPALIVE_MID`, topic 0x1C / MID 0x187C.
All global packets use `CFE_GLOBAL_CMD_TOPICID_TO_MIDV` as in S2; they
have a command header and function code 0. Their MIDs do not change on
cpu2. SFI_FAULT_MID is local (0x19F3 on cpu2). Command and telemetry
spaces are distinct: STOR_FILE_INFO_TLM does not alias CF_CH0_RX.
The new allocations were checked against the pinned bundle
`apps/*/{fsw/inc,config}/*topic*`, cFE topic headers and
`sample_defs/inc/global_sbn_topicids.h`; SBN keeps 0xFA-0xFF.

Command MIDs 0x18E0 to 0x18EF on cpu1 and 0x19E0 to 0x19EF on cpu2 (topics
0xE0 to 0xEF via `CFE_PLATFORM_CMD_TOPICID_TO_MIDV`) are reserved for
verification: no app publishes on them or uses them as its own command
MID. Some default tables below subscribe to them so that tests can inject
packets through CMDI. They are command MIDs because CMDI forwards only
command packets (S3.2): a verification packet is a CCSDS command packet
(8-byte header: 6-byte primary header, function code, checksum) followed by
whatever payload the test chooses, so packet byte offset 16 is payload byte
8. Below, "reserved MID 0xE8" means topic 0xE8, that is 0x18E8 on cpu1.

### C.4 Commands, events, byte order, tables

- Every app accepts NOOP (code 0) and RESET_COUNTERS (code 1) and follows
  sample_app for an unknown function code (`<APP>_CC_ERR_EID`), a wrong
  length (`<APP>_CMD_LEN_ERR_EID`) and an unexpected MID
  (`<APP>_MID_ERR_EID`). Every accepted command increments
  `CommandCounter`; every rejected one increments `CommandErrorCounter`,
  sends the named error event, and changes nothing else, except the
  explicitly described GW SET_ENABLED_MASK partial failure. CommandCounter
  counts only accepted function-code commands on an app's CMD MID;
  SEND_HK/WAKEUP, heartbeat/transfer/fault/health/mode protocol packets and
  sampled streams do not increment it. Malformed protocol packets use
  their subsystem error rules, increment CommandErrorCounter once where
  specified, and never increment CommandCounter.
- Every HK payload begins `uint8 CommandCounter; uint8 CommandErrorCounter;`,
  so `CommandErrorCounter` is byte 17 of every new HK packet.
- Event texts print values in decimal and MIDs as `0x%04X`. `*_ERR` events
  are ERROR type, `*_INF` INFORMATION, `*_DBG` DEBUG. No event filters.
  Event IDs 1 to 10 are the same in every new app: 1 INIT_INF, 2 CC_ERR,
  3 NOOP_INF, 4 RESET_INF, 5 MID_ERR, 6 CMD_LEN_ERR, 7 PIPE_ERR,
  8 TBL_REG_ERR, 9 TBL_LOAD_ERR, 10 TBL_VAL_ERR.
- All payload structs use natural alignment, no packing, fixed-width cFS
  integer types; a packet is its cFE header followed by the named payload.
  All explicit spare fields and unused telemetry elements are zero. Input
  spare bytes are ignored unless a subsystem explicitly requires a value;
  table spares are ignored by validation. Natural-alignment padding on
  generated packets and table images is zero-filled.
  RESET_COUNTERS leaves both command counters 0, never resets protocol
  sequences, replay windows, fault latches, cooldowns or retry caps.
- Payload and telemetry fields are in host byte order (little-endian here),
  as in the existing apps; the CMDI frame (S3) and stream marker (S5) are
  big-endian where stated.
- Every app subscribes to its command, send-HK and wakeup MIDs. Tables are
  validated on every load (initial or update through cFE Table Services);
  a rejected table leaves the previous one active.

### C.5 Heartbeats

SFI owns the heartbeat message, defined in its public headers:

```c
typedef struct
{
    char   AppName[CFE_MISSION_MAX_API_LEN]; /* ES app name, NUL-terminated */
    uint32 Sequence;                         /* 1 for the first heartbeat, +1 per heartbeat */
} SFI_Heartbeat_Payload_t;

typedef struct { CFE_MSG_CommandHeader_t CommandHeader; SFI_Heartbeat_Payload_t Payload; } SFI_HeartbeatCmd_t; /* code 0 */
```

STOR, CMDI, TLMO, GW and MODE each send one heartbeat per second on
`SFI_HEARTBEAT_MID`: STOR and MODE on every 1 Hz wakeup, the others on every 10th
10 Hz wakeup, using SFI's `fsw/inc` and `config` headers.

### C.6 Mission integration

- `sample_defs/targets.cmake`: add `cmdval_lib stor cmdi tlmo gw sfi mode` to
  `MISSION_GLOBAL_APPLIST` so they build and install on both processors,
  and remove `ci_lab` and `to_lab` from `cpu1_APPLIST` and `cpu2_APPLIST`
  (each becomes `sch_lab`). CI_LAB and TO_LAB are then neither built nor
  installed nor started on either node; their directories under `apps/`
  stay in the tree unchanged.
- `sample_defs/generate_startup.cmake`: add
  `CFE_LIB, cmdval_lib,  CMDVAL_LibInit,     CMDVAL_LIB,    0,   0,     0x0, 0;`
  after the `sample_lib` line; replace the fixed `ci_lab` line with
  `CFE_APP, cmdi,        CMDI_AppMain,       CMDI,         40,   32768, 0x0, 0;`
  and the fixed `to_lab` line with
  `CFE_APP, tlmo,        TLMO_AppMain,       TLMO,         45,   32768, 0x0, 0;`
  (CMDI and TLMO take over the lab apps' startup slots and priorities);
  and set `CFS_APP_STARTUP_ORDER` to `lc cf ds fm hk hs sc md cs stor gw sfi mode sbn`
  (STOR, GW, SFI and MODE start with the loop's priority 70 and stack 131072).
  The generated `cf/cfe_es_startup.scr` of each node names neither
  `ci_lab` nor `to_lab`.
- `sample_defs/cpu1/install_custom.cmake` and `cpu2/install_custom.cmake`:
  remove `ci_lab` and `to_lab` from `APP_INCLUDE_TABLE_LIST` and add the
  new apps as `hk` is; remove the `to_lab.table` include and definition
  lines and the `add_cfe_tables(to_lab to_lab_sub_alt.c)` line, and give
  `tlmo.table`, `stor.table`, `sfi.table` and `mode.table` the include directories and
  definitions (`APP_INCLUDE_TABLE_LIST`, `APP_DEFINITION_TABLE_LIST`) that
  `to_lab.table` had, since their default tables name other apps' MIDs
  (`CFS_SBN_ENABLED` is defined on this native build).
- In `sample_defs/default_osconfig.cmake`, set `OSAL_CONFIG_MAX_FILE_NAME`
  to 32 (S1) and `OSAL_CONFIG_QUEUE_MAX_DEPTH` to 512 for both processors.
- Mission SB pool configuration overrides are permitted to support the
  declared live burst envelope (C.7); a pipe depth alone implies no
  lossless burst guarantee.
- `sample_defs/tables/sch_lab_table.c` (0.1 s ticks): remove the
  `CI_LAB_SEND_HK_MID` and `TO_LAB_SEND_HK_MID` entries and the
  `ci_lab_msgids.h` and `to_lab_msgids.h` includes; add STOR SEND_HK 49 and
  WAKEUP 10, CMDI SEND_HK 50 and WAKEUP 1, TLMO SEND_HK 51 and WAKEUP 1, GW
  SEND_HK 53 and WAKEUP 1, SFI SEND_HK 55 and WAKEUP 10, MODE SEND_HK
  57 and WAKEUP 10. Slot budget: the
  table holds 22 entries today (20 unconditional, of which 2 are the lab
  apps, plus 2 under `CFS_SBN_ENABLED`, which is defined here); removing the
  lab entries leaves 20, and the 12 new ones (two each for STOR, CMDI, TLMO, GW, SFI and
  MODE) make exactly 32 of SCH_LAB's 32
  (`SCH_LAB_MISSION_MAX_SCHEDULE_ENTRIES`), leaving 0 spare. Keep that
  limit at 32; health, mode sync, event windows, keep-alives and response
  timers run inside these wakeups and need no additional schedule slots.
- `sample_defs/tables/to_lab_sub.c` and the other `to_lab_sub*.c` files are
  no longer built and are left unchanged; the telemetry they routed is
  routed by TLMO's default table (S4.1).
- `tools/udp_harness.py`: the frame builder of S3.5. `README.md` (mission
  root): update the "Ground interface" section to name CMDI and TLMO on
  their ports and the framed commands, and the "Registering an app" step
  that names `to_lab_sub.c` to name TLMO's table.

### C.7 Evaluation and acceptance timing

All deadline arithmetic uses cFE onboard time; intervals include
subseconds unless a uint32 Seconds telemetry field is explicitly named.
For a deadline expressed in seconds, act on the first owner wakeup at or
after the deadline, never before it. Hidden unit tests use exact synthetic
time and wakeup counts. Live acceptance allows 2 additional seconds for
1 Hz consumers and 1 additional second for 10 Hz consumers, plus one
SEND_HK period when asserting a changed HK value; explicit narrower or
wider live bounds in a subsystem override these defaults. Status request
replies must reach TLMO within 2 seconds at a nonzero sufficient budget.
Healthy recovery after 20 seconds is checked in [20,23] seconds from the
first MODE healthy wakeup, or [20,25] from SFI's clear report. Tests use
telemetry timestamps and event receipt bounds, not sleep-only proofs.
Timing-sensitive live checks run in a host-coordinated serialized lane
with reserved CPU capacity and no concurrent builds. A detected scheduling
stall invalidates the attempt and triggers one clean serialized rerun;
a stalled rerun is an environment failure, not an implementation failure.
Exact deadline correctness is graded with synthetic-time tests.

The harness sets fs.mqueue.msg_max to 512 in each run's IPC namespace.
Before live checks, acceptance verifies effective kernel queue capacity
in each node's IPC namespace: msg_max >= 512 and sufficient
RLIMIT_MSGQUEUE for both nodes' queues, including kernel per-message
bookkeeping and any other queues charged to the same user. Verify queue
attributes/capacity, not merely successful initialization. Insufficient
kernel/resource limits are an environment prerequisite failure, not an
implementation failure. Permissive queue truncation never satisfies a
pipe-depth requirement; the required depths in S4, S6 and S7 still apply.
Live checks pin packet sizes, burst counts and pacing in the acceptance
plan and verify no SB allocation failures within that envelope. SB pool
sizing must include buffer descriptors, allocator buckets and other live
traffic; TLMO's copied application queue does not retain SB buffers.

The 57 default TLMO streams fit 64 slots; tests remove streams explicitly
when they need more than seven additions. Large SFI status, STOR HK and TLMO stream-statistics
packets use TLMO's 8192-byte maximum; CMDI and GW command frames retain
their 1024-byte embedded limit.

For S1-S6 live checks that require no autonomous interference, send
MODE SET_ENABLE 0 and wait for its HK acknowledgement. Before arranging
faults or applying isolated-test overrides, also await completion of the
mandatory initial profile application, or two MODE wakeups after all
profile targets register. Complete scheduled profile reapplications
before resetting counters and beginning assertions. For SFI/GW tests
that configure peer paths, explicitly enable/disable both peer monitors
as the case needs.
Counters are reset after setup; protocol serials continue across resets.
All live commands, including these setup commands, are CMDI frames.
Acceptance never restarts SFI while MODE remains running: SFI recovery
restarts the node, and peer-loss tests use relay cuts instead of an
in-run SFI restart. SFI report serials and MODE's per-slot history share
that node lifetime; no producer-epoch resynchronization is required.

## S1. STOR storage manager (base)

STOR records selected software-bus messages into packet files under a
telemetry directory, with per-message priority and decimation, purges
low-priority data above a threshold, and offers a separate file area with
add, delete and list operations that fills until full.

### S1.1 Table

```c
#define STOR_MAX_ENTRIES 32
#define STOR_PATH_LEN    64

typedef struct
{
    CFE_SB_MsgId_t MsgId;      /* CFE_SB_INVALID_MSG_ID marks an unused entry */
    uint8          Priority;   /* 1..255, higher is kept longer */
    uint8          Decimation; /* 1..255, store every Nth received packet */
    uint16         Spare;
} STOR_EntryDef_t;

typedef struct
{
    char            TelemetryRoot[STOR_PATH_LEN]; /* directory for packet files */
    char            FileRoot[STOR_PATH_LEN];      /* directory for the file area */
    uint32          TelemetryQuotaBytes;
    uint32          FileQuotaBytes;
    uint32          MaxPacketFileBytes;           /* roll over when exceeded */
    uint8           PurgeThresholdPercent;        /* 1..100, of TelemetryQuotaBytes */
    uint8           Spare[3];
    STOR_EntryDef_t Entries[STOR_MAX_ENTRIES];
} STOR_StorageTable_t;
```

Validation rejects: empty or unterminated roots; zero quotas;
`MaxPacketFileBytes` below 1024 or above `TelemetryQuotaBytes`; threshold
outside 1..100; a used entry with `Priority` or `Decimation` 0; duplicate
used `MsgId`s. Default: roots `/cf/stor/tlm` and `/cf/stor/files`, quotas
262144 and 1048576, `MaxPacketFileBytes` 16384, threshold 80, entries
`CFE_EVS_HK_TLM_MID` (priority 10, decimation 1), `CFE_ES_HK_TLM_MID` (20, 1),
`SAMPLE_APP_HK_TLM_MID` (30, 2), `CFE_TIME_HK_TLM_MID` (40, 1). On every
table load STOR subscribes to each used entry's `MsgId`, unsubscribes from
entries no longer present, and resets the runtime priority, decimation and
threshold to the table values.

### S1.2 Telemetry storage

- Startup: create both roots if missing (`OS_mkdir`, one level at a time),
  delete any `*.part` file in the file area (S2.5), and initialize
  `TelemetryBytesUsed` from the packet files and `FileBytesUsed` and
  `FileCount` from all regular files of the file area.
- Packet files are `<TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin`: MMMM the
  four-digit upper-case hex MID value, NNNNNNNN the zero-padded decimal
  sequence per MID, from 0 on a clean directory, else the highest sequence
  found for that MID at startup plus one. A packet file archived by a
  transfer (S2.4) ends in `.arc` and is still a packet file for accounting,
  purge and sequence purposes. Each file starts with a `CFE_FS_Header_t`
  written with `CFE_FS_WriteHeader`, sub type `STOR_FILE_SUBTYPE` = 0x5354,
  description "STOR packet file".
- For every received packet whose MID is a used entry: `Received`++ for the
  entry; store it when `(Received - 1) % Decimation == 0`, else
  `Decimated`++ and `PacketsDecimated`++. Storing appends the full packet as
  received to the entry's open packet file; if that would make the file
  exceed `MaxPacketFileBytes`, close it and open the next sequence first.
- `TelemetryBytesUsed` is the total size on disk of the packet files.
- Quota: if storing (including a new file's header) would make
  `TelemetryBytesUsed` exceed `TelemetryQuotaBytes` after purging, the
  packet is dropped: `Dropped`++ for the entry, `PacketsDropped`++, and
  `STOR_TLM_QUOTA_ERR_EID` once per entry until it stores again.
- Purge: after every store and on every WAKEUP, while `TelemetryBytesUsed`
  exceeds `PurgeThresholdPercent` percent of `TelemetryQuotaBytes`, delete
  one closed packet file: archived (`.arc`) files first, then `.bin` files;
  within each group the file whose MID has the lowest current priority
  (priority 0 for a MID no longer in the table), lowest sequence first.
  Open files are never purged. Each deletion: `FilesPurged`++, bytes
  subtracted. Each pass deleting at least one file: `PurgeCycles`++ and one
  `STOR_PURGE_INF_EID` with the count. If nothing can be purged, stop.
- WAKEUP (1 Hz): purge check, close packet files not written for 10 s or
  more, transfer processing (S2), heartbeat (C.5).

### S1.3 File area

- Command paths are relative to `FileRoot`; a path containing `..` or
  starting with `/` is rejected with `STOR_PATH_ERR_EID`.
- ADD_FILE `{ char SourcePath[STOR_PATH_LEN]; char DestPath[STOR_PATH_LEN]; }`:
  copy the source (absolute onboard path) to `<FileRoot>/<DestPath>`,
  creating intermediate directories. Rejected, leaving no partial file, when
  the source cannot be opened (`STOR_FILE_SRC_ERR_EID`), the destination
  exists (`STOR_FILE_EXISTS_ERR_EID`), or `FileBytesUsed` plus the source
  size exceeds `FileQuotaBytes` (`STOR_FILE_QUOTA_ERR_EID`,
  `FileAddsRejected`++). Success: `FileCount`++, bytes added,
  `STOR_FILE_ADD_INF_EID`.
- DELETE_FILE `{ char Path[STOR_PATH_LEN]; }`: delete a regular file;
  missing or a directory is `STOR_FILE_DEL_ERR_EID`. Success: `FileCount`--,
  bytes subtracted, `STOR_FILE_DEL_INF_EID`.
- LIST_DIR `{ char Path[STOR_PATH_LEN]; }`: the immediate entries of
  `<FileRoot>/<Path>` (empty = root), directories with a trailing `/` and
  size 0, in `OS_DirectoryRead` order. LIST_ALL (no payload): every regular
  file under `FileRoot`, named relative to it, depth-first pre-order (a
  directory's entries in `OS_DirectoryRead` order, descending into each
  subdirectory at its position).
- Listings are one or more `STOR_ListTlm_t`; an empty directory yields one
  packet with `TotalEntries` 0; a missing one is `STOR_LIST_ERR_EID`;
  success `STOR_LIST_INF_EID` with the count:

  ```c
  #define STOR_LIST_ENTRIES_PER_PKT 4
  typedef struct { char Name[STOR_PATH_LEN]; uint32 Size; uint32 Spare; } STOR_ListEntry_t;
  typedef struct
  {
      uint16 TotalEntries; uint16 PacketIndex; /* 0-based */ uint16 EntriesInPacket; /* 0..4 */ uint16 Spare;
      STOR_ListEntry_t Entries[STOR_LIST_ENTRIES_PER_PKT];
  } STOR_List_Payload_t;
  ```

### S1.4 Commands

| Code | Name | Payload |
|------|------|---------|
| 0, 1 | NOOP, RESET_COUNTERS | none |
| 2 | SET_PRIORITY | `{ CFE_SB_MsgId_t MsgId; uint8 Priority; uint8 Spare[3]; }` |
| 3 | SET_DECIMATION | `{ CFE_SB_MsgId_t MsgId; uint8 Decimation; uint8 Spare[3]; }` |
| 4 | SET_PURGE_THRESHOLD | `{ uint8 Percent; uint8 Spare[3]; }` |
| 5, 6, 7 | ADD_FILE, DELETE_FILE, LIST_DIR | S1.3 |
| 8, 9, 10 | LIST_ALL, SEND_STATS, CLOSE_FILES | none |
| 11 | TRANSFER | S2.2 |
| 12 | ABORT_TRANSFER | none |

SET_PRIORITY and SET_DECIMATION reject value 0 or an unknown MsgId
(`STOR_SET_PARAM_ERR_EID`); success `STOR_SET_PARAM_INF_EID`.
SET_PURGE_THRESHOLD accepts 1..100 (else `STOR_SET_PARAM_ERR_EID`) and
triggers a purge check. CLOSE_FILES closes every open packet file
(`STOR_CLOSE_INF_EID` with the count). SEND_STATS sends one
`STOR_StatsTlm_t` (`STOR_STATS_INF_EID`):

```c
typedef struct
{
    CFE_SB_MsgId_t MsgId; uint8 Priority; uint8 Decimation; uint16 Spare;
    uint32 Received; uint32 Stored; uint32 Decimated; uint32 Dropped;
} STOR_EntryStats_t;
typedef struct { STOR_EntryStats_t Entries[STOR_MAX_ENTRIES]; } STOR_Stats_Payload_t;
```

RESET_COUNTERS zeroes the command counters, the per-entry counters,
`PacketsStored`, `PacketsDecimated`, `PacketsDropped`, `FilesPurged`,
`PurgeCycles`, `FileAddsRejected` and the transfer counters of S2.6; not
byte accounting, file counts, runtime parameters or transfer state.

### S1.5 Housekeeping and events

`STOR_HkTlm_Payload_t`, in this order:

```c
uint8  CommandCounter; uint8 CommandErrorCounter; uint8 PurgeThresholdPercent; uint8 Spare;
uint32 TelemetryBytesUsed; uint32 TelemetryQuotaBytes; uint32 FileBytesUsed; uint32 FileQuotaBytes;
uint32 PacketsStored; uint32 PacketsDecimated; uint32 PacketsDropped; uint32 FilesPurged; uint32 PurgeCycles;
uint16 FileAddsRejected; uint16 FileCount; uint16 ActiveEntries; uint16 OpenPacketFiles;
uint8  XferState; uint8 XferLastStatus; uint8 RxState; uint8 Spare2;          /* S2.6 */
uint32 XferActiveId; uint16 XfersCompleted; uint16 XfersFailed;
uint32 XferSegmentsSent; uint32 XferSegmentsReceived; uint16 FilesReceived; uint16 RxErrors;
```

Event IDs: 1 to 10 as in C.4,
11 SUB_ERR, 12 DIR_CREATE_ERR, 13 TLM_QUOTA_ERR, 14 PURGE_INF (DEBUG type),
15 FILE_WRITE_ERR, 16 SET_PARAM_INF, 17 SET_PARAM_ERR, 18 PATH_ERR,
19 FILE_SRC_ERR, 20 FILE_EXISTS_ERR, 21 FILE_QUOTA_ERR, 22 FILE_ADD_INF,
23 FILE_DEL_ERR, 24 FILE_DEL_INF, 25 LIST_ERR, 26 LIST_INF, 27 CLOSE_INF,
28 STATS_INF, 29 XFER_START_INF, 30 XFER_ERR, 31 XFER_DONE_INF,
32 XFER_FAIL_ERR, 33 XFER_ABORT_INF, 34 XFER_RX_INF, 35 XFER_RX_ERR,
36 XFER_ACTION_ERR.

### S1.6 Quotas, file information and priority accounting

Commands on STOR_CMD_MID extend S1.4:

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 13 | SET_QUOTA | `{ uint32 TelemetryQuotaBytes; uint32 FileQuotaBytes; }` | telemetry quota at least current MaxPacketFileBytes, file quota nonzero; else SET_PARAM_ERR; success SET_PARAM_INF, apply both atomically then purge |
| 14 | FILE_INFO | `{ uint8 Area; uint8 Spare[3]; char Path[STOR_PATH_LEN]; }` | Area and Path as S2.2; regular closed file required; success one FILE_INFO_TLM and FILE_INFO_INF; invalid path PATH_ERR, other failure FILE_INFO_ERR |

A quota may be reduced below current usage; existing file-area files are
kept, subsequent additions follow the new quota, and telemetry follows the
purge and drop rules. Table activation restores both quotas from the table.
FILE_INFO never changes a file or transfer state. Its payload type is
STOR_FileInfo_Payload_t; reply STOR_FileInfoTlm_t has payload
STOR_FileInfoTlm_Payload_t in this order:

```c
uint8 Area; uint8 Spare[3]; char Path[STOR_PATH_LEN]; uint32 Size; uint32 Crc;
```

Crc is CRC-32/ISO-HDLC exactly as S2.1, over all file bytes, including a
packet file's cFE file header; an empty file has CRC 0. No reply on error.
Append to S1.5 HK `uint32 PriorityBytes[256];`: index p is the bytes in
telemetry packet files at current priority p, open and archived included;
unknown MIDs use 0. Their sum equals TelemetryBytesUsed. SET_PRIORITY,
table load, transfer success actions and purge update this accounting.
Append events 37 FILE_INFO_INF and 38 FILE_INFO_ERR (C.4 types).
MODE uses the ordinary TRANSFER command, including its busy rejection,
segment timing and success actions; it has no privileged transfer path.

### S1 Acceptance

STOR builds on both nodes and its unit tests pass with the coverage of N.
Hidden coverage tests compiled against the names above exercise table
validation, decimation, roll-over, purge order including archived files,
quota drops, path rejection and the listing packets. Live, acceptance
runs its own headless checks against your build: on a running cpu1, with
a table whose entries name reserved MIDs (C.3) loaded through cFE Table
Services, verification packets injected as CMDI frames (S3) fill the
telemetry quota, and the files on disk, purge order, housekeeping and
statistics (received through TLMO, S4) are checked; the file-area
commands, sent as CMDI frames, are exercised including quota rejection,
SET_QUOTA atomic validation and reductions, FILE_INFO size and CRC (empty
and nonempty files), and per-priority accounting across reprioritising,
archiving and purge. Hidden tests cover these boundaries and I/O errors.

## S2. STOR transfer

STOR transfers one stored file (file area) or one packet file (telemetry
area) to the ground or to the STOR of another node, and on success applies
the action the command names to the source.

### S2.1 Transfer messages

```c
#define STOR_XFER_DATA_LEN        512
#define STOR_XFER_SEGS_PER_WAKEUP 8
#define STOR_XFER_ACK_TIMEOUT     10 /* wakeups after the last segment */
#define STOR_XFER_RX_TIMEOUT      10 /* wakeups without a segment */

typedef struct
{
    uint32 TransferId;   /* by the sender: 1 for the first TRANSFER after start, +1 per TRANSFER */
    uint8  SrcNode;      /* sender node ID */
    uint8  DstNode;      /* 0 = ground, else receiver node ID */
    uint8  Area;         /* STOR_XferArea_t of the source */
    uint8  Spare;
    uint16 SegmentIndex; /* 0-based */
    uint16 SegmentCount; /* >= 1 */
    uint32 FileSize;     /* bytes of the whole source file */
    uint32 FileCrc;      /* CRC-32 of the whole source file */
    uint32 Offset;       /* SegmentIndex * STOR_XFER_DATA_LEN */
    uint16 DataLength;   /* valid bytes in Data; STOR_XFER_DATA_LEN except in the last segment */
    uint16 Spare2;
    char   DestPath[STOR_PATH_LEN];  /* peer: path relative to the receiver's FileRoot; ground: the source Path */
    uint8  Data[STOR_XFER_DATA_LEN]; /* unused bytes 0 */
} STOR_XferSeg_Payload_t;

typedef struct
{
    uint32 TransferId; uint8 SrcNode; /* the receiver */ uint8 DstNode; /* the original sender */
    uint8  Status;     /* STOR_XferStatus_t */ uint8 Spare; uint32 BytesReceived;
} STOR_XferAck_Payload_t;
```

- Node-to-node segments are `STOR_XferSegCmd_t` (function code 0) on
  `STOR_XFER_SEG_MID` (0x1878); acknowledgements are `STOR_XferAckCmd_t`
  (code 0) on `STOR_XFER_ACK_MID` (0x1879); ground segments are telemetry
  `STOR_XferTlm_t` on `STOR_XFER_TLM_MID` (0x08C7) with the same payload.
  Segment packets always have the full structure size.
- `FileCrc` is CRC-32/ISO-HDLC (zlib `crc32`: reflected polynomial
  0x04C11DB7, initial 0xFFFFFFFF, final XOR 0xFFFFFFFF; check value of the
  ASCII bytes `123456789` is 0xCBF43926). A zero-length file is one segment
  with `DataLength` 0.
- STOR subscribes to both global MIDs at initialization and ignores,
  without counting, segments and acknowledgements whose `DstNode` is not
  its own node ID (including its own segments, which also appear on its
  local bus).
- Enumerations in `config/default_stor_msgdefs.h`:
  `STOR_XferArea_FILE` 0, `STOR_XferArea_TLM` 1;
  `STOR_XferAction_KEEP` 0, `STOR_XferAction_DELETE` 1,
  `STOR_XferAction_MARK_ARCHIVED` 2;
  `STOR_XferStatus_OK` 0, `_EXISTS` 1, `_QUOTA` 2, `_SEQUENCE` 3, `_CRC` 4,
  `_IO` 5, `_PATH` 6 (sent by receivers), `_TIMEOUT` 7, `_ABORTED` 8,
  `_ACTION_FAILED` 9 (local to the sender).

### S2.2 TRANSFER (code 11)

```c
typedef struct
{
    uint8 Area;          /* STOR_XferArea_t */
    uint8 Destination;   /* 0 = ground, 1..255 = node ID of a peer STOR */
    uint8 SuccessAction; /* STOR_XferAction_t */
    uint8 Spare;
    char  Path[STOR_PATH_LEN];     /* FILE: relative to FileRoot; TLM: a packet file name in TelemetryRoot */
    char  DestPath[STOR_PATH_LEN]; /* peer only: relative to the peer's FileRoot */
} STOR_Transfer_Payload_t;
```

Rejected with `STOR_XFER_ERR_EID` (text names the reason) when: a transfer
is active; `Area` or `SuccessAction` is out of range; `Destination` is the
own node ID; `Path` breaks the S1.3 rules or, for TLM, is not exactly a
packet file name (`tlm_MMMM_NNNNNNNN.bin` or `.arc`); the source is missing,
a directory, or an open packet file; for a peer, `DestPath` is empty,
unterminated or breaks the path rules. Accepted: `XferState` SENDING (1),
`XferActiveId` the new id, `STOR_XFER_START_INF_EID` with id, path and
destination.

### S2.3 Sending

- From the first WAKEUP after acceptance, up to `STOR_XFER_SEGS_PER_WAKEUP`
  segments per WAKEUP in index order, `XferSegmentsSent`++ each. A source
  read error fails the transfer with IO.
- Ground: segments on `STOR_XFER_TLM_MID`; the transfer completes after the
  last segment.
- Peer: segments on `STOR_XFER_SEG_MID` with `DstNode` = `Destination`; after
  the last one `XferState` is AWAITING_ACK (2). An acknowledgement with the
  same `TransferId`, `SrcNode` = `Destination` and `DstNode` = own node ID
  ends the transfer: OK completes it, any other status fails it with that
  status. None within `STOR_XFER_ACK_TIMEOUT` wakeups fails it with TIMEOUT.
- Completion: success action, `XfersCompleted`++, `STOR_XFER_DONE_INF_EID`,
  `XferLastStatus` OK, `XferState` IDLE (0). A failed success action still
  completes the transfer but sets `XferLastStatus` ACTION_FAILED and sends
  `STOR_XFER_ACTION_ERR_EID`.
- Failure: no success action, `XfersFailed`++, `XferLastStatus` = status,
  `STOR_XFER_FAIL_ERR_EID` naming the status, `XferState` IDLE.
- ABORT_TRANSFER (12): with a transfer active, fail it with ABORTED and
  `STOR_XFER_ABORT_INF_EID` instead of XFER_FAIL_ERR; otherwise rejected
  with `STOR_XFER_ERR_EID`.

### S2.4 Success actions

KEEP leaves the source unchanged. DELETE deletes it with the accounting of
DELETE_FILE (file area) or of a purge deletion without the purge counters
(telemetry area). MARK_ARCHIVED renames it: `<Path>` becomes `<Path>.arc` in
the file area (the action fails if that name exists), and
`tlm_MMMM_NNNNNNNN.bin` becomes `tlm_MMMM_NNNNNNNN.arc`; a name already
ending in `.arc` is left unchanged; byte accounting and `FileCount` do not
change.

### S2.5 Receiving

- One inbound transfer at a time. A segment with index 0 addressed to this
  node starts a new one, discarding any incomplete inbound transfer (partial
  file deleted, no acknowledgement for it).
- On index 0: `DestPath` must satisfy S1.3 and its final component plus
  `.part` must fit in 31 characters (else NAK PATH); `<FileRoot>/<DestPath>`
  must not exist (NAK EXISTS); `FileBytesUsed` + `FileSize` must not exceed
  `FileQuotaBytes` (NAK QUOTA). STOR then creates intermediate directories
  and writes `<FileRoot>/<DestPath>.part`.
- Each segment for the active `(SrcNode, TransferId)`:
  `XferSegmentsReceived`++; it must carry the next index, else NAK SEQUENCE.
  Segments of another `(SrcNode, TransferId)` with nonzero index are
  ignored.
- After the last segment the CRC-32 of the data must equal `FileCrc` (else
  NAK CRC); the `.part` file is renamed to `<DestPath>`, `FileCount`++,
  `FileBytesUsed` += size, `FilesReceived`++, acknowledgement OK with
  `BytesReceived` = size, `STOR_XFER_RX_INF_EID`.
- A write or rename failure is NAK IO. Every NAK deletes the partial file,
  increments `RxErrors`, sends `STOR_XFER_RX_ERR_EID` and an
  acknowledgement with that status. No segment for `STOR_XFER_RX_TIMEOUT`
  wakeups aborts without acknowledgement (partial file deleted,
  `RxErrors`++, `STOR_XFER_RX_ERR_EID`).
- `.part` bytes are not counted in `FileBytesUsed`. `RxState` is RECEIVING
  (1) during an inbound transfer, else IDLE (0).

### S2.6 Transfer telemetry

The HK fields of S1.5. `XferActiveId` is the active outgoing transfer's id,
else the last one's, else 0. RESET_COUNTERS zeroes `XfersCompleted`,
`XfersFailed`, `XferSegmentsSent`, `XferSegmentsReceived`, `FilesReceived`
and `RxErrors`.

### S2 Acceptance

Hidden coverage tests exercise TRANSFER validation, segmentation, the
acknowledgement and timeout paths, the receiver's NAK cases and the three
success actions. Live, acceptance runs its own headless checks against
your build. On cpu1 alone: a file added to the file area is sent to the
ground and reassembled from `STOR_XFER_TLM_MID` packets that TLMO's
default table (S4.1) forwards to UDP 2234; contents, CRC, segment fields
and the success action on disk are checked, as is a packet-file transfer
with MARK_ARCHIVED. With cpu1 and cpu2, segments and acknowledgements are
relayed by the acceptance environment itself, not by GW: TLMO ADD_STREAM
commands (S4.3), sent as CMDI frames, add `STOR_XFER_SEG_MID` to cpu1's
TLMO and `STOR_XFER_ACK_MID` to cpu2's, and every segment received on UDP
2234 is wrapped in a CMDI frame and sent to cpu2's CMDI on UDP 1235, every
acknowledgement received on UDP 2235 to cpu1's CMDI on UDP 1234. A
transfer from cpu1 to node 2 completes, the file is byte-identical under
`cf/stor/files/<DestPath>` in cpu2's run directory, the success action is
applied on cpu1, and a destination that already exists fails with EXISTS
and leaves the source unchanged.

## S3. CMDVAL library and CMDI command ingest

CMDI replaces CI_LAB as the node's command ingest app: a clone-and-own
copy of CI_LAB (C.2) on CI_LAB's port, extended to accept only validated
frames. It validates every datagram with `cmdval_lib` and forwards the
embedded command onto the software bus only when validation passes.

There is no exemption and no side path. CMDI's own commands, cFE commands
(ES, EVS, SB, TBL, TIME), table loads, and every other command into a node,
from any tool, arrive as CMDI frames and are validated like any other; no
port, MID, function code, source ID or app is exempt, and no unframed or
unauthenticated way into the node remains: CI_LAB is not started, and GW,
the only other way packets enter a node's software bus, validates every
inbound packet with the same frame format, library and table (S5.2).

The consequence is accepted: a node whose active validation table is
corrupt (contents that passed validation but reject every frame the ground
can build, for example an allow-list or age window the ground cannot meet)
or whose key is not the one the ground holds can be recovered only by a
table load commanded through a frame that the active configuration still
accepts, or by restarting the node, which loads `/cf/cmdi_tbl.tbl` again.
A table that fails validation (S3.3) never becomes active. Do not add a
recovery path that bypasses validation.

### S3.1 Frame format

Multi-byte frame fields are big-endian. A frame is exactly 50 + L bytes.

| Offset | Size | Field |
|--------|------|-------|
| 0 | 2 | `Magic` 0x4356 |
| 2 | 1 | `Version` 1 |
| 3 | 1 | `Flags` 0 |
| 4 | 2 | `SourceId` |
| 6 | 2 | `CommandLength` L, 8..1024 |
| 8 | 4 | `TimeSeconds`: onboard seconds (`CFE_TIME_GetTime().Seconds`, the seconds field of telemetry secondary headers, bytes 6 to 9 of a telemetry packet, big-endian) |
| 12 | 4 | `Sequence`: per-source replay serial (S3.6) |
| 16 | L | embedded CCSDS command packet |
| 16+L | 32 | `Mac`: HMAC-SHA-256 with the table key over bytes 0 to 15+L (header and command, not MAC or CRC) |
| 48+L | 2 | `Crc`: CRC-16/CCITT-FALSE over bytes 0 to 47+L (MAC included) |

CRC-16/CCITT-FALSE: polynomial 0x1021, not reflected, initial 0xFFFF, final
XOR 0; check value of `123456789` is 0x29B1 (Python
`binascii.crc_hqx(data, 0xFFFF)`). HMAC per RFC 2104 / FIPS 198-1 over
SHA-256 (FIPS 180-4); keys longer than 64 bytes are hashed first.

### S3.2 Library API

`libs/cmdval_lib/fsw/public_inc/cmdval_lib.h`:

```c
#define CMDVAL_KEY_LEN         32
#define CMDVAL_MAC_LEN         32
#define CMDVAL_MAX_SOURCES     8
#define CMDVAL_FRAME_HDR_LEN   16
#define CMDVAL_FRAME_OVERHEAD  50
#define CMDVAL_MAX_COMMAND_LEN 1024
#define CMDVAL_FRAME_MAGIC     0x4356
#define CMDVAL_FRAME_VERSION   1
#define CMDVAL_CHECK_CRC    0x01
#define CMDVAL_CHECK_HMAC   0x02
#define CMDVAL_CHECK_SOURCE 0x04
#define CMDVAL_CHECK_AGE    0x08
#define CMDVAL_CHECK_ALL    0x0F

typedef enum
{
    CMDVAL_OK = 0, CMDVAL_ERR_FORMAT = 1, CMDVAL_ERR_CRC = 2,
    CMDVAL_ERR_HMAC = 3, CMDVAL_ERR_SOURCE = 4, CMDVAL_ERR_AGE = 5
} CMDVAL_Status_t;

typedef struct
{
    uint8  HmacKey[CMDVAL_KEY_LEN];
    uint16 AllowedSources[CMDVAL_MAX_SOURCES]; /* 0 marks an unused slot */
    uint32 MaxAgeSeconds;
    uint8  EnableMask;                          /* CMDVAL_CHECK_* bits */
    uint8  Spare[3];
} CMDVAL_Config_t;

typedef struct
{
    uint16 SourceId; uint32 TimeSeconds; uint32 Sequence;
    const uint8 *Command; size_t CommandLen; /* Command points into the frame */
} CMDVAL_FrameInfo_t;

int32           CMDVAL_LibInit(void); /* returns CFE_SUCCESS */
uint16          CMDVAL_Crc16(const uint8 *Data, size_t Len);
void            CMDVAL_Sha256(const uint8 *Data, size_t Len, uint8 Digest[32]);
void            CMDVAL_HmacSha256(const uint8 *Key, size_t KeyLen, const uint8 *Data, size_t Len,
                                  uint8 Mac[CMDVAL_MAC_LEN]);
CMDVAL_Status_t CMDVAL_CheckSource(uint16 SourceId, const CMDVAL_Config_t *Config);
CMDVAL_Status_t CMDVAL_CheckAge(uint32 FrameSeconds, uint32 NowSeconds, uint32 MaxAgeSeconds);
CMDVAL_Status_t CMDVAL_ValidateFrame(const uint8 *Frame, size_t FrameLen, const CMDVAL_Config_t *Config,
                                     uint32 NowSeconds, CMDVAL_FrameInfo_t *Info);
```

- `CMDVAL_CheckSource`: OK when `SourceId` is nonzero and equals a nonzero
  `AllowedSources` entry, else ERR_SOURCE.
- `CMDVAL_CheckAge`: OK when the absolute difference of `NowSeconds` and
  `FrameSeconds`, in signed 64-bit arithmetic, is at most `MaxAgeSeconds`
  (future frames are bounded by the same window), else ERR_AGE.
- `CMDVAL_ValidateFrame` returns the first failure in this order: FORMAT
  (always checked: `FrameLen` at least 58, magic, version, flags 0, L in
  8..1024, `FrameLen` = 50 + L, the embedded CCSDS length field consistent
  with L, the embedded type bit set to command), then CRC, HMAC, SOURCE,
  AGE, each only when its `EnableMask` bit is set. `Info` (may be NULL) is
  filled on OK.
- The MAC comparison takes time independent of where the MACs differ. The
  library is self-contained C (no external crypto library) and does no
  dynamic allocation.

### S3.3 CMDI table

```c
typedef struct
{
    CMDVAL_Config_t Validation; /* ACTIVE key is Validation.HmacKey */
    uint8 NextKey[CMDVAL_KEY_LEN];
    uint8 NextValid; uint8 Spare[3];
} CMDI_ValTable_t;
```

Validation rejects an all-zero key, `MaxAgeSeconds` 0 or above 86400,
`EnableMask` bits above 0x0F, the SOURCE bit with no nonzero source, and
duplicate nonzero sources; NextValid outside 0..1; NextValid 1 with
an all-zero NextKey or NextKey equal to Validation.HmacKey. With NextValid
0, NextKey must be all zero. Default: NextValid 0, NextKey all zero; key = the 32 ASCII bytes
`cFS-cmdval-test-key-0123456789ab`, sources 0x0001 and 0x0002 (others 0),
`MaxAgeSeconds` 30, `EnableMask` 0x0F.

### S3.4 CMDI behaviour, commands and telemetry

- At initialization CMDI binds a UDP socket to 127.0.0.1 port
  1234 + node ID - 1, CI_LAB's port (`CMDI_SOCKET_ERR_EID` on failure; the
  app keeps running, sending housekeeping and heartbeats, but the node has
  no ground command path until it is restarted).
- CI_LAB's own message interface (its MIDs, its `READ_UPLINK` message, its
  housekeeping layout and its pass-through ingest of unframed packets) is
  not carried over; CMDI's interface is exactly the one in this section.
- Every WAKEUP (10 Hz) reads up to 16 pending datagrams without blocking.
  Each is one frame: `FramesReceived`++, then `CMDVAL_ValidateFrame` with
  the active configuration and the current onboard seconds. OK: the
  embedded command is sent with `CFE_SB_TransmitMsg(..., false)`,
  `FramesAccepted`++, `CMDI_ACCEPT_DBG_EID` naming its MID, only after
  the additional S3.6 authentication/replay checks also pass. Otherwise the
  reason's counter is incremented, nothing is sent, and one
  `CMDI_REJECT_ERR_EID` names the reason (`FORMAT`, `CRC`, `HMAC`, `SOURCE`,
  `AGE` or `REPLAY`) and, except for FORMAT, the source ID. Datagrams longer than
  1074 bytes are FORMAT rejections.
- Every 10th WAKEUP sends a heartbeat (C.5).
- Commands: 0 NOOP, 1 RESET_COUNTERS (command and frame counters),
  2 SET_MAX_AGE `{ uint32 Seconds; }` (1..86400), 3 SET_ENABLE_MASK
  `{ uint8 Mask; uint8 Spare[3]; }` (0..0x0F); success
  `CMDI_SET_PARAM_INF_EID`, error `CMDI_SET_PARAM_ERR_EID`; values set by
  command are overridden by the next table load. These commands reach CMDI
  on `CMDI_CMD_MID` only, which means only through a frame that CMDI itself
  validated; a frame carrying a CMDI command gets no special treatment
  (for example, a SET_ENABLE_MASK frame is checked against the mask in
  force when it arrives, and the new mask applies from the next frame).

```c
/* CMDI_HkTlm_Payload_t */
uint8  CommandCounter; uint8 CommandErrorCounter; uint8 EnableMask; uint8 Spare;
uint32 FramesReceived; uint32 FramesAccepted;
uint32 FormatErrors; uint32 CrcErrors; uint32 HmacErrors; uint32 SourceErrors; uint32 AgeErrors;
uint32 MaxAgeSeconds;
```

Event IDs: 1 to 10 as in C.4,
11 SOCKET_ERR, 12 REJECT_ERR, 13 ACCEPT_DBG, 14 SET_PARAM_INF,
15 SET_PARAM_ERR.

### S3.5 Frame builder in `tools/udp_harness.py`

Since a plain CCSDS packet sent to UDP 1234 is now a FORMAT rejection,
extend `tools/udp_harness.py` (still Python 3.12+, standard library only:
`hmac`, `hashlib`, `binascii`, `struct`) so a tester can send validated
commands with the table key. Existing functions, CLI subcommands and their
default ports stay as they are (`send` and `enable-to` still send unframed
packets, for builds that run CI_LAB; on a Task 5 node CMDI rejects them).
Add, with these names and signatures:

```python
CMDI_DEFAULT_KEY: bytes = b"cFS-cmdval-test-key-0123456789ab"   # the default table key (S3.3)
CMDI_DEFAULT_SOURCE_ID: int = 1
CMDI_BASE_PORT: int = 1234   # CMDI port = base + node ID - 1
TLMO_BASE_PORT: int = 2234   # TLMO port = base + node ID - 1

def node_ports(node_id: int) -> tuple[int, int]: ...
    # (CMDI port, TLMO port) of node 1 or 2; HarnessError otherwise
def crc16_ccitt_false(data: bytes) -> int: ...
    # S3.1; crc16_ccitt_false(b"123456789") == 0x29B1
def build_frame(command: bytes, *, time_seconds: int, key: bytes = CMDI_DEFAULT_KEY,
                source_id: int = CMDI_DEFAULT_SOURCE_ID, sequence: int = 0) -> bytes: ...
    # the S3.1 frame around one CCSDS command packet (from build_command);
    # HarnessError when len(command) is outside 8..1024 or a field is out of range
def onboard_seconds(timeout: float = 10.0, *, host: str = HOST, port: int = TLMO_BASE_PORT) -> int: ...
    # seconds field of the first telemetry packet (type bit clear) received on port;
    # HarnessError when none arrives within timeout
def send_frame(frame: bytes, *, host: str = HOST, port: int = CMDI_BASE_PORT) -> None: ...
def send_framed_command(mid: int, cc: int, payload: bytes = b"", *, key: bytes = CMDI_DEFAULT_KEY,
                        source_id: int = CMDI_DEFAULT_SOURCE_ID, time_seconds: int | None = None,
                        sequence: int | None = None, host: str = HOST, port: int = CMDI_BASE_PORT,
                        tlm_port: int | None = None) -> bytes: ...
    # builds the command, frames it and sends it; returns the frame sent.
    # time_seconds None: onboard_seconds() on tlm_port (None: the TLMO port of the node whose
    # CMDI port is port). sequence None: a per-process counter starting at 1.
```

CLI: `udp_harness.py send-framed --mid M --cc C [--payload-hex H]
[--node N] [--key-hex K] [--source-id S] [--time-seconds T]
[--sequence Q]` sends one framed command to node N's CMDI (default 1),
reading onboard time from node N's TLMO port unless `--time-seconds` is
given, and prints the frame length and sequence; `recv` gains
`--node N` to listen on node N's TLMO port. Errors are reported as
`HarnessError` (library) or exit status 1 with a one-line message (CLI),
as the existing code does. Update the module docstring accordingly.

### S3.6 Replay windows, key slots and source statistics

CMDVAL_ValidateFrame remains stateless with the same API, enum values and
validation order. CMDI performs replay checking only after it returns OK,
before publishing the embedded command. It is mandatory, independent of
EnableMask. SourceId 0 is always a SOURCE rejection at this stage. Eight
runtime source slots start empty, are allocated on first otherwise-valid
frame (even with SOURCE disabled), never evicted; a ninth distinct source
is a SOURCE rejection. No failed validation or replay consumes a sequence
or allocates a slot. A successful table activation clears all replay slots;
RESET_COUNTERS and SWITCH_KEY do not. CMDI app restart clears the slots
and reloads its default on-disk table, just as node startup does. GW ingress retains stateless
validation; S3 replay windows apply to CMDI UDP ingress only.

Each source has a 64-bit seen bitmap and uint32 highest serial. Its first
serial, including 0, is accepted. Let d = (candidate - highest) modulo
2^32. If 0 < d < 2^31, accept as newer, shift the bitmap left d (clear it
if d >= 64), set bit 0 and update highest. Otherwise let b =
(highest - candidate) modulo 2^32: accept only b < 64 and bit b clear,
then set it. Duplicate, b >= 64 and the exactly-half-range d = 2^31 are
REPLAY. Thus FFFFFFFF -> 00000000 is newer. Window state is independent
per node and source; an accepted sequence is consumed even if the target
app later rejects the embedded command. Count ReplayErrors and emit
CMDI_REJECT_ERR (12, ERROR) naming REPLAY and source once per rejection;
FramesReceived includes it, FramesAccepted does not. Changing a key does
not permit replay of an accepted frame.

Two key slots are ACTIVE (runtime copy of Validation.HmacKey) and NEXT.
Only ACTIVE validates incoming frames. LOAD_NEXT_KEY is a cFE TBL
LOAD/VALIDATE/ACTIVATE of CMDI.ValTable, sent through authenticated CMDI
frames. It replaces both slots with the table values and clears replay
state, as any successful table activation does; staging must carry the
current ACTIVE key in Validation.HmacKey to preserve it. There is no
key-bytes command, no file read outside cFE TBL, and no dual-key grace.

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 4 | SWITCH_KEY | none | reject with KEY_ERR unless NEXT valid; replace ACTIVE with NEXT, zero NEXT and clear NextValid; success KEY_SWITCH_INF |
| 5 | SEND_SOURCE_STATS | none | one CMDI_SourceTlm_t, SOURCE_STATS_INF |

SWITCH_KEY must itself arrive in a frame with a valid ACTIVE-key HMAC,
even if HMAC is disabled by EnableMask: after ordinary validation, perform
that HMAC check before replay checking. Failure is the ordinary HMAC
frame rejection and executes nothing. Its sequence is consumed before the
command changes the key. Later frames under the retired key fail HMAC
when HMAC checking is enabled; disabling that check deliberately permits
any key for ordinary commands, but never bypasses SWITCH_KEY's mandatory
HMAC. GW ingress performs that same extra ACTIVE-key HMAC check for a
frame carrying CMDI SWITCH_KEY, after ordinary validation and before
publishing; bad MAC is HMAC (GW reason counter, RxErrors and event 25),
even when the shared EnableMask has HMAC clear. GW's replay exemption
is unchanged. Authenticated onboard SB producers need no frame.
Before reading its next UDP datagram, CMDI processes any CMDI_CMD_MID
command it has just forwarded through its normal SB dispatch. Thus a
self-command's mask, age or key change applies to the next datagram,
including datagrams already waiting in the socket. GW shares the active runtime key using the
CMDI.ValTable working copy: CMDI writes ACTIVE/NEXT changes with
CFE_TBL_Modified, and GW reads them on its next wakeup. SET_MAX_AGE and
SET_ENABLE_MASK remain runtime-only in CMDI; SWITCH_KEY modifies only
key fields in the shared table working copy. A later table activation
replaces this copy. No disk key file is rewritten.

Append HK, in order: `uint32 ReplayErrors; uint32 KeySwitches; uint8
NextValid; uint8 SourceSlotsUsed; uint16 Spare2;`. No telemetry or events
contain key material. RESET_COUNTERS zeroes KeySwitches, ReplayErrors and
source statistics as well as S3.4 counters, retaining slot identities and
bitmaps. Source statistics count all frames, even ones rejected before
slot allocation: a separate array has the eight AllowedSources in table
order (unused slots have SourceId 0 and zero counters) plus a ninth aggregate OTHER row (SourceId 0, including malformed
frames without a readable source). It is reset on table activation.

```c
typedef struct
{
    uint16 SourceId; uint16 Spare;
    uint32 Accepted; uint32 FormatErrors; uint32 CrcErrors; uint32 HmacErrors;
    uint32 SourceErrors; uint32 AgeErrors; uint32 ReplayErrors;
} CMDI_SourceStats_t;
typedef struct { CMDI_SourceStats_t Sources[CMDVAL_MAX_SOURCES + 1]; } CMDI_Source_Payload_t;
```

For FORMAT errors read SourceId only if FrameLen >= 6; map it to a row
by equality with a nonzero AllowedSources entry, otherwise OTHER. All
other failures use the parsed source. Exactly one accepted/reason counter
in one row changes per datagram. Append event IDs 16 KEY_SWITCH_INF,
17 KEY_ERR, 18 SOURCE_STATS_INF. Table update validation includes NEXT.
The frame builder still accepts an explicit sequence; callers of the
CLI across separate processes must use --sequence with a fresh serial.
CTF, the harness and acceptance use monotonic per-source sequences for
an entire node run, including commands issued by different plugins.

### S3 Acceptance

A hidden unit test linked against `cmdval_lib` checks the CRC check value,
SHA-256 and HMAC-SHA-256 against FIPS 180-4 and RFC 4231 test vectors
(including a key longer than 64 bytes), the age window at its boundaries in
both directions, the allow-list, and `CMDVAL_ValidateFrame` for every
status in the pinned order (a frame with a bad CRC and a bad MAC is a CRC
rejection; with CRC disabled, an HMAC rejection). The functions of S3.5
are imported from your `tools/udp_harness.py` and checked against an
independent frame encoder (byte-identical frames for the same inputs, the
CRC check value, the range errors). Live, acceptance runs its own headless
checks against your build. On cpu1, with nothing but CMDI listening on UDP
1234: an unframed SAMPLE_APP NOOP sent to 1234 is a FORMAT rejection and
does not execute; frames sent to 1234 carry a SAMPLE_APP NOOP; a valid
frame executes it (SAMPLE_APP `CommandCounter` rises); frames with a bad
CRC, a bad MAC, an unlisted source, a timestamp outside the window and a
malformed header are each rejected, counted under their reason in CMDI HK
with the event, and do not execute; SET_ENABLE_MASK and SET_MAX_AGE, sent
as frames, change outcomes as specified, and the same commands sent
unframed or in a frame with a bad MAC change nothing; a frame built with
`send_framed_command` from your harness is accepted. On cpu2 the same
valid frame sent to UDP 1235 executes there. The startup scripts of both
nodes are checked for CMDI and TLMO and for the absence of CI_LAB and
TO_LAB. Hidden tests also cover the 64-bit window (63/64 boundaries,
out-of-order gaps, half-range, wrap, slot exhaustion), key staging and
switch order, mandatory SWITCH_KEY authentication, replay retention on
switch/reset and source attribution at each validation failure. Live
checks accept serial 100, execute unseen serial 37, reject unseen serial
36 as REPLAY and reject duplicate serial 100 as REPLAY, with matching
execution/non-execution, reason/event/statistics. They stage NEXT through TBL,
switch under ACTIVE, verify retired-key rejection and NEXT-key acceptance
without resetting the sequence window, and observe GW using the new key, including GW
rejection of unauthenticated SWITCH_KEY while ordinary HMAC is disabled.

## S4. TLMO prioritised telemetry output

TLMO replaces TO_LAB as the node's telemetry output app: a clone-and-own
copy of TO_LAB (C.2) on TO_LAB's port. It forwards subscribed packets to
the ground over UDP under a per-cycle packet budget, highest priority class
first. TO_LAB's subscription table is replaced by the priority table
below, its ADD_PKT and REMOVE_PKT commands by ADD_STREAM and REMOVE_STREAM
(S4.3); its DATA_TYPES packet, REMOVE_ALL_PKT and OUTPUT_ENABLE are not
carried over, because TLMO's output is always on.

### S4.1 Table

```c
#define TLMO_MAX_STREAMS 64
#define TLMO_NUM_CLASSES 4    /* class 0 is the highest priority, 3 the lowest */
#define TLMO_MAX_QUEUE   64
#define TLMO_MAX_BUDGET  256
#define TLMO_MAX_PACKET  8192

typedef struct { CFE_SB_MsgId_t MsgId; /* INVALID = unused */ uint8 Class; uint8 Decimation; uint16 Spare; } TLMO_StreamDef_t;

typedef struct { uint16 Budget; uint8 ClassMap[4]; uint16 Spare; } TLMO_ProfileDef_t;

typedef struct
{
    uint16           BudgetPacketsPerCycle; /* 0..256; 0 holds all output */
    uint16           QueueCapacity;         /* 1..64 packets, shared by all classes */
    TLMO_StreamDef_t Streams[TLMO_MAX_STREAMS];
    TLMO_ProfileDef_t Profiles[3]; /* NOMINAL, DEGRADED, SAFE */
} TLMO_PriorityTable_t;
```

Validation rejects a budget above 256, a capacity outside 1..64, a class
above 3 or Decimation 0 in a used entry, duplicate used MIDs, a profile
budget above 256 or any profile ClassMap value above 3. Default: budget 16,
capacity 64, Decimation 1 for every used stream, Profiles budgets
16, 8, 4 and ClassMaps {0,1,2,3}, {0,1,2,2}, {0,1,1,1}, and the streams below with their classes, so that every
stream TO_LAB's default subscription table
(`sample_defs/tables/to_lab_sub.c`) carried still reaches the ground port,
apart from the lab apps' own `TO_LAB_HK_TLM_MID`, `TO_LAB_DATA_TYPES_MID`
and `CI_LAB_HK_TLM_MID`, whose apps no longer run:

| Class | Streams |
|-------|---------|
| 0 | `CFE_EVS_LONG_EVENT_MSG_MID`, `SFI_HK_TLM_MID`, verification 0xE8 |
| 1 | `CFE_ES_HK_TLM_MID`, `CFE_EVS_HK_TLM_MID`, `CFE_SB_HK_TLM_MID`, `CFE_TBL_HK_TLM_MID`, `STOR_HK_TLM_MID`, `CMDI_HK_TLM_MID`, `TLMO_HK_TLM_MID`, `GW_HK_TLM_MID`, `CF_HK_TLM_MID`, `MD_HK_TLM_MID`, `HS_HK_TLM_MID`, `HK_HK_TLM_MID`, `SC_HK_TLM_MID`, `FM_HK_TLM_MID`, `DS_HK_TLM_MID`, `LC_HK_TLM_MID`, `CS_HK_TLM_MID`, verification 0xE9 |
| 2 | `CFE_TIME_HK_TLM_MID`, `SAMPLE_APP_HK_TLM_MID`, `CFE_TIME_DIAG_TLM_MID`, `CFE_SB_STATS_TLM_MID`, `CFE_TBL_REG_TLM_MID`, `CFE_ES_APP_TLM_MID`, `CFE_ES_MEMSTATS_TLM_MID`, `CF_EOT_TLM_MID`, `FM_FILE_INFO_TLM_MID`, `FM_DIR_LIST_TLM_MID`, `FM_OPEN_FILES_TLM_MID`, `FM_MONITOR_TLM_MID`, `STOR_LIST_TLM_MID`, `STOR_STATS_TLM_MID`, `TLMO_CLASS_TLM_MID`, `GW_CONN_TLM_MID`, `SFI_STATUS_TLM_MID`, verification 0xEA |
| 3 | `CF_CH0_TX_MID`, `CF_CH1_TX_MID`, `STOR_XFER_TLM_MID`, verification 0xEB |
| 1 (only under `CFS_SBN_ENABLED`, as in `to_lab_sub.c`) | `SBN_HK_TLM_MID`, `SBN_HKNET_TLM_MID`, `SBN_HKPEER_TLM_MID`, `SBN_HKMYSUBS_TLM_MID`, `SBN_HKPEERSUBS_TLM_MID` |

Also include MODE_HK_TLM_MID and SFI_FAULT_TLM_MID in class 0;
STOR_FILE_INFO_TLM_MID, CMDI_SOURCE_TLM_MID, TLMO_STREAM_TLM_MID and
MODE_STATUS_TLM_MID in class 2; SFI_FAULT_MID, SFI_HEALTH_MID and
MODE_PEER_MID in class 0. These last three are command packets, forwarded
unchanged like verification streams, so a headless observer sees onset,
clear, mode and health without accessing a node console.

Default slot order is the listed table rows top to bottom, MIDs left to
right within each row, followed by the nine added streams in the prose
order above. Without SBN omit those five slots, not holes. Unused slots
have INVALID MsgId and zero remaining fields.

That is 57 streams on this build (52 without SBN), within
`TLMO_MAX_STREAMS`. The verification streams are the reserved command MIDs
of C.3 for topics 0xE8 to 0xEB (`CFE_PLATFORM_CMD_TOPICID_TO_MIDV`, 0x18E8
to 0x18EB on cpu1).

### S4.2 Behaviour

- TLMO keeps a runtime stream list of `TLMO_MAX_STREAMS` slots. Every
  table load (initial or update) sets it to a copy of the table's
  `Streams` (same slot indices); ADD_STREAM fills the lowest-indexed unused
  slot and REMOVE_STREAM empties a slot (S4.3). "Used stream" below means
  a used slot of the runtime list.
- TLMO subscribes to every used stream with a message limit of 32 on a pipe
  of depth at least 128, and sends to 127.0.0.1 port 2234 + node ID - 1, TO_LAB's
  port. Output is always on from initialization: no enable command is
  needed, and none exists.
- Enqueue: each received stream packet (`PacketsReceived`++) is copied into
  one queue shared by all classes, after decimation (S4.4), tagged with
  the selected profile's mapping of the stream's base class.
  A packet longer than `TLMO_MAX_PACKET` is dropped and counted as a drop
  of its class. If the queue holds `QueueCapacity` packets: when it holds a
  packet of lower priority (higher class number) than the arriving one, the
  oldest packet of the lowest-priority class present is evicted and counted
  as a drop of its class, and the arriving packet is enqueued; otherwise
  the arriving packet is dropped and counted as a drop of its own class.
- Drain: on every WAKEUP (10 Hz) TLMO sends up to the current budget,
  each time the oldest packet of the highest-priority non-empty class, as
  one UDP datagram with the packet bytes unchanged. Success increments
  `SentByClass[class]`, failure `SocketErrors`; either way the packet
  leaves the queue. Budget 0 sends nothing; the queue keeps filling and
  evicting.
- The first drop in each class after initialization, RESET_COUNTERS or a
  table load sends one `TLMO_DROP_INF_EID` naming the class.
- Table update: the runtime list is replaced by the table's (streams added
  by command are dropped), subscriptions follow the new list, runtime
  classes and budget reset to the table, queued packets of streams no
  longer listed are discarded without counting. Retained packets follow
  their MID to its new slot and retain their queued class. Reset cumulative
  per-stream counters and phases, then recompute Queued from retained
  packets. `TLMO_TBL_UPDATE_INF_EID` is sent (not on the initial load).
- Every 10th WAKEUP sends a heartbeat (C.5).

### S4.3 Commands and telemetry

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 0 | NOOP | none | |
| 1 | RESET_COUNTERS | none | command counters, `PacketsReceived`, `SentByClass`, `DroppedByClass`, `SocketErrors` |
| 2 | SET_CLASS | `{ CFE_SB_MsgId_t MsgId; uint8 Class; uint8 Spare[3]; }` | used stream and class 0..3, else `TLMO_SET_CLASS_ERR_EID`; success `TLMO_SET_CLASS_INF_EID`; affects packets enqueued afterwards |
| 3 | SET_BUDGET | `{ uint16 Packets; uint16 Spare; }` | 0..256, else `TLMO_SET_BUDGET_ERR_EID`; success `TLMO_SET_BUDGET_INF_EID` |
| 4 | SEND_CLASS_MAP | none | one `TLMO_ClassTlm_t`, `TLMO_CLASS_MAP_INF_EID` |
| 5 | ADD_STREAM | `TLMO_AddStream_Payload_t { CFE_SB_MsgId_t MsgId; uint8 Class; uint8 Spare[3]; }` | the runtime replacement for TO_LAB's ADD_PKT. `MsgId` valid and not a used stream, class 0..3, an unused slot left, and the subscription (message limit 32) succeeds, else `TLMO_ADD_STREAM_ERR_EID` naming the reason, nothing changed; success fills the lowest unused slot, `TLMO_ADD_STREAM_INF_EID` with MID and class |
| 6 | REMOVE_STREAM | `TLMO_RemoveStream_Payload_t { CFE_SB_MsgId_t MsgId; }` | used stream, else `TLMO_REMOVE_STREAM_ERR_EID`; success unsubscribes, empties the slot, discards that stream's queued packets without counting, `TLMO_REMOVE_STREAM_INF_EID` |

ADD_STREAM and REMOVE_STREAM apply to table streams and added streams
alike, and last until the next table load.

```c
typedef struct
{
    uint16 BudgetPacketsPerCycle; uint16 StreamCount; /* used slots */
    TLMO_StreamDef_t Streams[TLMO_MAX_STREAMS];       /* runtime list in slot order, runtime classes, unused slots INVALID */
} TLMO_Class_Payload_t;

/* TLMO_HkTlm_Payload_t */
uint8  CommandCounter; uint8 CommandErrorCounter; uint16 BudgetPacketsPerCycle;
uint16 QueuedPackets; uint16 QueueCapacity; uint16 ActiveStreams; uint16 Spare;
uint32 PacketsReceived;
uint32 SentByClass[TLMO_NUM_CLASSES];
uint32 DroppedByClass[TLMO_NUM_CLASSES];
uint32 SocketErrors;
```

Event IDs: 1 to 10 as in C.4,
11 SUB_ERR, 12 SOCKET_ERR, 13 SET_CLASS_INF, 14 SET_CLASS_ERR,
15 SET_BUDGET_INF, 16 SET_BUDGET_ERR, 17 DROP_INF, 18 CLASS_MAP_INF,
19 TBL_UPDATE_INF, 20 ADD_STREAM_INF, 21 ADD_STREAM_ERR,
22 REMOVE_STREAM_INF, 23 REMOVE_STREAM_ERR.

### S4.4 Decimation, profiles and stream statistics

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 7 | SET_DECIMATION | `{ CFE_SB_MsgId_t MsgId; uint8 Decimation; uint8 Spare[3]; }` | used stream and 1..255, else SET_DECIMATION_ERR; success SET_DECIMATION_INF; reset its decimation phase |
| 8 | SELECT_PROFILE | `{ uint8 Mode; uint8 Spare[3]; }` | 0..2, else PROFILE_ERR; success PROFILE_INF, apply that profile's budget and ClassMap |
| 9 | SEND_STREAM_STATS | none | one full TLMO_StreamTlm_t snapshot, STREAM_STATS_INF |

Initial profile 0, with BudgetPacketsPerCycle from the table (which may
differ from Profiles[0].Budget). SELECT_PROFILE applies Profiles[Mode].Budget;
SET_BUDGET overrides the budget until the next SELECT_PROFILE or table
activation. SET_CLASS and ADD_STREAM set the base class. Enqueue maps the
base class through the selected ClassMap; queued packets keep their class
when either class or profile changes. SEND_CLASS_MAP reports effective
classes. ADD_STREAM's existing Spare bytes remain zero and it creates a
stream with Decimation 1. A table activation selects profile 0, installs
the table budget and resets decimation phases and cumulative per-stream
counters, then recomputes Queued by MID from retained packets, including
when table slots are reordered (S4.2).

Each used slot has a phase starting at 0. On reception increment
PacketsReceived and that stream's Received; keep the packet if phase is
0, then set phase = (phase + 1) % Decimation. Otherwise increment its
Decimated and do not enqueue or count a class drop. Length checks and
queue/drop processing apply only to kept packets. Stats track queue
ownership even after SET_CLASS. Successful UDP sends increment stream
Sent; overflow, oversize and eviction increment the affected stream's
Dropped; UDP failure increments stream SendErrors. Queued is the current
number owned by the stream. Command removal and table discards are not
drops. RESET_COUNTERS zeroes cumulative stream counters, retaining phase
and Queued. SEND_STREAM_STATS snapshots all used slots into one full-sized packet,
entries in ascending slot order, unused tail entries zero. StreamCount 0
sends the same packet with every entry zero; no empty-list variant.
Class is the effective mapped class, Decimation the runtime factor and
Queued the current number of queued packets owned by that slot.

```c
typedef struct
{
    uint16 Slot; uint16 Spare;
    CFE_SB_MsgId_t MsgId;
    uint8 Class; uint8 Decimation; uint16 Queued;
    uint32 Received; uint32 Sent; uint32 Dropped; uint32 Decimated; uint32 SendErrors;
} TLMO_StreamStats_t;
typedef struct
{
    uint16 StreamCount; uint16 Spare;
    TLMO_StreamStats_t Streams[TLMO_MAX_STREAMS];
} TLMO_Stream_Payload_t;
```

Append HK `uint8 SelectedProfile; uint8 Spare2[3]; uint32
PacketsDecimated;`. PacketsDecimated is reset with the counters.
Append events 24 SET_DECIMATION_INF, 25 SET_DECIMATION_ERR,
26 PROFILE_INF, 27 PROFILE_ERR, 28 STREAM_STATS_INF.

### S4 Acceptance

Hidden coverage tests exercise enqueue, eviction, drain order, budget 0,
the commands (including ADD_STREAM and REMOVE_STREAM with a full runtime
list) and table validation. Live, acceptance runs its own headless checks
against your build. On cpu1, receiving on UDP 2234 with no enable command
sent: within one housekeeping period after startup, housekeeping of cFE
ES, EVS, SB, TBL and TIME, SAMPLE_APP and every new app, and event
messages arrive, as TO_LAB delivered them before; with the budget at 0,
verification packets injected as CMDI frames on the verification streams
overflow the queue and the drop counters show drops only where the
eviction rule puts them; after SET_BUDGET to a small value the datagrams
arrive class 0 first, then class 1, and so on, each class in arrival order;
SET_CLASS moves a stream between classes with the expected effect;
ADD_STREAM of an unlisted reserved MID makes its injected packets arrive
and REMOVE_STREAM stops them, with the error cases; a table update loaded
through cFE Table Services changes classes and budget and drops added
streams; SEND_CLASS_MAP reports the runtime map. On cpu2, telemetry of
cpu2 arrives on UDP 2235. Hidden tests and live injections verify phase
0 selection, N=1/255, invalid N=0, phase reset, per-stream attribution of
evictions and send failures, Queued with held output, every profile map
and budget, unchanged queued classes, table reset and counter reset.
Each statistics snapshot is checked against injected stream totals.

## S5. GW gateway

GW moves software-bus packets over connections. A connection has an
interface (UDP, TCP or serial), a destination and a subscription list of
MIDs to egress. GW writes each subscribed packet, wrapped in a CMDI frame
(S3.1), to every enabled connection that lists it, and publishes the
command embedded in each frame arriving on a connection onto the software
bus, after validating the frame exactly as CMDI does. GW ingress is a
command path like CMDI, with no exemption.

### S5.1 Connection definition and table

```c
#define GW_MAX_CONNECTIONS 8
#define GW_MAX_SUBS        16
#define GW_ADDR_LEN        16
#define GW_DEV_PATH_LEN    64
#define GW_MAX_PACKET      1024
#define GW_TCP_ATTEMPT_SEC 5    /* TCP: seconds between the starts of two connect attempts, and each attempt's bound */
#define GW_TCP_MAX_RETRIES 3    /* TCP: retries after the first attempt of a cycle */

typedef enum { GW_IfType_NONE = 0, GW_IfType_UDP = 1, GW_IfType_TCP = 2, GW_IfType_SERIAL = 3 } GW_IfType_Enum_t;

typedef struct
{
    uint8          Interface;                   /* GW_IfType_Enum_t; NONE = unused slot */
    uint8          Enabled;                     /* 0 or 1 */
    uint16         LocalPort;                   /* UDP: receive port, 0 = egress only; TCP, SERIAL: 0 */
    char           RemoteAddr[GW_ADDR_LEN];     /* UDP, TCP: dotted IPv4 */
    uint16         RemotePort;                  /* UDP, TCP: nonzero */
    uint16         Spare;
    char           DevicePath[GW_DEV_PATH_LEN]; /* SERIAL: host path of a PTY slave, e.g. /dev/pts/7 */
    uint8 KeepAliveEnabled; uint8 PeerNode; uint16 Spare2;
    CFE_SB_MsgId_t Subs[GW_MAX_SUBS];           /* egress MIDs, CFE_SB_INVALID_MSG_ID = unused */
} GW_ConnDef_t;

#define GW_MAX_ROUTES 16
typedef struct
{
    CFE_SB_MsgId_t MsgId; /* INVALID = unused */
    uint8 Primary; uint8 Backup; uint16 Spare;
} GW_RouteDef_t;
typedef struct
{
    GW_ConnDef_t Conns[GW_MAX_CONNECTIONS];
    GW_RouteDef_t Routes[GW_MAX_ROUTES];
} GW_ConnTable_t;
```

A used definition (table or ADD_CONN) is valid when: `Interface` is 1..3;
`Enabled` is 0 or 1; for UDP and TCP `RemoteAddr` is terminated and a valid
dotted IPv4 address and `RemotePort` nonzero; for TCP and SERIAL
`LocalPort` is 0; for SERIAL `DevicePath` is terminated and starts with
`/`; `Subs` has no duplicate used MID; no two UDP connections share a
nonzero `LocalPort`. KeepAliveEnabled is 0 or 1; with 1, PeerNode is 1 or 2 and not own node,
and UDP LocalPort must be nonzero; with 0, PeerNode must be 0. A used route
has a valid command MID, Primary and Backup distinct in 0..7, both naming
used connections listing that MID, and no other route uses that MID.
The default table has every connection and route unused, spares zero.

### S5.2 Interfaces and behaviour

- UDP: one CMDI frame per datagram to `RemoteAddr:RemotePort`; with a nonzero
  `LocalPort`, GW binds 0.0.0.0:`LocalPort` and reads inbound datagrams.
- TCP: GW is the client and connects to `RemoteAddr:RemotePort`. A
  connect attempt never blocks GW's main loop; with the pinned OSAL, which
  opens every socket non-blocking, it is done this way: open a stream
  socket and call `OS_SocketConnect` with a timeout of 0. `OS_SUCCESS`
  means the connection is UP; `OS_ERROR` means the attempt failed (for
  example the peer refused); `OS_ERROR_TIMEOUT` means the attempt is in
  progress, and on each later WAKEUP GW polls it with `OS_SelectSingle`
  (`OS_STREAM_STATE_READABLE | OS_STREAM_STATE_WRITABLE`, timeout 0):
  writable and not readable means UP; readable means the attempt has
  ended, and one `OS_TimedRead` with timeout 0 decides it (an error or end
  of stream: failed; data: UP, and the bytes go to the stream deframer). Do
  not call `OS_SocketConnect` again on a socket whose attempt is in
  progress (OSAL then reports `OS_ERROR`). An attempt still undecided
  `GW_TCP_ATTEMPT_SEC` (5) seconds after it started fails. A failed
  attempt's socket is closed.
- TCP connect cycle: a cycle is a first attempt and at most
  `GW_TCP_MAX_RETRIES` (3) retries, 4 attempts in all, started
  `GW_TCP_ATTEMPT_SEC` seconds apart (each attempt starts 5 s after the
  previous one started, however early that one failed). While a cycle runs
  the connection is CONNECTING (2). When no attempt of the cycle reaches
  UP, the connection enters DOWN (3) at the end of the fourth attempt's
  period, 4 x 5 = 20 s after the cycle began: GW sends one
  `GW_CONN_TIMEOUT_ERR_EID` naming the connection index, `RemoteAddr` and
  `RemotePort`, and makes no further attempt; `Enabled` stays 1. A cycle
  begins, with its first attempt at once, whenever the connection is
  opened (ADD_CONN or ENABLE_CONN with `Enabled` 1, RELOAD, a table
  update) and whenever an UP connection drops (peer close or an I/O
  error). From DOWN, DISABLE_CONN then ENABLE_CONN begins a new cycle. A
  peer that does not listen or does not answer is not an open failure:
  ADD_CONN and ENABLE_CONN succeed and the connection is CONNECTING. Failed
  attempts are counted in none of the connection's counters.
- SERIAL: GW opens `DevicePath` read-write when enabled and does not change
  terminal attributes (the other side sets raw mode). GW uses OSAL for file
  and socket I/O; mapping `/dev/pts` into the OSAL file system with
  `OS_FileSysAddFixedMap` is acceptable. Two properties of this path are
  requirements you must make work and demonstrate (S5 Acceptance, S8),
  not assumptions:
  - Reads of a PTY never block GW's main loop: a read when no byte is
    pending returns at once (for example a zero-timeout `OS_TimedRead`, or
    `OS_SelectSingle` with a zero timeout before the read; whichever you
    use, show it working on a real PTY), and a WAKEUP with nothing pending
    costs no more than a poll.
  - GW can be stopped with cFE ES STOP_APP and started again with ES
    START_APP within one run of the node, whether or not a SERIAL
    connection was open (and the `/dev/pts` mapping made) before the stop,
    and after the restart it opens SERIAL connections and carries traffic
    over them as before. If OSAL reports the mapping as already present
    after a restart, or keeps file or socket resources of the stopped
    instance, GW handles that (reuse the mapping, or release it on exit)
    rather than failing initialization.
- Frames on every interface. Every unit GW sends or receives on a
  connection is one CMDI frame (S3.1, big-endian fields, 50 + L bytes, L
  in 8..1024): UDP carries one frame per datagram; TCP and SERIAL carry
  each frame preceded by the stream marker below. GW links `cmdval_lib`
  and uses its functions for building and validating frames; it has no
  frame code of its own beyond the stream deframer.
- Validation table. GW does not own a validation table: it shares CMDI's
  with `CFE_TBL_Share` on the table name `CMDI.ValTable` (a second
  registration loading `/cf/cmdi_tbl.tbl` would fail, because the file's
  header names `CMDI.ValTable`, and two copies could diverge). GW tries
  the share at initialization and, until it succeeds, again on every
  WAKEUP (CMDI may start later or be restarted); each failure streak sends
  one `GW_VALTBL_ERR_EID`, success sends `GW_VALTBL_INF_EID`. On every
  WAKEUP GW calls `CFE_TBL_GetAddress` on the shared handle, so a table
  update of `CMDI.ValTable` (initial load or through cFE Table Services)
  takes effect for GW on its next WAKEUP, the same update event CMDI
  acts on; GW keeps no copy across wakeups that could go stale. GW
  validates against the table's `CMDVAL_Config_t` as loaded; CMDI's
  SET_MAX_AGE and SET_ENABLE_MASK change CMDI's runtime values only, not
  GW's. `ValTableShared` in HK is 1 while GW holds a valid address, else
  0; while it is 0, GW reads nothing from its connections (inbound data
  waits in the socket or PTY) and egresses nothing (each skipped write
  counts as an egress error).
- Ingress validation. Each inbound frame: `FramesReceived`++, then
  `CMDVAL_ValidateFrame` with the shared configuration and GW's current
  onboard seconds, the same call, check order (FORMAT, then CRC, HMAC,
  SOURCE, AGE as enabled by the table's `EnableMask`) and statuses as
  CMDI. A rejection increments GW's counter for the reason
  (`FormatErrors`, `CrcErrors`, `HmacErrors`, `SourceErrors`,
  `AgeErrors`) and the connection's `RxErrors`, publishes nothing, and
  sends one `GW_FRAME_REJECT_ERR_EID` naming the connection index, the
  reason (`FORMAT`, `CRC`, `HMAC`, `SOURCE` or `AGE`) and, except for
  FORMAT, the source ID. An accepted frame's embedded command then goes
  through the loop rule below and is published. Because CMDVAL accepts
  only command packets (S3.2), GW can ingest only commands; telemetry is
  not bridged into a node.
- Egress framing. GW wraps each egressed packet with `CMDVAL_HmacSha256`
  and `CMDVAL_Crc16` into a frame with the shared table's key,
  `SourceId` = GW's node ID (1 or 2, both allowed by the default table),
  `TimeSeconds` = GW's onboard seconds, `Sequence` = a per-connection
  counter starting at 1 after the connection is opened, `Flags` 0 and
  `Version` 1. A packet longer than 1024 bytes cannot be framed and counts
  as an egress error. Egressed telemetry is framed too, for a ground or
  test endpoint; a peer GW rejects it as FORMAT, so only commands are
  bridged between nodes. For two GWs to accept each other's frames, the
  nodes' onboard clocks must agree within the table's `MaxAgeSeconds`
  (30 s by default); start the nodes so that they do and record how in
  `docs/reports/system.md`.
- TCP and SERIAL carry each frame preceded by the 4-byte marker
  `1A CF FC 1D`. The receiver hunts for the marker, reads the 16-byte frame
  header, takes L from its `CommandLength` field and reads the remaining
  L + 34 bytes. A header whose L is outside 8..`GW_MAX_PACKET` (1024) is a
  FORMAT rejection without reading further, and hunting resumes after that
  marker; any other complete frame is validated as above. Partial frames
  are kept across wakeups.
- Inbound: every WAKEUP (10 Hz) GW reads without blocking up to 16 frames
  per enabled connection. A UDP datagram is one frame; one whose size is
  not 50 + L is a FORMAT rejection (CMDVAL's own length check). A read or
  receive failure is an ingress error (`IngressErrors`++). After
  validation, an embedded command whose MID is in the `Subs` of any
  enabled connection is dropped (`LoopDrops`++), so two gateways cannot
  echo traffic. Every other embedded command is published with
  `CFE_SB_TransmitMsg(..., false)`, `PacketsIngressed`++.
- Outbound: each subscribed packet is framed and written to every enabled
  connection listing its MID, `PacketsEgressed`++ per write; a TCP connection that is
  CONNECTING or DOWN is skipped and counts an egress error. GW subscribes, message
  limit 16, to the union of the enabled connections' `Subs` and
  unsubscribes MIDs no longer needed whenever the union changes.
- An open, bind or I/O failure on a UDP or SERIAL connection disables it
  (`Enabled` 0, `GW_CONN_DOWN_ERR_EID`). `GW_EGRESS_ERR_EID` /
  `GW_INGRESS_ERR_EID` are sent once per connection until its next
  successful write / read; counters always increment.
- Connection state: DISABLED 0, UP 1, CONNECTING 2, DOWN 3 (TCP connect exhaustion or keep-alive loss, see
  the connect cycle above); reaching UP sends `GW_CONN_UP_INF_EID`,
  TCP connect-cycle exhaustion emits `GW_CONN_TIMEOUT_ERR_EID` (28).
  Keep-alive expiry emits `GW_KEEPALIVE_ERR_EID` (29), without
  `GW_CONN_TIMEOUT_ERR_EID`. Each event occurs once per corresponding edge.
- Every 10th WAKEUP sends a heartbeat (C.5).

### S5.3 Commands and telemetry

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 0, 1 | NOOP, RESET_COUNTERS | none | reset: command, HK and per-connection counters |
| 2 | ADD_CONN | `{ uint8 Index; uint8 Spare[3]; GW_ConnDef_t Conn; }` | `Index` below 8, slot unused, `Conn` valid and not NONE, else `GW_CONN_ADD_ERR_EID`; with `Enabled` 1 opened at once, and an open failure rejects the command leaving the slot unused (for TCP only a socket that cannot be opened is an open failure; a peer that refuses or does not answer is not); success `GW_CONN_ADD_INF_EID` |
| 3 | REMOVE_CONN | `GW_ConnIndex_Payload_t { uint8 Index; uint8 Spare[3]; }` | slot used, else `GW_CONN_REMOVE_ERR_EID`; closes and frees it, `GW_CONN_REMOVE_INF_EID` |
| 4 | ENABLE_CONN | `GW_ConnIndex_Payload_t` | slot used and disabled and opening succeeds (as for ADD_CONN), else `GW_CONN_ENABLE_ERR_EID`; success `GW_CONN_ENABLE_INF_EID`; a TCP connection begins a new connect cycle |
| 5 | DISABLE_CONN | `GW_ConnIndex_Payload_t` | slot used and enabled, else `GW_CONN_DISABLE_ERR_EID`; success `GW_CONN_DISABLE_INF_EID` |
| 6 | RELOAD | none | close all, re-apply the active table, discarding command changes, `GW_RELOAD_INF_EID`; a table update is applied the same way |
| 7 | SEND_CONN_STATUS | none | one `GW_ConnTlm_t`, `GW_CONN_STATUS_INF_EID` |

```c
typedef struct
{
    uint8  Interface; uint8 Enabled; uint8 State; uint8 SubCount;
    uint32 TxPackets; uint32 RxPackets; uint32 TxErrors; uint32 RxErrors;
} GW_ConnStatus_t;
typedef struct { GW_ConnStatus_t Conns[GW_MAX_CONNECTIONS]; } GW_Conn_Payload_t;

/* GW_HkTlm_Payload_t */
uint8  CommandCounter; uint8 CommandErrorCounter; uint8 ConnectionsConfigured; uint8 ConnectionsUp;
uint32 PacketsEgressed; uint32 PacketsIngressed; uint32 EgressErrors; uint32 IngressErrors; uint32 LoopDrops;
uint16 Subscriptions; uint8 ValTableShared; uint8 Spare;
uint32 FramesReceived;
uint32 FormatErrors; uint32 CrcErrors; uint32 HmacErrors; uint32 SourceErrors; uint32 AgeErrors;
```

`GW_ConnStatus_t.RxPackets` counts accepted frames and `RxErrors` read
errors plus rejected frames of that connection. RESET_COUNTERS also zeroes
`FramesReceived` and the five reason counters.

Event IDs: 1 to 10 as in C.4,
11 CONN_ADD_INF, 12 CONN_ADD_ERR, 13 CONN_REMOVE_INF, 14 CONN_REMOVE_ERR,
15 CONN_ENABLE_INF, 16 CONN_ENABLE_ERR, 17 CONN_DISABLE_INF,
18 CONN_DISABLE_ERR, 19 RELOAD_INF, 20 CONN_UP_INF, 21 CONN_DOWN_ERR,
22 EGRESS_ERR, 23 INGRESS_ERR, 24 CONN_STATUS_INF, 25 FRAME_REJECT_ERR,
26 VALTBL_ERR, 27 VALTBL_INF, 28 CONN_TIMEOUT_ERR.

### S5.4 Keep-alives, routing and connection accounting

Keep-alives are enabled per connection, so a ground/PTY test endpoint may
retain KeepAliveEnabled 0. An enabled UP connection with keep-alives
sends one GW_KeepAliveCmd_t each 10 GW wakeups, regardless of Subs and
routes, wrapped in the ordinary authenticated link frame (and marker).
It uses global GW_KEEPALIVE_MID, function code 0, payload:

```c
typedef struct
{
    uint8 SrcNode; uint8 DstNode; uint8 Reply; uint8 Spare;
    uint32 Sequence;
} GW_KeepAlive_Payload_t;
```

Sequence starts at 1 per open, increments per probe and wraps modulo
2^32. Reply 0 is a probe; its receiving connection sends Reply 1 with
the same Sequence, nodes reversed, in that wakeup's outbound phase.
Valid incoming probes and replies (exact payload size and FC 0) from
configured PeerNode addressed to own node refresh that connection's
receive time; malformed payloads, wrong nodes and Reply >1
increment RxErrors and emit FRAME_REJECT_ERR naming KEEPALIVE, without
refreshing time. GW consumes keep-alives, never publishing them on SB,
and never replies to a reply. No replay test applies on these links.
KeepAliveEnabled 0 ignores a well-formed keep-alive after validation.

Each GW wakeup processes pending inbound frames first (within the S5.2
per-connection bound), evaluates keep-alive deadlines second, updates
route selections third, and performs outbound work afterward. A valid
keep-alive processed on the deadline wakeup refreshes the timer without
a DOWN edge. Probe replies are sent in that same wakeup after route
selection, along with other outbound work.

Timer starts at UP. Six onboard seconds with no valid keep-alive makes
the connection DOWN on the first GW wakeup at or after the deadline:
GW_KEEPALIVE_ERR (29), without CONN_TIMEOUT_ERR (28), and SFI REPORT_LINK
(S6.6) Down 1 once. Keep Enabled 1, retain the transport and read it; send probes once per second while DOWN.
Any valid keep-alive makes it UP, emits CONN_UP_INF and REPORT_LINK Down 0.
TCP transport failure follows the existing connect cycle; if previously
UP with keep-alives, also reports link fault immediately, clearing it only
on a later UP with valid keep-alive. Exhausting a TCP connect cycle keeps
the existing no-more-connect-attempts rule. A TCP connection that has never been UP sends no
REPORT_LINK when its connect cycle is exhausted: link reports mark edges
from UP only. UDP/SERIAL open or I/O failures
still disable the connection per S5.2 and also report link fault when
keep-alives are enabled. Explicit DISABLE/REMOVE clears that link fault
and stops timers; administrative disabling is not a fault.

For each used route GW egresses its MID only on the selected connection,
not on both connections' Subs. Initially choose Primary if UP, else
Backup if UP, else no connection (0xFF). A selected link not UP switches
to the other UP link on the same wakeup; if neither is UP, drop/count one
egress error for each packet. Backup stays selected until Primary has
been continuously UP for 10 onboard seconds, then fail back on the first
GW wakeup at or after that deadline. A Primary interruption resets that
timer. Failover/failback emit ROUTE_INF naming route and old/new index;
selection changes do not queue or duplicate traffic. Non-routed MIDs
retain S5.2 fanout. Routes whose endpoint is removed are removed too.

The loop guard has exactly two exceptions: SFI_HEALTH_MID and
MODE_PEER_MID. GW publishes their validated inbound commands despite
local Subs, tagging those SB messages as received for its own egress
handler (bounded cache of the last 64 full packet byte strings, retained
for 2 onboard seconds). It never egresses a cached inbound packet. The
originating messages include SrcNode and a sequence; own-node messages
are ignored by SFI/MODE. Cache comparison includes headers and full
payload. This permits bidirectional same-MID health subscriptions without
echoing. STOR's directional subscriptions retain the ordinary loop rule.

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 8 | SET_ROUTE | `{ uint8 Index; uint8 Spare[3]; GW_RouteDef_t Route; }` | index 0..15 and route valid against current connections; INVALID MsgId removes route; success ROUTE_INF, invalid ROUTE_ERR; apply at once |
| 9 | SET_ENABLED_MASK | `{ uint8 Mask; uint8 Spare[3]; }` | for each used connection, bit i enables/disables slot i; unused bits ignored; unchanged states untouched; otherwise ordinary open/close and report rules; success MASK_INF, open failure MASK_ERR |

SET_ENABLED_MASK is applied in increasing index order. An open failure
leaves that slot disabled and continues the other slots, increments
CommandErrorCounter once (not CommandCounter), with MASK_ERR identifying
the failed-slot mask. This intentional partial outcome is the only
exception to C.4's rejected-command no-change rule for this command.

Append to GW_ConnStatus_t, in order:

```c
uint32 TxFrames; uint32 RxFrames; uint32 TxBytes; uint32 RxBytes;
uint32 LastKeepAliveSeconds; uint32 KeepAliveTimeouts;
```

TxFrames counts successful complete frame writes; TxBytes their frame
bytes excluding stream markers. RxFrames counts every complete frame
received, even invalid; RxBytes counts their bytes, excluding markers.
A truncated UDP datagram counts one RxFrame and its actual received byte
length (read buffers must detect oversize datagrams); incomplete stream
bytes count only when a complete frame is assembled. TxPackets/RxPackets
retain data-packet meanings and exclude keep-alives; errors include
keep-alive validation/I/O failures. KeepAliveTimeouts increments only on
UP -> DOWN by its 6 s timer. LastKeepAliveSeconds is 0 before any valid
keep-alive. Append to GW_Conn_Payload_t after Conns:
`uint8 RouteSelected[GW_MAX_ROUTES];` (unused/no UP = 0xFF).
Append HK `uint32 KeepAliveTimeouts; uint32 RouteSwitches;`.
RESET_COUNTERS zeroes all cumulative additions, retaining timers,
LastKeepAliveSeconds and selection. Append events 29 KEEPALIVE_ERR,
30 ROUTE_INF, 31 ROUTE_ERR, 32 MASK_INF, 33 MASK_ERR.

Command 10 SET_PREFERENCE payload is `{ uint8 Index; uint8
PreferBackup; uint16 Spare; }`. A used route and PreferBackup 0..1 are
required (ROUTE_ERR otherwise). Success ROUTE_INF. With 1, choose
Backup whenever UP, otherwise Primary when UP, otherwise 0xFF; the
ordinary automatic failback is held. With 0, resume S5.4 and start a new
10 s primary-stability interval if currently on Backup. RELOAD/table
activation resets preferences to 0. Append `uint8
RoutePreferBackup[GW_MAX_ROUTES];` after RouteSelected in GW_CONN_TLM.

### S5 Acceptance

Hidden coverage tests, linked against the `cmdval_lib` stubs, exercise
definition validation, the commands, subscription bookkeeping, the table
share and its retry, ingress validation for every reason, egress framing,
loop drops and stream deframing (marker hunting, split frames, an
out-of-range length). Live, acceptance runs its own headless checks
against your build, with every GW command sent as a CMDI frame, and with
every frame it sends to or checks from a GW connection built or verified
with the default table key. On cpu1: a UDP connection added by command
egresses a verification packet injected (as a CMDI frame) on a subscribed
reserved MID to a listening socket as a valid frame, and publishes an
inbound frame carrying a SAMPLE_APP NOOP (which executes); inbound
datagrams that are unframed, or framed with a bad CRC, a bad MAC, an
unlisted source or a timestamp outside the window, are each rejected,
counted under their reason in GW HK with `GW_FRAME_REJECT_ERR_EID`, and do
not execute; a table update of `CMDI.ValTable` changing the key is
followed by GW (old-key frames rejected, new-key frames accepted, egress
frames under the new key); a TCP connection reaches UP once a listener
opened by the acceptance environment appears and carries marker-framed
CMDI frames both ways; a TCP connection whose peer never listens makes
four attempts 5 s apart and enters DOWN with `GW_CONN_TIMEOUT_ERR_EID`
about 20 s after it was opened, then makes no attempt until DISABLE_CONN
and ENABLE_CONN begin a new cycle; a SERIAL connection on a PTY pair created by the
acceptance environment carries marker-framed CMDI frames both ways,
including a frame split across writes, garbage before a marker and a
frame with a bad MAC,
while GW keeps sending its heartbeat once per second (SFI reports no miss
for GW) and answers housekeeping requests with nothing pending on the PTY; GW is stopped with ES STOP_APP and started with ES
START_APP after a SERIAL connection was used, and a new SERIAL connection
on a fresh PTY pair then carries traffic both ways; DISABLE, ENABLE,
REMOVE, RELOAD, the error cases, the loop rule, HK and the status packet
behave as specified. Hidden tests cover every new route/table bound,
keep-alive timer edge, recovery and echo suppression. Live paired UDP
links check frame/byte counts in both directions, bad and absent
keep-alives, DOWN 6 s after last valid receipt (live [5,8] s from a
steady-link relay cut), SFI LINK reports, backup-only egress,
no duplicates, recovery and failback in 10.0-12.0 s. Both nodes enter
DEGRADED within 10 s of a primary cut while the backup carries health and mode.
The no-listener TCP test uses KeepAliveEnabled 0 and retains its 18-26 s
exhaustion bound; old endpoint tests need not implement keep-alives.

## S6. SFI system fault identifier

SFI watches application heartbeats and selected housekeeping counters and
reports faults and system-level metrics to the ground as events, HK and a
status packet.

### S6.1 Table

```c
#define SFI_MAX_APPS    16
#define SFI_MAX_WATCHES 16
#define SFI_HISTORY_LEN 16

typedef enum { SFI_Rule_UNUSED = 0, SFI_Rule_STALL = 1, SFI_Rule_RATE = 2,
               SFI_Rule_LIMIT = 3 } SFI_Rule_Enum_t;
typedef struct
{
    char AppName[CFE_MISSION_MAX_API_LEN]; /* empty = unused */
    uint8 Enabled; uint8 EventRateEnabled; uint8 EventLimit; uint8 EventWindowSec;
} SFI_AppDef_t;

typedef struct
{
    CFE_SB_MsgId_t MsgId;
    uint16         ByteOffset; /* from the first byte of the packet (primary header) */
    uint8          Width;      /* 1, 2 or 4 bytes, host byte order */
    uint8          Rule;       /* SFI_Rule_Enum_t */
    uint32         Limit;      /* STALL: packets without change; RATE: maximum increase per packet */
    uint32 Min; uint32 Max; /* LIMIT inclusive bounds */
    uint8 Persistence; uint8 AppIndex; uint8 Severity; uint8 Enabled;
} SFI_WatchDef_t;

typedef struct
{
    uint8  HbMissLimit;      /* N, 1..60 */
    uint8  Spare;
    uint16 ClusterWindowSec; /* W, 1..600 */
    uint16 HistorySec;       /* H, 1..3600 */
    uint16 Spare2;
    SFI_AppDef_t   Apps[SFI_MAX_APPS];
    SFI_WatchDef_t Watches[SFI_MAX_WATCHES];
    uint8 PeerEnabled; uint8 PeerTimeoutSec; uint16 Spare3;
} SFI_Table_t;
```

Validation rejects values outside these ranges, unterminated or duplicate
app names, and a used watch with width not 1, 2 or 4, or
`ByteOffset` + `Width` above 1024. Additionally reject Rule outside 0..3 or a used watch with invalid MsgId;
a used app's Enabled/EventRateEnabled outside 0..1, EventLimit outside
1..64 or EventWindowSec outside 1..60; a used watch's Enabled outside
0..1, Severity outside 1..2, AppIndex neither a used app index nor 0xFF;
LIMIT Min > Max, Max not representable in Width, or Persistence outside
1..255; PeerEnabled outside 0..1 or PeerTimeoutSec outside 3..60. LIMIT
uses Min/Max/Persistence and requires Limit 0; other used rules require
Limit nonzero and Min/Max/Persistence 0. UNUSED watches are ignored.
Default: N 3, W 5, H 60; apps `STOR`,
`CMDI`, `TLMO`, `GW` and the verification names `SFI_T1`, `SFI_T2`,
`SFI_T3`, `MODE` (indices 0..7 in that order); watches (MID, offset, width, rule, limit): `STOR_HK_TLM_MID`,
`CMDI_HK_TLM_MID`, `TLMO_HK_TLM_MID` and `GW_HK_TLM_MID` each 17, 1, RATE,
2; reserved MID 0xEC (`CFE_PLATFORM_CMD_TOPICID_TO_MIDV(0xEC)`, 0x18EC on
cpu1) 16, 4, STALL, 3; reserved MID 0xED (0x18ED on cpu1) 16, 2, RATE, 5.
Add watch index 6: reserved MID 0xEF (0x18EF on cpu1), offset 16, width
4, LIMIT, Limit 0, Min 10, Max 20, Persistence 3. For watches 0..3
AppIndex equals the matching app index 0..3; other watches use 0xFF.
Default Severity 1 except watch 6 Severity 2. All used watches Enabled 1.
All used apps Enabled 1, EventRateEnabled 1, EventLimit 5,
EventWindowSec 10. Unused fields/elements are zero. PeerEnabled 1,
PeerTimeoutSec 10. MODE is a real heartbeat source, the three SFI_T names
are armed only by injected heartbeats. Offset 16 of a verification packet
is its payload byte 8 (C.3).

### S6.2 Heartbeats

- SFI subscribes to `SFI_HEARTBEAT_MID` with a message limit of 32. A
  heartbeat naming a table app marks it heard (and armed, the first time),
  `TotalHeartbeats`++. Other names count in `UnknownHeartbeats` with
  `SFI_HB_UNKNOWN_DBG_EID` and are otherwise ignored.
- Every SFI WAKEUP (1 Hz), for each armed app: heard since the previous
  wakeup sets `MissedCycles` to 0, otherwise `MissedCycles`++ (saturating at 255); the heard
  flag is then cleared. An app never heard is UNARMED (0) and not evaluated.
- When `MissedCycles` reaches N the app becomes MISSING (2): its onset time
  is `CFE_TIME_GetTime()` at that wakeup, `TotalMisses`++ for the app,
  `HbMissEvents`++, the onset is appended to the miss history (a ring of
  `SFI_HISTORY_LEN`, oldest overwritten), and one `SFI_HB_MISS_ERR_EID`
  names the app and the onset seconds.
- A heartbeat from a MISSING app makes it OK (1) at once
  (`SFI_HB_RESTORED_INF_EID`).

### S6.3 Clustering

Every wakeup SFI evaluates the history entries no older than H seconds
("recent misses"): sorted by onset, a cluster is a maximal run in which
each consecutive pair is at most W seconds apart (full time, subseconds
included; equal times are 0 apart). `ClusterCount` is the number of
clusters, `LargestClusterSize` the size of the largest, `RecentMisses` the
number of recent misses, and `Correlation` is 0 NONE with no recent misses,
1 UNCORRELATED when the largest cluster has size 1, 2 CLUSTERED when 2 or
more. `SFI_CLUSTER_ERR_EID` (text: cluster size and W) is sent whenever
`Correlation` becomes CLUSTERED or `LargestClusterSize` grows while
CLUSTERED. The computation is a pure function in `fsw/src/sfi_cluster.c`,
declared in `fsw/src/sfi_cluster.h`:

```c
typedef struct
{
    uint16 ClusterCount; uint16 LargestClusterSize;
    uint8  Correlation;  /* 0 NONE, 1 UNCORRELATED, 2 CLUSTERED */ uint8 Spare[3];
} SFI_ClusterResult_t;

/* Onsets in any order, Count 0..SFI_HISTORY_LEN; the caller filters recency. Onsets is not modified. */
void SFI_ComputeClusters(const CFE_TIME_SysTime_t *Onsets, uint16 Count, uint16 WindowSec,
                         SFI_ClusterResult_t *Result);
```

`SFI_ComputeClusters` must not modify the caller's `Onsets` array: it is
`const`, and casting the qualifier away is not allowed. It sorts a local
copy of at most `SFI_HISTORY_LEN` entries, with no dynamic allocation.

### S6.4 Counter watches

SFI subscribes to each distinct watch MID with a message limit of 8. Each
received packet updates every watch on its MID whose `ByteOffset` + `Width`
fits in the packet. The first packet sets the baseline; each later one is
compared with the previous value:

- STALL: an unchanged value increments the watch's stall count, any change
  resets it to 0; reaching `Limit` puts the watch in FAULT: `StallFaults`++,
  `SFI_STALL_ERR_EID` naming MID and offset.
- RATE: delta = new - previous, or the new value itself if it is smaller
  than the previous (counter reset). Delta above `Limit` puts the watch in
  FAULT: `RateFaults`++, `SFI_RATE_ERR_EID` naming MID, offset and delta.
- LIMIT: every packet, including the first, compares the value with
  inclusive Min..Max. Outside increments a consecutive count (saturates
  at Persistence); inside resets it. Reaching Persistence enters FAULT,
  LimitFaults++ and LIMIT_ERR. Clear on the first in-range value. A first
  LIMIT packet also sets its baseline and LastValue.
- A watch in FAULT returns to OK on the first packet that does not trigger
  its rule (a changed value; a delta at most `Limit`; an in-range LIMIT value), with
  `SFI_WATCH_CLEAR_INF_EID`. No repeated events while in FAULT.

### S6.5 Commands and telemetry

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 0 | NOOP | none | |
| 1 | RESET_COUNTERS | none | command counters, `TotalHeartbeats`, `UnknownHeartbeats`, `HbMissEvents`, `StallFaults`, `RateFaults`; not states or history |
| 2 | SET_MISS_LIMIT | `{ uint8 Limit; uint8 Spare[3]; }` | 1..60 |
| 3 | SET_CLUSTER_WINDOW | `{ uint16 Seconds; uint16 Spare; }` | 1..600 |
| 4 | SEND_STATUS | none | one `SFI_StatusTlm_t`, `SFI_STATUS_INF_EID` |
| 5 | CLEAR_HISTORY | none | empties the history and cluster metrics, `SFI_HISTORY_CLEAR_INF_EID` |

SET_* success `SFI_SET_PARAM_INF_EID`, error `SFI_SET_PARAM_ERR_EID`;
values set by command are overridden by the next table load. A table load
returns all apps to UNARMED and all watches to baseline-pending and keeps
the history.

```c
/* SFI_HkTlm_Payload_t */
uint8  CommandCounter; uint8 CommandErrorCounter; uint8 AppsMonitored; uint8 AppsMissing;
uint8  WatchesInFault; uint8 Correlation; uint8 HbMissLimit; uint8 Spare;
uint16 RecentMisses; uint16 ClusterCount; uint16 LargestClusterSize; uint16 ClusterWindowSec;
uint32 TotalHeartbeats; uint32 UnknownHeartbeats; uint32 HbMissEvents; uint32 StallFaults; uint32 RateFaults;

typedef struct
{
    char   AppName[CFE_MISSION_MAX_API_LEN]; /* empty for unused slots */
    uint8  State;                            /* 0 UNARMED, 1 OK, 2 MISSING */
    uint8  MissedCycles; uint16 TotalMisses;
    uint32 LastMissSeconds; uint32 LastMissSubsecs; /* onset of the latest miss, 0 if none */
} SFI_AppStatus_t;

typedef struct { uint8 AppIndex; uint8 Spare[3]; uint32 Seconds; uint32 Subsecs; } SFI_MissRecord_t;

typedef struct
{
    uint8  State; /* 0 UNUSED, 1 BASELINE_PENDING, 2 OK, 3 FAULT */ uint8 Spare[3];
    uint32 LastValue;
} SFI_WatchStatus_t;

typedef struct
{
    uint8  AppsMissing; uint8 Correlation; uint16 HistoryCount; /* entries below, oldest first */
    uint16 ClusterCount; uint16 LargestClusterSize;
    SFI_AppStatus_t   Apps[SFI_MAX_APPS];
    SFI_MissRecord_t  History[SFI_HISTORY_LEN];
    SFI_WatchStatus_t Watches[SFI_MAX_WATCHES];
} SFI_Status_Payload_t;
```

Event IDs: 1 to 10 as in C.4,
11 SUB_ERR, 12 HB_MISS_ERR, 13 HB_RESTORED_INF, 14 HB_UNKNOWN_DBG,
15 STALL_ERR, 16 RATE_ERR, 17 WATCH_CLEAR_INF, 18 CLUSTER_ERR,
19 SET_PARAM_INF, 20 SET_PARAM_ERR, 21 STATUS_INF, 22 HISTORY_CLEAR_INF.

### S6.6 Fault reports, log and latching

Enumerations in default_sfi_msgdefs.h: SFI_FaultType_NONE 0, _HEARTBEAT
1, _STALL 2, _RATE 3, _LIMIT 4, _EVENT_RATE 5, _LINK 6, _NODE_LOSS 7;
SFI_Severity_WARNING 1 and _CRITICAL 2. Each source has independent
physical Active and retained Latched flags. An inactive -> active edge
sets Latched 1, stores onset time, logs and publishes one report. An
active -> inactive edge logs and publishes one clear with the original
onset and current change time, retaining Latched. Repeated triggering
samples do not report again. ACK_FAULT clears Latched only if inactive;
active ACK is rejected and has no effect. A later new onset re-latches.
Existing app/watch State fields describe physical state, not latching.

```c
#define SFI_FAULT_LOG_LEN 64
typedef struct
{
    uint32 ReportSequence; /* 1 at app start, +1 per report, modulo 2^32 */
    uint8 FaultType; uint8 SourceIndex; uint8 Severity; uint8 Active;
    uint8 Latched; uint8 NodeId; uint8 Spare[2];
    char AppName[CFE_MISSION_MAX_API_LEN];
    uint32 OnsetSeconds; uint32 OnsetSubsecs;
    uint32 ChangeSeconds; uint32 ChangeSubsecs;
} SFI_Fault_Payload_t;
```

Publish SFI_FaultCmd_t, FC 0, on local SFI_FAULT_MID, then
SFI_FaultTlm_t on SFI_FAULT_TLM_MID with the identical payload. MODE
subscribes to the former; the latter is ground telemetry. SourceIndex is
the table app slot for HEARTBEAT/EVENT_RATE, watch slot for STALL/RATE/
LIMIT, connection index for LINK, 0 for NODE_LOSS. AppName is the source
app, the watch's AppIndex name if associated, `GW` for LINK, or empty
for an unassociated watch/NODE_LOSS. HEARTBEAT, EVENT_RATE, LINK and
NODE_LOSS are WARNING; watches use their table Severity. NodeId is the
reporting node, including NODE_LOSS (its source is the peer).

Fault slots 0..56 are fixed: heartbeat = AppIndex; watch = 16 + watch
index; event rate = 32 + AppIndex; link = 48 + connection index; peer =
56. Rule type changes on table activation retain this slot mapping.
Arrays indexed by these slots describe every current fault, including
inactive latched faults. Log is a ring of the last 64 reports (onsets and
clears, oldest overwritten); ACKs are not reports. All source metadata
is recorded when an edge occurs. CLEAR_HISTORY clears clustering only;
RESET_COUNTERS clears cumulative counters only; neither clears fault
state, log, report serial or latches.

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 6 | ACK_FAULT | `{ uint8 Slot; uint8 Spare[3]; }` | 0..56, inactive and Latched 1, else CONTROL_ERR; success clears latch, ACK_INF |
| 7 | SET_APP_ENABLE | `{ uint8 Index; uint8 Enabled; uint16 Spare; }` | used app index, Enabled 0..1, else CONTROL_ERR; success CONTROL_INF |
| 8 | SET_WATCH_ENABLE | same fields as SET_APP_ENABLE | used watch index, Enabled 0..1, else CONTROL_ERR; success CONTROL_INF |
| 9 | DUMP_FAULT_LOG | none | write /cf/sfi_faults.bin as below, DUMP_INF or DUMP_ERR |
| 10 | SET_PEER_ENABLE | `{ uint8 Enabled; uint8 Spare[3]; }` | 0..1, else CONTROL_ERR; success CONTROL_INF |
| 11 | reserved | none | rejected with CC_ERR |
| 12 | REPORT_LINK | `{ uint8 Index; uint8 Down; uint16 Spare; }` | index 0..7, Down 0..1, else CONTROL_ERR; success updates LINK Active with edge rules, no success event |

Disabling an app clears its active heartbeat and event-rate faults with
reports, retains latches, sets State UNARMED, clears heard/miss flags and
event timestamps, and ignores subsequent heartbeats/events for it (not
UnknownHeartbeats). Re-enabling starts unarmed with empty windows.
Disabling a watch clears its physical fault with a report, retains its
latch, and starts baseline-pending when re-enabled; it ignores samples
while disabled. Table activation applies these same clear/reset rules
for all configured apps/watches, retains latches, log and history, then
uses the new defaults; old-active clears precede new configuration.
Table activation also disarms health monitoring, clears NODE_LOSS with
a report if active, resets PeerLastSeconds to 0, and applies new
PeerEnabled/PeerTimeoutSec. LINK states are independent of SfiTable and
are retained, including latches. No ACK is implied by table activation.
A no-change enable command succeeds without resetting an enabled source.
Peer disable clears NODE_LOSS and disarms. While PeerEnabled is 0,
valid health messages neither arm nor refresh monitoring; publication
continues. Re-enable starts unarmed and requires a subsequently received
valid health message. Runtime enables are overridden by table activation.

Dump format is little-endian: 16-byte header `uint32 Magic` 0x5346494C,
`uint16 Version` 1, `uint16 RecordSize` sizeof(SFI_Fault_Payload_t),
`uint32 Count` 0..64, `uint32 LastSequence`; then Count report payloads
in oldest-first order. LastSequence is 0 before the first report. No cFE
file header, trailer or unused records. Snapshot at command receipt;
OSAL create/truncate/write/close must finish before DUMP_INF, which names
path, count and bytes. Any failure deletes a partial output and emits
DUMP_ERR, command error once. Dumping again replaces that one file. The
file is outside STOR's managed roots; STOR ADD_FILE can copy it into the
file area and TRANSFER can send it to ground. No direct ground file read
is needed to verify the dump.

### S6.7 Event-rate detection and combined cluster attribution

Subscribe to CFE_EVS_LONG_EVENT_MSG_MID (limit 64, pipe depth at least
256). Match the long event payload AppName and EventType ==
CFE_EVS_EventType_ERROR to enabled table apps with EventRateEnabled 1;
ignore all other types/apps, including SFI's own reports. Timestamp on
receipt using cFE time, not the event's embedded timestamp. For each app
retain the most recent EventLimit+1 timestamps within the half-open
window (now - EventWindowSec, now]; older/equal lower-bound timestamps
expire. The observed count is capped at EventLimit+1. Above EventLimit
enters EVENT_RATE fault on that received event; at most EventLimit clears
it on a later receipt or wakeup. This remains exact for the threshold
predicate even during a larger burst. Count includes unarmed but enabled apps; heartbeat arming is independent.
EventRateFaults counts onsets;
EVENT_RATE_ERR names app, capped count and window. SFI-generated ERRORs
are never eligible even if a table names SFI.

The history consumed by S6.3 now contains onsets of HEARTBEAT and watch
faults together (STALL/RATE/LIMIT); clears, event-rate, LINK and NODE_LOSS
do not enter it. Each new onset appends one entry to the same 16-entry
ring, even if that source has had an earlier onset. RecentMisses is the
number of these recent combined entries, despite its name. Preserve
SFI_ComputeClusters exactly, including const input and result layout.
For attribution, sort a local copy of records by full onset, then by
insertion order for ties, and apply that function's same gap rule.
Largest cluster ties choose earliest onset then earliest insertion.
Membership includes every source appearing in that chosen cluster;
repeat onsets count toward size but set a membership bit only once.
LargestAppsMask includes heartbeat app indices and watch AppIndex when
not 0xFF; LargestWatchesMask includes watch indices. Both are 0 with no
recent records. CLEAR_HISTORY empties records and both masks without
changing fault Active/Latched state. CLUSTER_ERR keeps its S6.3 trigger.

Extend SFI_MissRecord_t by appending `uint8 FaultType; uint8 SourceIndex;
uint16 Spare2;`. For a heartbeat AppIndex is its index; a watch uses its
associated AppIndex or 0xFF. Seconds/Subsecs remain the onset. Existing
fields and SFI_ComputeClusters signature remain unchanged. The history
includes faults even when a prior occurrence remains latched. Records retain
their onset-time slot indices across table activation; history and masks
are not remapped if app/watch definitions move. CLEAR_HISTORY starts a
new history for the new slot assignment.

### S6.8 Cross-node health

Each SFI wakeup sends SFI_HealthCmd_t, code 0, on global SFI_HEALTH_MID:

```c
typedef struct
{
    uint8 SrcNode; uint8 DstNode; uint8 ActiveFaults; uint8 LatchedFaults;
    uint32 Sequence; uint32 Seconds;
} SFI_Health_Payload_t;
```

DstNode is 3 - own node ID. Sequence starts at 1 and increases modulo
2^32 each publication; Seconds is current onboard seconds. Subscribe to
the same global MID; own SrcNode is ignored. Require correct packet size,
FC 0, SrcNode equal the other node and DstNode equal own node, otherwise
CONTROL_ERR and no refresh. With PeerEnabled 1, a first valid peer
packet arms monitoring; while disabled, valid packets neither arm nor
refresh it. Re-enable requires a subsequently received valid packet;
no NODE_LOSS is raised before first contact, so a standalone node with
no GW connection is supported. No application-level sequence freshness
check is applied (CMDI replay or GW frame validation is the ingress
boundary); the sequence is diagnostic. With PeerEnabled 1, losing valid
health for PeerTimeoutSec (default 10) onboard seconds enters NODE_LOSS
on the first SFI wakeup at/after that deadline; first valid packet clears
it. GW routes health on both directions as S5.4. LatchedFaults counts
all slots with Latched 1, ActiveFaults all slots with Active 1.

Every SFI 1 Hz wakeup, after queued input processing, evaluates heartbeat
misses, expires event windows, evaluates peer health loss, computes
clusters, then publishes health. Within each phase process indices
ascending; heartbeat reports precede event-rate reports, then NODE_LOSS.
Watch faults are evaluated on packet receipt. MODE's 1 Hz wakeup first
evaluates peer-mode expiry and cap exhaustion, then mode/profile changes,
then response attempts/restart continuation, then peer publication and
heartbeat. An in-progress restart gets its continuation before new
restart-bearing response attempts, so freeing it is visible that wakeup.

### S6.9 Expanded telemetry and events

Append HK, in order:

```c
uint32 LimitFaults; uint32 EventRateFaults; uint32 LinkFaults; uint32 NodeLossFaults;
uint32 FaultReports; uint32 FaultLogDumps;
uint16 LargestAppsMask; uint16 LargestWatchesMask;
uint8 ActiveFaults; uint8 LatchedFaults; uint8 PeerArmed; uint8 PeerLost;
uint32 PeerLastSeconds; uint32 HealthSequence;
```

Append SFI_AppStatus_t `uint8 Enabled; uint8 EventCount; uint16 Spare2;`;
append SFI_WatchStatus_t `uint8 Enabled; uint8 PersistenceCount; uint16
Spare2;`. A disabled watch keeps State BASELINE_PENDING (unused remains
UNUSED). Append to SFI_Status_Payload_t, after Watches:

```c
uint16 LargestAppsMask; uint16 LargestWatchesMask;
uint8 Active[57]; uint8 Latched[57]; uint16 Spare;
SFI_Fault_Payload_t Faults[57];
```

AppsMonitored counts used enabled app slots, AppsMissing counts enabled
MISSING apps; WatchesInFault counts enabled physically faulted watches. Watch State
FAULT clears exactly as S6.4; PersistenceCount is the current consecutive
out-of-range LIMIT count, 0 for all other rules. EventCount is the
current capped event-window count, 0 when event monitoring is disabled.
FaultReports increments per report (including clears); FaultLogDumps per
successful dump. LinkFaults/NodeLossFaults count onsets. HealthSequence
is the latest sent sequence, 0 before the first publication; PeerLastSeconds
is the onboard receipt time of the last valid health packet received
while PeerEnabled 1, not the peer's Seconds field; it does not refresh
while disabled.
Faults[slot] is its latest edge report, all zero before any edge; ACK
updates only Latched[], not that historical report. This status exceeds
1024 bytes: SEND_STATUS sends one full cFE packet and TLMO_MAX_PACKET is
raised to 8192 (GW_MAX_PACKET and frame limit remain 1024). No truncation
or alternate packet layouts. All cumulative additions reset with
RESET_COUNTERS, not PeerLastSeconds/HealthSequence or state.
Append events 23 LIMIT_ERR, 24 EVENT_RATE_ERR, 25 ACK_INF, 26 CONTROL_INF,
27 CONTROL_ERR, 28 DUMP_INF, 29 DUMP_ERR, 30 NODE_LOSS_ERR,
31 NODE_RESTORED_INF, 32 LINK_ERR, 33 LINK_CLEAR_INF. Link/node onset
and clear events accompany their reports; REPORT_LINK counts accepted
commands under C.4, even if it reports unchanged state.

### S6 Acceptance

A hidden unit test calls `SFI_ComputeClusters` on chosen onset sets (empty,
single, equal times, gaps of exactly W and just above W, unsorted input);
hidden coverage tests exercise arming, the miss and restore transitions,
the STALL and RATE rules including counter reset, and the commands. Live,
acceptance runs its own headless checks against your build, with every
command sent as a CMDI frame. MODE SET_ENABLE 0 is issued first for isolated legacy SFI checks.
On cpu1: the five real heartbeat apps are OK after
startup with no misses; stopping GW with the cFE ES STOP_APP command gives
one miss event naming GW within N + 2 seconds, `AppsMissing` 1 and an
onset time in the status packet, and starting it again with ES START_APP
(`/cf/gw.so`, entry `GW_AppMain`, priority 70, stack 131072) restores it;
heartbeats injected as CMDI frames for `SFI_T1`, `SFI_T2` and `SFI_T3`,
then stopped for two of them at once, give a cluster of size 2 and
CLUSTERED, and after CLEAR_HISTORY two misses more than W apart give
UNCORRELATED; verification packets injected as CMDI frames on reserved
MIDs 0xEC and 0xED raise and clear the STALL and RATE faults. Hidden
and live checks cover LIMIT persistence/bounds, each onset/clear report,
ACK active rejection and retained latches, enable controls, mixed
heartbeat/watch clusters and tie attribution, ERROR-only sliding windows
and boundary expiry, NODE_LOSS arming/deadline/clear, link reports and
binary log chronology/wrap. A dumped log is copied by STOR and reassembled
from ground transfer telemetry; payloads and CRC match observed reports.
No live check depends on access to the dump on the node filesystem.

## S7. MODE system mode manager and fault response

MODE is an onboard SB command producer, not a network ingest path. Its
commands to ES, TLMO, STOR and GW use the target app's normal command MID,
function code, payload and CFE_SB_TransmitMsg(..., false). They do not
need CMDI frames. Ground operations, including ground commands to MODE,
still enter only through CMDI frames; GW link acceptance probes retain
S5's authenticated link ingress. MODE never opens a command socket.

### S7.1 Table and profiles

```c
#define MODE_NUM_MODES 3
#define MODE_NUM_FAULT_CLASSES 7
#define MODE_NUM_FAULT_SLOTS 57
#define MODE_MAX_RESTART_APPS 4
typedef enum { MODE_NOMINAL = 0, MODE_DEGRADED = 1, MODE_SAFE = 2 } MODE_Mode_t;
typedef struct
{
    uint8 TlmProfile; uint8 PurgeThreshold; uint8 GwEnabledMask; uint8 Spare;
} MODE_ProfileDef_t;
typedef struct
{
    char AppName[CFE_MISSION_MAX_API_LEN]; /* empty = unused */
    char EntryPoint[CFE_MISSION_MAX_API_LEN];
    char FileName[CFE_MISSION_MAX_PATH_LEN];
    uint32 StackSize; uint16 Priority; uint16 Spare;
} MODE_RestartDef_t;
typedef struct
{
    uint8 TargetMode; uint8 Restart; uint8 SetPriority; uint8 Transfer;
    uint8 SwitchRoute; uint8 RouteIndex; uint8 PreferBackup; uint8 Priority;
    uint16 CooldownSec; uint8 RetryCap; uint8 Spare;
    CFE_SB_MsgId_t RecordMid;
    STOR_Transfer_Payload_t Dump;
} MODE_ResponseDef_t;
typedef struct
{
    uint8 Enabled; uint8 PeerEnabled; uint8 PeerTimeoutSec; uint8 RecoverySec;
    MODE_ProfileDef_t Profiles[MODE_NUM_MODES];
    MODE_RestartDef_t RestartApps[MODE_MAX_RESTART_APPS];
    MODE_ResponseDef_t Responses[MODE_NUM_FAULT_CLASSES]; /* fault type - 1 */
} MODE_ModeTable_t;
```

Validation rejects Enabled/PeerEnabled outside 0..1, PeerTimeoutSec
outside 3..60, RecoverySec outside 1..120; a profile TlmProfile above 2
or PurgeThreshold outside 1..100; used restart entries with empty or
unterminated strings, FileName not absolute, Priority outside 1..255,
StackSize outside 16384..262144, duplicate names, or names other than
STOR/CMDI/TLMO/GW. Each used name requires exactly its listed entry point
and /cf/<app>.so path below; other executable or entry-point strings are
rejected. Every response requires TargetMode 1..2, boolean
Restart/SetPriority/Transfer/SwitchRoute/PreferBackup, CooldownSec
10..300, RetryCap 1..5; with SetPriority, RecordMid valid and Priority
1..255; with SwitchRoute, RouteIndex 0..15; with Transfer, Dump must
have Area FILE, Destination 0, SuccessAction KEEP and a terminated,
nonempty relative Path satisfying S1.3, with DestPath empty. Disabled
action fields need not validate except the booleans; all default spares
are zero. There is no table-configured arbitrary ES executable target.

Defaults: Enabled 1, PeerEnabled 1, PeerTimeoutSec 10, RecoverySec 20.
Profiles indexed by mode:

| Mode | TlmProfile | PurgeThreshold | GwEnabledMask |
|------|------------|----------------|---------------|
| NOMINAL | 0 | 80 | 0xFF |
| DEGRADED | 1 | 65 | 0xFF |
| SAFE | 2 | 50 | 0x03 |

Connections 0 and 1 are reserved by mission configuration for the
primary/backup health path when peer links are configured; SAFE retains
both. The default GW table is empty; profile masks ignore unused slots.
MODE does not create connections. In two-node tests the pack configures
them through CMDI and verifies that its routes carry health and mode.

RestartApps, in order: STOR, CMDI, TLMO, GW; EntryPoint APP_AppMain,
FileName /cf/app.so (lowercase library base name), StackSize
131072/32768/32768/131072, Priority 70/40/45/70. Pin the concrete strings
STOR_AppMain, CMDI_AppMain, TLMO_AppMain, GW_AppMain and /cf/stor.so,
/cf/cmdi.so, /cf/tlmo.so, /cf/gw.so. Restart ExceptionAction is always 0.

Every response has the common defaults below; TargetMode is specified
separately by the per-fault table. CooldownSec 30, RetryCap 3,
RecordMid CFE_EVS_HK_TLM_MID, Priority 200, RouteIndex 0, PreferBackup 1,
Dump = {FILE, ground, KEEP, 0, "mode/response.bin", ""}. Enabled actions:

| Fault type | Restart | SetPriority | Transfer | SwitchRoute | TargetMode |
|------------|---------|-------------|----------|-------------|------------|
| HEARTBEAT | 1 | 1 | 0 | 0 | DEGRADED |
| STALL | 0 | 1 | 0 | 0 | DEGRADED |
| RATE | 0 | 1 | 0 | 0 | DEGRADED |
| LIMIT | 0 | 1 | 0 | 0 | SAFE |
| EVENT_RATE | 0 | 1 | 1 | 0 | DEGRADED |
| LINK | 0 | 0 | 0 | 1 | DEGRADED |
| NODE_LOSS | 0 | 0 | 0 | 1 | DEGRADED |

Transfer sends the configured existing file to ground. MODE does not
create that file or replace a missing file: STOR rejects a
missing source with XFER_ERR. Tests of that action first ADD_FILE a
fixture at mode/response.bin. The independent fault-log workflow uses
SFI DUMP, STOR ADD_FILE and TRANSFER (S8.6).

### S7.2 State transitions and precedence

Subscribe to SFI_FAULT_MID and MODE_PEER_MID at initialization (limits
64 and 8; main pipe depth at least 128), plus command/HK/wakeup. A fault
report is valid only with exact length, FC 0, own NodeId, FaultType 1..7,
SourceIndex valid for that type, Severity 1..2, Active/Latched 0..1 and
terminated AppName. Invalid reports emit FAULT_ERR and count one command
error; no state changes. Reports from the other node on this local MID
are ignored. The latest accepted report serial is tracked per slot:
accept its first report or a modulo-2^32 newer serial with distance in
1..2^31-1, otherwise ignore as a duplicate/stale report. Onset and clear
from a given slot retain S6's original onset identity.

On initialization current mode is NOMINAL; apply that profile once.
On every mode entry, in order, send TLMO SELECT_PROFILE, STOR
SET_PURGE_THRESHOLD, then GW SET_ENABLED_MASK with that mode's values;
emit TRANSITION_INF with old/new mode and cause. A same-mode request
succeeds without profile reapplication, transition count or resetting an
already-running healthy interval. SET_MODE DEGRADED starts an interval
only when entering DEGRADED from another mode while healthy. APPLY_PROFILE
explicitly reapplies it. Table activation replaces configuration and
reapplies current profile on the next wakeup; it does not clear latches,
report serials, response attempts or cooldowns. On the initial profile
application wait until TLMO, STOR and GW are registered with ES, then
apply on the next wakeup, never later than 2 MODE wakeups after that.
Failure to transmit any profile command increments ActionErrors and
emits ACTION_ERR naming MID/FC; continue the remaining commands.

Input decisions occur on the next MODE wakeup after input receipt;
process commands/reports to completion before evaluating that wakeup.
While enabled, every accepted onset also records a one-wakeup minimum
mode demand (the maximum of its severity and response target). A clear
before the next wakeup cancels its actions but does not erase that mode
demand; a transient CRITICAL onset therefore still enters SAFE. Consume
and clear this demand after that decision. Disabled monitoring does not
accumulate stale demands; re-enable evaluates the currently active set.
If multiple triggers are pending, use this precedence, highest first:

| Trigger/guard | Result |
|---------------|--------|
| Current SAFE with no accepted exit command | remain SAFE, regardless of clear or peer recovery |
| SET_MODE SAFE, any active CRITICAL report, an active fault targeting SAFE, a pending SAFE demand, or any exhausted response slot | SAFE |
| Any active WARNING report, pending DEGRADED demand, or armed peer mode/health loss | at least DEGRADED |
| SET_MODE NOMINAL with no active faults and no peer loss | NOMINAL immediately |
| SET_MODE DEGRADED with no SAFE trigger | DEGRADED; start a healthy timer only on entry, preserve one already running |
| DEGRADED, no active faults/peer loss continuously for RecoverySec | NOMINAL on first wakeup at/after deadline |
| Otherwise | retain current mode |

Severity CRITICAL always requires SAFE, even if its response table row
targets DEGRADED. Latched but inactive SFI faults do not block automatic
recovery. A clear starts the healthy interval on the first MODE wakeup
observing all guards clear; any new active report/loss resets it. A
commanded exit from SAFE is allowed only to NOMINAL when all guards are
clear, or to DEGRADED when no CRITICAL/SAFE-target fault or cap exhaustion
remains; otherwise SET_MODE is rejected with MODE_ERR. Thus SAFE never
automatically unlatches. SET_MODE received during an active fault cannot
override its required minimum mode. On conflicting SET_MODE commands
before a wakeup the last accepted command is the requested mode; fault
and SAFE precedence still applies. Manual NOMINAL ends a healthy
DEGRADED interval at once. Disabled automatic response (SET_ENABLE 0)
retains monitoring and fault state but suspends automatic transitions,
responses and recovery; explicit SET_MODE/APPLY_PROFILE still work.
Re-enable evaluates existing state on the next wakeup. Route-preference
release is cleanup and remains enabled during SET_ENABLE 0 (S7.3).

### S7.3 Responses, cooldowns and restart protocol

For each slot, maintain Active, Attempts, Exhausted and NextEligibleSeconds.
An onset makes its response pending; a clear cancels pending retries but
retains Attempts and cooldown. At the first eligible MODE wakeup while
Active and enabled, dispatch one response attempt, then set
NextEligibleSeconds = now + CooldownSec and increment Attempts. A
recurrence in the same slot shares that cooldown and cap across clears,
ACKs and RESET_COUNTERS. No action before eligibility; one attempt per
slot per wakeup. Slots are processed ascending. At most one ES restart
sequence is in progress; another restart-bearing response waits without
consuming an attempt until that sequence finishes. Non-restart responses
may run meanwhile. Missing RestartApps match skips only Restart, emits
ACTION_ERR, and still consumes an attempt; it never restarts MODE itself.

Each attempt sends enabled actions in this order: restart protocol,
STOR SET_PRIORITY (RecordMid/Priority), STOR TRANSFER (Dump), GW
SET_PREFERENCE (RouteIndex/PreferBackup). Mode entry/profile commands
precede these actions. Non-restart actions follow the initial restart
step in that same wakeup; they do not wait for STOP/START completion.
A failure of one transmission never skips later non-restart actions. ResponsesSent counts attempts (RESPONSE_INF once each), ActionsSent counts
successful SB command transmissions (including profiles), ActionErrors
counts transmit errors or restart-sequence timeouts. Target app command
acceptance/rejection remains visible in that app's events/HK and is not
claimed as a successful action result. No response requires CMDI frames.
A clear for LINK/NODE_LOSS with SwitchRoute sends SET_PREFERENCE 0 once
to release the override, even when no attempt is due; ordinary 10 s
failback then applies. If multiple active slots prefer the same route,
release only when the last such active requesting slot clears. This
release is cleanup, remains enabled during SET_ENABLE 0, and is sent
once at that last clear, without deferral until re-enable. Count its
transmission normally in ActionsSent or ActionErrors.

Snapshot the restart target and parameters when an attempt is
dispatched. An in-progress restart sequence uses that snapshot through
completion, including START after WAIT_STOP. Table changes affect
subsequent attempts; existing cooldown deadlines remain unchanged.

For Restart, query CFE_ES_GetAppIDByName. If present, send ES STOP_APP
FC 5 with CFE_ES_AppNameCmd_Payload_t.Application = target; poll presence
at each MODE wakeup. If still present after 5 onboard seconds, terminate
this sequence with ACTION_ERR. If absent initially or after STOP, send
ES START_APP FC 4 on the next MODE wakeup with
CFE_ES_StartAppCmd_Payload_t fields Application, AppEntryPoint,
AppFileName, StackSize, ExceptionAction 0, Priority from RestartApps.
Reserved fields zero; these are the bundle's native payload types, not
private replacements. Finish the sequence when the app is registered,
or 5 onboard seconds after START if it never appears (ACTION_ERR).
STOP transmission failure ends the restart sequence at once with
ACTION_ERR, no presence wait and no START. START transmission failure
also ends the sequence at once with ACTION_ERR, no registration wait.
Either failure consumes the response attempt and obeys normal cooldown;
no intra-attempt retransmission. WAIT_STOP begins after a successful
STOP transmission or for the initial-absence delay below; WAIT_START
begins only after successful START.
For an initially absent target, queue START for the following wakeup
(RestartPhase WAIT_STOP until then, with no STOP-timeout timer), rather
than sending it in the onset wakeup. For a successfully transmitted STOP, absence detected on a
later wakeup sends START on the next wakeup. A timeout deadline is tested
after checking app presence, so disappearance/registration at the exact
deadline succeeds. ActionErrors counts each transmit failure or timeout
once. A clear cancels pending future retries, not a restart sequence
already in progress; disabling MODE cancels that sequence.
RestartsSent counts successfully transmitted START commands, not STOP
or failed transmissions. Recovery of the physical
fault is confirmed only by SFI's clear report, not ES registration.
Stopping an already absent app sends no STOP. SET_ENABLE 0 cancels a
pending restart sequence and sends no further START; re-enable may retry
at eligibility subject to its consumed attempt.

After RetryCap attempts, do not dispatch another. If Active is still 1
at its full-precision next-eligible deadline, mark Exhausted 1, emit RETRY_ERR once and enter
SAFE. Thus a successful restart clearing its fault does not exhaust a
cap merely by being its third attempt. A later recurrence after the cap
marks Exhausted on its first eligible wakeup without sending a command.
Exhausted persists until RESET_RESPONSE on an inactive slot; clearing
SFI latches or counters never resets it. Cooldown deadlines retain full cFE time internally; NextEligibleSeconds
exports the Seconds field of that deadline. HealthySinceSeconds and
ModeSinceSeconds similarly export the Seconds part of their full-time
instants. No fractional interval is rounded down to shorten a cooldown
or recovery. All elapsed comparisons use those full-time values. Attempt errors do not bypass cooldown/cap.

### S7.4 Peer mode synchronisation

Every MODE wakeup sends MODE_PeerCmd_t, FC 0, on global MODE_PEER_MID:

```c
typedef struct
{
    uint8 SrcNode; uint8 DstNode; uint8 Mode; uint8 ActiveFaults;
    uint32 Sequence; uint32 Seconds;
} MODE_Peer_Payload_t;
```

SrcNode own, DstNode 3 - own, Mode current; Sequence starts 1 at app
start, +1 per publication modulo 2^32; Seconds current onboard seconds.
A correctly sized FC 0 message with peer SrcNode, own DstNode and Mode
0..2 updates diagnostic PeerMode and PeerSequence even while PeerEnabled
is 0. With PeerEnabled 1 it also arms/refreshes the peer-mode timer and
updates PeerLastSeconds; while disabled it neither arms nor refreshes
monitoring. Ignore own SrcNode, reject all other malformed messages with PEER_ERR.
A receive from the peer is not retransmitted (S5.4). No separate mode
freshness check is applied. PeerMode reports the peer state but does not
force matching local mode; faults are evaluated locally to avoid a
self-sustaining DEGRADED feedback loop.

PeerEnabled 1 uses two independent armed timers: MODE peer message and
SFI health NODE_LOSS reports. No peer-mode loss before first valid mode
message; after arming, PeerTimeoutSec (default 10) without one sets
PeerLost 1 and PEER_LOSS_ERR once, requiring DEGRADED. First valid packet
clears PeerLost with PEER_RESTORED_INF. SFI NODE_LOSS independently blocks
recovery while active. Disabling peer monitoring disarms the mode timer
and clears PeerLost, but does not disable SFI health monitoring; use
SFI SET_PEER_ENABLE too for an isolated-node test. Re-enable starts
unarmed and requires a subsequently received valid message. PeerMode
and PeerSequence diagnostics keep updating while disabled. Mode messages
are published even when response/peer monitoring is disabled.

### S7.5 Commands, telemetry and events

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 0 | NOOP | none | C.4 |
| 1 | RESET_COUNTERS | none | command counters, Transitions, ResponsesSent, ActionsSent, ActionErrors, RestartsSent, PeerLosses; not protocol or response state |
| 2 | SET_ENABLE | `{ uint8 Enabled; uint8 Spare[3]; }` | 0..1, else MODE_ERR; success CONTROL_INF |
| 3 | SET_MODE | `{ uint8 Mode; uint8 Spare[3]; }` | 0..2 and S7.2 guards; success CONTROL_INF, error MODE_ERR |
| 4 | SEND_STATUS | none | one MODE_StatusTlm_t, STATUS_INF |
| 5 | SET_PEER_ENABLE | `{ uint8 Enabled; uint8 Spare[3]; }` | 0..1; success CONTROL_INF, error MODE_ERR |
| 6 | RESET_RESPONSE | `{ uint8 Slot; uint8 Spare[3]; }` | 0..56 and inactive, else MODE_ERR; clears Attempts, Exhausted and NextEligibleSeconds for that slot, CONTROL_INF |
| 7 | APPLY_PROFILE | none | apply current profile at next wakeup, CONTROL_INF |

SET_MODE guards are evaluated at receipt and again on the decision
wakeup; if a newly received fault supersedes a previously accepted
request, the higher-priority transition wins without another command
counter change. Applies to both command sources, ground and onboard.

```c
/* MODE_HkTlm_Payload_t */
uint8 CommandCounter; uint8 CommandErrorCounter; uint8 Mode; uint8 Enabled;
uint8 ActiveFaults; uint8 ExhaustedFaults; uint8 PeerMode; uint8 PeerLost;
uint32 Transitions; uint32 ResponsesSent; uint32 ActionsSent; uint32 ActionErrors;
uint32 RestartsSent; uint32 PeerLosses; uint32 ModeSinceSeconds;
uint32 HealthySinceSeconds; uint32 PeerLastSeconds; uint32 PeerSequence;

typedef struct
{
    uint8 Active; uint8 Attempts; uint8 Exhausted; uint8 Pending;
    uint32 NextEligibleSeconds; uint32 LastReportSequence;
} MODE_ResponseStatus_t;
typedef struct
{
    uint8 Mode; uint8 Enabled; uint8 PeerEnabled; uint8 PeerArmed;
    uint8 PeerMode; uint8 PeerLost; uint8 RestartSlot; uint8 RestartPhase;
    uint32 ModeSinceSeconds; uint32 HealthySinceSeconds; uint32 PeerLastSeconds;
    MODE_ProfileDef_t Profile;
    MODE_ResponseStatus_t Faults[MODE_NUM_FAULT_SLOTS];
} MODE_Status_Payload_t;
```

HK PeerSequence is the last received peer MODE sequence, 0 before
contact; the local publication sequence is carried by MODE_PEER itself.
Initial PeerMode 0xFF, PeerArmed/PeerLost 0; RestartSlot 0xFF when idle.
RestartPhase 0 IDLE, 1 WAIT_STOP, 2 WAIT_START. Pending is exactly Active && !Exhausted && Attempts < RetryCap
(using the current response row), including while automatic response is
suspended; Attempts remain visible after a clear; Active becomes 0. The
profile field is the configured current-mode profile, even if a ground
command subsequently overrides a target app. HealthySinceSeconds is 0
unless a healthy recovery interval is running. MODE sends the C.5
heartbeat each 1 Hz wakeup, even if automatic response is disabled.

Event IDs 1..10 C.4; 11 SUB_ERR, 12 TRANSITION_INF, 13 CONTROL_INF,
14 MODE_ERR, 15 RESPONSE_INF, 16 ACTION_ERR, 17 RETRY_ERR,
18 PEER_LOSS_ERR, 19 PEER_RESTORED_INF, 20 STATUS_INF, 21 FAULT_ERR,
22 PEER_ERR. RESPONSE_INF names fault type, slot and attempt number;
RETRY_ERR names slot and cap; no event repeats while exhaustion persists.

### S7 Acceptance

Build, unit tests, per-source coverage and DESIGN.md meet N. Hidden
tests exercise table boundaries, simultaneous trigger precedence, SAFE
latching, quiet recovery and reset semantics, serial wrap/duplicate
reports, each action's exact MID/FC/payload, STOP/START presence and
timeouts, cooldown across clears, cap exhaustion and explicit reset,
peer arming/timeout/recovery and command error paths. Live through CMDI
and TLMO: stop GW, observe SFI onset/clear and MODE DEGRADED, automatic
START with pinned parameters, response/cooldown telemetry, then NOMINAL
after 20 healthy seconds. Recurrent stopped GW faults exhaust three
attempts without a fourth START; SAFE persists through clear until
RESET_RESPONSE and SET_MODE. LIMIT forces SAFE; warning/command
precedence and all three profiles are verified in target HK/stats.
Fixture files exercise the configured STOR TRANSFER action to ground;
GW preference/masks and missing peer messages are exercised on two
nodes. MODE disabled isolates the existing S1-S6 checks; MODE live
checks enable it explicitly after setup. No live check requires raw SB
injection, an unframed command or a MODE-private control channel.

## S8. System-level tests in CTF

Deliver the system-level tests as CTF test scripts under `ctf/tests/`, one
script per demonstrated behaviour, run with `ctf/run_ctf.sh` (CTF v1.11 on
Python 3.12 with this fixture's shims, set up by `ctf/setup_ctf.sh`; see
`ctf/README.md`) against a tree built with `make native_std.install`. Add
CCDD definitions for every new app's commands and telemetry, and, where CTF
lacks a capability the tests need, add it as a CTF plugin or as an update
to an existing CTF module (S8.1). No standalone Python system scripts are
required; acceptance runs its own headless checks against your build.

### S8.1 Where CTF code goes

- `ctf/CTF/` and `ctf/venv/` are git-ignored and recreated by
  `ctf/setup_ctf.sh` from the pinned CTF commit, so nothing you change
  inside them survives. New plugins live in `ctf/plugins/`, a Python
  package (`__init__.py`) with one subdirectory per plugin, loaded through
  `additional_plugins_path` in `ctf/configs/mission.ini`, which names
  `${CTF_PLUGINS_DIR}`; `ctf/run_ctf.sh` exports `CTF_PLUGINS_DIR` as the
  absolute path of `ctf/plugins`. An update to one of CTF's own modules is
  delivered as a patch file in `ctf/shims/patches/` that `setup_ctf.sh`
  applies after checkout, idempotently (a re-run, or a run on a fresh
  clone, ends in the same patched tree), failing with a clear message if a
  patch does not apply. Each patch and each plugin is listed in the
  "Known quirks" or a new "Plugins" section of `ctf/README.md`.
- Plugins use only the standard library and the packages already in the
  venv (`shims/constraints.txt`); add no requirement.
- Every instruction validates its `data` and, on a bad argument, a timeout
  or an I/O error, logs the reason and returns failure (the script goes
  on, as CTF's `end_test_on_fail = false` intends); it never raises out of
  the plugin. Instruction names are unique across plugins, as CTF requires.

### S8.2 Two nodes in one CTF run

CTF's cfs plugin already supports several targets, each configured by a
section of `mission.ini`; a test registers and starts the targets it needs
and names one in each instruction's `target`. Configure:

- Target sections `[cpu1]` and `[cpu2]` in `ctf/configs/mission.ini`.
  `[cpu1]`: `cfs_run_dir = ${CFS_RUN_DIR}`, `cfs_exe = core-cpu1`,
  `cmd_udp_port = 1234`, `tlm_udp_port = 2234`, `CCSDS_target = set1`.
  `[cpu2]`: `cfs_run_dir = ${CFS_RUN_DIR_CPU2}`, `cfs_exe = core-cpu2`,
  `cmd_udp_port = 1235`, `tlm_udp_port = 2235`, `CCSDS_target = set2`.
  Both also carry `cmdi_key_hex` (default: the hex of the default table
  key of S3.3) and `cmdi_source_id` (default 1), read by the plugins of
  S8.3. The existing `[mission]` section stays, equivalent to `[cpu1]`, so
  single-node scripts may keep using it; a script registers either
  `mission` or `cpu1`, never both.
- `ctf/ccdd/json/mission_mids.json`: a second target `set2` listing every
  MID name of `set1` with its cpu2 value (local command and telemetry MIDs
  0x19xx and 0x09xx; global MIDs such as `STOR_XFER_SEG_MID` unchanged).
- `ctf/run_ctf.sh`: export `CFS_RUN_DIR_CPU2` (default
  `$CFS_ROOT/build-native_std/exe/cpu2`; a script that registers `cpu2`
  fails with a clear message if `core-cpu2` is not there); the free-port
  check covers UDP 1235 as well as 1234; the clean-up after an aborted run
  also stops a `core-cpu2` started from `CFS_RUN_DIR_CPU2`; the
  `shims/bin/pgrep` and `pkill` wrappers work for `core-cpu2 --reset PO` as
  they do for `core-cpu1`.
- Starting the second node must not disturb the first: in particular,
  CTF deletes a target's `cfs_ram_drive_path` before starting it, which
  must never be the live RAM disk of the other node (C.1).

### S8.3 Required capabilities, plugins and instructions

The plugin directory names, class names and instruction names below are
pinned so that reviewers can find them; the `data` keys listed are the
minimum each instruction accepts. Every instruction that talks to a node
takes `target` (`cpu1`, `cpu2` or `mission`).

| Capability | Plugin (`ctf/plugins/<dir>`, class) | Instructions |
|------------|-------------------------------------|--------------|
| Build and send CMDI frames from the table key; stage NEXT through cFE TBL | `cmdi_plugin`, `CmdiPlugin` | `SendCmdiCommand`, `SendCmdiRawPacket`, `ReplayCmdiFrame`, `PrepareCmdiKeyTable`, `ActivateCmdiKeyTable` |
| Drive two nodes in one test | CTF's own cfs plugin, configured as in S8.2 | CTF's `RegisterCfs`, `StartCfs`, `ShutdownCfs`, `CheckTlmValue`, `CheckEvent`, ... with `target` |
| Record telemetry arrival order/counts on a node's TLMO port | `tlm_order_plugin`, `TlmOrderPlugin` | `StartTlmRecording`, `CheckTlmOrder`, `CheckTlmCounters`, `StopTlmRecording` |
| UDP and TCP endpoints and PTY pairs facing GW, carrying CMDI frames with stream framing | `link_plugin`, `LinkPlugin` | `OpenUdpLink`, `OpenTcpServer`, `WaitLinkConnected`, `OpenPtyPair`, `SendLinkCmdiCommand`, `SendLinkPacket`, `CheckLinkPacket`, `CheckNoLinkPacket`, `CloseLink`, `OpenGwRelay`, `SetGwRelayEnabled` |
| Stop and start an app through ES commands and wait on events | `es_app_plugin`, `EsAppPlugin` | `StopApp`, `StartApp`, `WaitForEvent` |
| Reassemble ground STOR transfers from the raw TLMO listener tap | `stor_ground_plugin`, `StorGroundPlugin` | `StartStorGroundCapture`, `CheckStorGroundTransfer` |
| Check and compare files in a node's run directory | `node_file_plugin`, `NodeFilePlugin` | `CheckNodeFile`, `CompareNodeFiles`, `RemoveNodeFile` |

**`cmdi_plugin`.** CTF's `SendCfsCommand` sends unframed packets, which
CMDI rejects, so every command in these tests goes through this plugin.

- `SendCmdiCommand`: `target`, `mid`, `cc`, `args`, with the same names and
  argument encoding as CTF's `SendCfsCommand` (from the target's CCDD
  definitions, macros and `$variables$` resolved); optional `source_id`,
  `key_hex`, `save_frame` (user variable name receiving the complete encoded
  frame as hex), `time_offset` (seconds added to the onboard time, may be
  negative), `sequence`, and `corrupt`: `none` (default), `mac` (one MAC
  bit flipped, CRC recomputed), `crc` (one CRC bit flipped), or `magic`,
  `version`, `flags`, `length` (that header field made invalid, MAC and CRC
  recomputed). It builds the command packet (CCSDS header, length and
  checksum as CTF does), wraps it in the S3.1 frame (HMAC-SHA-256 with the
  key, CRC-16/CCITT-FALSE, `TimeSeconds` = onboard seconds +
  `time_offset`, `Sequence` = `sequence` or a per-target counter starting
  at 1, `SourceId` = `source_id` or the target's `cmdi_source_id`) and
  sends it to the target's `cmd_udp_port`. Onboard seconds are the seconds
  field of the most recent telemetry packet CTF received from that target;
  with none received within the verification timeout the instruction
  fails.
- `SendCmdiRawPacket`: `target`, `mid` (a CCDD name or a number), `cc`
  (default 0), `payload_hex`; optional `count` (default 1), `interval`
  (seconds between copies, default 0), `counter_offset` and `counter_start`
  (when given, a little-endian uint32 written at that packet byte offset,
  `counter_start` in the first copy and one more in each next copy, the
  checksum recomputed), and the frame options of `SendCmdiCommand`. It
  sends each copy as its own frame. This is how tests inject verification
  packets (C.3) and other packets without CCDD definitions.

- `ReplayCmdiFrame`: `target`, `variable`; sends the complete frame saved
  by SendCmdiCommand's save_frame unchanged, with no new timestamp,
  sequence, MAC or CRC. Fails if the variable is absent or not a complete
  frame. All automatic sequence allocation is shared per target/source
  across cmdi, es_app and link plugins; explicit sequences advance the
  allocator if newer by S3.6 serial arithmetic. Replaying does not.

- `PrepareCmdiKeyTable`: `target`, `path`, `active_key_hex`,
  `next_key_hex`; before StartCfs only. Creates a native cFE table image
  for `CMDI.ValTable` at `path` relative to that target's scratch run
  directory (the same path restrictions as node_file_plugin). Both keys
  must decode to CMDVAL_KEY_LEN bytes; ACTIVE/NEXT must be nonzero and
  distinct. Payload is S3.3's default CMDI_ValTable_t with only
  Validation.HmacKey, NextKey and NextValid (1) changed. Use the build's
  native cFE table-file header and layout, zero padding/spares; no new
  flight command or key file format. Fail on invalid arguments, a running
  target, an existing destination or image-generation/write errors.
- `ActivateCmdiKeyTable`: `target`, `path`, `key_hex`, `source_id`,
  optional `timeout` (default 10 s, for the whole instruction). Uses that
  prepared image only; `cf/x.tbl` maps to onboard `/cf/x.tbl`. Send native
  cFE TBL LOAD (file source, that onboard filename), VALIDATE (inactive
  buffer, table `CMDI.ValTable`), then ACTIVATE (`CMDI.ValTable`), on
  CFE_TBL_CMD_MID using the bundled CFE_TBL_LOAD_CC,
  CFE_TBL_VALIDATE_CC and CFE_TBL_ACTIVATE_CC and their native payloads.
  Each command is an authenticated CMDI frame with the supplied key/source
  and shared serial allocator. Await successful load, validation and
  activation completion in native TBL telemetry/events before the next
  step; fail on any negative result or timeout, with its reason. After
  activation await CMDI HK NextValid 1 and GW HK ValTableShared 1 and two
  GW wakeups, all within timeout. This is S3.6 table activation, including
  its replay/statistics reset; it never switches keys, rewrites the live
  default table, restarts CMDI/GW, or changes the plugin's configured key.
  Later commands explicitly supply key_hex. The prepared image is removed
  with RemoveNodeFile even on failure.

**Two nodes.** No new instruction: the configuration of S8.2 plus
`target` on every instruction, CTF's and the plugins'. If CTF's cfs plugin
needs a change to run two local targets side by side, it is a patch
(S8.1).

**`tlm_order_plugin`.** CTF's telemetry listener owns the target's
telemetry port and drops packets whose MID has no telemetry definition,
including command-type verification packets, and it keeps no order across
MIDs. This plugin taps the raw datagrams in arrival order before that
filtering (which may need a patch to CTF's listener, S8.1). CTF's listener
must also keep working when command-type or short datagrams arrive on a
telemetry port, as they do whenever TLMO forwards verification packets or
STOR segments; patch it if it does not.

- `StartTlmRecording`: `target`, `name`; from now on records every
  datagram the target's telemetry port receives, in arrival order, with
  its MID and bytes, under `name`.
- `CheckTlmOrder`: `target`, `name`, `mids` (MIDs in expected order, CCDD
  names or numbers); optional `min_counts` (one per MID), `counter_offset`.
  Passes when, among the recorded datagrams whose MID is in `mids`, every
  datagram of `mids[i]` arrived before any of `mids[j]` for i < j, each
  MID reached its minimum count, and, with `counter_offset`, the uint32 at
  that offset strictly increases within each MID.
- `StopTlmRecording`: `target`, `name`.
- `CheckTlmCounters`: `target`, `name`, `mid`, `counter_offset`,
  `values` (ordered list of uint32 values). Inspect all datagrams of that
  MID recorded so far under name, reading a little-endian uint32 at the
  absolute packet byte offset. Require exactly the listed values in that
  order, with no missing, extra or short packet; fail with actual/expected
  values. MID accepts a CCDD name or number. This is a synchronous check,
  not a wait/minimum-count assertion; scripts first await a stream snapshot
  proving their final injection has drained. Recording continues, so a
  later check can verify the cumulative sequence. Unknown recording,
  malformed arguments or raw-tap errors fail the instruction/test.

**`link_plugin`.** Endpoints on the test's side of a GW connection, each
known by `name`. GW is a TCP client (S5.2), so the plugin plays the
server.

- `OpenUdpLink`: `name`, `local_port`, `remote_port`; a UDP socket bound to
  127.0.0.1:`local_port` that sends to 127.0.0.1:`remote_port`.
- `OpenTcpServer`: `name`, `port`; listens on 127.0.0.1:`port` and accepts
  GW's connection in the background (again after GW reconnects). GW starts
  a connect attempt every 5 s while CONNECTING (S5.2), so a test that
  opens the server after adding the connection waits at least 6 s.
- `WaitLinkConnected`: `name`, `timeout`; passes once a client is
  connected.
- `OpenPtyPair`: `name`, `variable`; opens a PTY pair, sets the slave to
  raw mode, keeps the master, and stores the slave's path (for example
  `/dev/pts/7`) in the CTF user variable `variable`, so later instructions
  can pass `$variable$`, for example as `DevicePath` in GW ADD_CONN.
- `SendLinkCmdiCommand`: `name`, `target`, and either `mid`, `cc`,
  `args` (encoded from CCDD as `SendCmdiCommand` does) or `packet_hex`;
  the frame options of `SendCmdiCommand` (`source_id`, `key_hex`,
  `time_offset`, `sequence`, `corrupt`), with the key, source ID and
  onboard time taken from `target` as there; optional `prefix_hex` (raw
  bytes written before the marker) and `split` (a list of byte counts: the
  bytes are written in chunks of these sizes, `split_delay` seconds apart,
  default 0.2) for stream links. It builds the CMDI frame with the same
  frame builder as `cmdi_plugin` (one shared implementation, not a copy)
  and sends it over the link instead of to the node's CMDI port: one
  datagram on a UDP link, the marker `1A CF FC 1D` and the frame on a TCP
  or PTY link (S5.2). This is how tests send commands into GW.
- `SendLinkPacket`: `name`, `bytes_hex`; optional `marker` (default true
  on stream links) and `prefix_hex`, `split` as above. Writes the given
  bytes unchanged (after the marker if requested), for negative cases such
  as an unframed packet or a frame header with an out-of-range length.
- `CheckLinkPacket`: `name`, `mid` (CCDD name with `target`, or a number),
  `timeout`; optional `target` and `key_hex` (the key to verify with,
  default the target's `cmdi_key_hex`), `count` (default 1) and
  `payload_hex` (the bytes expected after the embedded packet's header).
  Stream links are deframed by marker hunting as GW does; each unit
  received must be a CMDI frame whose CRC and HMAC verify, and its
  embedded packet is matched. Passes when that many matching frames
  arrive within the timeout; a received unit that fails verification
  fails the instruction.
- `CheckNoLinkPacket`: `name`, `mid`, `duration`; passes when no frame
  embedding that MID arrives.
- `CloseLink`: `name`.

- `OpenGwRelay`: `name`, `port_a`, `node_a_port`, `port_b`,
  `node_b_port`, `target_a`, `target_b`; binds two UDP sockets on loopback.
  A datagram on port_a is verified with target_a's configured key and
  forwarded unchanged to node_b_port; port_b similarly verifies target_b
  and forwards to node_a_port. Polling is in the plugin, never a cFS
  task. No reframing, echo or spontaneous keep-alive replies. Errors fail
  the owning instruction/test. Initially enabled.
- `SetGwRelayEnabled`: `name`, `enabled` boolean; false consumes and
  discards datagrams in both directions while keeping sockets open, true
  resumes forwarding. This cuts a link without an ICMP error or a new
  connection, so GW's keep-alive timer, rather than host behaviour,
  detects loss. CloseLink closes both relay sockets and its worker.

**`stor_ground_plugin`.** Uses the raw TLMO tap of tlm_order_plugin,
never another socket bound to the telemetry port.

- `StartStorGroundCapture`: `target`, `name`; starts collecting local
  SFI_FAULT_TLM reports and STOR_XFER_TLM segments received after this
  instruction. Retains all bytes, not CCDD-decoded strings.
- `CheckStorGroundTransfer`: `target`, `name`, `timeout`, optional
  `expected_hex`, `fault_log` (default false). Waits for one complete
  ground transfer, verifies SrcNode, DstNode 0, identical id/metadata,
  contiguous indices/offsets, segment count and zero padding, file size
  and ISO-HDLC CRC. Duplicate/conflicting segments fail. Saves the
  reconstructed file as <CTF log directory>/<name>.bin, exposes its size
  and CRC as variables <name>_size and <name>_crc. With expected_hex,
  compare all bytes. With fault_log true, parse the exact S6.6 header and
  record sizes and require at least one onset and matching clear observed
  since capture, equal byte-for-byte to captured fault report payloads,
  records strictly in report serial order (modulo wrap), and Count <=64.
  Malformed/partial transfers fail with a reason and no success artifact.

**`es_app_plugin`.** Sends its ES commands as CMDI frames, as
`SendCmdiCommand` does. Add `CFE_ES_START_APP_CC` (code 4,
`CFE_ES_StartAppCmd_Payload_t`) and `CFE_ES_STOP_APP_CC` (code 5,
`CFE_ES_AppNameCmd_Payload_t`) to `ctf/ccdd/json/cfe_es_cmd.json`.

- `StopApp`: `target`, `app`, optional `timeout` (default 10 s); sends
  ES STOP_APP and passes when ES's `CFE_ES_STOP_INF_EID` (8) event naming
  the app arrives.
- `StartApp`: `target`, `app`, `entry`, `file`, `priority`, `stack_size`,
  optional `exception_action` (default 0) and `timeout`; sends ES START_APP
  and passes when `CFE_ES_START_INF_EID` (6) naming the app arrives.
- `WaitForEvent`: `target`, `app_name`, `event_id`, optional `event_str`
  and `is_regex` (as CTF's `CheckEvent`), `timeout` (seconds, which may
  exceed CTF's verification timeout); passes when a matching event message
  arrives after the instruction starts; an event received earlier does
  not count.

**`node_file_plugin`.** Paths are relative to the target's `cfs_run_dir`
(for example `cf/stor/files/xfer/copy.tbl`); absolute paths and `..` are
rejected.

- `CheckNodeFile`: `target`, `path`, `exists` (true or false), optional
  `size`.
- `CompareNodeFiles`: `target_a`, `path_a`, `target_b`, `path_b`; passes
  when both exist and are byte-identical.
- `RemoveNodeFile`: `target`, `path`; removes the file if present and
  passes when it is absent afterwards (for clean-up).

### S8.4 CCDD definitions

Under `ctf/ccdd/json/`, following `ctf/ccdd/README.md`: every command
message of STOR, CMDI, TLMO, GW, SFI and MODE with all its function codes
(including the `SEND_HK` and `WAKEUP` MIDs, and `SFI_HEARTBEAT_MID`,
`STOR_XFER_SEG_MID`, `STOR_XFER_ACK_MID`, `SFI_FAULT_MID`,
`SFI_HEALTH_MID`, `MODE_PEER_MID` and `GW_KEEPALIVE_MID`), every telemetry message
(HK and the other packets of C.3), their MIDs in both `set1` and `set2`,
the two ES commands and the three native TBL commands used in S8.3.
Add probe lines for every new message to
`ctf/ccdd/tools/layout_probe.c` (and include paths to `probe_layout.sh`)
so that `ccdd/tools/probe_layout.sh | ccdd/tools/check_layout.py` covers
them, and make that check pass. The `ci_lab_*` and `to_lab_*` definitions
may stay.

### S8.5 Test scripts and run order

Each script registers and starts the nodes it needs and shuts them down
(each script has its own `RegisterCfs` ... `ShutdownCfs`, as
`ctf/README.md` requires), sends every command through `cmdi_plugin`,
`es_app_plugin` or, into GW, `SendLinkCmdiCommand` (never CTF's
`SendCfsCommand`, which CMDI rejects), resets
counters before asserting absolute values, removes the files it created in
node file areas (with `RemoveNodeFile`, which runs even after a failed
check), and finishes within 180 seconds. `ctf/run_system_tests.sh` runs
the scripts below in this order, prints each one's result, and exits
nonzero if any fails.

| Order | Script | Demonstrates |
|-------|--------|--------------|
| 1 | `st_gw_commands.json` | adds a GW UDP connection by command, checks `ConnectionsUp` in GW HK, sends GW NOOP and checks `CommandCounter`, and sends an invalid REMOVE_CONN and checks `CommandErrorCounter` |
| 2 | `st_cmdval_hmac.json` | a CMDI frame wrapping a SAMPLE_APP NOOP with a valid MAC executes; the same command in a frame with one MAC byte changed (CRC recomputed) is rejected, `HmacErrors` rises by 1, the REJECT event names HMAC, and SAMPLE_APP `CommandCounter` does not change |
| 3 | `st_tlmo_priority.json` | with budget 0, class 0 and class 3 verification streams are injected past capacity; only class 3 drops; after raising the budget every class 0 packet reaches UDP 2234 before any class 3 packet (`tlm_order_plugin`) |
| 4 | `st_gw_udp.json` | a GW UDP connection added by command egresses a verification packet injected on a subscribed reserved MID to the test's UDP link as a valid CMDI frame (`CheckLinkPacket`); a SAMPLE_APP NOOP sent over the link with `SendLinkCmdiCommand` executes; the same command with `corrupt` `mac` does not execute, and GW `HmacErrors` rises by 1 with `GW_FRAME_REJECT_ERR_EID` naming HMAC |
| 5 | `st_gw_tcp.json` | a GW TCP connection to the test's TCP server reaches UP (`GW_CONN_UP_INF_EID`, `ConnectionsUp`) within one 5 s attempt period, egresses a verification packet on a subscribed reserved MID as a marker-framed CMDI frame, and ingests a SAMPLE_APP NOOP sent with `SendLinkCmdiCommand`, which executes |
| 6 | `st_gw_serial.json` | a GW SERIAL connection on a PTY pair opened by the test egresses a verification packet as a marker-framed CMDI frame and ingests a SAMPLE_APP NOOP sent with `SendLinkCmdiCommand` split across writes after garbage bytes, which executes |
| 7 | `st_sfi_heartbeat.json` | stopping GW with ES STOP_APP makes SFI report GW missing (event and `AppsMissing` 1); starting it again with ES START_APP (`/cf/gw.so`, `GW_AppMain`, priority 70, stack 131072) restores it |
| 8 | `st_stor_transfer.json` | with cpu1 and cpu2 running, GW UDP connections on both nodes carry `STOR_XFER_SEG_MID` from cpu1 to cpu2 and `STOR_XFER_ACK_MID` back, as CMDI frames each GW validates; a file added on cpu1 is transferred to node 2 with DELETE; it is byte-identical on cpu2, gone on cpu1, and `XfersCompleted` is 1 |
| 9 | `st_mode_fault_response.json` | pinned fault-to-response and quiet recovery sequence below |
| 10 | `st_gw_failover.json` | pinned two-node primary cut, backup selection and failback sequence below |
| 11 | `st_cmdi_replay.json` | replay an identical frame, rejection accounting and non-execution below |
| 12 | `st_fault_log_transfer.json` | fault onset/clear, binary log dump and byte-checked STOR transfer to ground below |
| 13 | `st_safe_mode_node_loss.json` | CRITICAL precedence, both-link peer loss, guarded SAFE exit and quiet recovery with TLMO/STOR profiles below |
| 14 | `st_key_switch.json` | authenticated CMDI/GW key switches, retired-key rejection, retained replay windows and exact source statistics below |
| 15 | `st_tlmo_profiles.json` | exact per-stream decimation counts and MODE-driven budget/class profiles through stream snapshots below |

`st_gw_commands.json` is the single CTF test an earlier revision of this
task required as `gw_udp.json`. The two fixture scripts
`ctf/tests/smoke_sample_app.json` and `smoke_housekeeping.json` send
unframed commands and enable TO_LAB, which no longer runs: port them to
`cmdi_plugin` (and drop the TO_LAB enable step) so that every script in
`ctf/tests/` passes against your build; `run_system_tests.sh` runs them
before the fifteen above. Update `ctf/README.md` for the CMDI command path,
the always-on TLMO output, the two targets and the plugins.

### S8.6 Required integrated script contents

The following sequences are normative script contents, in addition to
S8.5 setup/cleanup. Every observation is from TLMO; no script edits a
running node's files to simulate a fault. The scripts use C.7 timing
bounds, timeouts up to 30 s for recovery and up to 15 s for transfer, and
finish within 180 s each. Every script and smoke script first sends MODE
SET_ENABLE 0 on each started node and waits for HK Enabled 0 and the
initial profile completion barrier of C.7. Scripts enable MODE only as
specified below (script 9 enables after that barrier). The
SFI heartbeat script therefore retains its explicit StartApp step.
No script restarts SFI while MODE runs. Register event/report capture
before stimuli; use CheckEvent's captured history for early arrivals,
and WaitForEvent only for events still expected after it starts.
Commands below use the target's C.3 MID names and CCDD payloads, with all
spares zero. Scheduled HK observations include C.7's one-HK-period
allowance; request replies use its 2 s bound at sufficient budget. Scripts
13-15 use packets <=1024 bytes for injected verification traffic, at most
8 per burst and burst starts >=0.6 s apart. For full stream snapshots use
budget 256, one outstanding large request, capture it and wait >=0.6 s
before another large request (the PLAN timing/envelope rules apply).

9. `st_mode_fault_response.json`: start cpu1; keep peer monitors unarmed,
   MODE enabled. Wait for all five real SFI apps OK; RESET_COUNTERS on
   MODE and GW, record mode/response status. StopApp GW, without a manual
   StartApp. Observe SFI HB_MISS_ERR (12) and an active HEARTBEAT report
   for GW, then MODE TRANSITION_INF (12) to DEGRADED and RESPONSE_INF
   (15), ES START_INF (6) naming GW, GW INIT_INF (1) and SFI
   HB_RESTORED_INF (13)/clear report. Require one MODE START, Attempts 1
   for slot 3, no extra START during the 30 s cooldown, DEGRADED profile
   TLMO budget 8 and STOR threshold 65, GW heartbeats restored, and
   NOMINAL only after the continuous 20 s healthy interval (S7.2).
   Then NOMINAL budget 16 and threshold 80; the final START count is 1.
   Record events before StopApp so an early event is not missed.
10. `st_gw_failover.json`: start both nodes, MODE disabled during setup.
   Configure two UDP connections per node, indices 0 primary/1 backup,
   KeepAliveEnabled 1, PeerNode other node. Primary: cpu1 LocalPort 3101,
   RemotePort 3201, cpu2 LocalPort 3102, RemotePort 3202. Backup:
   cpu1 LocalPort 3111, RemotePort 3211, cpu2 LocalPort 3112, RemotePort
   3212. All addresses 127.0.0.1. OpenGwRelay primary uses ports
   3201/3101/3202/3102 and backup 3211/3111/3212/3112 (port_a,
   node_a_port, port_b, node_b_port). Each node's Subs list its own
   verification topic 0xE7 plus SFI_HEALTH_MID and MODE_PEER_MID. Set
   routes 0 health, 1 mode, 2 own verification MID, all Primary 0,
   Backup 1. On each TLMO ADD_STREAM the other node's verification MID
   (class 2). Enable MODE, wait for both links UP and both peer monitors
   armed, then cut only the primary relay. Within 5-8 s of the cut both connections
   0 are DOWN (6 s from each last valid keep-alive receipt); within 10 s SFI LINK slot 48 is active and both modes
   DEGRADED (GW detection plus SFI/MODE wakeup pipeline). All
   three routes select 1. Inject one unique E7 packet per node through
   CMDI, require exactly one copy at the opposite TLMO. Backup keeps
   health/mode fresh: neither node has PeerLost or NODE_LOSS. Restore
   primary relay, require LINK clears, no premature failback, then
   automatic selection 0 after 10 continuous healthy seconds measured
   from release of MODE's preference for route 0; other routes use
   S5.4's primary-UP timer. Require NOMINAL after 20 s quiet recovery
   on both. Close both relays on cleanup.
11. `st_cmdi_replay.json`: cpu1, MODE disabled. Reset CMDI and SAMPLE_APP
   counters using source 2; use source 2 for all setup and observations in
   this script, and source 1 only for the two NOOPs and their replays.
   SendCmdiCommand SAMPLE_APP NOOP with automatic fresh serial
   and save_frame `replay_frame`; confirm one accepted command. Invoke
   ReplayCmdiFrame twice within the 30 s age window. Read CMDI HK and
   SEND_SOURCE_STATS: Accepted rose once, ReplayErrors twice for source
   1, CMDI_REJECT_ERR names REPLAY twice, SAMPLE_APP CommandCounter
   remains 1. A new fresh-serial NOOP executes and raises that counter
   to 2. Account separately for authenticated commands used to request
   statistics; they use source 2, so source-1 statistics stay exact.
12. `st_fault_log_transfer.json`: cpu1, MODE disabled; start raw ground
   capture before the stimulus. Send SFI SET_WATCH_ENABLE index 6,
   Enabled 1; inject three values 9 on topic 0xEF payload byte 8, then
   one value 10. Require one CRITICAL LIMIT onset and one clear in
   SFI_FAULT_TLM, Latched slot 22 still 1. Send DUMP_FAULT_LOG and wait
   DUMP_INF (28). STOR ADD_FILE SourcePath /cf/sfi_faults.bin, DestPath
   fault/log.bin, FILE_INFO FILE fault/log.bin, TRANSFER FILE to ground,
   KEEP. CheckStorGroundTransfer with fault_log true: exact log header,
   report bytes/order and matching CRC/size from FILE_INFO telemetry.
   Require XfersCompleted 1, then delete fault/log.bin, ACK_FAULT 22 and
   verify latch clear. Remove /cf/sfi_faults.bin with RemoveNodeFile
   path cf/sfi_faults.bin during cleanup.
13. `st_safe_mode_node_loss.json`: start cpu1 and cpu2, disable MODE
    first on both as above. Use script 10's exact primary/backup relay
    ports (3101/3201, 3102/3202; 3111/3211, 3112/3212), connection
    indices 0/1, addresses 127.0.0.1, KeepAliveEnabled 1 and PeerNode
    other node, Interface UDP 1, Enabled 1, DevicePath empty and unused
    Subs INVALID. Here Subs contain only SFI_HEALTH_MID and MODE_PEER_MID;
    SET_ROUTE 0 health and 1 mode, Primary 0, Backup 1. No verification
    route/stream is needed. Send SFI SET_PEER_ENABLE 1 (CC 10) and MODE
    SET_PEER_ENABLE 1 (CC 5) on both; require both connections UP, SFI
    PeerArmed 1 and MODE SEND_STATUS PeerArmed 1 after real cross-node
    contact. Wait for the five real monitored apps OK. Enable MODE
    (CC 2, Enabled 1), require NOMINAL budget 16/threshold 80, then reset
    SFI, MODE and GW counters (CC 1 each) on both. Do not reset them
    again until all final assertions are captured.
    On cpu1 SFI SET_WATCH_ENABLE (CC 8, Index 6, Enabled 1); inject with
    SendCmdiRawPacket MID 0x18EF, CC 0, payload_hex
    `000000000000000009000000`, count 3, interval 0.6. Require LIMIT_ERR
    (SFI 23), one FaultType LIMIT 4, SourceIndex 6, Severity CRITICAL 2,
    Active/Latched 1 report (slot 22), and MODE TRANSITION_INF (12) to
    SAFE 2 within the next MODE wakeup plus C.7's 2 s. MODE RESPONSE_INF
    (15) names slot 22 attempt 1. Require MODE status Profile
    {TlmProfile 2, PurgeThreshold 50, GwEnabledMask 0x03}; actual TLMO HK
    SelectedProfile 2/BudgetPacketsPerCycle 4 and STOR HK
    PurgeThresholdPercent 50, with TLMO PROFILE_INF (26), STOR
    SET_PARAM_INF (16) and GW MASK_INF (32). SET_MODE NOMINAL (CC 3,
    Mode 0) and SET_MODE DEGRADED (Mode 1) both fail with MODE_ERR (14),
    increment CommandErrorCounter to 2, and leave SAFE unchanged.
    Cut BOTH relays with SetGwRelayEnabled false, leaving sockets and
    both nodes/apps running. After the cut, GW SEND_CONN_STATUS (CC 7)
    records the now-stable LastKeepAliveSeconds for each connection;
    SFI/MODE HK give their independent PeerLastSeconds. Require GW
    KEEPALIVE_ERR (29),
    ConnectionsUp 0 and links 0/1 DOWN within [5,8] s of the cut (never
    before the 6 s keep-alive deadline); SFI LINK_ERR (32), active
    slots 48/49 within 10 s of the cut. Require SFI NODE_LOSS_ERR (30),
    slot 56 FaultType 7/SourceIndex 0/Severity 1/Active 1 and PeerLost 1
    within C.7's [10,12] s of last health receipt; independently MODE
    PEER_LOSS_ERR (18)/PeerLost 1 within [10,12] s of last mode receipt.
    Those receipt fields export uint32 Seconds: a value L denotes
    [L,L+1), not exact time L. Require each onset timestamp in [L+10,L+13)
    for its own peer field; similarly use [K+6,K+8) for a connection's
    last keep-alive Seconds K (6 s plus the 10 Hz 1 s tolerance and
    <1 s quantization). Full-precision deadline boundaries remain the
    synthetic-time checks of C.7; do not invent a raw keep-alive SB MID.
    cpu1 remains SAFE despite WARNING losses; cpu2 enters DEGRADED.
    On cpu1 inject one in-range value with the same MID/payload, last
    uint32 10 (`0A000000`); require WATCH_CLEAR_INF (17), slot 22 clear
    with Latched 1, but no automatic SAFE exit. While loss is active,
    SET_MODE NOMINAL fails (MODE_ERR, CommandErrorCounter 3). SET_MODE
    DEGRADED now succeeds (CONTROL_INF 13/TRANSITION_INF 12), with no
    CRITICAL/SAFE-target fault remaining; actual budget 8/threshold 65.
    Restore both relays by 20 s after the cut and clear the LIMIT within
    30 s of its onset; all slots must clear before their first 30 s
    retry deadlines. These are script deadlines, not relaxed fault timers.
    Require GW UP, SFI LINK_CLEAR_INF (33) for 48/49 and
    NODE_RESTORED_INF (31) for 56, MODE PEER_RESTORED_INF (19), both
    PeerLost 0 and no active fault. Stay DEGRADED until a continuous
    healthy interval completes: NOMINAL in C.7's [20,23] s from the first
    healthy MODE wakeup, or [20,25] s from the last SFI clear when that
    clear removes the final guard. When peer restore removes the final
    guard, use [20,25] s from its event/receipt bound instead; the healthy
    MODE wakeup remains the [20,23] s reference in either ordering.
    Require actual NOMINAL budget 16/threshold 80 on both. Final SFI
    LinkFaults 2, NodeLossFaults 1 each; LimitFaults 1/0 and FaultReports
    8/6 on cpu1/cpu2. MODE PeerLosses 1 each, Transitions 3/2,
    ResponsesSent 4/3, RestartsSent/ActionErrors 0; Attempts 1 and
    Exhausted/Active 0 for slots 48,49,56 on both and 22 on cpu1.
    ACK_FAULT these inactive slots (CC 6); require ACK_INF (25). Disable
    MODE, remove routes with SET_ROUTE INVALID MsgId, REMOVE_CONN 0/1
    (CC 3), restore/CloseLink both relays even on failure, and shut down
    both nodes. No app restart, SFI restart or forged SFI fault report.
14. `st_key_switch.json`: cpu1, MODE disabled throughout. Before startup
    PrepareCmdiKeyTable at cf/ctf_key1.tbl with ACTIVE K0 (S3.3 default)
    and NEXT K1 (32 bytes 0x01), and cf/ctf_key2.tbl with ACTIVE K1 and
    NEXT K2 (32 bytes 0x02). Start cpu1, apply the initial-profile barrier,
    and use source 2 for all setup/observations. OpenUdpLink `key_link`
    local_port 3301, remote_port 3302. GW ADD_CONN (CC 2) Index 0,
    Interface UDP 1, Enabled 1, LocalPort 3302, RemotePort 3301,
    RemoteAddr 127.0.0.1, DevicePath empty, remaining Subs INVALID,
    KeepAliveEnabled 0, PeerNode 0, Subs only verification MID 0x18E7;
    no routes. Wait for GW CONN_UP_INF (20)/ConnectionsUp 1 and
    ValTableShared 1. SAMPLE_APP commands and CMDI_CMD_MID are not Subs.
    Run two consecutive lanes, first K0->K1 with SWITCH_KEY over CMDI
    UDP 1234, then K1->K2 with SWITCH_KEY over key_link into GW. In each
    lane ActivateCmdiKeyTable for its prepared file under the current
    ACTIVE key, require NextValid 1, then reset CMDI, GW and SAMPLE_APP
    (CC 1) using source 2/current key. Record a source-statistics baseline
    after these resets; no table activation or app restart until that
    lane's final observations. Reset in the stated CMDI, GW, SAMPLE_APP
    order, then issue exactly one baseline SEND_SOURCE_STATS: source 1 is
    all zero, source 2 Accepted 3 and all errors zero (CMDI's own reset
    has cleared its frame's count). Use scheduled HK for subsequent
    observations; the only further source-2 CMDI UDP frames are lane 1's
    switch, the verification injection, GW SEND_CONN_STATUS (CC 7), and
    the final SEND_SOURCE_STATS. No extra polling commands are permitted.
    Source 1 sends SAMPLE_APP NOOP (CC 0) over CMDI with explicit serial
    100 and save_frame `old_frame`; require SAMPLE_APP HK
    CommandCounter 1. Send CMDI SWITCH_KEY (CC 4) under current ACTIVE,
    source 2/fresh serial, by SendCmdiCommand in lane 1 or
    SendLinkCmdiCommand in lane 2. Require KEY_SWITCH_INF (CMDI 16),
    KeySwitches 1 and NextValid 0. The switch frame's MAC is valid under
    the retiring key; do not disable any validation check. Supply the new
    key explicitly to every subsequent setup/observation command. Wait
    two GW wakeups before link checks, including C.7's 10 Hz +1 s bound.
    Within 30 s of the saved frame's timestamp, ReplayCmdiFrame
    old_frame unchanged: require CMDI REJECT_ERR (12) HMAC/source 1.
    Send the same SAMPLE_APP NOOP over CMDI with source 1/sequence 100,
    new key and fresh onboard timestamp: require REJECT_ERR REPLAY.
    The first rejection proves retired-key rejection; the second proves
    retained serial state after SWITCH_KEY. Neither executes. Send a
    fresh new-key NOOP, source 1/sequence 101 over CMDI; it executes,
    CommandCounter 2. On key_link send a retired-key NOOP then a fresh
    new-key NOOP, source 1/serials 102 then 103: require GW
    FRAME_REJECT_ERR (25) HMAC/connection 0 for the first and unchanged
    SAMPLE_APP CommandCounter 2; the second raises it to 3. SendCmdiRawPacket
    MID 0x18E7, CC 0, payload_hex `000000000000000000000000`, count 1,
    source 2/new key, then CheckLinkPacket
    key_link MID 0x18E7, key_hex new key, count 1, timeout 2; shared GW
    egress must authenticate with the switched key too.
    Request GW SEND_CONN_STATUS, then final SEND_SOURCE_STATS (CMDI CC 5,
    SOURCE_STATS_INF 18) and scheduled HK under the new key: source-1
    row Accepted 2, HmacErrors 1, ReplayErrors 1,
    Format/Crc/Source/AgeErrors 0. All unused rows and OTHER stay zero.
    Source-2 Accepted is exactly 7 in lane 1 and 6 in lane 2, including
    the final statistics request itself; every source-2 error is zero.
    GW ingress bypasses CMDI UDP statistics: lane-2 SWITCH_KEY and both
    link NOOPs never enter those source rows or frame counters.
    CMDI HK FramesAccepted 9/8 and FramesReceived 11/10 in lanes 1/2,
    HmacErrors/ReplayErrors 1 each, SourceSlotsUsed 2, all other reason
    counters 0. GW since reset: FramesReceived/RxFrames
    2 in lane 1, 3 in lane 2; HmacErrors/RxErrors 1, RxPackets and
    PacketsIngressed 1/2 respectively, other reason/ingress errors and
    LoopDrops 0. In particular GW has no replay rejection/window.
    Finish lane 1 before preparing lane 2's serial-100 stimulus: its
    table activation intentionally clears replay/source state. Each lane
    has a 30 s stimulus-to-replay deadline, not a wait of 30 s.
    Cleanup under K2 (or the last confirmed ACTIVE key on failure): GW
    REMOVE_CONN 0, CloseLink key_link, RemoveNodeFile both prepared
    images even on failure, StopTlmRecording if used, ShutdownCfs. No
    default table rewrite or restart is used to switch or restore keys;
    fresh scratch nodes in the next script start with K0.
15. `st_tlmo_profiles.json`: cpu1, MODE disabled for the whole script;
    explicit MODE SET_MODE still applies profiles per S7.2. Use source 2
    for control and source 1 for injections. After the initial profile
    barrier, MODE SET_MODE NOMINAL (CC 3, Mode 0), APPLY_PROFILE (CC 7)
    and verify TLMO SelectedProfile 0/budget 16 and STOR threshold 80.
    Use default verification streams A=0x18EA (base class 2, slot 38)
    and B=0x18EB (base class 3, slot 42), with SBN's 57-stream default
    table and no additions/removals. SEND_CLASS_MAP (TLMO CC 4) must
    confirm those slots/MIDs/classes before counters are asserted.
    SET_DECIMATION (TLMO CC 7) A=2 and B=3, each SET_DECIMATION_INF (24),
    establishes phase 0. SET_BUDGET (CC 3, Packets 256) for snapshots
    and RESET_COUNTERS (CC 1); resetting retains those zero phases.
    StartTlmRecording `profiles`. SendCmdiRawPacket for each MID, CC 0,
    payload_hex `000000000000000000000000`, count 12, interval 0.6,
    counter_offset 16, counter_start 1. Run streams sequentially with
    >=0.6 s between bursts; only these injections use A/B. Wait until
    all 12 frames per stream have been processed and kept packets drain
    (allow CMDI ingress and following TLMO drain each its next 10 Hz
    wakeup +1 s under C.7); then SEND_STREAM_STATS
    (CC 9, STREAM_STATS_INF 28), capture its full reply within 2 s.
    Match by Slot and MsgId, not array position alone: A has Received 12,
    Sent 6, Decimated 6; B Received 12, Sent 4, Decimated 8. Both have
    Dropped/SendErrors/Queued 0, Decimation 2/3 and Class 2/3. Exact
    delivered payload counters are A {1,3,5,7,9,11}, B {1,4,7,10};
    CheckTlmCounters on recording profiles, each MID, counter_offset 16,
    must match those lists exactly as well as the stream Sent counts.
    No global Sent total is asserted because background traffic continues.
    After the first snapshot, MODE APPLY_PROFILE restores actual budget
    16; then MODE SET_MODE DEGRADED (Mode 1) changes it to 8. Require
    CONTROL_INF (13),
    TRANSITION_INF (12), TLMO PROFILE_INF (26), actual SelectedProfile 1
    and budget 8, STOR threshold 65, MODE status Profile.TlmProfile 1.
    MODE remains disabled, so no automatic 20 s recovery races the test.
    Do not send SELECT_PROFILE directly. Do not change factors or reset
    counters/phases. Inject 6 more packets per stream, counter_start 13,
    otherwise the same encoding/pacing; phases after 12 were both 0.
    Require at budget 8 the next kept A counters {13,15,17} and B
    {13,16} drain without drops. After observing budget 8, temporarily
    SET_BUDGET 256, await >=0.6 s since the prior full snapshot and
    SEND_STREAM_STATS again, with no other large request outstanding.
    A now Received 18/Sent 9/Decimated 9, B 18/6/12; both still
    Dropped/SendErrors/Queued 0, factors 2/3, effective Class 2/2.
    CheckTlmCounters cumulative values A {1,3,5,7,9,11,13,15,17},
    B {1,4,7,10,13,16}; require StreamCount 57, entries in ascending
    slots with the remaining seven entries all zero in both snapshots.
    TLMO HK PacketsDecimated 21; other streams remain factor 1.
    MODE APPLY_PROFILE must restore actual budget 8 after the temporary
    override. Cleanup: SET_DECIMATION A/B 1, MODE SET_MODE NOMINAL,
    require budget 16/threshold 80, StopTlmRecording profiles and
    ShutdownCfs even on failure. No file artifacts or SFI restart.

### S8 Acceptance

On a clean build of your repository, `ctf/setup_ctf.sh` creates a fresh
CTF checkout and venv (applying your patches), the CCDD layout check of
S8.4 is run and must pass, and CTF's plugin listing
(`ctf --pluginInfo`) must name every pinned plugin instruction of S8.3.
Each script of S8.5, and each ported smoke script, is then run by name
with `ctf/run_ctf.sh`, in a private network namespace with nothing else
running, with `CFS_RUN_DIR` and `CFS_RUN_DIR_CPU2` pointing at scratch
copies of the installed trees and a 300-second limit per script, and must
pass; `ctf/run_system_tests.sh` must exist and run both smoke scripts then
all fifteen st_ scripts in S8.5 order (17 results). Each script's own
180 s completion requirement remains within the 300 s grading limit. The
scripts, plugins and patches are also read in human review for whether
they test what the table says. Acceptance runs its own headless checks
against your build for the live behaviour of S1 to S7; those do not use
your CTF code.

## N. Non-functional requirements

- Build: `make native_std.prep && make -j16 native_std.install` with no
  warnings (`-Werror`); `make -j16 native_std.runtest` passes (CI_LAB's
  and TO_LAB's own unit tests are no longer built, so the test count
  differs from the mission README's). Existing apps, including
  `apps/ci_lab` and `apps/to_lab`, are unchanged; the files you change
  outside the new apps and library are those of C.6, the run scripts of
  C.1, `tools/udp_harness.py` (S3.5) and the CTF files of S8.
- Unit tests: one coverage test file per source module of each new app in
  `apps/<app>/unit-test/coveragetest/coveragetest_<module>.c` (the build
  requires it) and for the library in `libs/cmdval_lib/unit-test/coveragetest/`.
  Provide `ut-stubs` for `cmdval_lib` so CMDI's and GW's tests can stub it.
- Coverage: line coverage of every source file of `stor`, `cmdi`, `tlmo`,
  `gw`, `sfi`, `mode` and `cmdval_lib` at least 95 percent (the project convention
  is 100), measured from the gcov data the unit-test build produces, for
  example `gcov --json-format` on the coverage objects under
  `build-native_std`; lcov is not installed. This covers all of CMDI's
  and TLMO's sources, including the code they inherited from CI_LAB and
  TO_LAB.
- Design notes: `DESIGN.md` in each of `apps/stor`, `apps/cmdi`,
  `apps/tlmo`, `apps/gw`, `apps/sfi`, `apps/mode` and `libs/cmdval_lib`, at most 80 lines
  each: purpose, data flow, main data structures, timing (what runs on which
  wakeup), error handling, and the decisions you made where this document
  left a choice.
- MODE DESIGN.md additionally records transition precedence, profile commands,
  restart protocol, slot mapping/cooldown/cap lifetime and both peer timers.
  SFI DESIGN.md records latch versus physical state, mixed cluster history,
  event-window saturation and fault-log format. CMDI/GW notes cover shared
  runtime key ownership and CMDI replay-state lifetime. TLMO notes cover
  decimation phases, profile mapping/budget overrides and snapshot counters.
  Required unit tests
  exercise these behaviours and error paths with synthetic cFE time, not
  host sleeps; line coverage for MODE is per source file as for every app.
- Report at `docs/reports/system.md` with the sections `Design summary`,
  `Interfaces`, `Unit tests` (counts per app and library), `Coverage` (per
  source file), `System tests` (results of `ctf/run_system_tests.sh`, and
  the CTF plugins and patches you added) and `Limitations`. `Interfaces` includes the full MID/topic inventory,
  mode/fault transitions and profiles; `System tests` includes all fifteen
  st_ scripts and both smoke scripts; `Design summary` includes scheduler
  arithmetic (32 used, 0 spare) and peer/replay/key lifetimes.
- Commit the work.

### N Acceptance

The build and the full unit-test run are checked; coverage is measured per
source file from the build's gcov data; the design notes and the report are
checked for presence, length and sections, and are read in human review.

## Acceptance summary

Each subsystem (S1 to S8, and N) is scored on its own Acceptance paragraph;
the task passes when all pass. A live check starts the nodes it needs from
your build in a scratch copy of the installed tree, so state you left in
`build-native_std/exe/*/cf` does not matter. Every live check commands a
node only through CMDI frames on UDP 1234 (cpu1) or 1235 (cpu2), built
with the default table key unless the check loads another, and reads the
node only through TLMO on UDP 2234 or 2235 without any enable step;
verification packets are injected on the reserved command MIDs of C.3.
Every unit sent to or received from a GW connection is a CMDI frame
(marker-framed on TCP and SERIAL) validated or built with the default
table key; GW, like CMDI, executes nothing that fails validation.
The live checks of S1 to S7 are acceptance's own headless checks;
GW is used in S5 and the peer-health/mode checks of S6/S7. S8 runs your
CTF scripts. MODE automation is disabled for isolated S1-S6 checks (C.7). Feedback on a failing check names
the subsystem and the term of this document involved.
