# Task: two-node flight system build (STOR, CMDVAL, TLMO, GW, SFI)

## Background

The mission is growing from a single lab processor into a two-node system.
Operations needs onboard storage with file transfer between nodes and to the
ground, authenticated command ingest, prioritised telemetry output under a
bandwidth budget, a gateway that moves software-bus traffic over UDP, TCP
and serial links, and a system fault identifier that watches every
application and reports system-level health. Build these as cFS
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
problems; you may study them, but STOR, CMDVAL, GW and SFI are new code,
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
| S6 | SFI system fault identifier app | `apps/sfi` | C (its heartbeat definitions are used by S1, S3, S4, S5) |
| S7 | System-level tests in CTF, CTF plugins and CCDD definitions | `ctf/` | all |

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
`CFE_GLOBAL_CMD_TOPICID_TO_MIDV` and are identical on both nodes. All
network traffic is on loopback. CMDI's and TLMO's ports are their base
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
| STOR | 0xC4 / 0x18C4 | 0xC5 / 0x18C5 | 0xC6 / 0x18C6 (1 Hz) | 0xC4 / 0x08C4 | `LIST_TLM` 0xC5 / 0x08C5, `STATS_TLM` 0xC6 / 0x08C6, `XFER_TLM` 0xC7 / 0x08C7; global `XFER_SEG` 0x18 / 0x1878, global `XFER_ACK` 0x19 / 0x1879 |
| CMDI | 0xD0 / 0x18D0 | 0xD1 / 0x18D1 | 0xD2 / 0x18D2 (10 Hz) | 0xD0 / 0x08D0 | |
| TLMO | 0xD4 / 0x18D4 | 0xD5 / 0x18D5 | 0xD6 / 0x18D6 (10 Hz) | 0xD4 / 0x08D4 | `CLASS_TLM` 0xD5 / 0x08D5 |
| GW | 0xD8 / 0x18D8 | 0xD9 / 0x18D9 | 0xDA / 0x18DA (10 Hz) | 0xD8 / 0x08D8 | `CONN_TLM` 0xD9 / 0x08D9 |
| SFI | 0xDC / 0x18DC | 0xDD / 0x18DD | 0xDE / 0x18DE (1 Hz) | 0xDC / 0x08DC | `STATUS_TLM` 0xDD / 0x08DD, `HEARTBEAT` (command) 0xDF / 0x18DF |

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
  sends the named error event, and changes nothing else.
- Every HK payload begins `uint8 CommandCounter; uint8 CommandErrorCounter;`,
  so `CommandErrorCounter` is byte 17 of every new HK packet.
- Event texts print values in decimal and MIDs as `0x%04X`. `*_ERR` events
  are ERROR type, `*_INF` INFORMATION, `*_DBG` DEBUG. No event filters.
  Event IDs 1 to 10 are the same in every new app: 1 INIT_INF, 2 CC_ERR,
  3 NOOP_INF, 4 RESET_INF, 5 MID_ERR, 6 CMD_LEN_ERR, 7 PIPE_ERR,
  8 TBL_REG_ERR, 9 TBL_LOAD_ERR, 10 TBL_VAL_ERR.
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

STOR, CMDI, TLMO and GW each send one heartbeat per second on
`SFI_HEARTBEAT_MID`: STOR on every 1 Hz wakeup, the others on every 10th
10 Hz wakeup, using SFI's `fsw/inc` and `config` headers.

### C.6 Mission integration

- `sample_defs/targets.cmake`: add `cmdval_lib stor cmdi tlmo gw sfi` to
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
  and set `CFS_APP_STARTUP_ORDER` to `lc cf ds fm hk hs sc md cs stor gw sfi sbn`
  (STOR, GW and SFI start with the loop's priority 70 and stack 131072).
  The generated `cf/cfe_es_startup.scr` of each node names neither
  `ci_lab` nor `to_lab`.
- `sample_defs/cpu1/install_custom.cmake` and `cpu2/install_custom.cmake`:
  remove `ci_lab` and `to_lab` from `APP_INCLUDE_TABLE_LIST` and add the
  new apps as `hk` is; remove the `to_lab.table` include and definition
  lines and the `add_cfe_tables(to_lab to_lab_sub_alt.c)` line, and give
  `tlmo.table`, `stor.table` and `sfi.table` the include directories and
  definitions (`APP_INCLUDE_TABLE_LIST`, `APP_DEFINITION_TABLE_LIST`) that
  `to_lab.table` had, since their default tables name other apps' MIDs
  (`CFS_SBN_ENABLED` is defined on this native build).
- `sample_defs/default_osconfig.cmake`: `OSAL_CONFIG_MAX_FILE_NAME` 32 (S1).
- `sample_defs/tables/sch_lab_table.c` (0.1 s ticks): remove the
  `CI_LAB_SEND_HK_MID` and `TO_LAB_SEND_HK_MID` entries and the
  `ci_lab_msgids.h` and `to_lab_msgids.h` includes; add STOR SEND_HK 49 and
  WAKEUP 10, CMDI SEND_HK 50 and WAKEUP 1, TLMO SEND_HK 51 and WAKEUP 1, GW
  SEND_HK 53 and WAKEUP 1, SFI SEND_HK 55 and WAKEUP 10. Slot budget: the
  table holds 22 entries today (20 unconditional, of which 2 are the lab
  apps, plus 2 under `CFS_SBN_ENABLED`, which is defined here); removing the
  lab entries leaves 20, and the 10 new ones make 30 of SCH_LAB's 32
  (`SCH_LAB_MISSION_MAX_SCHEDULE_ENTRIES`), leaving 2 spare.
- `sample_defs/tables/to_lab_sub.c` and the other `to_lab_sub*.c` files are
  no longer built and are left unchanged; the telemetry they routed is
  routed by TLMO's default table (S4.1).
- `tools/udp_harness.py`: the frame builder of S3.5. `README.md` (mission
  root): update the "Ground interface" section to name CMDI and TLMO on
  their ports and the framed commands, and the "Registering an app" step
  that names `to_lab_sub.c` to name TLMO's table.

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

### S1 Acceptance

STOR builds on both nodes and its unit tests pass with the coverage of N.2.
Hidden coverage tests compiled against the names above exercise table
validation, decimation, roll-over, purge order including archived files,
quota drops, path rejection and the listing packets. Live, acceptance
runs its own headless checks against your build: on a running cpu1, with
a table whose entries name reserved MIDs (C.3) loaded through cFE Table
Services, verification packets injected as CMDI frames (S3) fill the
telemetry quota, and the files on disk, purge order, housekeeping and
statistics (received through TLMO, S4) are checked; the file-area
commands, sent as CMDI frames, are exercised including quota rejection.

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
| 12 | 4 | `Sequence`: covered by the MAC, not checked |
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

`typedef struct { CMDVAL_Config_t Validation; } CMDI_ValTable_t;`.
Validation rejects an all-zero key, `MaxAgeSeconds` 0 or above 86400,
`EnableMask` bits above 0x0F, the SOURCE bit with no nonzero source, and
duplicate nonzero sources. Default: key = the 32 ASCII bytes
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
  `FramesAccepted`++, `CMDI_ACCEPT_DBG_EID` naming its MID. Otherwise the
  reason's counter is incremented, nothing is sent, and one
  `CMDI_REJECT_ERR_EID` names the reason (`FORMAT`, `CRC`, `HMAC`, `SOURCE`
  or `AGE`) and, except for FORMAT, the source ID. Datagrams longer than
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
TO_LAB.

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
#define TLMO_MAX_PACKET  1024

typedef struct { CFE_SB_MsgId_t MsgId; /* INVALID = unused */ uint8 Class; uint8 Spare[3]; } TLMO_StreamDef_t;

typedef struct
{
    uint16           BudgetPacketsPerCycle; /* 0..256; 0 holds all output */
    uint16           QueueCapacity;         /* 1..64 packets, shared by all classes */
    TLMO_StreamDef_t Streams[TLMO_MAX_STREAMS];
} TLMO_PriorityTable_t;
```

Validation rejects a budget above 256, a capacity outside 1..64, a class
above 3 in a used entry, and duplicate used MIDs. Default: budget 16,
capacity 64, and the streams below with their classes, so that every
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

That is 48 streams on this build (43 without SBN), within
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
  of depth 48, and sends to 127.0.0.1 port 2234 + node ID - 1, TO_LAB's
  port. Output is always on from initialization: no enable command is
  needed, and none exists.
- Enqueue: each received stream packet (`PacketsReceived`++) is copied into
  one queue shared by all classes, tagged with the stream's current class.
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
  longer listed are discarded without counting, other queued packets keep
  their class, and `TLMO_TBL_UPDATE_INF_EID` is sent (not on the initial
  load).
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
cpu2 arrives on UDP 2235.

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
    CFE_SB_MsgId_t Subs[GW_MAX_SUBS];           /* egress MIDs, CFE_SB_INVALID_MSG_ID = unused */
} GW_ConnDef_t;

typedef struct { GW_ConnDef_t Conns[GW_MAX_CONNECTIONS]; } GW_ConnTable_t;
```

A used definition (table or ADD_CONN) is valid when: `Interface` is 1..3;
`Enabled` is 0 or 1; for UDP and TCP `RemoteAddr` is terminated and a valid
dotted IPv4 address and `RemotePort` nonzero; for TCP and SERIAL
`LocalPort` is 0; for SERIAL `DevicePath` is terminated and starts with
`/`; `Subs` has no duplicate used MID; no two UDP connections share a
nonzero `LocalPort`. The default table has every slot unused.

### S5.2 Interfaces and behaviour

- UDP: one CMDI frame per datagram to `RemoteAddr:RemotePort`; with a nonzero
  `LocalPort`, GW binds 0.0.0.0:`LocalPort` and reads inbound datagrams.
- TCP: GW is the client and connects to `RemoteAddr:RemotePort`; while not
  connected the connection is CONNECTING and GW retries once per second;
  peer close or an I/O error returns it to CONNECTING.
- SERIAL: GW opens `DevicePath` read-write when enabled and does not change
  terminal attributes (the other side sets raw mode). GW uses OSAL for file
  and socket I/O; mapping `/dev/pts` into the OSAL file system with
  `OS_FileSysAddFixedMap` is acceptable. Two properties of this path are
  requirements you must make work and demonstrate (S5 Acceptance, S7),
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
  CONNECTING is skipped and counts an egress error. GW subscribes, message
  limit 16, to the union of the enabled connections' `Subs` and
  unsubscribes MIDs no longer needed whenever the union changes.
- An open, bind or I/O failure on a UDP or SERIAL connection disables it
  (`Enabled` 0, `GW_CONN_DOWN_ERR_EID`). `GW_EGRESS_ERR_EID` /
  `GW_INGRESS_ERR_EID` are sent once per connection until its next
  successful write / read; counters always increment.
- Connection state: DISABLED 0, UP 1, CONNECTING 2; reaching UP sends
  `GW_CONN_UP_INF_EID`.
- Every 10th WAKEUP sends a heartbeat (C.5).

### S5.3 Commands and telemetry

| Code | Name | Payload | Rules |
|------|------|---------|-------|
| 0, 1 | NOOP, RESET_COUNTERS | none | reset: command, HK and per-connection counters |
| 2 | ADD_CONN | `{ uint8 Index; uint8 Spare[3]; GW_ConnDef_t Conn; }` | `Index` below 8, slot unused, `Conn` valid and not NONE, else `GW_CONN_ADD_ERR_EID`; with `Enabled` 1 opened at once, and an open failure rejects the command leaving the slot unused; success `GW_CONN_ADD_INF_EID` |
| 3 | REMOVE_CONN | `GW_ConnIndex_Payload_t { uint8 Index; uint8 Spare[3]; }` | slot used, else `GW_CONN_REMOVE_ERR_EID`; closes and frees it, `GW_CONN_REMOVE_INF_EID` |
| 4 | ENABLE_CONN | `GW_ConnIndex_Payload_t` | slot used and disabled and opening succeeds, else `GW_CONN_ENABLE_ERR_EID`; success `GW_CONN_ENABLE_INF_EID` |
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
26 VALTBL_ERR, 27 VALTBL_INF.

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
CMDI frames both ways; a SERIAL connection on a PTY pair created by the
acceptance environment carries marker-framed CMDI frames both ways,
including a frame split across writes, garbage before a marker and a
frame with a bad MAC,
while GW keeps sending its heartbeat once per second (SFI reports no miss
for GW) and answers housekeeping requests with nothing pending on the PTY; GW is stopped with ES STOP_APP and started with ES
START_APP after a SERIAL connection was used, and a new SERIAL connection
on a fresh PTY pair then carries traffic both ways; DISABLE, ENABLE,
REMOVE, RELOAD, the error cases, the loop rule, HK and the status packet
behave as specified.

## S6. SFI system fault identifier

SFI watches application heartbeats and selected housekeeping counters and
reports faults and system-level metrics to the ground as events, HK and a
status packet.

### S6.1 Table

```c
#define SFI_MAX_APPS    16
#define SFI_MAX_WATCHES 16
#define SFI_HISTORY_LEN 16

typedef enum { SFI_Rule_UNUSED = 0, SFI_Rule_STALL = 1, SFI_Rule_RATE = 2 } SFI_Rule_Enum_t;
typedef struct { char AppName[CFE_MISSION_MAX_API_LEN]; } SFI_AppDef_t; /* empty = unused */

typedef struct
{
    CFE_SB_MsgId_t MsgId;
    uint16         ByteOffset; /* from the first byte of the packet (primary header) */
    uint8          Width;      /* 1, 2 or 4 bytes, host byte order */
    uint8          Rule;       /* SFI_Rule_Enum_t */
    uint32         Limit;      /* STALL: packets without change; RATE: maximum increase per packet */
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
} SFI_Table_t;
```

Validation rejects values outside these ranges, unterminated or duplicate
app names, and a used watch with width not 1, 2 or 4, `Limit` 0, or
`ByteOffset` + `Width` above 1024. Default: N 3, W 5, H 60; apps `STOR`,
`CMDI`, `TLMO`, `GW` and the verification names `SFI_T1`, `SFI_T2`,
`SFI_T3`; watches (MID, offset, width, rule, limit): `STOR_HK_TLM_MID`,
`CMDI_HK_TLM_MID`, `TLMO_HK_TLM_MID` and `GW_HK_TLM_MID` each 17, 1, RATE,
2; reserved MID 0xEC (`CFE_PLATFORM_CMD_TOPICID_TO_MIDV(0xEC)`, 0x18EC on
cpu1) 16, 4, STALL, 3; reserved MID 0xED (0x18ED on cpu1) 16, 2, RATE, 5.
Offset 16 of a verification packet is its payload byte 8 (C.3).

### S6.2 Heartbeats

- SFI subscribes to `SFI_HEARTBEAT_MID` with a message limit of 32. A
  heartbeat naming a table app marks it heard (and armed, the first time),
  `TotalHeartbeats`++. Other names count in `UnknownHeartbeats` with
  `SFI_HB_UNKNOWN_DBG_EID` and are otherwise ignored.
- Every SFI WAKEUP (1 Hz), for each armed app: heard since the previous
  wakeup sets `MissedCycles` to 0, otherwise `MissedCycles`++; the heard
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

/* Onsets in any order, Count 0..SFI_HISTORY_LEN; the caller filters recency. */
void SFI_ComputeClusters(const CFE_TIME_SysTime_t *Onsets, uint16 Count, uint16 WindowSec,
                         SFI_ClusterResult_t *Result);
```

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
- A watch in FAULT returns to OK on the first packet that does not trigger
  its rule (a changed value; a delta at most `Limit`), with
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

### S6 Acceptance

A hidden unit test calls `SFI_ComputeClusters` on chosen onset sets (empty,
single, equal times, gaps of exactly W and just above W, unsorted input);
hidden coverage tests exercise arming, the miss and restore transitions,
the STALL and RATE rules including counter reset, and the commands. Live,
acceptance runs its own headless checks against your build, with every
command sent as a CMDI frame. On cpu1: the four real apps are OK after
startup with no misses; stopping GW with the cFE ES STOP_APP command gives
one miss event naming GW within N + 2 seconds, `AppsMissing` 1 and an
onset time in the status packet, and starting it again with ES START_APP
(`/cf/gw.so`, entry `GW_AppMain`, priority 70, stack 131072) restores it;
heartbeats injected as CMDI frames for `SFI_T1`, `SFI_T2` and `SFI_T3`,
then stopped for two of them at once, give a cluster of size 2 and
CLUSTERED, and after CLEAR_HISTORY two misses more than W apart give
UNCORRELATED; verification packets injected as CMDI frames on reserved
MIDs 0xEC and 0xED raise and clear the STALL and RATE faults.

## S7. System-level tests in CTF

Deliver the system-level tests as CTF test scripts under `ctf/tests/`, one
script per demonstrated behaviour, run with `ctf/run_ctf.sh` (CTF v1.11 on
Python 3.12 with this fixture's shims, set up by `ctf/setup_ctf.sh`; see
`ctf/README.md`) against a tree built with `make native_std.install`. Add
CCDD definitions for every new app's commands and telemetry, and, where CTF
lacks a capability the tests need, add it as a CTF plugin or as an update
to an existing CTF module (S7.1). No standalone Python system scripts are
required; acceptance runs its own headless checks against your build.

### S7.1 Where CTF code goes

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

### S7.2 Two nodes in one CTF run

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
  S7.3. The existing `[mission]` section stays, equivalent to `[cpu1]`, so
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

### S7.3 Required capabilities, plugins and instructions

The plugin directory names, class names and instruction names below are
pinned so that reviewers can find them; the `data` keys listed are the
minimum each instruction accepts. Every instruction that talks to a node
takes `target` (`cpu1`, `cpu2` or `mission`).

| Capability | Plugin (`ctf/plugins/<dir>`, class) | Instructions |
|------------|-------------------------------------|--------------|
| Build and send CMDI frames from the table key | `cmdi_plugin`, `CmdiPlugin` | `SendCmdiCommand`, `SendCmdiRawPacket` |
| Drive two nodes in one test | CTF's own cfs plugin, configured as in S7.2 | CTF's `RegisterCfs`, `StartCfs`, `ShutdownCfs`, `CheckTlmValue`, `CheckEvent`, ... with `target` |
| Record telemetry arrival order on a node's TLMO port | `tlm_order_plugin`, `TlmOrderPlugin` | `StartTlmRecording`, `CheckTlmOrder`, `StopTlmRecording` |
| UDP and TCP endpoints and PTY pairs facing GW, carrying CMDI frames with stream framing | `link_plugin`, `LinkPlugin` | `OpenUdpLink`, `OpenTcpServer`, `WaitLinkConnected`, `OpenPtyPair`, `SendLinkCmdiCommand`, `SendLinkPacket`, `CheckLinkPacket`, `CheckNoLinkPacket`, `CloseLink` |
| Stop and start an app through ES commands and wait on events | `es_app_plugin`, `EsAppPlugin` | `StopApp`, `StartApp`, `WaitForEvent` |
| Check and compare files in a node's run directory | `node_file_plugin`, `NodeFilePlugin` | `CheckNodeFile`, `CompareNodeFiles`, `RemoveNodeFile` |

**`cmdi_plugin`.** CTF's `SendCfsCommand` sends unframed packets, which
CMDI rejects, so every command in these tests goes through this plugin.

- `SendCmdiCommand`: `target`, `mid`, `cc`, `args`, with the same names and
  argument encoding as CTF's `SendCfsCommand` (from the target's CCDD
  definitions, macros and `$variables$` resolved); optional `source_id`,
  `key_hex`, `time_offset` (seconds added to the onboard time, may be
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

**Two nodes.** No new instruction: the configuration of S7.2 plus
`target` on every instruction, CTF's and the plugins'. If CTF's cfs plugin
needs a change to run two local targets side by side, it is a patch
(S7.1).

**`tlm_order_plugin`.** CTF's telemetry listener owns the target's
telemetry port and drops packets whose MID has no telemetry definition,
including command-type verification packets, and it keeps no order across
MIDs. This plugin taps the raw datagrams in arrival order before that
filtering (which may need a patch to CTF's listener, S7.1). CTF's listener
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

**`link_plugin`.** Endpoints on the test's side of a GW connection, each
known by `name`. GW is a TCP client (S5.2), so the plugin plays the
server.

- `OpenUdpLink`: `name`, `local_port`, `remote_port`; a UDP socket bound to
  127.0.0.1:`local_port` that sends to 127.0.0.1:`remote_port`.
- `OpenTcpServer`: `name`, `port`; listens on 127.0.0.1:`port` and accepts
  GW's connection in the background (again after GW reconnects).
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

### S7.4 CCDD definitions

Under `ctf/ccdd/json/`, following `ctf/ccdd/README.md`: every command
message of STOR, CMDI, TLMO, GW and SFI with all its function codes
(including the `SEND_HK` and `WAKEUP` MIDs, and `SFI_HEARTBEAT_MID`,
`STOR_XFER_SEG_MID` and `STOR_XFER_ACK_MID`), every telemetry message
(HK and the other packets of C.3), their MIDs in both `set1` and `set2`,
and the two ES commands of S7.3. Add probe lines for every new message to
`ctf/ccdd/tools/layout_probe.c` (and include paths to `probe_layout.sh`)
so that `ccdd/tools/probe_layout.sh | ccdd/tools/check_layout.py` covers
them, and make that check pass. The `ci_lab_*` and `to_lab_*` definitions
may stay.

### S7.5 Test scripts and run order

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
| 5 | `st_gw_tcp.json` | a GW TCP connection to the test's TCP server reaches UP (`GW_CONN_UP_INF_EID`, `ConnectionsUp`), egresses a verification packet on a subscribed reserved MID as a marker-framed CMDI frame, and ingests a SAMPLE_APP NOOP sent with `SendLinkCmdiCommand`, which executes |
| 6 | `st_gw_serial.json` | a GW SERIAL connection on a PTY pair opened by the test egresses a verification packet as a marker-framed CMDI frame and ingests a SAMPLE_APP NOOP sent with `SendLinkCmdiCommand` split across writes after garbage bytes, which executes |
| 7 | `st_sfi_heartbeat.json` | stopping GW with ES STOP_APP makes SFI report GW missing (event and `AppsMissing` 1); starting it again with ES START_APP (`/cf/gw.so`, `GW_AppMain`, priority 70, stack 131072) restores it |
| 8 | `st_stor_transfer.json` | with cpu1 and cpu2 running, GW UDP connections on both nodes carry `STOR_XFER_SEG_MID` from cpu1 to cpu2 and `STOR_XFER_ACK_MID` back, as CMDI frames each GW validates; a file added on cpu1 is transferred to node 2 with DELETE; it is byte-identical on cpu2, gone on cpu1, and `XfersCompleted` is 1 |

`st_gw_commands.json` is the single CTF test an earlier revision of this
task required as `gw_udp.json`. The two fixture scripts
`ctf/tests/smoke_sample_app.json` and `smoke_housekeeping.json` send
unframed commands and enable TO_LAB, which no longer runs: port them to
`cmdi_plugin` (and drop the TO_LAB enable step) so that every script in
`ctf/tests/` passes against your build; `run_system_tests.sh` runs them
before the eight above. Update `ctf/README.md` for the CMDI command path,
the always-on TLMO output, the two targets and the plugins.

### S7 Acceptance

On a clean build of your repository, `ctf/setup_ctf.sh` creates a fresh
CTF checkout and venv (applying your patches), the CCDD layout check of
S7.4 is run and must pass, and CTF's plugin listing
(`ctf --pluginInfo`) must name every pinned plugin instruction of S7.3.
Each script of S7.5, and each ported smoke script, is then run by name
with `ctf/run_ctf.sh`, in a private network namespace with nothing else
running, with `CFS_RUN_DIR` and `CFS_RUN_DIR_CPU2` pointing at scratch
copies of the installed trees and a 300-second limit per script, and must
pass; `ctf/run_system_tests.sh` must exist and run them in order. The
scripts, plugins and patches are also read in human review for whether
they test what the table says. Acceptance runs its own headless checks
against your build for the live behaviour of S1 to S6; those do not use
your CTF code.

## N. Non-functional requirements

- Build: `make native_std.prep && make -j16 native_std.install` with no
  warnings (`-Werror`); `make -j16 native_std.runtest` passes (CI_LAB's
  and TO_LAB's own unit tests are no longer built, so the test count
  differs from the mission README's). Existing apps, including
  `apps/ci_lab` and `apps/to_lab`, are unchanged; the files you change
  outside the new apps and library are those of C.6, the run scripts of
  C.1, `tools/udp_harness.py` (S3.5) and the CTF files of S7.
- Unit tests: one coverage test file per source module of each new app in
  `apps/<app>/unit-test/coveragetest/coveragetest_<module>.c` (the build
  requires it) and for the library in `libs/cmdval_lib/unit-test/coveragetest/`.
  Provide `ut-stubs` for `cmdval_lib` so CMDI's and GW's tests can stub it.
- Coverage: line coverage of every source file of `stor`, `cmdi`, `tlmo`,
  `gw`, `sfi` and `cmdval_lib` at least 95 percent (the project convention
  is 100), measured from the gcov data the unit-test build produces, for
  example `gcov --json-format` on the coverage objects under
  `build-native_std`; lcov is not installed. This covers all of CMDI's
  and TLMO's sources, including the code they inherited from CI_LAB and
  TO_LAB.
- Design notes: `DESIGN.md` in each of `apps/stor`, `apps/cmdi`,
  `apps/tlmo`, `apps/gw`, `apps/sfi` and `libs/cmdval_lib`, at most 80 lines
  each: purpose, data flow, main data structures, timing (what runs on which
  wakeup), error handling, and the decisions you made where this document
  left a choice.
- Report at `docs/reports/system.md` with the sections `Design summary`,
  `Interfaces`, `Unit tests` (counts per app and library), `Coverage` (per
  source file), `System tests` (results of `ctf/run_system_tests.sh`, and
  the CTF plugins and patches you added) and `Limitations`.
- Commit the work.

### N Acceptance

The build and the full unit-test run are checked; coverage is measured per
source file from the build's gcov data; the design notes and the report are
checked for presence, length and sections, and are read in human review.

## Acceptance summary

Each subsystem (S1 to S7, and N) is scored on its own Acceptance paragraph;
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
The live checks of S1 to S6 are acceptance's own headless checks and use
GW only in S5; S7 runs your CTF scripts. Feedback on a failing check names
the subsystem and the term of this document involved.
