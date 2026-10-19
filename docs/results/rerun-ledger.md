# Rerun ledger

Runs whose result was affected by a defect in an acceptance pack, an ambiguous spec, or
unfair feedback. Each is marked with an asterisk in the results and is rerun on a corrected
pack; a complete rerun of the experiment follows later.

Status (2026-10-19), after an adversarial audit of the Task 3 and Task 4 packs:

- Task 4: stand-in defects in the hidden test decided pass or fail (every Task 4 run that used
  all five rounds), so Task 4 is redone from scratch on a corrected pack and spec. All
  Task 4 runs so far are superseded; the T4-1 section below is kept as the record that
  started the audit.
- Task 3: defects cost rounds but decided no outcome. The pack is fixed, every round of every
  Task 3 run is re-graded on the fixed pack to confirm the other runs stand, and the runs
  below are rerun on it.
- Task 2: the pack stays as is for now; the OSAL test race below is fixed later and its
  impact re-evaluated then.

## Pack defects

### T4-1: hidden test rejects existing storage roots (Task 4)

The hidden unit test's `OS_mkdir` stand-in returns `OS_ERROR` for any path that already
exists (`coveragetest_stor_hidden.c`, `H_OS_mkdir`). Real OSAL returns `OS_SUCCESS` when the
path is an existing directory (`osal/src/os/portable/os-impl-posix-dirs.c`,
`OS_DirCreate_Impl`). An app that calls `OS_mkdir` on its roots at every start is correct on
cFS but fails assertion 07.001, "existing roots are not an error". The feedback for that test
group names only start-up byte accounting and sequence numbers, not root creation.

| Run | Arm | Task | Rep | Effect | Gate |
|-----|-----|------|----:|--------|------|
| ee0e6aeb | A11 | task4 | 2 | Failed 07.001 alone in rounds 2 to 5; failed the run | Exempt (owner, 2026-10-19) |

Voided attempts that also hit 07.001 before a usage limit cut them off, not counted and
already replaced: 68fbc5ae (A12 rep 1), d3623279 (A4 rep 2).

### T3-1 to T3-4: fresh-event timing, misleading feedback, implementation-detail test, OSAL race (Task 3)

- T3-3 (spec): the spec defined the fresh event as sent when a stale entry's input arrives,
  but other sentences and its own CTF scenario supported sending it at the next combined
  send. The pack accepts only "on arrival". The corrected spec states on-arrival throughout.
- T3-4 (feedback): those failures were reported as a RESET_COUNTERS defect, which the code
  already met.
- T3-1 (hidden test): one test required the counter reset to happen inside `HK_ResetHkData`,
  an implementation detail; two runs moved a correct reset there after the T3-4 feedback and
  lost another round.
- T3-2 (OSAL race, see below): two runs failed a round on an OSAL functional test while their
  HK code was unchanged and correct.

Every run below lost round 1 to T3-3 and T3-4; runs marked with T3-1 or T3-2 lost a further
round. All passed. Outcomes are unchanged; round counts and spend are inflated.

| Run | Arm | Rep | Rounds | Further defect |
|-----|-----|----:|-------:|----------------|
| 5f3bc7f2 | A10 | 4 | 2 | |
| 48593815 | A11 | 3 | 2 | |
| cca9ca4e | A11 | 4 | 2 | |
| 0ae35670 | A11 | 5 | 2 | |
| 42e2df75 | A12 | 1 | 2 | |
| 03c71dce | A12 | 2 | 2 | |
| be6ea4c9 | A12 | 3 | 2 | |
| 00472ead | A12 | 4 | 2 | |
| 8b94fc6e | A12 | 5 | 2 | |
| 68a2ef63 | A16 | 2 | 2 | |
| b5e0f533 | A16 | 3 | 2 | |
| 2932c5a8 | A16 | 4 | 2 | |
| 7681f7fe | A16 | 5 | 2 | |
| 386a4084 | A16 | 6 | 2 | |
| 8a759a54 | A16 | 7 | 2 | |
| a8329bee | A17 | 3 | 3 | T3-1 |
| ddba0caf | A17 | 4 | 3 | T3-2 |
| d43fbb41 | A17 | 7 | 3 | T3-1 |
| 19a7c17b | A17 | 8 | 3 | T3-2 |
| b76a9d09 | A17 | 10 | 2 | |

Re-grade (2026-10-19, `harness/regrade.py` at 47a3c75, every round of all 97 completed Task 3
runs on the fixed pack; results in `/workspace/audit-scratch/t3-fix/regrade-final/`): 116 of 139
rounds graded the same; 4 changed from fail to pass, exactly the T3-1 and T3-2 rounds above
(a8329bee and d43fbb41 round 2 on the hidden test, ddba0caf and 19a7c17b round 2 on the unit
tests). The T3-3 rounds still fail on the fixed pack, as they should: the spec, not the grader,
was ambiguous. The other 19 differing rounds all belong to the ceiling-hit runs below, graded in
rounds where the agent had not committed its work, which a checkout cannot reproduce; their
final verdicts do not change. No run outside these two tables lost a round to a pack defect, so
the other runs stand.

### Task 3 runs cut off by the print-mode wait ceiling

The CLI stopped a background subagent at its ten-minute wait ceiling in these runs (see the
design document); they are rerun with the raised ceiling like the Task 1 and Task 2 ones.
cca9ca4e is also in the table above.

| Run | Arm | Rep | Rounds |
|-----|-----|----:|-------:|
| 5f0aa93f | A6 | 1 | 2 |
| 85ef1e31 | A6 | 2 | 4 |
| 3b06a362 | A6 | 3 | 2 |
| 6dfaf2db | A6 | 4 | 4 |
| 57a62334 | A6 | 5 | 2 |
| 708bb929 | A8 | 1 | 2 |
| 5be745a3 | A8 | 2 | 3 |
| b6ee0d71 | A8 | 4 | 2 |
| 8c8534f9 | A8 | 5 | 2 |
| cca9ca4e | A11 | 4 | 2 |
| 1b191f23 | A14 | 2 | 2 |
| 7001758f | A14 | 4 | 2 |
| 875dd8d3 | A14 | 5 | 2 |
| d7444424 | A15 | 4 | 2 |

### OSAL functional-test race (Tasks 2 and 3; Task 4 superseded)

The unit-test check runs the whole mission's tests in parallel. OSAL functional tests that
share a working directory (`file-api-test`, `file-sys-add-fixed-map-api-test`,
`osal_filesys_UT`) race on creating the `./test` mount point ("Cannot create mount point
./test: File exists"), more often under load. The check then fails the round, and the
feedback blames the app's own unit tests.

| Run | Arm | Task | Rep | Effect |
|-----|-----|------|----:|--------|
| 52e0a8ed | A9 | task2 | 6 | `file-api-test` failed round 1 alone; passed round 2 unchanged |
| ddba0caf | A17 | task3 | 4 | see the Task 3 table |
| 19a7c17b | A17 | task3 | 8 | see the Task 3 table |

1b191f23 (A14, task3, rep 2) also hit it in round 1, but failed a genuine check in the same
round, so its round count is unchanged.

## Harness faults

### H-1: Codex pilots ran before the no-polling instruction (A21, A22)

The A22 Task 1 pilot (4326636d) stalled for two hours in a self-matching
`pgrep` loop the main agent wrote to wait on a background Codex subagent (see
the Codex arms in `docs/experiment-design.md`). The delegation section now
forbids polling a background subagent. All four pilots ran on the old text,
so all four are kept as pilots, excluded from the results, and rerun as rep 1:

| Run | Arm | Task | Outcome on the old text |
|---|---|---|---|
| d78a28d2 | A21 | task1 | passed, round 1 |
| 020d8055 | A21 | task2 | passed, round 1 |
| 6a951429 | A22 | task2 | passed, round 1 |
| 4326636d | A22 | task1 | stopped in round 1 during the stall |

## Gate exemptions

- A11 on Task 4: rep 2's failure comes from defect T4-1, so it does not count against the
  task gate. A11 remains eligible for Task 5.

## After Experiment A

- Review the specs of all tasks (Tasks 1 to 5) for ambiguity before any rerun, so that each
  requirement has one reading and the acceptance packs test that reading.
- Make acceptance feedback name the behaviour that fails rather than restating a test
  group's requirements (required for Task 5 from the start).
- Fix the OSAL race in the Task 2 pack and re-evaluate the impacted Task 2 runs.
- Selectively rerun the runs listed above on corrected packs, then rerun the whole
  experiment.
