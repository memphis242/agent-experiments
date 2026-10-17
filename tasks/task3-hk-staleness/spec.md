# Task: HK input staleness tracking

## Background

The Housekeeping app (`apps/hk`) copies fields from input telemetry packets
into combined output packets according to its copy table. Today it only
counts, per combined packet sent, whether some input was missing
(`MissingDataCtr`), and that counter does not say which input, for how long,
or whether the condition has cleared. Operations needs per-input staleness
tracking with a configurable threshold, events on transitions, counters in
HK housekeeping, and a commanded report of stale inputs. Follow the existing
HK conventions (config headers, dispatch, events, `unit-test/*_tests.c`).

## Definitions

- "Combined send" for an output packet: one execution of
  `HK_SendCombinedHkPacket` for that output message ID.
- Each copy-table entry (input MID, output MID pair) has a "missed cycles"
  count: the number of consecutive combined sends of its output packet
  during which that entry's input data was not present. It resets to 0 when
  a packet for the entry's input MID arrives.
- An entry is "stale" when missed cycles >= `StaleCycles`.

## Functional requirements

### Threshold

- Runtime value `StaleCycles`, range 1 to 255, initialized at app start to
  `HK_STALE_CYCLES` from `fsw/inc/hk_internal_cfg.h` following the existing
  `HK_INTERNAL_CFGVAL` pattern with `DEFAULT_HK_INTERNAL_STALE_CYCLES` = 3.

### New command: SET_STALE_CYCLES

- `HK_FunctionCode_SET_STALE_CYCLES = 2` in
  `config/default_hk_fcncode_values.h`; `HK_SET_STALE_CYCLES_CC` macro in
  `fsw/inc/hk_fcncodes.h`.
- Payload `HK_SetStaleCycles_Payload_t { uint8 StaleCycles; uint8 Spare[3]; }`
  and command `HK_SetStaleCyclesCmd_t` in the config message headers.
- Valid 1 to 255: set the value, `CommandCounter`++, event
  `HK_STALE_CYCLES_INF_EID` (38, INFORMATION) containing the new value.
  Value 0: unchanged, `CommandErrorCounter`++, event
  `HK_STALE_CYCLES_ERR_EID` (39, ERROR).
- Changing the threshold re-evaluates staleness at the next combined send,
  not immediately.

### New command: REPORT_STALE

- `HK_FunctionCode_REPORT_STALE = 3`; `HK_REPORT_STALE_CC`.
- No payload (`HK_ReportStaleCmd_t`). On receipt, `CommandCounter`++ and
  transmit one `HK_StaleReportTlm_t` packet:

  ```c
  #define HK_STALE_REPORT_MAX_ENTRIES 16

  typedef struct
  {
      uint16         StaleCount;   /* total stale entries right now */
      uint16         ReportedCount; /* entries listed below, <= 16 */
      CFE_SB_MsgId_t InputMid[HK_STALE_REPORT_MAX_ENTRIES];
      CFE_SB_MsgId_t OutputMid[HK_STALE_REPORT_MAX_ENTRIES];
  } HK_StaleReport_Payload_t;
  ```

  with telemetry header, listed in copy-table order, unused slots set to
  `CFE_SB_INVALID_MSG_ID`. Topic `CFE_MISSION_HK_STALE_RPT_TLM_TOPICID`
  default 0xA2 in `fsw/inc/hk_topicids.h`; `HK_STALE_RPT_TLM_MID` via
  `CFE_PLATFORM_HK_TLM_MIDVAL` in `config/default_hk_msgids.h`. Add it to
  `sample_defs/tables/to_lab_sub.c`.

### Transitions and events

- When an entry becomes stale (missed cycles reaches `StaleCycles` during a
  combined send): `StaleTransitionCtr`++ and one event `HK_INPUT_STALE_EID`
  (40, INFORMATION) naming the input and output MIDs in hex. No repeat while
  it stays stale.
- When a stale entry's input arrives: one event `HK_INPUT_FRESH_EID`
  (41, INFORMATION) naming both MIDs.
- Loading a new copy table resets all missed-cycle counts and stale states
  without events.

### Housekeeping telemetry

Append to `HK_HkTlm_Payload_t`, after `MemPoolHandle`, in this order:

```c
uint16 StaleInputCount;   /* entries currently stale */
uint16 StaleTransitionCtr; /* transitions into stale since init or reset */
uint8  StaleCycles;        /* current threshold */
uint8  Spare2[3];
```

`RESET_COUNTERS` zeroes `StaleTransitionCtr` only; it does not alter
staleness states or the threshold.

### Mission configuration for verification

Add one entry to the default copy table `apps/hk/fsw/tables/hk_cpy_tbl.c`:
input `SAMPLE_APP_HK_TLM_MID`, input offset 12, output
`HK_COMBINED_PKT2_MID`, output offset 12, 2 bytes. Combined packet 2 is
requested by ground command (`HK_SEND_COMBINED_PKT_MID` with the output MID
as payload); nothing schedules it automatically.

## Non-functional requirements

- `make native_std.prep && make -j native_std.install` with no warnings
  (`-Werror`), `make -j native_std.runtest` passing.
- Unit tests in HK's existing style for every new or changed path; line
  coverage of each HK source file per `make native_std.lcov` must not fall
  below its current value.
- CTF definitions for the new command, the report packet and the extended
  housekeeping payload under `ctf/ccdd/json/`; a CTF test
  `ctf/tests/hk_staleness.json` that sets `StaleCycles` to 2, requests
  combined packet 2 three times in quick succession, and checks that
  `StaleInputCount` becomes 1 and the stale event fires; then waits for the
  next SAMPLE_APP housekeeping packet, requests combined packet 2 again, and
  checks the fresh event. Run it with `ctf/run_ctf.sh` and make it pass.
- Report at `docs/reports/hk_staleness.md`: what changed, unit test counts,
  HK coverage figures, CTF results, open issues.
- Commit the work.

## Acceptance

Build and unit tests are run; a hidden HK unit test compiled against the
names above exercises threshold validation, the stale and fresh transitions
with exact event counts, the report packet contents, the reset behavior and
the table reload reset; cFS is started and driven over UDP through the
scenario above with observation of events, housekeeping and the report
packet; your CTF test is executed; the report is checked.
