# CTF acceptance layer for the cFS mission

Runs the NASA cFS Test Framework (CTF v1.11, github.com/nasa/CTF) against a built
cFS tree. Tests are JSON scripts that boot `core-cpu1`, command it over UDP through
CI_LAB, and read telemetry and events back through TO_LAB.

```
setup_ctf.sh        one-time, idempotent: clone CTF, build the Python 3.12 venv, apply shims
run_ctf.sh          run test scripts; non-zero exit when any test fails
configs/mission.ini CTF configuration (headless, ports 1234/2234, ToApi telemetry)
ccdd/               command/telemetry/MID definitions CTF reads (see ccdd/README.md)
headers/            CCSDS header types matching the cFE 7 wire format
tests/              test scripts
shims/              compatibility files the setup script installs (see "Known quirks")
CTF/, venv/         created by setup_ctf.sh (git-ignored)
results/            created by run_ctf.sh (git-ignored)
```

## Set up

```
./setup_ctf.sh
```

Needs `python3.12` (with the `venv` module), `git`, and network access on the first run.
It fails with an explicit message if Python 3.12 is missing; point `CTF_PYTHON` at another
3.12 interpreter if it is not on `PATH`. Re-running is cheap and repairs a partial setup.
Afterwards `venv/bin/python CTF/ctf --help` works.

CTF's own pinned requirements do not install on Python 3.12, so the script installs the
unpinned package list `shims/requirements.txt` held to the exact versions in
`shims/constraints.txt` (the set this fixture was validated with).

## Run

```
CFS_ROOT=/path/to/cFS ./run_ctf.sh tests/smoke_sample_app.json
./run_ctf.sh tests/                       # a directory runs every *.json inside, sorted
CTF_RESULTS_DIR=/tmp/out ./run_ctf.sh tests/smoke_housekeeping.json
```

The mission tree is selected by environment variables (read by `run_ctf.sh`, handed to CTF,
and referenced as `${NAME}` inside `configs/mission.ini`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `CFS_ROOT` | parent of `ctf/` (the mission tree root) | cFS source and build tree |
| `CFS_RUN_DIR` | `$CFS_ROOT/build-native_std/exe/cpu1` | directory with `core-cpu1` and `cf/`; cFS runs with this as its working directory |
| `CTF_RESULTS_DIR` | `$CFS_ROOT/ctf/results` | where each run writes `Run_<date>_<time>/` |
| `CTF_BUILD_CFS` | `false` | `true` makes CTF run `CTF_BUILD_CMD` in `CFS_ROOT` at registration |
| `CTF_BUILD_CMD` | `make install` | rebuild command, only used when `CTF_BUILD_CFS=true` |
| `CTF_SKIP_PORT_CHECK` | unset | `1`: skip the UDP 1234 free check (runs inside a private network namespace) |
| `CTF_ATTACH` | unset | `1`: use an already-running cFS (see below) |

cFS writes into its run directory (RAM-disk files, `cf/` tables), so point `CFS_RUN_DIR` at a
scratch copy of `exe/cpu1` when the build tree must stay untouched.

**How cFS is started.** The default is that CTF starts the already-built `core-cpu1`
itself: a test calls `RegisterCfs` and `StartCfs`, and `ShutdownCfs` stops it. CTF is
headless (`cfs_run_in_xterm = False`); cFS stdout goes to
`logs/<script>_<id>/mission_cfs_stdout.txt` in the run folder. The alternative is
attaching to a cFS you started yourself (`./core-cpu1 --reset PO` in `CFS_RUN_DIR`): omit
`StartCfs` and `ShutdownCfs` from the test and run with `CTF_ATTACH=1`. Self-start is the
default because every test then begins from a cold boot with known counters.

**Results.** `run_ctf.sh` prints the folder and the summary table at the end:

```
results/Run_<MM_DD_YYYY>_<HH_MM_SS>/
  results_summary.txt, results_summary.json   pass/fail per script and test
  CTF_Log_File.log                            full CTF log (instructions, verifications, errors)
  logs/<script>_<id>/                         <script>.log, mission_cfs_stdout.txt,
                                              mission_evs_msgs.log, mission_tlm_msgs.log
```

Exit status: `0` all passed; `255` (CTF's `-1`) a test failed or CTF aborted; `2` the
wrapper refused to start (CTF not set up, bad `CFS_ROOT`/`CFS_RUN_DIR`, UDP 1234 busy).
Search a failed log with `grep -E "TEST_FAIL|ERROR" CTF_Log_File.log`.

## Write a test

Copy `tests/smoke_sample_app.json`. A script is a list of `tests`, each a list of
`instructions`. The ones used here:

| Instruction | Purpose |
| --- | --- |
| `RegisterCfs`, `StartCfs`, `ShutdownCfs` | target lifecycle; `"target": ["mission"]` names the `[mission]` section of `mission.ini` |
| `SendCfsCommand` | `mid` and `cc` are names from `ccdd/json`; `args` mirrors the C payload, e.g. `{"Payload": {"ValU32": 5, "ValI16": -1, "ValStr": "x"}}`; no-payload commands take `{}` |
| `ClearTlmPacket` | drop buffered packets of one MID, so the next check sees only new ones |
| `CheckTlmValue` | `args` entries `{"compare": "==", "variable": "Payload.CommandCounter", "value": 1}`; polls until it passes or `verify_timeout` expires |
| `CheckEvent` | `{"app_name", "event_id", "event_str", "is_regex"}`; `event_str` may use `{}` placeholders with `event_str_args`, or `is_regex: true` |

Idioms that make tests deterministic:

* Every test script starts with the three-step boot in `SMOKE-1`: register, start, then send
  `TO_LAB_OUTPUT_ENABLE_CC` with `"dest_IP": "127.0.0.1"`. Nothing is received before that.
* Housekeeping telemetry only appears when the application receives its `SEND_HK` command
  (the sample scheduler sends them every few seconds). To assert on a counter, send
  `ClearTlmPacket`, send the app's `*_SEND_HK_CC`, then `CheckTlmValue`; otherwise the check can
  match a packet produced before your command.
* Put `RESET_COUNTERS` first when a test asserts an absolute counter value.
* `wait` is in CTF "time units" (seconds). Waits poll the telemetry socket, so they never
  need a separate sleep.
* Event text comes from the application source; take the exact string and event id from
  `apps/<app>/fsw/inc/<app>_eventids.h` and the `CFE_EVS_SendEvent` call.

`tests/smoke_housekeeping.json` additionally covers cFE ES, cFE EVS, and HK application
housekeeping and the `DISPLAY_PARAM` payload encoding.

## Add a new application

1. Add its definitions under `ccdd/json/` following `ccdd/README.md` (message ids, commands,
   telemetry), then verify them against the compiler with `ccdd/tools/`.
2. Write a test that sends its `*_SEND_HK_CC` command and checks its housekeeping telemetry.
3. Make sure the app is in the build's `cfe_es_startup.scr` and that the TO_LAB subscription
   table (`to_lab_sub.c`) lists its telemetry message ids, or the packets never reach CTF.

## Known quirks

* **Python 3.12 only, with shims.** `demjson` 2.2.4 and the pinned `cffi` do not build on
  3.12. `shims/demjson.py` re-exports `demjson3` under the old name; `shims/six.py` is copied into
  `invoke/vendor/` because invoke 2.x no longer ships it and fabric/paramiko still import it
  from there; `setuptools` supplies `distutils`.
* **cFE 7 telemetry header is 16 bytes** (6 primary + 6 time + 4 spare), not 12. CTF's
  `ccsds_v1` module has the right sizes but its telemetry class lacks `get_msg_size()`, which
  the parser calls on every packet; `headers/cfe7_ccsds.py` adds it. Without it the first
  telemetry packet aborts the run with `AttributeError: 'CcsdsV1TlmPacket' object has no
  attribute 'get_msg_size'`.
* **`pgrep`/`pkill` wrappers.** CTF checks for a running cFS with `sh -c 'pgrep -f "..."'`.
  Where `/bin/sh` is bash (Fedora, Arch), that shell matches its own command line, so CTF
  reports "CFS executable ... has already started" and refuses to start cFS; the stock
  `pkill` in `ShutdownCfs` likewise kills the shell that launched it. `shims/bin/` holds
  wrappers that ignore the caller and its ancestors; `run_ctf.sh` puts them first on `PATH`.
* **`cfs_run_args = --reset PO`** (cold boot) also makes those `pgrep`/`pkill` patterns
  specific to `core-cpu1 --reset PO`, so an unrelated `./core-cpu1` on the same host is neither
  mistaken for CTF's instance nor killed. It still cannot share the host with another cFS:
  CI_LAB's UDP port 1234 is fixed, and `run_ctf.sh` refuses to start while it is bound.
* **`EnableCfsOutput` does not work with this TO_LAB.** CTF's `ToApi` looks for a
  `TO_ENABLE_OUTPUT_CC` command with fields `cDestIp` and `usDestPort`; cFE 7 TO_LAB takes a
  single `dest_IP[16]`. Tests send `TO_LAB_OUTPUT_ENABLE_CC` themselves. The log line
  `Could not find TO_ENABLE_OUTPUT_CC in MID_Map` at start-up is therefore expected.
* **Unknown-MID warnings.** TO_LAB forwards telemetry of apps without definitions (SB, TIME,
  TBL, CF, MD, ...); CTF logs `received message with MID = 0x... not in the CCSDS MID Map` and
  ignores them. Add definitions to silence a specific one.
* **One cFS per script.** `reset_plugins_between_scripts = true`, so each script in a multi-script
  run registers and boots its own cFS (otherwise the second script fails with `CFS target mission is already
  registered`). Every script must therefore contain its own `RegisterCfs` ... `ShutdownCfs`.
* **A failing check does not stop the script** (`end_test_on_fail = false`); the remaining tests
  run and the final status is still failure.
