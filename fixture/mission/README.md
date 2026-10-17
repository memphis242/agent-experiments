# cFS mission tree

A single-repository copy of the NASA core Flight System (cFS) bundle, `dev`
branch, with all submodules flattened into plain directories (no submodules,
no nested `.git`). Mission name `SampleMission`, one native Linux target
(`native_std`), processor `cpu1`.

## Layout

| Path | Contents |
|---|---|
| `cfe/` | core Flight Executive: ES, EVS, SB, TBL, TIME, FS, MSG, plus the cFE build system (`cfe/cmake`) |
| `osal/` | Operating System Abstraction Layer |
| `psp/` | Platform Support Package (native Linux PSP is used here) |
| `apps/` | applications: `sample_app`, `ci_lab`, `to_lab`, `sch_lab` (lab apps) and `cf`, `cs`, `ds`, `fm`, `hk`, `hs`, `lc`, `md`, `sc`, `sbn` |
| `libs/` | libraries (`sample_lib`) |
| `sample_defs/` | mission definition: `targets.cmake` (apps per CPU), per-CPU config in `cpu1/`, tables in `tables/`, build options |
| `tools/` | ground tools from upstream, plus `udp_harness.py`, `run_cfs.sh`, `stop_cfs.sh` |
| `ctf/` | cFS Test Framework setup; see `ctf/README.md` |

## Build and test

Run from the tree root. Build output goes to `build-native_std/`.

```
make native_std.prep                 # configure (CMake); fails here if a unit-test file is missing
make -j16 native_std.install         # build and install to build-native_std/exe/cpu1/
make -j16 native_std.runtest         # run all unit tests; per-test logs go to `build-native_std/test-results/` (191 tests expected)
```

Installed runtime is in `build-native_std/exe/cpu1/`: the `core-cpu1` executable and
`cf/` (app and library `.so` files, tables, `cfe_es_startup.scr`).
Individual test runners (`coverage-<app>-<module>-testrunner`) are also installed there.

### Running one app's tests

Coverage tests live in `apps/<app>/unit-test/coveragetest/`. After a build:

```
cd build-native_std/native/default_cpu1
ctest -R sample_app                  # all sample_app coverage tests
ctest -R sample_app_cmds -V          # one module, verbose
```

or run a runner directly: `build-native_std/exe/cpu1/coverage-sample_app-sample_app_cmds-testrunner`.

### Unit-test convention

Every source file listed for an app in `fsw/src/*.c` needs a matching
`unit-test/coveragetest/coveragetest_<module>.c`, where `<module>` is the source
file name without extension (`fsw/src/foo_utils.c` needs
`coveragetest_foo_utils.c`). The app's `unit-test/CMakeLists.txt` derives the
path from the source list; a missing file makes CMake configure
(`make native_std.prep`) fail. The app's stubs under `unit-test/stubs/` are
also listed in that `CMakeLists.txt`.

## Registering an app

1. `sample_defs/targets.cmake`: add the app name to `MISSION_GLOBAL_APPLIST`
   (build it) and to the CPU's `cpu1_APPLIST` where lab apps are listed (install it
   for cpu1); the startup script `cf/cfe_es_startup.scr` is generated from these lists.
2. `sample_defs/tables/to_lab_sub.c`: add the app's telemetry message IDs
   (include its `<app>_msgids.h`) so TO_LAB forwards them to the ground.
3. `sample_defs/tables/sch_lab_table.c`: add scheduler entries that send the
   app's wakeup and/or send-housekeeping message at a chosen period (in
   SCH_LAB ticks, 0.1 s).

Message IDs, topic IDs and function codes come from each app's `config/`
and `fsw/inc/` headers.

## Running cFS

```
tools/run_cfs.sh        # starts core-cpu1 in the background, prints its PID
tools/stop_cfs.sh       # stops it
```

`run_cfs.sh` writes `build-native_std/run/cfs.log` and `cfs.pid`, and waits up
to 30 s for the log line `CFE_ES_Main entering OPERATIONAL state`; on failure it
prints the log tail and exits non-zero. To run by hand:
`cd build-native_std/exe/cpu1 && ./core-cpu1` (the working directory must be
that directory so `./cf` resolves). Messages about undefined `SBN_*` symbols
at startup are expected.

## Ground interface (UDP, loopback)

| App | Port | Direction |
|---|---|---|
| CI_LAB | 1234 | commands into cFS |
| TO_LAB | 2234 | telemetry out of cFS (after output is enabled) |

TO_LAB sends nothing until it receives OUTPUT_ENABLE (function code 6,
message ID `0x1880`, 16-byte destination IP string). Key message IDs:
SAMPLE_APP command `0x1882`, SAMPLE_APP housekeeping telemetry `0x0883`
(payload: `CommandCounter`, `CommandErrorCounter`, one byte each, after a
16-byte telemetry header (6 primary + 10 secondary)). Housekeeping is requested by SCH_LAB every 5.4 s,
so receive for 6 s or more.

### `tools/udp_harness.py`

Python 3.12+, standard library only. Importable module and CLI:

```
tools/udp_harness.py enable-to
tools/udp_harness.py send --mid 0x1882 --cc 0 [--payload-hex 2a000700]
tools/udp_harness.py recv --seconds 8 [--mid 0x0883]
```

Module functions: `build_command(mid, cc, payload)`, `send_command(...)`,
`enable_to_output()`, `receive_telemetry(seconds, mid=None)` returning a list of
`(mid, raw_bytes)`, and `payload(raw)` to strip the telemetry header. Receive (`recv` or `receive_telemetry`) for at least one housekeeping period after sending a command; HK is periodic, so it reflects commands sent earlier.

## Test framework

`ctf/` holds the cFS Test Framework (CTF) setup. See `ctf/README.md`.
