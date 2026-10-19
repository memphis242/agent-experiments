# Task 3 results (housekeeping staleness detection), 95 runs, 2026-10-19 (preliminary: repetitions 6 to 10 of A1 to A15 in progress)

All 95 valid runs passed acceptance. Billed cost includes compaction requests
(one occurred, in a subagent). Ranges are min to max over the repetitions (five per arm so
far for A1 to A15, ten for A16 and A17); spend per pass is total spend over all attempts
divided by passes. Nineteen attempts from the first wave were cut off by the subscription
session limit (recorded as `cli_error`), dropped and rerun from scratch; they are excluded
here. The first eight runs were made on six parallel slots, the rest of repetitions 1 to 3
on three. A10 to A12 were added later and run on six; nine of their attempts stopped at a
session limit within seconds of starting (recorded as `usage_limit`) and were likewise
rerun. A13 to A15 were added after that and run with up to nine runs in parallel, with no
dropped attempts. A16 and A17 run on six slots. Repetitions 4 and up of A1 to A15 run on
three slots, with up to twelve shortly before a usage-window reset.

## Spread across arms

![Task 3 billed cost per run](figures/task3-cost.svg)

The cost chart's right-hand columns give, per arm, the largest value seen in any one run: input, output and cache-read tokens summed over the main agent and all subagents, then the main agent's context peak and the largest subagent context peak (`-` when the arm spawned no subagents). Bars span min to max, dots are single runs, the tick is the median, and a lighter tint marks conservative arms.

![Task 3 wall time per run](figures/task3-time.svg)

## Full results

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A1 | solo Opus 5.5 | task3 | 5/5 | 8.39 | 7.35 to 10.53 | 3.17 | 19.2 to 24.9 | 95 to 141 | 0 | 1 | 0 | 275.1 to 330.5 | - |
| A2 | Opus 5.5 subagents, eager | task3 | 5/5 | 11.36 | 9.64 to 12.78 | 3.14 | 21.0 to 27.4 | 5 to 15 | 3 to 4 | 1 | 0 | 165.1 to 199.0 | 148.0 to 230.4 |
| A3 | Opus 5.5 subagents, conservative | task3 | 5/5 | 7.12 | 6.16 to 8.25 | 2.09 | 18.1 to 22.6 | 88 to 112 | 0 | 1 | 0 | 242.4 to 291.8 | - |
| A4 | Sonnet 5.5 subagents, eager | task3 | 5/5 | 6.87 | 6.36 to 7.37 | 1.01 | 21.1 to 32.8 | 7 to 13 | 3 to 4 | 1 | 0 | 126.1 to 172.0 | 149.8 to 188.7 |
| A5 | Sonnet 5.5 subagents, conservative | task3 | 5/5 | 8.39 | 7.34 to 9.30 | 1.96 | 19.5 to 30.9 | 101 to 123 | 0 | 1 | 0 | 268.4 to 310.8 | - |
| A6 | Sonnet 5 subagents, eager | task3 | 5/5 | 15.06 | 10.19 to 18.49 | 8.30 | 27.1 to 62.6 | 30 to 83 | 2 to 9 | 2 to 4 | 0 | 164.8 to 224.7 | 191.6 to 381.9 |
| A7 | Sonnet 5 subagents, conservative | task3 | 5/5 | 8.41 | 7.94 to 8.85 | 0.91 | 21.5 to 31.3 | 106 to 123 | 0 | 1 | 0 | 284.2 to 307.3 | - |
| A8 | Sonnet 4.6 subagents, eager | task3 | 5/5 | 14.79 | 10.05 to 18.95 | 8.90 | 28.0 to 48.4 | 22 to 104 | 2 to 4 | 1 to 3 | 0 to 1 | 196.6 to 251.2 | 162.8 to 167.1 |
| A9 | Sonnet 4.6 subagents, conservative | task3 | 5/5 | 7.50 | 6.60 to 8.81 | 2.21 | 19.1 to 27.4 | 93 to 114 | 0 | 1 | 0 | 250.3 to 318.7 | - |
| A10 | solo Sonnet 5.5, xhigh effort | task3 | 5/5 | 7.85 | 6.15 to 12.08 | 5.93 | 26.3 to 62.6 | 6 to 204 | 0 | 1 to 2 | 0 | 403.8 to 595.7 | - |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task3 | 5/5 | 3.96 | 3.20 to 4.90 | 1.71 | 22.9 to 52.6 | 1 to 88 | 1 to 2 | 1 to 2 | 0 | 166.3 to 264.9 | 192.5 to 267.6 |
| A12 | solo Sonnet 5.5, high effort | task3 | 5/5 | 4.13 | 3.74 to 4.52 | 0.77 | 21.8 to 34.5 | 7 to 107 | 0 | 2 | 0 | 265.8 to 316.1 | - |
| A13 | solo Opus 5.5, medium effort | task3 | 5/5 | 7.42 | 6.30 to 8.04 | 1.74 | 21.5 to 28.6 | 89 to 129 | 0 | 1 | 0 | 257.5 to 282.4 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task3 | 5/5 | 3.09 | 2.42 to 3.57 | 1.15 | 20.8 to 26.5 | 14 to 21 | 3 | 1 to 2 | 0 | 53.8 to 91.5 | 182.2 to 302.2 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered | task3 | 5/5 | 4.94 | 4.06 to 5.64 | 1.58 | 17.8 to 43.8 | 1 to 28 | 3 to 4 | 1 to 2 | 0 | 139.9 to 158.5 | 230.7 to 250.3 |
| A16 | solo Haiku 5.5, high effort | task3 | 10/10 | 3.85 | 2.61 to 5.65 | 3.04 | 23.9 to 38.0 | 144 to 279 | 0 | 1 to 2 | 0 | 407.9 to 573.8 | - |
| A17 | solo Haiku 5.5, xhigh effort | task3 | 10/10 | 5.93 | 3.58 to 11.44 | 7.86 | 25.3 to 79.8 | 39 to 412 | 0 | 1 to 3 | 0 | 498.3 to 856.0 | - |

Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A14 0.37, A16 0.46, A11 0.47, A12
0.49, A15 0.59, A17 0.71, A4 0.82, A3 0.85, A13 0.88, A9 0.89, A10 0.94, A5 1.00, A7 1.00,
A2 1.35, A8 1.76, A6 1.79.

Notes:

- Among the original nine arms, eager Sonnet 5.5 delegation (A4) is the cheapest at 0.82
  of A1, with conservative Opus (A3) at 0.85. A4's runs took 7 to 13 main-agent turns: the
  orchestrator did little besides delegate, and its main context peaked at 126K to 172K,
  about half of solo's. With five repetitions its lead over A3 is within the noise band
  below.
- Every conservative arm again spawned zero subagents (20 of 20 runs on this task, 100 of
  100 across Tasks 1 to 3). Their costs sit within noise of solo; the A3 and A5 gap (7.12
  against 8.39 per pass, identical main-agent behaviour) bounds that noise.
- Eager Opus 5.5 (A2) costs 35 percent more than solo: delegating to the same frontier
  model buys context headroom, not savings.
- Eager Sonnet 5 (A6) and eager Sonnet 4.6 (A8) are the expensive outliers at 1.79 and
  1.76 of A1, with cost spreads of 8.30 and 8.90 USD, wider than most arms' whole range.
  Every A6 run needed two to four acceptance rounds; one spawned nine subagents
  (including two debuggers) and took 63 minutes, and one subagent reached 382K tokens of
  context. Four of A8's five runs needed two or three rounds, and its runs took 28 to 48
  minutes. Three of these runs (A6 reps 2 and 4, A8 rep 1) failed every check in round
  one, starting with the build.
- Ending the round while subagents are still working: in seven runs of delegating arms
  (A6 reps 1 and 3, A8 rep 5, A14 reps 2, 4 and 5, A15 rep 4) the main agent ended its
  round-one turn with a line such as "Both test agents are still running. I'll pick up
  once they report." In a headless run that ends the round, so acceptance found no report
  (`report-present`) and sometimes an unfinished test suite. Three of them (A14 reps 4 and
  5, A15 rep 4) failed nothing else, and each passed in round two. The extra round is a
  real cost of orchestration in an unattended session; it also appears on Task 4.
- First compaction in Experiment A: in A8 rep 3 a Sonnet 4.6 subagent auto-compacted
  at 172K tokens (195 seconds for the compaction request). Sonnet 4.6 runs in a 200K
  window; no main agent compacted, including solo Haiku runs peaking at 856K.
- Solo and conservative Opus main agents peaked at 242K to 331K tokens; eager main agents
  at 126K to 251K; eager subagents at 148K to 382K.
- Second or later acceptance rounds: A6 five runs, A8 four, A10 one, A11 three, A12 five,
  A14 three, A15 one, A16 six, A17 five; every other run passed on the first round.
- The fresh-transition defect: no fresh event (ID 41) when input resumes after a stale
  period, caught by the hidden unit test and the live check. It cost a round in every
  A12 run, three A11 runs, one A10 run, six A16 runs and five A17 runs, all arms whose main
  agent is Sonnet 5.5 or Haiku 5.5. No run with an Opus 5.5 main agent failed that check on
  its own. Every A10 to A12 and A16 run fixed it from the round-one feedback.
- A10 to A12 run a Sonnet 5.5 main agent. Solo Sonnet 5.5 at high effort (A12) costs
  0.49 of A1, with every run needing the second round above. At xhigh effort (A10) it
  costs 0.94 of A1 and is among the slowest arms (26 to 63 min); its main context peaked
  at 404K to 596K tokens.
- A11 (Sonnet 5.5 main, Haiku 5.5 implementer and tester only) costs 0.47 of A1. Unlike
  Task 2 it delegated in every run, to one or two Haiku subagents. Its subagents reached 193K
  to 268K tokens of context, comparable to its own main agent's 166K to 265K.
- A13 (solo Opus 5.5 at medium effort) costs 0.88 of A1 with single-round passes and a
  similar context peak (258K to 282K); on this task its saving over high effort is within
  the A3 to A5 noise band.
- A14 (Opus 5.5 at low effort orchestrating Sonnet 5.5 and Haiku 5.5) is the cheapest arm
  on Task 3 at 0.37 of A1. Its main agent peaked at only 54K to 92K tokens of context while
  its subagents reached 182K to 302K. A15 (medium effort) costs 0.59, 60 percent more per
  pass than A14, with a main-agent peak of 140K to 159K. Both delegated in every run,
  nearly always to a Sonnet 5.5 implementer and a Haiku tester.
- A14 rep 2's round one also ended with the unit-test build stopping on an OSAL test
  failure (`osal_filesys_UT`), which left coverage unevaluated. The run did not touch
  OSAL, no other acceptance run has hit that failure, and its cause was not established.
- A15 rep 2 took 44 minutes in a single round, with seven to nine runs sharing the
  machine; it was also the arm's most expensive run at 5.64 USD.
- Turn counts below an arm's typical range (A10's 6, A11's and A15's 1) are the CLI
  counting artefact described in the Task 2 notes; the column will be recomputed from
  transcripts.
- Haiku 5.5 is billed at five times its base rates for any request whose prompt
  exceeds 100,000 tokens. Costs here apply that rule per request; the Haiku subagents of
  A11, A14 and A15 crossed it in some runs, and every solo Haiku run (A16, A17) did.
- A16 (solo Haiku 5.5 at high effort) passed all ten runs at 0.46 of A1, the second
  cheapest arm. Six runs needed a second round, all for the fresh-transition defect, and
  none more than two. Its main context reached 408K to 574K tokens and its runs took 24 to
  38 minutes.
- A17 (solo Haiku 5.5 at xhigh effort) passed all ten runs at 0.71 of A1. Five needed more
  than one round, all starting with the fresh-transition defect; four needed a third,
  two because the hidden unit test still failed and two because the round-two fix broke
  the run's own unit tests. Its main context reached 498K to 856K tokens, the highest of any
  arm on Tasks 1 to 3, with costs of 3.58 to 11.44 USD and wall times of 25 to 80 minutes.
- A16 and A17 both qualify for Task 4 under the task gate: no run failed and none reached
  the final round.
