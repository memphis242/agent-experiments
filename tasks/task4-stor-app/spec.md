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
them, but STOR is a new app with the interfaces below. Where this
specification and sample_app differ, this specification wins.

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

All structures below are in native byte order with natural alignment (no
packing pragmas).

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
    uint32          MaxPacketFileBytes;           /* a packet file never exceeds this; exactly reaching it is allowed */
    uint8           PurgeThresholdPercent;        /* 1..100, of TelemetryQuotaBytes */
    uint8           Spare[3];
    STOR_EntryDef_t Entries[STOR_MAX_ENTRIES];
} STOR_StorageTable_t;
```

Validation (table validation function) rejects exactly these cases and
accepts every other table: empty or unterminated roots; zero quotas;
`MaxPacketFileBytes` below 1024 or above `TelemetryQuotaBytes`; threshold
outside 1..100; a used entry with `Priority` 0 or `Decimation` 0; duplicate
used `MsgId`s. It may also reject a root too long to form OSAL paths (the
root plus `/` plus a file name must fit in `OS_MAX_PATH_LEN`).

Default table contents: roots `/cf/stor/tlm` and `/cf/stor/files`,
`TelemetryQuotaBytes` 262144, `FileQuotaBytes` 1048576,
`MaxPacketFileBytes` 16384, threshold 80, entries:
`CFE_EVS_HK_TLM_MID` priority 10 decimation 1, `CFE_ES_HK_TLM_MID`
priority 20 decimation 1, `SAMPLE_APP_HK_TLM_MID` priority 30
decimation 2, `CFE_TIME_HK_TLM_MID` priority 40 decimation 1.

On table load (initial or update) STOR subscribes to every used entry's
`MsgId` (with `CFE_SB_Subscribe`, the default message limit), unsubscribes
from entries no longer present, and resets the runtime copies of priority,
decimation and threshold to the table values. STOR uses a single pipe,
of depth at least 32, for its commands and the messages it stores. On an update, an entry whose `MsgId` is still present keeps its
counters, open file and sequence; a removed entry's open file is closed and
its counters discarded; new entries start at 0. Quotas, `MaxPacketFileBytes`
and the threshold apply immediately; changing the roots by a table update
need not be supported.

STOR checks for a table update (`CFE_TBL_Manage`, then
`CFE_TBL_GetAddress`; either one reporting `CFE_TBL_INFO_UPDATED` means a
new table is active) at least once per SEND_HK cycle, for example on every
WAKEUP or in the SEND_HK handler. A table update takes effect no later than
the second SEND_HK after the table is activated: the housekeeping that
SEND_HK produces already reflects it. Release the table address
(`CFE_TBL_ReleaseAddress`) after each use, as sample_app does.

## Telemetry and command storage

- Startup: create both roots if missing, one directory level at a time
  with `OS_mkdir` (an already existing directory is not an error), then
  scan them. `TelemetryBytesUsed` is initialized to the total size of the
  packet files directly in `TelemetryRoot` (not recursive);
  `FileBytesUsed` and `FileCount` to the total size and number of all
  regular files under `FileRoot`, recursively. Every directory traversal
  (this scan, LIST_DIR, LIST_ALL) skips the `.` and `..` entries that
  `OS_DirectoryRead` returns. No purge runs during initialization.
- A packet file is a regular file directly in `TelemetryRoot` whose name
  matches `tlm_<MMMM>_<NNNNNNNN>.bin` exactly; its contents are not
  inspected. Other files and directories in `TelemetryRoot` are ignored by
  accounting and purge. STOR creates nothing in `TelemetryRoot` except
  packet files (no index, temporary or state files, no subdirectories).
- For every received packet whose MID is a used entry: `Received`++ for that
  entry; store it when `(Received - 1) % Decimation == 0`, otherwise
  `Decimated`++ and `PacketsDecimated`++.
- Storing appends the full packet (headers and payload, as received) to the
  entry's open packet file. Packet files are named
  `<TelemetryRoot>/tlm_<MMMM>_<NNNNNNNN>.bin`, where MMMM is the four-digit
  upper-case hex MID value and NNNNNNNN the zero-padded decimal sequence,
  per MID, starting at 0 on a clean directory and continuing from the
  highest existing sequence found for that MID at startup plus one. These
  names are 21 characters, longer than OSAL's default 20-character file name
  limit, so set `OSAL_CONFIG_MAX_FILE_NAME` to 32 in
  `sample_defs/default_osconfig.cmake`. Each file starts with a
  `CFE_FS_Header_t` written with `CFE_FS_WriteHeader`, sub type
  `STOR_FILE_SUBTYPE` = 0x5354, description "STOR packet file".
- A packet file, once closed (roll-over, CLOSE_FILES, idle close or table
  update), is never reopened: the next packet stored for that MID creates a
  new file with the next sequence number.
- Roll over: if appending would make the file exceed `MaxPacketFileBytes`,
  close it first; the packet then goes to a new file (next sequence),
  subject to the quota rule. A packet that cannot fit even in an empty file
  (header plus packet larger than `MaxPacketFileBytes`) is dropped without
  closing the open file: `STOR_FILE_WRITE_ERR_EID`, `Dropped`++,
  `PacketsDropped`++.
- `TelemetryBytesUsed` is the total size on disk of the packet files,
  headers included.
- Quota: a packet that is due to be stored (not decimated) is dropped when
  `TelemetryBytesUsed` as it stands when the packet arrives, plus the packet
  size, plus `sizeof(CFE_FS_Header_t)` if the packet would start a new file
  (first packet for the entry, after a close, or on roll-over), would exceed
  `TelemetryQuotaBytes`. The quota check never deletes files; space is freed
  only by the purge rule. A dropped packet never creates a file. On a drop:
  `Dropped`++ for the entry, `PacketsDropped`++, and `STOR_TLM_QUOTA_ERR_EID`
  on the entry's first drop and not again for that entry until a packet of
  that entry is stored.
- Purge: while `TelemetryBytesUsed > (uint64)TelemetryQuotaBytes *
  PurgeThresholdPercent / 100` (integer division), delete one closed packet
  file: the first in ascending order of (current priority of the file's MID,
  sequence number, MID value). A packet file whose MID is not a used entry
  has priority 0. Open files are never purged. Each deletion: `FilesPurged`++,
  bytes subtracted. Each purge pass that deletes at least one file:
  `PurgeCycles`++ and one event `STOR_PURGE_INF_EID` with the count. If
  nothing can be purged, stop. A purge pass runs after every store, after
  SET_PRIORITY, SET_PURGE_THRESHOLD and CLOSE_FILES, after a table update,
  and on every WAKEUP, and at no other time.
- WAKEUP (1 Hz): table check (if done on WAKEUP), purge pass, then close
  every packet file whose last write is 10 seconds or more ago (a file
  closed this way becomes purgeable at the next pass). Measure idle time
  with cFE time (any `CFE_TIME_*` call), never host clocks.
- Write failures: if a packet file cannot be created or its header cannot
  be written, `STOR_FILE_WRITE_ERR_EID`; any partly created file is removed,
  the sequence number is not consumed, and the packet counts as neither
  stored nor dropped. If a packet write fails (`OS_write` returns anything
  other than the packet size), `STOR_FILE_WRITE_ERR_EID`, the file is
  closed, the packet counts as neither stored nor dropped, and
  `TelemetryBytesUsed` includes whatever bytes reached the file.
- The runtime priority, decimation and threshold can be changed by command
  (below) without a table update; a table update overrides them.

## File area

- Paths given in commands are relative to `FileRoot`. A path field with no
  NUL within `STOR_PATH_LEN` bytes, or a relative path that is empty (except
  for LIST_DIR), starts with `/`, or contains the substring `..`, is
  rejected with `STOR_PATH_ERR_EID`. An unterminated `SourcePath` is also
  rejected with `STOR_PATH_ERR_EID`.
- `ADD_FILE { char SourcePath[STOR_PATH_LEN]; char DestPath[STOR_PATH_LEN]; }`:
  copy the source (an absolute onboard path) to `<FileRoot>/<DestPath>`,
  creating intermediate directories. Checks in this order, each rejection
  with `CommandErrorCounter`++ and no file or directory left behind: path
  (`STOR_PATH_ERR_EID`); the source cannot be opened or is a directory
  (`STOR_FILE_SRC_ERR_EID`); the destination exists
  (`STOR_FILE_EXISTS_ERR_EID`); `FileBytesUsed` plus the source size
  exceeds `FileQuotaBytes` (`STOR_FILE_QUOTA_ERR_EID`, `FileAddsRejected`++).
  Directories are created only after all checks pass. A directory-creation
  or copy failure sends `STOR_FILE_WRITE_ERR_EID`, removes any partial
  destination and counts as a command error. `FileAddsRejected` counts only
  quota rejections. Success: `FileCount`++, bytes added, event
  `STOR_FILE_ADD_INF_EID`.
- `DELETE_FILE { char Path[STOR_PATH_LEN]; }`: delete a regular file;
  missing or a directory is an error (`STOR_FILE_DEL_ERR_EID`). Success:
  `FileCount`--, bytes subtracted, `STOR_FILE_DEL_INF_EID`.
- `LIST_DIR { char Path[STOR_PATH_LEN]; }`: list the immediate entries of
  `<FileRoot>/<Path>` (empty path = root) in `OS_DirectoryRead` order. Names
  are the bare entry names (no directory prefix); directories have a
  trailing `/` and size 0.
- `LIST_ALL` (no payload): list every regular file under `FileRoot`
  recursively, names as paths relative to `FileRoot`, in depth-first
  pre-order: a directory's files in `OS_DirectoryRead` order, descending
  into each subdirectory at its position.
- Listing output: one or more `STOR_ListTlm_t` packets, each the full
  `sizeof(STOR_ListTlm_t)` (entries beyond `EntriesInPacket` zero-filled):

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
  path, or a path that is not a directory, is an error
  (`STOR_LIST_ERR_EID`) and sends no packet. Each successful listing
  command sends exactly one `STOR_LIST_INF_EID` with the entry count.

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

Only commands on `STOR_CMD_MID` change `CommandCounter`: every accepted one
increments it (except RESET_COUNTERS, below); every rejected one increments
`CommandErrorCounter` and sends the named error event, and nothing else
changes (except `FileAddsRejected` as stated for ADD_FILE). Command types
are named `STOR_<Name>Cmd_t` in CamelCase (`STOR_NoopCmd_t`,
`STOR_SetPriorityCmd_t`, `STOR_AddFileCmd_t`, ...), payloads
`STOR_<Name>_Payload_t`.

- SET_PRIORITY and SET_DECIMATION reject value 0 or an unknown MsgId
  (`STOR_SET_PARAM_ERR_EID`); success `STOR_SET_PARAM_INF_EID`.
- SET_PURGE_THRESHOLD accepts 1..100: it sets the runtime threshold, sends
  `STOR_SET_PARAM_INF_EID`, then runs a purge pass. Other values are
  rejected with `STOR_SET_PARAM_ERR_EID`.
- CLOSE_FILES closes every open packet file (`STOR_CLOSE_INF_EID` with the
  count), then runs a purge pass, so files it closed are deleted at once if
  `TelemetryBytesUsed` is above the threshold.
- SEND_STATS transmits one `STOR_StatsTlm_t`, then sends
  `STOR_STATS_INF_EID`. It holds one element per used entry, in any order;
  the remaining elements are all zero:

  ```c
  typedef struct
  {
      CFE_SB_MsgId_t MsgId; uint8 Priority; uint8 Decimation; uint16 Spare;
      uint32 Received; uint32 Stored; uint32 Decimated; uint32 Dropped;
  } STOR_EntryStats_t;

  typedef struct { STOR_EntryStats_t Entries[STOR_MAX_ENTRIES]; } STOR_Stats_Payload_t;
  ```

- RESET_COUNTERS zeroes `CommandCounter`, `CommandErrorCounter`, all
  per-entry counters, `PacketsStored`, `PacketsDecimated`, `PacketsDropped`,
  `FilesPurged`, `PurgeCycles` and `FileAddsRejected`, and does not
  increment `CommandCounter` afterwards: both command counters read 0. It
  sends `STOR_RESET_INF_EID`. It does not touch byte accounting, file
  counts, open files, or runtime parameters.
- Bad command code: `STOR_CC_ERR_EID` and `CommandErrorCounter`++ (unlike
  sample_app, which does not count it). Bad length on any `STOR_CMD_MID`
  command: `STOR_CMD_LEN_ERR_EID`, `CommandErrorCounter`++, and the command
  is not executed.
- SEND_HK and WAKEUP never increment `CommandCounter`. A SEND_HK or WAKEUP
  whose length is not `sizeof(CFE_MSG_CommandHeader_t)` is not acted on (no
  housekeeping is sent, no WAKEUP processing runs): STOR sends
  `STOR_CMD_LEN_ERR_EID` and increments `CommandErrorCounter`;
  `CommandCounter` is unchanged.
- A message on any other MID that is not a used entry sends
  `STOR_MID_ERR_EID` and changes no counter.

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

`ActiveEntries` is the number of used entries (`MsgId` not
`CFE_SB_INVALID_MSG_ID`) in the currently active table;
`OpenPacketFiles` the number of packet files currently open.

## Event IDs (`fsw/inc/stor_eventids.h`)

1 INIT_INF, 2 CC_ERR, 3 NOOP_INF, 4 RESET_INF, 5 MID_ERR, 6 CMD_LEN_ERR,
7 PIPE_ERR, 8 TBL_REG_ERR, 9 TBL_LOAD_ERR, 10 TBL_VAL_ERR, 11 SUB_ERR,
12 DIR_CREATE_ERR, 13 TLM_QUOTA_ERR, 14 PURGE_INF, 15 FILE_WRITE_ERR,
16 SET_PARAM_INF, 17 SET_PARAM_ERR, 18 PATH_ERR, 19 FILE_SRC_ERR,
20 FILE_EXISTS_ERR, 21 FILE_QUOTA_ERR, 22 FILE_ADD_INF, 23 FILE_DEL_ERR,
24 FILE_DEL_INF, 25 LIST_ERR, 26 LIST_INF, 27 CLOSE_INF, 28 STATS_INF.
Macro names are `STOR_<NAME>_EID`. ERR events are ERROR type, INF events
INFORMATION, except PURGE_INF which is DEBUG. STOR registers with EVS
(`CFE_EVS_Register`) before sending its first event.

## Mission integration

- Add `stor` to `MISSION_GLOBAL_APPLIST` in `sample_defs/targets.cmake` so it
  starts on cpu1. Follow how `hk` is wired everywhere it appears under
  `sample_defs/`, including `CFS_APP_STARTUP_ORDER` in
  `generate_startup.cmake` and the include list in `cpu1/install_custom.cmake`
  and `cpu2/install_custom.cmake` (the default table references
  `SAMPLE_APP_HK_TLM_MID`, so the table build needs sample_app's include
  directory).
- `sample_defs/tables/sch_lab_table.c`: `STOR_SEND_HK_MID` every 4.9 s,
  `STOR_WAKEUP_MID` every 1.0 s.
- `sample_defs/tables/to_lab_sub.c`: `STOR_HK_TLM_MID` and
  `STOR_STATS_TLM_MID` with buffer limit 1, and `STOR_LIST_TLM_MID` with
  buffer limit 4 so that a whole listing reaches the ground:
  `{CFE_SB_MSGID_WRAP_VALUE(STOR_LIST_TLM_MID), {0, 0}, 4}`.

## Non-functional requirements

- All STOR processing runs in the main task: no child tasks, ES memory
  pools, background file dumps, or `CFE_FS_ParseInputFileName`. File and
  directory work uses the OSAL file and directory API (`OS_OpenCreate`,
  `OS_read`, `OS_write`, `OS_close`, `OS_stat`, `OS_mkdir`, `OS_remove`,
  `OS_cp`, `OS_Directory*`).
- Build with no warnings (`-Werror`); `make -j native_std.runtest` passes,
  including STOR's tests and the mission's existing ones.
- One coverage test file per source module in
  `apps/stor/unit-test/coveragetest/` (the build requires it); line coverage
  of every STOR source file at least 95 percent (the project convention is
  100). Measure it from the gcov data the unit-test build produces, for
  example `gcov --json-format` on the coverage objects under
  `build-native_std`; lcov is not installed.
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
