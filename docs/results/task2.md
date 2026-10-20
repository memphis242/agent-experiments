# Task 2 results (SAMPLE_APP status command), 204 runs, 2026-10-19 (preliminary: A19 in progress)

All 204 completed runs passed acceptance (10 repetitions per arm, except A19, which has 4 so far; for A1 to A9 the first three
were run on three parallel slots and the remaining seven on six; A10 to A15 were added
later and run on six or seven, A16 and A17 on up to nine, A19 on six, the Codex arms A21 and A22 on three, A23 on four). Billed cost includes compaction
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
| A2 | Opus 5.5 subagents, eager | task2 | 10/10 | 5.06 | 3.90 to 6.20 | 2.30 | 10.6 to 23.0 | 3 to 19 | 2 to 4 | 1 | 0 | 96.3 to 133.3 | 75.1 to 120.6 |
| A3 | Opus 5.5 subagents, conservative | task2 | 10/10 | 3.66 | 3.21 to 4.40 | 1.19 | 11.9 to 16.9 | 7 to 75 | 0 | 1 | 0 | 154.6 to 190.8 | - |
| A4 | Sonnet 5.5 subagents, eager | task2 | 10/10 | 3.28 | 2.51 to 4.44 | 1.93 | 9.8 to 28.5 | 6 to 22 | 2 to 4 | 1 | 0 | 91.0 to 140.3 | 67.1 to 148.2 |
| A5 | Sonnet 5.5 subagents, conservative | task2 | 10/10 | 3.75 | 3.13 to 4.66 | 1.54 | 11.1 to 21.7 | 3 to 80 | 0 | 1 | 0 | 149.8 to 199.2 | - |
| A6 | Sonnet 5 subagents, eager | task2 | 10/10 | 6.76 | 4.63 to 10.94 | 6.31 | 11.9 to 44.8 | 7 to 29 | 2 to 4 | 1 to 2 | 0 | 81.0 to 138.4 | 135.7 to 274.6 |
| A7 | Sonnet 5 subagents, conservative | task2 | 10/10 | 3.68 | 3.18 to 4.29 | 1.11 | 12.3 to 17.9 | 52 to 74 | 0 | 1 | 0 | 154.2 to 186.9 | - |
| A8 | Sonnet 4.6 subagents, eager | task2 | 10/10 | 5.56 | 4.59 to 7.18 | 2.59 | 14.7 to 25.6 | 2 to 17 | 2 to 4 | 1 | 0 | 97.2 to 169.9 | 77.2 to 129.5 |
| A9 | Sonnet 4.6 subagents, conservative | task2 | 10/10 | 3.81 | 3.18 to 4.89 | 1.71 | 10.3 to 17.8 | 3 to 75 | 0 | 1 to 2* | 0 | 150.7 to 203.3 | - |
| A10 | solo Sonnet 5.5, xhigh effort | task2 | 10/10 | 3.13 | 2.61 to 4.09 | 1.48 | 17.3 to 25.1 | 66 to 99 | 0 | 1 | 0 | 218.1 to 290.6 | - |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task2 | 10/10 | 1.30 | 1.02 to 2.02 | 1.00 | 7.3 to 20.7 | 1 to 52 | 0 to 1 | 1 | 0 | 118.6 to 169.4 | 140.6 to 154.4 |
| A12 | solo Sonnet 5.5, high effort | task2 | 10/10 | 1.39 | 1.13 to 1.96 | 0.83 | 7.8 to 21.6 | 37 to 52 | 0 | 1 to 2 | 0 | 124.3 to 169.3 | - |
| A13 | solo Opus 5.5, medium effort | task2 | 10/10 | 2.91 | 2.22 to 3.47 | 1.26 | 9.8 to 15.4 | 36 to 66 | 0 | 1 | 0 | 121.0 to 159.6 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task2 | 10/10 | 1.45 | 1.19 to 1.78 | 0.58 | 7.5 to 16.7 | 2 to 11 | 2 to 3 | 1 | 0 | 29.1 to 58.2 | 90.7 to 216.1 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered | task2 | 10/10 | 2.19 | 1.92 to 2.74 | 0.82 | 9.5 to 20.7 | 4 to 13 | 3 to 4 | 1 | 0 | 78.8 to 102.8 | 136.5 to 186.8 |
| A16 | solo Haiku 5.5, high effort | task2 | 10/10 | 1.26 | 0.93 to 2.07 | 1.14 | 12.1 to 31.7 | 87 to 183 | 0 | 1 to 2 | 0 | 245.1 to 323.3 | - |
| A17 | solo Haiku 5.5, xhigh effort | task2 | 10/10 | 2.25 | 1.55 to 4.16 | 2.61 | 18.0 to 33.2 | 6 to 221 | 0 | 1 to 2 | 0 | 315.8 to 469.0 | - |
| A19 | solo Sonnet 5, high effort | task2 | 4/4† | 6.52 | 5.61 to 7.36 | 1.75 | 15.9 to 27.8 | 135 to 185 | 0 | 1 | 0 | 248.8 to 294.0 | - |
| A21 | Claude Code Opus 5.5 high + headless Codex GPT-6.1 Sol high, eager | task2 | 10/10 | 2.80 | 2.19 to 3.32 | 1.13 | 11.0 to 16.5 | 3 to 44 | 3 to 4 | 1 | 0 | 76.7 to 132.1 | 67.7 to 103.5 |
| A22 | Claude Code Opus 5.5 high + headless Codex GPT-6.1 Sol high, implementer, tester and secondary debugger | task2 | 10/10 | 3.15 | 2.57 to 3.80 | 1.22 | 9.4 to 12.6 | 10 to 56 | 0 to 1 | 1 | 0 | 137.2 to 171.8 | 57.4 to 82.7 |
| A23 | Opus 5.5 low main, Codex GPT-6.1 Sol + GPT-6 Luna, tiered | task2 | 10/10 | 1.22 | 1.00 to 1.52 | 0.53 | 7.4 to 13.8 | 6 to 19 | 1 to 2 | 1 | 0 | 32.4 to 59.9 | 70.3 to 127.6 |

Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A23 0.31, A16 0.32, A11 0.33, A12
0.35, A14 0.37, A15 0.55, A17 0.57, A21 0.71, A13 0.74, A10 0.79, A22 0.80, A4 0.83, A3
0.93, A7 0.93, A5 0.95, A9 0.96, A2 1.28, A8 1.41, A6 1.71. A19 (4 of 10 runs so far) is not
ranked; it would be 1.65.

Notes:

- Every conservative arm (A3, A5, A7, A9) spawned zero subagents in all 40 runs, so
  they are behaviourally solo Opus 5.5 runs; their cost ranges sit on top of A1's.
  The configured subagent model had no effect because no subagent ran.
- Eager Sonnet 5.5 delegation (A4) is the only delegation arm cheaper than solo on this
  task. Its ten-run range still overlaps A1's fully, so the difference is a shift of
  the distribution rather than a separation.
- Eager Opus 5.5 (A2) and eager Sonnet 4.6 (A8) cost 28 and 41 percent more per pass
  than solo. Eager Sonnet 5 (A6) is the worst cell: 71 percent more per pass, the
  widest spread (6.31 USD), the longest wall time (44.8 min), three of ten runs
  needing a second acceptance round, and a subagent context peak of 275K tokens.
  Those three runs (reps 4, 5 and 10) are also the A6 runs in which the CLI stopped a
  background subagent at its ten-minute print-mode wait ceiling; they are still to be
  rerun with the raised ceiling, so A6's row is provisional.
- Delegation lowers the main agent's context peak (81 to 170K against 147 to 203K for
  solo and conservative arms) and gives the shortest wall times, but also the widest
  cost and wall-time spreads.
- No run compacted at the default window, although several solo and conservative runs
  peaked above 190K tokens of main-agent context.
- Second acceptance rounds: A6 three runs, A9 one run, A12 one run, A16 one
  run, A17 one run; all other runs passed on the first round.
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
  of A11, A14 and A15 crossed it in some runs, and every solo Haiku run (A16, A17) did.
- A16 and A17 (solo Haiku 5.5 at high and xhigh effort) both passed all ten runs and
  qualify for Task 3 under the task gate. At high effort (A16) solo Haiku is the cheapest
  arm on Task 2 at 0.32 of A1, level with A11 and A12; at xhigh (A17) it costs 0.57 with
  the widest spread of the cheap arms (1.55 to 4.16 USD). Each needed one second round,
  both for the same check (the status packet's default divisor). Solo Haiku is slower
  than most arms (12 to 33 minutes; only eager Sonnet 5, A6, runs longer) and reaches the highest main contexts on
  the task, 245K to 323K (A16) and 316K to 469K (A17), with no compaction. A17's turn
  floor of 6 is the CLI counting artefact described below.
- A21 and A22 run the A2 main agent (Claude Code Opus 5.5 at high effort) with
  headless Codex GPT-6.1 Sol subagents at high effort; both passed all ten runs in one
  round. Their cost includes the Codex subagents, priced at OpenAI's API rates for
  GPT-6.1 Sol (see the design document). Eager Codex delegation (A21) is the cheapest
  eager arm on the task at 0.71 of A1 (2.80 USD per run, of which Codex is 0.98, 35
  percent), against 0.83 for eager Sonnet 5.5 subagents (A4) and 1.28 for eager Opus 5.5
  subagents (A2), and it lowers the main agent's context peak to 77K to 132K. A22's
  selective delegation costs 0.80 of A1 (3.15 USD per run, of which Codex is 0.18, 6
  percent); its main agent worked alone in 4 of 10 runs (reps 1, 4, 5 and 6) and peaked
  at 137K to 172K, close to the solo arms. The four Codex pilot runs, made on the
  delegation text before it forbade polling a background subagent, are excluded (see
  `superseded.json` and the rerun ledger).
- A23 (added 2026-10-20) is the Codex counterpart of A14: Opus 5.5 at low effort
  directing headless Codex subagents, the implementer on GPT-6.1 Sol and the small
  implementer and tester on GPT-6 Luna, all at high effort, with A14's tiered delegation
  text. It is the cheapest arm on the task: all ten runs passed in one round at 1.22 USD
  per pass, 0.31 of A1 against A14's 0.37, of which Codex is 0.58 per run (48 percent).
  Its Opus main context peaked at 32K to 60K tokens. Twelve of its fourteen Codex calls
  went to the Sol implementer.
- A2 rep 7 and A6 rep 2 originally lost a background subagent to the CLI's ten-minute
  print-mode wait ceiling. Both were rerun with the raised ceiling and the originals are
  excluded (`superseded.json`); A2's spend per pass is unchanged at 1.28 of A1 and A6's
  moves from 1.73 to 1.71.
- † A19 (solo Sonnet 5 at high effort) failed Task 1 under the task gate (see the Task 1
  report) and runs Task 2 as a deliberate trial: one repetition, and the full ten only if
  it passes. Rep 1 passed on the first round at 5.89 USD, so reps 2 to 10 were queued,
  at the end of the queue behind Task 4. Reps 1 to 4 all passed on the first round, at
  1.65 times A1's spend per pass, the most expensive solo arm on the task; this row will be
  completed when reps 5 to 10 finish.
- \* A9 rep 6 lost round 1 to a race between OSAL's own functional tests (a shared `./test`
  mount point under the parallel mission test run), not to its code; it passed round 2
  unchanged. It is listed in [rerun-ledger.md](rerun-ledger.md) for a rerun once the race is
  fixed.
