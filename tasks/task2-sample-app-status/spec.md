# Task: SAMPLE_APP status telemetry and configurable rate

## Background

SAMPLE_APP (`apps/sample_app`) emits only a housekeeping packet with two
counters. Operations wants a periodic status packet carrying the example
table's CRC and activity counters, emitted every Nth housekeeping cycle,
with N changeable by ground command. The change must follow the existing
sample_app conventions (config headers, dispatch, events, coverage tests),
be downlinked through TO_LAB, be exercised by a CTF test, and be reported.

## Functional requirements

### New command: SET_STATUS_RATE

- Function code `SAMPLE_APP_FunctionCode_SET_STATUS_RATE = 4` in
  `config/default_sample_app_fcncode_values.h`, and
  `#define SAMPLE_APP_SET_STATUS_RATE_CC SAMPLE_APP_CCVAL(SET_STATUS_RATE)`
  in `fsw/inc/sample_app_fcncodes.h`.
- Payload in `config/default_sample_app_msgdefs.h`:

  ```c
  typedef struct SAMPLE_APP_SetStatusRate_Payload
  {
      uint16 Divisor; /* status packet every Divisor-th housekeeping cycle */
      uint16 Spare;
  } SAMPLE_APP_SetStatusRate_Payload_t;
  ```

  and the command type `SAMPLE_APP_SetStatusRateCmd_t` (command header plus
  `Payload`) in `config/default_sample_app_msgstruct.h`.
- Valid `Divisor` is 1 to 100 inclusive. On a valid command: store the
  divisor, increment `CommandCounter`, send event
  `SAMPLE_APP_STATUS_RATE_INF_EID` (13, INFORMATION) whose text contains the
  new divisor. On an invalid divisor: leave the divisor unchanged, increment
  `CommandErrorCounter`, send `SAMPLE_APP_STATUS_RATE_ERR_EID` (14, ERROR)
  whose text contains the rejected value.
- A command of the wrong length is handled by the existing length check
  (`SAMPLE_APP_CMD_LEN_ERR_EID`).
- The default divisor is `SAMPLE_APP_PLATFORM_DEFAULT_STATUS_DIVISOR` in
  `fsw/inc/sample_app_internal_cfg.h` following the existing
  `SAMPLE_APP_PLATFORM_CFGVAL` pattern, default value 2.

### New telemetry: status packet

- Topic `SAMPLE_APP_MISSION_STATUS_TLM_TOPICID` with default value 0x8F in
  `fsw/inc/sample_app_topicids.h`, and
  `SAMPLE_APP_STATUS_TLM_MID` built with `SAMPLE_APP_TLM_PLATFORM_MIDVAL` in
  `config/default_sample_app_msgids.h`.
- Payload and packet in the config headers:

  ```c
  typedef struct SAMPLE_APP_StatusTlm_Payload
  {
      uint32 TableCrc;        /* CRC of ExampleTable from CFE_TBL_GetInfo, 0 if unavailable */
      uint32 ProcessCmdCount; /* PROCESS commands accepted since init or reset */
      uint16 StatusDivisor;   /* current divisor */
      uint16 HkCycleCount;    /* SEND_HK messages received since init or reset */
  } SAMPLE_APP_StatusTlm_Payload_t;

  typedef struct
  {
      CFE_MSG_TelemetryHeader_t      TelemetryHeader;
      SAMPLE_APP_StatusTlm_Payload_t Payload;
  } SAMPLE_APP_StatusTlm_t;
  ```

- Emission rule: on every SEND_HK message, increment `HkCycleCount` first;
  when `HkCycleCount` is a multiple of the current divisor, time-stamp and
  transmit the status packet after the housekeeping packet. With the default
  divisor 2, the status packet goes out on cycles 2, 4, 6, and so on.
- `RESET_COUNTERS` resets `ProcessCmdCount` and `HkCycleCount` to 0 in
  addition to the existing counters. It does not change the divisor.
- `ProcessCmdCount` increments once per PROCESS command that completes
  successfully (returns `CFE_SUCCESS`).
- Add `SAMPLE_APP_STATUS_TLM_MID` to the TO_LAB subscription table
  `sample_defs/tables/to_lab_sub.c` so the packet reaches the ground.
- The existing housekeeping packet layout is unchanged.

## Non-functional requirements

- Build with `make native_std.prep && make -j native_std.install` must
  succeed with no warnings (the mission compiles with `-Werror`).
- `make -j native_std.runtest` passes. Every new or changed code path has a
  coverage test in `apps/sample_app/unit-test/coveragetest/`, following the
  existing style; line coverage of each sample_app source file, as reported
  by `make native_std.lcov`, must not fall below its current value.
- CTF: add command and telemetry definitions for SET_STATUS_RATE and the
  status packet under `ctf/ccdd/json/` following `ctf/ccdd/README.md`, and
  a test `ctf/tests/sample_app_status_rate.json` that sets divisor 1,
  confirms a status packet arrives with `StatusDivisor` 1, then sends
  divisor 0 and confirms `CommandErrorCounter` increments. Run it with
  `ctf/run_ctf.sh` and make it pass.
- Report: `docs/reports/sample_app_status_rate.md` stating what changed
  (files and interfaces), unit test results (segment and assertion counts),
  coverage figures for sample_app, CTF results, and any open issues.
- Commit the work.

## Acceptance

The build and unit tests are run; a hidden coverage test compiled against
the names above exercises the divisor validation, the emission rule and the
reset behavior; cFS is started and driven over UDP to verify the status
packet contents and the error path; your CTF test is executed; the report is
checked for the required sections.
