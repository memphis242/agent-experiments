# SAMPLE_APP status rate

## What changed
Files under apps/sample_app (config headers, fsw/inc, fsw/src), sample_defs/tables/to_lab_sub.c, ctf/.
Interfaces: SET_STATUS_RATE command (code 4), SAMPLE_APP_StatusTlm_t telemetry (0x088F).

## Unit tests
4 segments, 94 assertions, all pass.

## Coverage
sample_app line coverage 100 % per file.

## CTF
ctf/tests/sample_app_status_rate.json passes.

## Open issues
None.
