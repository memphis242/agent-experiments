# Rerun ledger

Runs whose result was affected by a defect in an acceptance pack or by unfair feedback. Each
is marked with an asterisk in the results and is scheduled for a selective rerun after
Experiment A, on a corrected pack; a complete rerun of the experiment follows later. Packs
for Tasks 1 to 4 stay unchanged until Experiment A finishes, so the remaining runs stay
comparable with the ones already done.

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

## Gate exemptions

- A11 on Task 4: rep 2's failure comes from defect T4-1, so it does not count against the
  task gate. A11 remains eligible for Task 5.

## After Experiment A

- Review the specs of all tasks (Tasks 1 to 5) for ambiguity before any rerun, so that each
  requirement has one reading and the acceptance packs test that reading.
- Make acceptance feedback name the behaviour that fails rather than restating a test
  group's requirements (required for Task 5 from the start).
- Selectively rerun the runs listed above on corrected packs, then rerun the whole
  experiment.
