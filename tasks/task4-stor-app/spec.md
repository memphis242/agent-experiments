# Task: STOR, a storage management application

## Background

The mission needs an onboard storage manager, STOR, that records selected
software-bus messages (telemetry or commands) into packet files under a
configurable telemetry directory, with per-message priority and decimation,
and that manages finite space by purging low-priority data above a
configurable threshold. STOR also offers a separate file area with
add, delete and list operations that simply fills until full. Build it as a
new cFS application in `apps/stor` following the conventions of
`apps/sample_app` (v7 `config/` layout, dispatch, events, coverage tests),
register it in the mission, exercise it with CTF, and report.

NASA's DS and FM apps in this tree solve related problems; you may study
them, but STOR is a new app with the interfaces below.

## Naming

App name `STOR`, prefix `STOR_`, entry point `STOR_AppMain`, directory
`apps/stor` with `fsw/src`, `fsw/inc`, `fsw/tables`, `config/`,
`unit-test/`. Event IDs, function codes, topic IDs and structures are pinned
below; internal function names are yours.

Topics (`fsw/inc/stor_topicids.h`, defaults): commands `CMD` 0xC4,
`SEND_HK` 0xC5, `WAKEUP` 0xC6; telemetry `HK_TLM` 0xC4, `LIST_TLM` 0xC5,
`STATS_TLM` 0xC6. Message IDs `STOR_CMD_MID`, `STOR_SEND_HK_MID`,
`STOR_WAKEUP_MID`, `STOR_HK_TLM_MID`, `STOR_LIST_TLM_MID`,
`STOR_STATS_TLM_MID`.

## Configuration table

Table name `StorageTable`, file `/cf/stor_tbl.tbl`, default source
`fsw/tables/stor_tbl.c`, type in `config/default_stor_tbldefs.h`:

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

Validation (table validation function) rejects: empty or unterminated
roots; zero quotas; `MaxPacketFileBytes` below 1024 or above
`TelemetryQuotaBytes`; threshold outside 1..100; a used entry with
`Priority` 0 or `Decimation` 0; duplicate used `MsgId`s.

Default table contents: roots `/cf/stor/tlm` and `/cf/stor/files`,
`TelemetryQuotaBytes` 262144, `FileQuotaBytes` 1048576,
`MaxPacketFileBytes` 16384, threshold 80, entries:
`CFE_EVS_HK_TLM_MID` priority 10 decimation 1, `CFE_ES_HK_TLM_MID`
priority 20 decimation 1, `SAMPLE_APP_HK_TLM_MID` priority 30
decimation 2, `CFE_TIME_HK_TLM_MID` priority 40 decimation 1.

On table load (initial or update) STOR subscribes to every used entry's
`MsgId`, unsubscribes from entries no longer present, and resets the
runtime copies of priority, decimation and threshold to the table values.

## Telemetry and command storage

- Startup: create both roots if missing (`OS_mkdir`, parents must exist or
  be created one level at a time), scan them, and initialize
  `TelemetryBytesUsed` and `FileBytesUsed` from the sizes of regular files
  found (packet files for the telemetry area; all files for the file area).
- For every received packet whose MID is a used entry: `Received`++ for that
  entry; store it when `(Received - 1) % Decimation == 0`, otherwise
  `Decimated`++ and `PacketsDecimated`++.
- Storing appends the full packet (headers and payload, as received) to the
  entry's open packet file. Packet files are named
  `<TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin`, where MMMM is the four-digit
  upper-case hex MID value and NNNNNNNN the zero-padded decimal sequence,
  starting at 0 at app start and continuing from the highest existing
  sequence found at startup plus one. Each file starts with a
  `CFE_FS_Header_t` written with `CFE_FS_WriteHeader`, sub type
  `STOR_FILE_SUBTYPE` = 0x5354, description "STOR packet file".
- Roll over: if appending would make the file exceed `MaxPacketFileBytes`,
  close it and open the next sequence before writing.
- Quota: if storing the packet would make `TelemetryBytesUsed` exceed
  `TelemetryQuotaBytes` after purging, the packet is dropped: `Dropped`++
  for the entry, `PacketsDropped`++, and event `STOR_TLM_QUOTA_ERR_EID`
  once per entry until it successfully stores again.
- Purge: after every store, and on every WAKEUP, while `TelemetryBytesUsed`
  exceeds `PurgeThresholdPercent` percent of `TelemetryQuotaBytes`, delete
  one closed packet file: the one whose MID has the lowest current
  priority, lowest sequence first; among equal priorities, the oldest
  sequence. Open files are never purged. Each deletion: `FilesPurged`++,
  bytes subtracted. Each purge pass that deletes at least one file:
  `PurgeCycles`++ and one event `STOR_PURGE_INF_EID` with the count. If
  nothing can be purged, stop.
- WAKEUP (1 Hz): run the purge check and close any packet file that has not
  been written for 10 seconds or more.
- The runtime priority, decimation and threshold can be changed by command
  (below) without a table update; a table update overrides them.

## File area

- Paths given in commands are relative to `FileRoot`; a path containing
  `..` or starting with `/` is rejected with `STOR_PATH_ERR_EID`.
- `ADD_FILE { char SourcePath[STOR_PATH_LEN]; char DestPath[STOR_PATH_LEN]; }`:
  copy the source (an absolute onboard path) to `<FileRoot>/<DestPath>`,
  creating intermediate directories. Rejected, with `CommandErrorCounter`++
  and no partial file left behind, when: the source cannot be opened
  (`STOR_FILE_SRC_ERR_EID`), the destination exists
  (`STOR_FILE_EXISTS_ERR_EID`), or `FileBytesUsed` plus the source size
  exceeds `FileQuotaBytes` (`STOR_FILE_QUOTA_ERR_EID`, `FileAddsRejected`++).
  Success: `FileCount`++, bytes added, event `STOR_FILE_ADD_INF_EID`.
- `DELETE_FILE { char Path[STOR_PATH_LEN]; }`: delete a regular file;
  missing or a directory is an error (`STOR_FILE_DEL_ERR_EID`). Success:
  `FileCount`--, bytes subtracted, `STOR_FILE_DEL_INF_EID`.
- `LIST_DIR { char Path[STOR_PATH_LEN]; }`: list the immediate entries of
  `<FileRoot>/<Path>` (empty path = root), directories with a trailing `/`
  and size 0, in `OS_DirectoryRead` order.
- `LIST_ALL` (no payload): list every regular file under `FileRoot`
  recursively, names as paths relative to `FileRoot`, in depth-first
  directory order.
- Listing output: one or more `STOR_ListTlm_t` packets:

  ```c
  #define STOR_LIST_ENTRIES_PER_PKT 4

  typedef struct { char Name[STOR_PATH_LEN]; uint32 Size; uint32 Spare; } STOR_ListEntry_t;

  typedef struct
  {
      uint16           TotalEntries;    /* in the whole listing */
      uint16           PacketIndex;     /* 0-based */
      uint16           EntriesInPacket; /* 0..4 */
      uint16           Spare;
      STOR_ListEntry_t Entries[STOR_LIST_ENTRIES_PER_PKT];
  } STOR_List_Payload_t;
  ```

  An empty directory yields one packet with `TotalEntries` 0. A missing
  directory is an error (`STOR_LIST_ERR_EID`). Success events:
  `STOR_LIST_INF_EID` with the entry count.

## Commands

`config/default_stor_fcncode_values.h` and `fsw/inc/stor_fcncodes.h`:

| Code | Name | Payload |
|------|------|---------|
| 0 | NOOP | none |
| 1 | RESET_COUNTERS | none |
| 2 | SET_PRIORITY | `{ CFE_SB_MsgId_t MsgId; uint8 Priority; uint8 Spare[3]; }` |
| 3 | SET_DECIMATION | `{ CFE_SB_MsgId_t MsgId; uint8 Decimation; uint8 Spare[3]; }` |
| 4 | SET_PURGE_THRESHOLD | `{ uint8 Percent; uint8 Spare[3]; }` |
| 5 | ADD_FILE | as above |
| 6 | DELETE_FILE | as above |
| 7 | LIST_DIR | as above |
| 8 | LIST_ALL | none |
| 9 | SEND_STATS | none |
| 10 | CLOSE_FILES | none |

Command types are named `STOR_<Name>Cmd_t` in CamelCase
(`STOR_NoopCmd_t`, `STOR_SetPriorityCmd_t`, `STOR_AddFileCmd_t`, ...),
payloads `STOR_<Name>_Payload_t`. SET_PRIORITY and SET_DECIMATION reject
value 0 or an unknown MsgId (`STOR_SET_PARAM_ERR_EID`); success
`STOR_SET_PARAM_INF_EID`. SET_PURGE_THRESHOLD accepts 1..100
(`STOR_SET_PARAM_ERR_EID` otherwise) and triggers a purge check.
CLOSE_FILES closes every open packet file (`STOR_CLOSE_INF_EID` with the
count). SEND_STATS transmits one `STOR_StatsTlm_t`:

```c
typedef struct
{
    CFE_SB_MsgId_t MsgId; uint8 Priority; uint8 Decimation; uint16 Spare;
    uint32 Received; uint32 Stored; uint32 Decimated; uint32 Dropped;
} STOR_EntryStats_t;

typedef struct { STOR_EntryStats_t Entries[STOR_MAX_ENTRIES]; } STOR_Stats_Payload_t;
```

RESET_COUNTERS zeroes `CommandCounter`, `CommandErrorCounter`, all per-entry
counters, `PacketsStored`, `PacketsDecimated`, `PacketsDropped`,
`FilesPurged`, `PurgeCycles` and `FileAddsRejected`. It does not touch
byte accounting, file counts, or runtime parameters. Bad command code and
bad length follow the sample_app pattern (`STOR_CC_ERR_EID`,
`STOR_CMD_LEN_ERR_EID`); bad MID `STOR_MID_ERR_EID`.

## Housekeeping telemetry

`STOR_HkTlm_Payload_t`, sent on SEND_HK, in this order:

```c
uint8  CommandCounter; uint8 CommandErrorCounter;
uint8  PurgeThresholdPercent; uint8 Spare;
uint32 TelemetryBytesUsed; uint32 TelemetryQuotaBytes;
uint32 FileBytesUsed;      uint32 FileQuotaBytes;
uint32 PacketsStored; uint32 PacketsDecimated; uint32 PacketsDropped;
uint32 FilesPurged;   uint32 PurgeCycles;
uint16 FileAddsRejected; uint16 FileCount;
uint16 ActiveEntries;    uint16 OpenPacketFiles;
```

## Event IDs (`fsw/inc/stor_eventids.h`)

1 INIT_INF, 2 CC_ERR, 3 NOOP_INF, 4 RESET_INF, 5 MID_ERR, 6 CMD_LEN_ERR,
7 PIPE_ERR, 8 TBL_REG_ERR, 9 TBL_LOAD_ERR, 10 TBL_VAL_ERR, 11 SUB_ERR,
12 DIR_CREATE_ERR, 13 TLM_QUOTA_ERR, 14 PURGE_INF, 15 FILE_WRITE_ERR,
16 SET_PARAM_INF, 17 SET_PARAM_ERR, 18 PATH_ERR, 19 FILE_SRC_ERR,
20 FILE_EXISTS_ERR, 21 FILE_QUOTA_ERR, 22 FILE_ADD_INF, 23 FILE_DEL_ERR,
24 FILE_DEL_INF, 25 LIST_ERR, 26 LIST_INF, 27 CLOSE_INF, 28 STATS_INF.
Macro names are `STOR_<NAME>_EID`. ERR events are ERROR type, INF events
INFORMATION, except PURGE_INF which is DEBUG.

## Mission integration

- Add `stor` to `MISSION_GLOBAL_APPLIST` in `sample_defs/targets.cmake` so it
  starts on cpu1 (follow how `hk` is listed and started).
- `sample_defs/tables/sch_lab_table.c`: `STOR_SEND_HK_MID` every 4.9 s,
  `STOR_WAKEUP_MID` every 1.0 s.
- `sample_defs/tables/to_lab_sub.c`: `STOR_HK_TLM_MID`, `STOR_LIST_TLM_MID`,
  `STOR_STATS_TLM_MID`.

## Non-functional requirements

- Build with no warnings (`-Werror`); `make -j native_std.runtest` passes.
- One coverage test file per source module in
  `apps/stor/unit-test/coveragetest/` (the build requires it); line coverage
  of every STOR source file per `make native_std.lcov` at least 95 percent
  (the project convention is 100).
- CTF definitions for all STOR commands and telemetry under `ctf/ccdd/json/`
  and a test `ctf/tests/stor_basic.json` covering: NOOP, a SET_PRIORITY
  error on an unknown MID, ADD_FILE of `/cf/stor_tbl.tbl` as `t/copy.tbl`
  followed by LIST_ALL showing one entry, DELETE_FILE of it, and a SEND_STATS
  showing `CFE_EVS_HK_TLM_MID` with `Received` greater than 0 after a few
  seconds. Make it pass with `ctf/run_ctf.sh`.
- Report at `docs/reports/stor.md`: design summary, file layout, unit test
  counts, coverage figures, CTF results, limitations.
- Commit the work.

## Acceptance

Build and unit tests are run; hidden coverage tests compiled against the
names above exercise table validation, decimation, roll-over, purge order,
quota drops, path rejection and the listing packets; cFS is started and
driven over UDP: packets are injected at a chosen MID to fill the telemetry
quota and the resulting files, purge order, housekeeping counters and stats
are checked, then the file-area commands are exercised including quota
rejection; your CTF test is executed; the report is checked.
