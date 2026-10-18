# Task 2 results (SAMPLE_APP status command), 150 runs, 2026-10-18

All 150 runs passed acceptance (10 repetitions per arm; for A1 to A9 the first three
were run on three parallel slots and the remaining seven on six; A10 to A15 were added
later and run on six or seven). Billed cost includes compaction
requests (none occurred). Ranges are min to max over the ten repetitions; spend per
pass is total spend over all attempts divided by passes.

## Spread across arms

![Task 2 billed cost per run](figures/task2-cost.svg)

The cost chart's right-hand columns give, per arm, the largest value seen in any one run: input, output and cache-read tokens summed over the main agent and all subagents, then the main agent's context peak and the largest subagent context peak (`-` when the arm spawned no subagents). Bars span min to max, dots are single runs, the tick is the median, and a lighter tint marks conservative arms.

![Task 2 wall time per run](figures/task2-time.svg)

## Full results

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A1 | solo Opus 5.5 | task2 | 10/10 | 3.95 | 2.99 to 4.73 | 1.73 | 11.5 to 18.4 | 53 to 80 | 0 | 1 | 0 | 146.8 to 196.0 | - |
| A2 | Opus 5.5 subagents, eager | task2 | 10/10 | 5.05 | 3.90 to 6.20 | 2.30 | 10.7 to 25.9 | 3 to 19 | 2 to 4 | 1 to 2 | 0 | 71.6 to 133.3 | 75.1 to 120.6 |
| A3 | Opus 5.5 subagents, conservative | task2 | 10/10 | 3.66 | 3.21 to 4.40 | 1.19 | 11.9 to 16.9 | 7 to 75 | 0 | 1 | 0 | 154.6 to 190.8 | - |
| A4 | Sonnet 5.5 subagents, eager | task2 | 10/10 | 3.28 | 2.51 to 4.44 | 1.93 | 9.8 to 28.5 | 6 to 22 | 2 to 4 | 1 | 0 | 91.0 to 140.3 | 67.1 to 148.2 |
| A5 | Sonnet 5.5 subagents, conservative | task2 | 10/10 | 3.75 | 3.13 to 4.66 | 1.54 | 11.1 to 21.7 | 3 to 80 | 0 | 1 | 0 | 149.8 to 199.2 | - |
| A6 | Sonnet 5 subagents, eager | task2 | 10/10 | 6.83 | 5.27 to 10.94 | 5.67 | 15.6 to 44.8 | 6 to 29 | 2 to 4 | 1 to 2 | 0 | 81.0 to 138.4 | 135.7 to 274.6 |
| A7 | Sonnet 5 subagents, conservative | task2 | 10/10 | 3.68 | 3.18 to 4.29 | 1.11 | 12.3 to 17.9 | 52 to 74 | 0 | 1 | 0 | 154.2 to 186.9 | - |
| A8 | Sonnet 4.6 subagents, eager | task2 | 10/10 | 5.56 | 4.59 to 7.18 | 2.59 | 14.7 to 25.6 | 2 to 17 | 2 to 4 | 1 | 0 | 97.2 to 169.9 | 77.2 to 129.5 |
| A9 | Sonnet 4.6 subagents, conservative | task2 | 10/10 | 3.81 | 3.18 to 4.89 | 1.71 | 10.3 to 17.8 | 3 to 75 | 0 | 1 to 2 | 0 | 150.7 to 203.3 | - |
| A10 | solo Sonnet 5.5, xhigh effort | task2 | 10/10 | 3.13 | 2.61 to 4.09 | 1.48 | 17.3 to 25.1 | 66 to 99 | 0 | 1 | 0 | 218.1 to 290.6 | - |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task2 | 10/10 | 1.30 | 1.02 to 2.02 | 1.00 | 7.3 to 20.7 | 1 to 52 | 0 to 1 | 1 | 0 | 118.6 to 169.4 | 140.6 to 154.4 |
| A12 | solo Sonnet 5.5, high effort | task2 | 10/10 | 1.39 | 1.13 to 1.96 | 0.83 | 7.8 to 21.6 | 37 to 52 | 0 | 1 to 2 | 0 | 124.3 to 169.3 | - |
| A13 | solo Opus 5.5, medium effort | task2 | 10/10 | 2.91 | 2.22 to 3.47 | 1.26 | 9.8 to 15.4 | 36 to 66 | 0 | 1 | 0 | 121.0 to 159.6 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5 tiered | task2 | 10/10 | 1.45 | 1.19 to 1.78 | 0.58 | 7.5 to 16.7 | 2 to 11 | 2 to 3 | 1 | 0 | 29.1 to 58.2 | 90.7 to 216.1 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5 tiered | task2 | 10/10 | 2.19 | 1.92 to 2.74 | 0.82 | 9.5 to 20.7 | 4 to 13 | 3 to 4 | 1 | 0 | 78.8 to 102.8 | 136.5 to 186.8 |

Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A11 0.33, A12 0.35, A14 0.37,
A15 0.55, A13 0.74, A10 0.79, A4 0.83, A3 0.93, A7 0.93, A5 0.95, A9 0.96, A2 1.28,
A8 1.41, A6 1.73.

Notes:

- Every conservative arm (A3, A5, A7, A9) spawned zero subagents in all 40 runs, so
  they are behaviourally solo Opus 5.5 runs; their cost ranges sit on top of A1's.
  The configured subagent model had no effect because no subagent ran.
- Eager Sonnet 5.5 delegation (A4) is the only delegation arm cheaper than solo on this
  task. Its ten-run range still overlaps A1's fully, so the difference is a shift of
  the distribution rather than a separation.
- Eager Opus 5.5 (A2) and eager Sonnet 4.6 (A8) cost 28 and 41 percent more per pass
  than solo. Eager Sonnet 5 (A6) is the worst cell: 73 percent more per pass, the
  widest spread (5.67 USD), the longest wall time (44.8 min), four of ten runs
  needing a second acceptance round, and a subagent context peak of 275K tokens.
- Delegation lowers the main agent's context peak (72 to 170K against 147 to 203K for
  solo and conservative arms) and gives the shortest wall times, but also the widest
  cost and wall-time spreads.
- No run compacted at the default window, although several solo and conservative runs
  peaked above 190K tokens of main-agent context.
- Second acceptance rounds: A2 one run, A6 four runs, A9 one run, A12 one run; all
  other runs passed on the first round.
- The aggregate warns that Task 1 runs carry a different run-code hash. The difference
  is the move of post-run analysis out of the run module between the two waves; the
  run pipeline itself was unchanged.
- A10 to A12 run a Sonnet 5.5 main agent. Solo Sonnet 5.5 at high effort (A12) costs
  about a third of solo Opus 5.5 with similar wall time (7.8 to 21.6 min). At xhigh
  effort (A10) the cost rises 2.3-fold (0.79 of A1), wall time roughly doubles (17.3 to
  25.1 min) and the main context peaks at 218 to 291K tokens, above every other arm.
- A11 (Sonnet 5.5 main, Haiku 5.5 implementer and tester only) is the cheapest arm at
  0.33 of A1. It spawned a Haiku tester in 2 of 10 runs (to write coverage tests) and
  otherwise worked alone; the two delegating runs cost 1.31 and 1.62 USD, within its
  solo runs' range.
- Grader fix: the report check required a literal `%` sign in the coverage figures,
  and its feedback did not say so. Three Sonnet 5.5 runs stated coverage as a Percent
  column or "100 percent" and failed that check in every round (A10 reps 2 and 4, A12
  rep 5). The check now also accepts "percent" and covered/total line fractions (it
  only relaxes the rule, so no earlier verdict changes); the three runs were set aside
  under `/workspace/agent-experiments-superseded/grader-coverage-pct/` and their cells
  rerun. The A12 rerun needed a second round for a genuine defect (no status packets
  at the default divisor).
- Turn counts of 1 (A11's "1 to 52") are a counting artefact: when a background job's
  completion notification triggers a short final turn, the CLI's reported turn count
  covers only that turn. The transcripts show the full tool activity; the column will
  be recomputed from transcripts.
- A13 (solo Opus 5.5 at medium effort) costs 0.74 of A1 at high effort, with shorter
  runs (9.8 to 15.4 min) and a lower context peak (121K to 160K against 147K to 196K);
  every run passed in the first round.
- A14 and A15 (tiered delegation) delegated in all 20 runs, always to the Sonnet 5.5
  implementer, and both passed every run in the first round. With the orchestrator at
  low effort (A14, 0.37 of A1) the main agent's context peaked at only 29K to 58K
  tokens, the lowest of any arm; at medium effort (A15, 0.55) it peaked at 79K to 103K
  and cost 51 percent more per pass. A14 sent tests to the Haiku tester in 7 of 10 runs
  and never used the Haiku small-implementer; A15 used the tester in every run and the
  small-implementer in 6.
- Haiku 5.5 is billed at five times its base rates for any request whose prompt
  exceeds 100,000 tokens. Costs here apply that rule per request; the Haiku subagents
  of A11, A14 and A15 crossed it in some runs.
