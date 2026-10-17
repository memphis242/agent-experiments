# CCDD JSON definitions

CTF learns message ids, command codes, and payload layouts from "CCDD export" JSON files
(CCDD is the NASA command and data dictionary tool). NASA ships neither a generator for cFS
nor a sample set, so these files are written by hand and then proved correct against the
compiler. `configs/mission.ini` points `CCSDS_data_dir` at `ccdd/json/`; every `*.json` there
is read.

Files in `json/`:

| File | Content |
| --- | --- |
| `mission_mids.json` | the MID list: `{"target": "set1", "mids": [{"mid_name", "mid_value"}]}` |
| `mission_constants.json` | macros usable in tests as `#NAME#` |
| `*_cmd.json`, `*_send_hk.json`, `hk_send_combined_pkt.json` | command messages (`cmd_mid_name`) |
| `*_tlm.json`, `cfe_evs_*_event.json` | telemetry messages (`tlm_mid_name`) |

Covered: cFE ES and EVS (housekeeping, NOOP/RESET, long and short event messages), SAMPLE_APP,
TO_LAB, CI_LAB, and the HK application (commands, combined-packet request, housekeeping).

## Schema in short

Only the payload is described; CTF adds the CCSDS headers itself (`CCSDS_header_info_included
= false`). A command payload and a telemetry payload are both wrapped in one parameter named
`Payload`, so tests address fields as `Payload.<field>`.

```json
{ "cmd_mid_name": "SAMPLE_APP_CMD_MID",
  "cmd_codes": [
    { "cc_name": "SAMPLE_APP_DISPLAY_PARAM_CC", "cc_value": "3",
      "cc_data_type": "SAMPLE_APP_DisplayParamCmd_t",
      "cc_parameters": [
        { "name": "Payload", "data_type": "SAMPLE_APP_DisplayParam_Payload_t", "array_size": "0", "bit_length": "0",
          "parameters": [
            { "name": "ValU32", "data_type": "uint32", "array_size": "0", "bit_length": "0", "parameters": [] },
            { "name": "ValStr", "data_type": "char",   "array_size": "10", "bit_length": "0", "parameters": [] } ] } ] } ] }

{ "tlm_mid_name": "SAMPLE_APP_HK_TLM_MID", "tlm_data_type": "SAMPLE_APP_HkTlm_t",
  "tlm_parameters": [ { "name": "Payload", "data_type": "SAMPLE_APP_HkTlm_Payload_t", "parameters": [ ... ] } ] }
```

Rules the reader enforces or relies on (from `core_plugins/ccsds_plugin/readers/ccdd_export_reader.py`):

* Leaf `data_type` must be a primitive: `uint8 int8 uint16 int16 uint32 int32 uint64 int64 float
  double char`. Resolve C typedefs (`CFE_ES_MemOffset_t`, `CFE_SB_MsgId_t`, enums, resource ids)
  to the primitive of the same size. `char` with an `array_size` is a NUL-padded string.
* A parameter with a non-empty `parameters` list is a nested struct; with `array_size` it is an
  array of structs (`AppData` in the EVS housekeeping).
* Layout is packed (no padding). Where the C compiler inserts padding, add an explicit
  `Padding` field - the check below finds any that is missing.
* The telemetry/command type names (`tlm_data_type`, `cc_data_type`) are the C typedef names;
  the checker uses them to find sizes.
* `cc_name` values are global across all files (they become macros). Use the C macro name
  (`SAMPLE_APP_NOOP_CC`). A command message whose dispatch ignores the function code (for example
  a `SEND_HK` MID) still gets one `cc_codes` entry with value `0` so tests can name it.
* `mid_name` values must be unique, and every `*_mid_name` used in a message file must appear in
  the MID list for the `target` named by `CCSDS_target` (`set1`), otherwise the message is skipped
  with a "Unknown MID name" warning.
* Telemetry whose MID is missing here is logged and ignored when received.

## Derive definitions for an application

Source of truth is the compiler, not the headers read by eye: `probe_layout.sh` compiles
`tools/layout_probe.c` with the same include paths and defines the build used for
`sample_app`, so every macro and `sizeof`/`offsetof` is exactly what the flight software sees.

1. **Find the interface headers.** In `apps/<app>/config/` (and `fsw/inc/`):
   `default_<app>_msgids.h` (MID macros, e.g. `SAMPLE_APP_CMD_MID`), `default_<app>_msgdefs.h`
   (payload structs), `default_<app>_msgstruct.h` (full command/telemetry structs: header plus
   payload), `<app>_fcncodes.h` plus `default_<app>_fcncode_values.h` (command codes), and
   `<app>_eventids.h` (event ids). cFE core modules keep the same files under
   `cfe/modules/<es|evs|sb|tbl|time>/config/`.
2. **Write the JSON.** One file per command MID and per telemetry MID. Take field names and
   order from the payload struct, map each type to a primitive, copy array lengths (resolve
   macros such as `CFE_MISSION_MAX_API_LEN` = 20 to numbers), and name `cc_data_type` /
   `tlm_data_type` after the C message typedef (`SAMPLE_APP_HkTlm_t`). Add the app's MIDs to
   `json/mission_mids.json`.
3. **Add probe lines.** In `tools/layout_probe.c` add the app's `#include`s (`<app>_msgids.h`,
   `<app>_msgdefs.h` if its `msgstruct` header does not include it, `<app>_msgstruct.h`,
   `<app>_fcncodes.h`; add `-I$CFS_ROOT/apps/<app>/fsw/inc` in `probe_layout.sh`), a `MID(...)`
   per MID, `CC(...)` per command code, `SZ(<message typedef>)` per message, and
   `OFF(<message typedef>, Payload.<field>)` for the fields tests will read (always the first and
   last field, and the field after any padding).
4. **Check.** From the mission fixture directory:

   ```
   CFS_ROOT=/path/to/cFS ccdd/tools/probe_layout.sh | ccdd/tools/check_layout.py
   ```

   The checker compares MID values, command codes, total message sizes (command header 8 bytes,
   telemetry header 16 bytes, from the probe's own `CFE_MSG_*Header_t` sizes), and every probed
   field offset against the JSON. It exits non-zero and names the field on any mismatch. The
   probe also has the `CC`/`MID` lines for a reviewer to diff by eye.
5. **Prove it on a running cFS.** Write a test as in `../README.md`; a wrong layout shows up as
   wrong counter values or a size-mismatch warning ("Received msg size ... does not match") in the CTF log.

When the cFS tree changes (new cFE release, changed `mission_cfg` values such as
`CFE_MISSION_ES_MAX_APPLICATIONS`), rerun step 4 first; array lengths and MID bases are the
usual things that move.
