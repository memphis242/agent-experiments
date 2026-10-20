# Task 3 results (housekeeping staleness detection), 117 runs, 2026-10-20

All 117 valid runs passed acceptance. Billed cost includes compaction requests
(one occurred, in a subagent). Ranges are min to max over the repetitions (five per arm so
far for A1 to A15, ten for A16 and A17); spend per pass is total spend over all attempts
divided by passes. Nineteen attempts from the first wave were cut off by the subscription
session limit (recorded as `cli_error`), dropped and rerun from scratch; they are excluded
here. The first eight runs were made on six parallel slots, the rest of repetitions 1 to 3
on three. A10 to A12 were added later and run on six; nine of their attempts stopped at a
session limit within seconds of starting (recorded as `usage_limit`) and were likewise
rerun. A13 to A15 were added after that and run with up to nine runs in parallel, with no
dropped attempts. A16 and A17 run on six slots. Repetitions 4 and up of A1 to A15 run on
three slots, with up to twelve shortly before a usage-window reset. A1 and A2 have a sixth
repetition from the start of the extension to ten, which is on hold. The Codex arms (A21,
A22) ran ten repetitions each on six slots; their cost includes the Codex subagents at
GPT-6.1 Sol API rates. Twenty runs that lost rounds to a defect outside their own code
have been rerun (see the footnote); each rerun replaces its original here.

## Spread across arms

![Task 3 billed cost per run](figures/task3-cost.svg)

The cost chart's right-hand columns give, per arm, the largest value seen in any one run: input, output and cache-read tokens summed over the main agent and all subagents, then the main agent's context peak and the largest subagent context peak (`-` when the arm spawned no subagents). Bars span min to max, dots are single runs, the tick is the median, and a lighter tint marks conservative arms.

![Task 3 wall time per run](figures/task3-time.svg)

## Full results

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A1 | solo Opus 5.5 | task3 | 6/6 | 8.51 | 7.35 to 10.53 | 3.17 | 19.2 to 26.7 | 1 to 141 | 0 | 1 | 0 | 275.1 to 330.9 | - |
| A2 | Opus 5.5 subagents, eager | task3 | 6/6 | 11.55 | 9.64 to 12.78 | 3.14 | 21.0 to 27.4 | 2 to 15 | 3 to 5 | 1 | 0 | 165.1 to 199.0 | 146.8 to 230.4 |
| A3 | Opus 5.5 subagents, conservative | task3 | 5/5 | 7.12 | 6.16 to 8.25 | 2.09 | 18.1 to 22.6 | 88 to 112 | 0 | 1 | 0 | 242.4 to 291.8 | - |
| A4 | Sonnet 5.5 subagents, eager | task3 | 5/5 | 6.87 | 6.36 to 7.37 | 1.01 | 21.1 to 32.8 | 7 to 13 | 3 to 4 | 1 | 0 | 126.1 to 172.0 | 149.8 to 188.7 |
| A5 | Sonnet 5.5 subagents, conservative | task3 | 5/5 | 8.39 | 7.34 to 9.30 | 1.96 | 19.5 to 30.9 | 101 to 123 | 0 | 1 | 0 | 268.4 to 310.8 | - |
| A6 | Sonnet 5 subagents, eager | task3 | 5/5 | 14.88 | 10.19 to 18.49 | 8.30 | 26.3 to 62.6 | 10 to 83 | 2 to 9 | 1 to 4* | 0 | 164.8 to 218.3 | 191.6 to 381.9 |
| A7 | Sonnet 5 subagents, conservative | task3 | 5/5 | 8.41 | 7.94 to 8.85 | 0.91 | 21.5 to 31.3 | 106 to 123 | 0 | 1 | 0 | 284.2 to 307.3 | - |
| A8 | Sonnet 4.6 subagents, eager | task3 | 5/5 | 16.51 | 11.86 to 18.95 | 7.09 | 28.0 to 48.4 | 9 to 79 | 3 to 5 | 1 to 3* | 0 to 1 | 146.4 to 214.2 | 162.8 to 167.1 |
| A9 | Sonnet 4.6 subagents, conservative | task3 | 5/5 | 7.50 | 6.60 to 8.81 | 2.21 | 19.1 to 27.4 | 93 to 114 | 0 | 1 | 0 | 250.3 to 318.7 | - |
| A10 | solo Sonnet 5.5, xhigh effort | task3 | 5/5 | 7.20 | 6.15 to 8.85 | 2.70 | 26.3 to 40.4 | 6 to 174 | 0 | 1 | 0 | 403.8 to 503.8 | - |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task3 | 5/5 | 3.36 | 3.12 to 3.54 | 0.42 | 18.1 to 35.8 | 1 to 33 | 1 to 2 | 1 | 0 | 163.6 to 227.8 | 237.4 to 276.6 |
| A12 | solo Sonnet 5.5, high effort | task3 | 5/5 | 3.70 | 3.25 to 3.90 | 0.65 | 18.2 to 24.2 | 89 to 109 | 0 | 1 | 0 | 255.1 to 290.0 | - |
| A13 | solo Opus 5.5, medium effort | task3 | 5/5 | 7.42 | 6.30 to 8.04 | 1.74 | 21.5 to 28.6 | 89 to 129 | 0 | 1 | 0 | 257.5 to 282.4 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task3 | 5/5 | 3.47 | 3.29 to 3.59 | 0.30 | 19.5 to 26.5 | 4 to 17 | 3 | 1 | 0 | 65.1 to 91.5 | 228.3 to 302.2 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered | task3 | 5/5 | 4.94 | 4.06 to 5.64 | 1.58 | 17.8 to 43.8 | 1 to 28 | 3 to 4 | 1 to 2* | 0 | 139.9 to 158.5 | 230.7 to 250.3 |
| A16 | solo Haiku 5.5, high effort | task3 | 10/10 | 3.19 | 1.85 to 4.24 | 2.38 | 22.3 to 45.3 | 130 to 232 | 0 | 1 to 2 | 0 | 361.9 to 549.8 | - |
| A17 | solo Haiku 5.5, xhigh effort | task3 | 10/10 | 5.93 | 3.58 to 11.44 | 7.86 | 25.3 to 79.8 | 39 to 412 | 0 | 1 to 3* | 0 | 498.3 to 856.0 | - |
| A21 | Opus 5.5 high main, Codex GPT-6.1 Sol high subagents, eager | task3 | 10/10 | 5.41 | 4.65 to 6.57 | 1.92 | 17.0 to 30.3 | 5 to 31 | 2 to 8 | 1 | 0 | 148.6 to 198.1 | 99.6 to 112.5 |
| A22 | Opus 5.5 high main, Codex GPT-6.1 Sol high, roles + debugger | task3 | 10/10 | 6.56 | 5.04 to 7.28 | 2.24 | 15.0 to 26.0 | 11 to 99 | 1 to 2 | 1 | 0 | 188.2 to 260.6 | 80.2 to 114.8 |

Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A16 0.37, A11 0.39, A14 0.41, A12
0.43, A15 0.58, A21 0.64, A17 0.70, A22 0.77, A4 0.81, A3 0.84, A10 0.85, A13 0.87, A9 0.88,
A5 0.99, A7 0.99, A2 1.36, A6 1.75, A8 1.94. Arms with asterisked runs not rerun (A6, A8,
A15, A17) carry rounds lost to the pack defect or the wait ceiling.

Notes:

- Among the original nine arms, eager Sonnet 5.5 delegation (A4) is the cheapest at 0.81
  of A1, with conservative Opus (A3) at 0.84. A4's runs took 7 to 13 main-agent turns: the
  orchestrator did little besides delegate, and its main context peaked at 126K to 172K,
  about half of solo's. With five repetitions its lead over A3 is within the noise band
  below.
- Every conservative arm again spawned zero subagents (20 of 20 runs on this task, 100 of
  100 across Tasks 1 to 3). Their costs sit within noise of solo; the A3 and A5 gap (7.12
  against 8.39 per pass, identical main-agent behaviour) bounds that noise.
- Eager Opus 5.5 (A2) costs 36 percent more than solo: delegating to the same frontier
  model buys context headroom, not savings.
- Eager Sonnet 5 (A6) and eager Sonnet 4.6 (A8) are the expensive outliers at 1.75 and
  1.94 of A1, with cost spreads of 8.30 and 7.09 USD, wider than most arms' whole range.
  Each original A6 run needed two to four acceptance rounds; one spawned nine subagents
  (including two debuggers) and took 63 minutes, and one subagent reached 382K tokens of
  context. A6 rep 2 and 4 and the original A8 rep 1 failed every check in round one,
  starting with the build. The reruns of A6 rep 1 and A8 rep 1, with the raised wait
  ceiling, passed in one round but still cost 12.67 and 18.63 USD, so these arms stay the
  most expensive without the lost rounds.
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
- Second or later acceptance rounds: A6 four runs, A8 three, A15 one, A16 one, A17 five;
  every other run passed on the first round. All are asterisked runs except A16 rep 3's
  rerun, whose round one missed a required entry in the default copy table.
- The fresh-transition defect: no fresh event (ID 41) when input resumes after a stale
  period, caught by the hidden unit test and the live check. It cost a round in every
  A12 run, three A11 runs, one A10 run, six A16 runs and five A17 runs, all arms whose main
  agent is Sonnet 5.5 or Haiku 5.5. No run with an Opus 5.5 main agent failed that check on
  its own. Every A10 to A12 and A16 run fixed it from the round-one feedback. On the corrected
  pack all seventeen reruns of these runs passed without that failure: A10 rep 4, A11
  reps 3 to 5, A12 reps 1 to 5 and A16 reps 2 to 7 (A16 rep 3 needed a second round for a
  different miss, above).
- A10 to A12 run a Sonnet 5.5 main agent. Solo Sonnet 5.5 at high effort (A12) costs
  0.43 of A1; every original run needed the second round above, and all five reruns passed
  in one round in 18 to 24 minutes. At xhigh effort (A10)
  it costs 0.85 of A1 and is among the slower arms (26 to 40 min); its main context peaked
  at 404K to 504K tokens.
- A11 (Sonnet 5.5 main, Haiku 5.5 implementer and tester only) is the cheapest arm on
  Task 3 at 0.39 of A1, every run in one round once its three affected runs were rerun.
  Unlike Task 2 it delegated in every run, to one or two Haiku subagents. Its subagents
  reached 237K to 277K tokens of context, above its own main agent's 164K to 228K.
- A13 (solo Opus 5.5 at medium effort) costs 0.87 of A1 with single-round passes and a
  similar context peak (258K to 282K); on this task its saving over high effort is within
  the A3 to A5 noise band.
- A14 (Opus 5.5 at low effort orchestrating Sonnet 5.5 and Haiku 5.5) costs 0.41 of A1.
  Its reruns cost more than the originals they replace (3.29 to 3.59 USD against 2.42 to
  3.57): the originals ended round one early while subagents were still working. Its main
  agent peaked at only 65K to 92K tokens of context while its subagents reached 228K to
  302K. A15 (medium effort) costs 0.58, 42 percent more per pass than A14, with a
  main-agent peak of 140K to 159K. Both delegated in every run,
  nearly always to a Sonnet 5.5 implementer and a Haiku tester.
- The original A14 rep 2's round one also ended with the unit-test build stopping on an OSAL test
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
- A16 (solo Haiku 5.5 at high effort) passed all ten runs at 0.37 of A1. One run needed
  a second round (the rerun of rep 3, above). Its main context
  reached 362K to 550K tokens and its runs took 22 to 45 minutes.
- A17 (solo Haiku 5.5 at xhigh effort) passed all ten runs at 0.70 of A1. Five needed more
  than one round, all starting with the fresh-transition defect; four needed a third,
  two because the hidden unit test still failed and two because the round-two fix broke
  the run's own unit tests. Its main context reached 498K to 856K tokens, the highest of any
  arm on Tasks 1 to 3, with costs of 3.58 to 11.44 USD and wall times of 25 to 80 minutes.
- The Codex arms passed all twenty runs in one round. A21 (eager delegation to Codex
  GPT-6.1 Sol) costs 0.64 of A1 at 5.41 USD per pass, of which Codex is 2.00 (37 percent;
  1.36 to 3.64 per run); it spawned two to eight Codex subagents per run and took 17 to 30
  minutes. A22 (roles plus a debugger) costs 0.77 of A1 at 6.56 USD, of which Codex is only
  0.66 (10 percent): it spawned one or two Codex subagents and kept more of the work on its
  Opus main agent, whose context peaked at 188K to 261K against A21's 149K to 198K. A22 had
  the shortest single run on Task 3, 15 minutes (15 to 26 overall). Codex subagent contexts peaked at 80K to
  115K tokens.
- A16 and A17 both qualify for Task 4 under the task gate: no run failed and none reached
  the final round.
- \* Round counts and spend of these arms include runs that lost rounds to a defect outside
  their own code, listed in [rerun-ledger.md](rerun-ledger.md) and to be rerun: an ambiguous
  fresh-event rule in the spec with misleading feedback (A10 rep 4, A11 reps 3 to 5, A12
  reps 1 to 5, A16 reps 2 to 7, A17 reps 3, 4, 7, 8 and 10), and the CLI stopping a background
  subagent at its ten-minute wait ceiling (A6 reps 1 to 5, A8 reps 1, 2, 4 and 5, A11 rep 4,
  A14 reps 2, 4 and 5, A15 rep 4). A re-grade of every round of all 97 runs on the corrected
  pack changed no other verdict, so the remaining runs stand. Rerun: A6 rep 1, A8 rep 1, A10 rep 4,
  A11 reps 3 to 5, A12 reps 1 to 5, A14 reps 2, 4 and 5 and A16 reps 2 to 7, each in one
  round except A16 rep 3 (two); A10, A11, A12, A14 and A16 no longer carry an asterisk. The owner dropped the remaining reruns (A6 reps 2 to 5, A8 reps 2,
  4 and 5, A15 rep 4 and A17's five) for the weekly usage budget, so those arms keep the
  asterisk. The originals are listed in [superseded.json](superseded.json).
