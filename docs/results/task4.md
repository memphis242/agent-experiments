# Task 4 results (STOR storage app), 17 runs, 2026-10-19 (preliminary: in progress)

Task 4 runs first for the finalist arms (A1, A2, A4, A11, A12, A14, A15) and the solo Haiku
arms that passed the Task 1 to 3 gates (A16, A17), three repetitions each; the other arms
follow. 17 of those 27 finalist runs are complete. Billed cost includes compaction requests.
Ranges are min to max over the completed repetitions; spend per pass is total spend over all
attempts divided by passes. Attempts cut off by a subscription usage limit (recorded as
`usage_limit` or `cli_error`) are dropped and rerun from scratch; ten finalist attempts were
lost this way on 2026-10-19 and are being rerun. Runs use three parallel slots, with up to
twelve shortly before a usage-window reset.

## Spread across arms

![Task 4 billed cost per run](figures/task4-cost.svg)

The cost chart's right-hand columns give, per arm, the largest value seen in any one run: input, output and cache-read tokens summed over the main agent and all subagents, then the main agent's context peak and the largest subagent context peak (`-` when the arm spawned no subagents). Bars span min to max, dots are single runs (hollow when the run did not pass), the tick is the median, and a lighter tint marks conservative arms.

![Task 4 wall time per run](figures/task4-time.svg)

## Full results

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A1 | solo Opus 5.5 | task4 | 3/3 | 20.75 | 18.23 to 22.69 | 4.46 | 59.7 to 70.3 | 171 to 194 | 0 | 2 | 0 | 474.1 to 549.2 | - |
| A2 | Opus 5.5 subagents, eager | task4 | 1/1 | 32.83 | 32.83 | 0.00 | 89.5 | 33 | 9 | 3 | 0 | 170.8 | 279.8 |
| A4 | Sonnet 5.5 subagents, eager | task4 | 1/1 | 15.35 | 15.35 | 0.00 | 67.9 | 48 | 6 | 4 | 0 | 193.5 | 258.5 |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task4 | 2/2 | 8.57 | 8.12 to 9.03 | 0.91 | 37.2 to 39.5 | 16 to 21 | 3 to 5 | 1 to 2 | 0 | 293.9 to 312.6 | 322.9 to 362.2 |
| A12 | solo Sonnet 5.5, high effort | task4 | 1/2 | 30.33 | 9.17 to 21.17 | 12.00 | 48.0 to 104.9 | 98 to 150 | 0 | 1 to 5 | 0 | 498.6 to 764.6 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task4 | 2/2 | 8.47 | 8.40 to 8.53 | 0.14 | 55.4 to 59.4 | 10 to 32 | 4 to 7 | 2 to 3 | 0 | 64.1 to 82.0 | 299.0 to 427.5 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered | task4 | 2/2 | 11.12 | 9.52 to 12.72 | 3.20 | 48.8 to 58.1 | 33 to 51 | 3 to 6 | 2 to 3 | 0 | 132.8 to 223.4 | 358.8 to 445.5 |
| A16 | solo Haiku 5.5, high effort | task4 | 0/2 | - | 13.36 to 14.43 | 1.07 | 99.2 to 100.3 | 182 to 478 | 0 | 5 | 1 | 963.7 to 965.9 | - |
| A17 | solo Haiku 5.5, xhigh effort | task4 | 1/2 | 24.16 | 10.61 to 13.55 | 2.95 | 95.2 to 108.2 | 321 to 335 | 0 | 2 to 5 | 1 | 964.8 to 965.0 | - |

Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A14 0.41, A11 0.41, A15 0.54, A4
0.74, A17 1.16, A12 1.46, A2 1.58. A16 has no passing run.

Notes:

- Task 4 is the first task on which arms fail runs: four runs used all five acceptance
  rounds without passing, A12 rep 3, A16 reps 2 and 3, and A17 rep 3. Under the task gate
  (no run may fail all five rounds) A12, A16 and A17 cannot advance to Task 5, whatever
  their remaining repetitions do.
- Every A1 run needed a second round, and so did every completed run of the delegating
  arms except one A11 run: Task 4 is the first task where a single round is the
  exception. The most common round-one misses were the hidden coverage test (unit tests
  driving `STOR_AppMain` through the software bus) and the quota-drop live check.
- A12 (solo Sonnet 5.5 at high effort) rep 3 was down to one failed assertion after round
  one, "subscribed to each of the four table entries", and kept it for four more rounds.
  Its `STOR_Subscribe` calls `CFE_SB_SubscribeEx` and then, on success, `CFE_SB_Subscribe`
  for the same message ID, so every table entry is subscribed twice and the test counts 8
  where it expects 4. The spec says STOR "subscribes to every used entry's `MsgId`"; it
  does not say "once", but a duplicate subscription is a defect in cFS. The feedback each
  round restated the startup requirements, which the run read as already met. Like solo
  Sonnet 5 on Task 1, the run did not change direction under repeated identical
  feedback. Its other run passed in one round at 9.17 USD; rep 3 cost 21.17 USD and took
  105 minutes; the arm's main context peaked at 765K tokens.
- Solo Haiku (A16, A17) reached the edge of the 1M context window: every run peaked at
  964K to 966K tokens and auto-compacted once, the first main-agent compactions in
  Experiment A. Its three failed runs kept failing packet-file roll-over assertions in
  the hidden coverage test (A16 rep 3 also the file listing check). Solo Haiku runs took
  95 to 108 minutes, the slowest of the task. A17 rep 2 passed in round two at 10.61 USD.
- Ending the round while subagents are still working (see the Task 3 notes) cost a round
  in six of the eight completed runs of delegating arms: A2 rep 3, A4 rep 1, A14 reps 2
  and 3, and A15 reps 2 and 3. A15 rep 2's round one and A2 rep 3's round two failed every
  check, the build included, consistent with the round ending part-way through the
  subagents' changes. A4 rep 1 needed four rounds, the last two only for the missing
  report.
- The tiered arms (A14, A15) and A11 are the cheapest so far at 0.41, 0.41 and 0.54 of
  A1, with the tightest spreads. A14's main agent peaked at only 64K to 82K tokens while
  its subagents reached 299K to 428K. A11 spawned three to five Haiku subagents per run,
  against at most two on Task 3.
- Solo Opus 5.5 (A1) peaked at 474K to 549K tokens of main context without compacting,
  and took 60 to 70 minutes per run.
- Eager Opus 5.5 (A2) is the most expensive arm so far at 1.58 of A1 (one run, nine
  subagents, three rounds, 90 minutes).
- Turn counts are main-agent turns summed over rounds and include the CLI counting
  artefact described in the Task 2 notes; the column will be recomputed from transcripts.
