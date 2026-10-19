# Task 3 results (housekeeping staleness detection), 58 runs, 2026-10-18 (preliminary: A17 in progress)

All 58 valid runs passed acceptance. Billed cost includes compaction requests
(one occurred, in a subagent). Ranges are min to max over the repetitions (three per arm, ten for A16 so far);
spend per pass is total spend over all attempts divided by passes. Nineteen
attempts from the first wave were cut off by the subscription session limit
(recorded as `cli_error`), dropped and rerun from scratch; they are excluded here.
The first eight runs were made on six parallel slots, the rest on three. A10 to A12
were added later and run on six; nine of their attempts stopped at a session limit
within seconds of starting (recorded as `usage_limit`) and were likewise rerun.
A13 to A15 were added after that and run with up to nine runs in parallel, with no
dropped attempts. A16 and A17 run on six slots.

## Spread across arms

![Task 3 billed cost per run](figures/task3-cost.svg)

The cost chart's right-hand columns give, per arm, the largest value seen in any one run: input, output and cache-read tokens summed over the main agent and all subagents, then the main agent's context peak and the largest subagent context peak (`-` when the arm spawned no subagents). Bars span min to max, dots are single runs, the tick is the median, and a lighter tint marks conservative arms.

![Task 3 wall time per run](figures/task3-time.svg)

## Full results

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A1 | solo Opus 5.5 | task3 | 3/3 | 8.83 | 7.53 to 10.53 | 3.00 | 21.4 to 24.8 | 96 to 141 | 0 | 1 | 0 | 275.1 to 330.5 | - |
| A2 | Opus 5.5 subagents, eager | task3 | 3/3 | 11.92 | 10.67 to 12.78 | 2.11 | 24.7 to 27.2 | 7 to 15 | 3 to 4 | 1 | 0 | 165.1 to 199.0 | 148.0 to 210.2 |
| A3 | Opus 5.5 subagents, conservative | task3 | 3/3 | 6.85 | 6.16 to 7.85 | 1.69 | 18.1 to 22.6 | 88 to 103 | 0 | 1 | 0 | 242.4 to 291.3 | - |
| A4 | Sonnet 5.5 subagents, eager | task3 | 3/3 | 6.87 | 6.56 to 7.18 | 0.62 | 21.1 to 23.7 | 7 | 3 to 4 | 1 | 0 | 143.1 to 172.0 | 162.3 to 188.7 |
| A5 | Sonnet 5.5 subagents, conservative | task3 | 3/3 | 8.45 | 7.34 to 9.30 | 1.96 | 19.5 to 30.9 | 101 to 123 | 0 | 1 | 0 | 268.4 to 310.8 | - |
| A6 | Sonnet 5 subagents, eager | task3 | 3/3 | 14.09 | 10.19 to 18.49 | 8.30 | 27.1 to 62.6 | 35 to 83 | 2 to 9 | 2 to 4 | 0 | 165.3 to 224.7 | 191.6 to 381.9 |
| A7 | Sonnet 5 subagents, conservative | task3 | 3/3 | 8.40 | 7.94 to 8.85 | 0.91 | 21.5 to 31.3 | 106 to 123 | 0 | 1 | 0 | 287.8 to 296.2 | - |
| A8 | Sonnet 4.6 subagents, eager | task3 | 3/3 | 15.31 | 10.05 to 18.95 | 8.90 | 35.5 to 43.6 | 22 to 104 | 2 to 4 | 1 to 3 | 0 to 1 | 196.6 to 251.2 | 163.0 to 165.3 |
| A9 | Sonnet 4.6 subagents, conservative | task3 | 3/3 | 7.58 | 6.60 to 8.81 | 2.21 | 19.1 to 27.4 | 96 to 114 | 0 | 1 | 0 | 250.3 to 318.7 | - |
| A10 | solo Sonnet 5.5, xhigh effort | task3 | 3/3 | 7.00 | 6.39 to 7.73 | 1.34 | 30.2 to 35.0 | 6 to 121 | 0 | 1 | 0 | 429.6 to 456.2 | - |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task3 | 3/3 | 3.51 | 3.20 to 3.80 | 0.60 | 22.9 to 52.6 | 1 to 27 | 1 to 2 | 1 to 2 | 0 | 166.3 to 227.8 | 237.4 to 267.6 |
| A12 | solo Sonnet 5.5, high effort | task3 | 3/3 | 4.03 | 3.74 to 4.52 | 0.77 | 21.8 to 28.0 | 97 to 107 | 0 | 2 | 0 | 265.8 to 316.1 | - |
| A13 | solo Opus 5.5, medium effort | task3 | 3/3 | 7.40 | 6.30 to 8.04 | 1.74 | 21.5 to 28.6 | 89 to 129 | 0 | 1 | 0 | 257.5 to 282.4 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task3 | 3/3 | 3.38 | 3.06 to 3.57 | 0.51 | 20.8 to 26.5 | 14 to 20 | 3 | 1 to 2 | 0 | 66.8 to 91.5 | 216.0 to 302.2 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered | task3 | 3/3 | 4.77 | 4.06 to 5.64 | 1.58 | 17.8 to 43.8 | 1 to 7 | 3 to 4 | 1 | 0 | 144.5 to 158.5 | 230.7 to 248.0 |
| A16 | solo Haiku 5.5, high effort | task3 | 10/10 | 3.85 | 2.61 to 5.65 | 3.04 | 23.9 to 38.0 | 144 to 279 | 0 | 1 to 2 | 0 | 407.9 to 573.8 | - |
| A17 | solo Haiku 5.5, xhigh effort | task3 | 3/3† | 4.59 | 3.58 to 5.46 | 1.88 | 25.3 to 33.7 | 176 to 274 | 0 | 1 | 0 | 498.3 to 584.4 | - |
Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A14 0.38, A11 0.40, A16 0.44, A12
0.46, A17 0.52, A15 0.54, A3 0.78, A4 0.78, A10 0.79, A13 0.84, A9 0.86, A7 0.95, A5
0.96, A2 1.35, A6 1.60, A8 1.73.

Notes:

- Among the Opus 5.5 main-agent arms, eager Sonnet 5.5 delegation (A4) is the
  cheapest together with conservative Opus (A3), 22 percent under solo, with the tightest spread of the task (0.62 USD).
  Its three runs each took seven main-agent turns: the orchestrator did little
  besides delegate, and the main agent's context peaked at about half of solo's.
  This is the first task on which a delegation arm separates from solo rather than
  merely shifting the distribution.
- Every conservative arm again spawned zero subagents (21 of 21 runs on this task,
  58 of 58 across Tasks 1 to 3). Their costs sit within noise of solo; the A3 and
  A5 gap (6.85 against 8.45 per pass, identical main-agent behaviour) bounds that
  noise.
- Eager Opus 5.5 (A2) costs 35 percent more than solo and is slower in wall time:
  delegating to the same frontier model buys context headroom, not savings.
- Eager Sonnet 5 (A6) and eager Sonnet 4.6 (A8) are the expensive outliers at 60
  and 73 percent above solo, with cost spreads of 8.30 and 8.90 USD, wider than any
  other arm's whole range. A6 needed two to four acceptance rounds in every run;
  one run spawned nine subagents (including two debuggers) and took 63 minutes, and
  one subagent reached 382K tokens of context. A8 needed one to three rounds, and
  its runs took 36 to 44 minutes.
- First compaction in Experiment A: in A8 rep 3 a Sonnet 4.6 subagent auto-compacted
  at 172K tokens (195 seconds for the compaction request). Sonnet 4.6 runs in a 200K
  window; no main agent compacted, including solo runs peaking at 331K on Opus 5.5's
  1M window.
- Solo and conservative Opus main agents peaked at 242K to 331K tokens; eager main agents
  at 143K to 251K; eager subagents at 148K to 382K.
- Second or later acceptance rounds: A6 three runs, A8 two runs, A11 one run, A12
  three runs, A14 one run, A16 six runs; every other run passed on the first round.
- A10 to A12 run a Sonnet 5.5 main agent. Solo Sonnet 5.5 at high effort (A12) costs
  0.46 of A1, but all three of its runs needed a second round for the same genuine
  defect: no fresh event (ID 41) when input resumes after a stale period, caught by
  the hidden unit tests and the live check. Each run fixed it from the round-one
  feedback. A11 rep 3 missed the same event and also passed in round two; no Opus
  5.5 main-agent run failed that check.
- At xhigh effort (A10) solo Sonnet 5.5 passed every run in the first round, at 0.79
  of A1 and the slowest wall time of the single-round arms (30 to 35 min). Its main
  context peaked at 430K to 456K tokens, the highest of any arm on Tasks 1 to 3, still
  well inside the 1M window with no compaction.
- A11 (Sonnet 5.5 main, Haiku 5.5 implementer and tester only) is the cheapest arm at
  0.40 of A1. Unlike Task 2 it delegated in every run: a Haiku tester in all three
  (two in rep 3) and a Haiku implementer once. Its subagents reached 237K to 268K
  tokens of context, above its own main agent's 166K to 228K. Rep 3's 53 minutes
  include the second round.
- A13 (solo Opus 5.5 at medium effort) costs 0.84 of A1 with single-round passes and
  a similar context peak (258K to 282K). Its saving over high effort is smaller than on
  Task 2 (0.74) and within the A3 to A5 noise band noted above.
- A14 (Opus 5.5 at low effort orchestrating Sonnet 5.5 and Haiku 5.5) is the cheapest
  arm on Task 3 at 0.38 of A1, edging out A11, with the tightest cost spread of the task
  (0.51 USD). Its main agent peaked at only 67K to 92K tokens of context while its
  subagents reached 216K to 302K. A15 (medium effort) costs 0.54, 41 percent more per
  pass than A14, with a main-agent peak of 145K to 159K. Both delegated in every run, to
  a Sonnet 5.5 implementer every time and a Haiku tester every time; the Haiku
  small-implementer was used in one A14 run and two A15 runs.
- A14 rep 2's second round came from orchestration rather than a code defect: the main
  agent ended its round-one turn with "Both testers are running; waiting for results"
  while two background Haiku testers were still working. In a headless run that ends the
  round, so acceptance found no report, and the unit-test build stopped on an OSAL test
  failure (`osal_filesys_UT`), which left coverage unevaluated. The run did not touch
  OSAL, no other acceptance run has hit that failure, and its cause was not established.
  Every other check, including the hidden unit test and the live checks, passed in round
  one; round two added 0.29 USD and passed.
- A15 rep 2 took 44 minutes in a single round, with seven to nine runs sharing the
  machine; it was also the arm's most expensive run at 5.64 USD.
- Turn counts below the arm's typical range (A10's 6, A11's 1) are the CLI counting
  artefact described in the Task 2 notes; the column will be recomputed from
  transcripts.
- Haiku 5.5 is billed at five times its base rates for any request whose prompt
  exceeds 100,000 tokens. Costs here apply that rule per request; the Haiku subagents of
  A11, A14 and A15 crossed it in some runs, and every solo Haiku run (A16, A17) did.
- A16 (solo Haiku 5.5 at high effort) passed all ten Task 3 runs, at 0.44 of A1, between
  A11 and A12, and qualifies for Task 4 under the task gate. Six of its runs needed a
  second round, every one for the defect that also cost A11 and A12 a round: no fresh
  event (ID 41) when input resumes after a stale period, caught by the hidden unit test
  and the live check. No run needed more than two rounds. Its main context reached 408K
  to 574K tokens, the highest of any arm so far, with no compaction, and its runs took 24
  to 38 minutes.
- † A17 (solo Haiku 5.5 at xhigh effort) has 3 of its 10 Task 3 runs complete, all
  passing in the first round at 0.52 of A1 with main contexts of 498K to 584K; the row is
  preliminary until the remaining seven finish.
