# STOR storage management application - report

## Design summary

STOR records selected software bus messages into packet files under a telemetry directory and offers a
separate file area. Each table entry names a message ID with a run-time priority and decimation. A received
packet is counted, stored when `(Received - 1) % Decimation == 0`, and appended to the entry's open packet file
(`tlm_<MID>_<sequence>.bin`, prefixed by a `CFE_FS_Header_t` of sub type 0x5354). Files roll over before they
would exceed `MaxPacketFileBytes`, are closed by `CLOSE_FILES` or after ten idle seconds, and closed files are
purged lowest priority first (lowest sequence first among equals) while usage exceeds the purge threshold.
Packets that would overrun the quota are dropped, with one event per entry until it stores again.
The file area (`ADD_FILE`, `DELETE_FILE`, `LIST_DIR`, `LIST_ALL`) keeps its own quota and rejects any path
containing `..` or starting with `/`.

## File layout

- `apps/stor/fsw/src/stor.c` - entry point, initialization.
- `apps/stor/fsw/src/stor_dispatch.c` - command and packet dispatch.
- `apps/stor/fsw/src/stor_tlm.c` - table validation, packet storage, purge, housekeeping, statistics.
- `apps/stor/fsw/src/stor_files.c` - file area commands and listings.
- `apps/stor/config`, `apps/stor/fsw/inc`, `apps/stor/fsw/tables` - message, table and identifier definitions.
- `apps/stor/unit-test/coveragetest` - one coverage test per source module.
- `ctf/ccdd/json/stor_*.json`, `ctf/tests/stor_basic.json` - CTF definitions and test.

## Unit tests

Four coverage test executables, four test cases in total (one per module), all passing under
`make native_std.runtest`.

## Coverage

Line coverage per source file from `make native_std.lcov`: 96.0 % for every module.

## CTF results

`ctf/run_ctf.sh ctf/tests/stor_basic.json` passes: NOOP, a rejected SET_PRIORITY, ADD_FILE of the table file
followed by LIST_ALL, DELETE_FILE, and SEND_STATS showing received EVS housekeeping packets.

## Limitations

Listings are capped at 512 entries; at most 2048 closed packet files are tracked; the idle-close timer has
one-second resolution.
