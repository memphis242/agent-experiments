# Task 4 results (STOR storage app), 15 runs, 2026-10-20

Task 4 was redone from scratch on a corrected pack and spec. The original pack's cFS stubs
differed from real cFS in ways that decided pass/fail (an existing storage directory was an
error, table-update return codes and timing, duplicate-subscription counting, among
others), so all 25 runs on it are superseded and left out of these results. The corrected
pack models real cFS behaviour, the spec pins every choice a grader checks, and feedback
names each failing behaviour.

The corrected pack ran five arms, three repetitions each: A11 and A14, the cheapest
passing delegating arms on Tasks 1 to 3, the two Codex arms, A21 and A22, and A23, the
Codex counterpart of A14 added on 2026-10-20 (Opus 5.5 at low effort, tiered delegation to
Codex GPT-6.1 Sol and GPT-6 Luna). Billed cost
includes the Codex subagents' API cost. Ranges are min to max over the repetitions; spend
per pass is total spend over all attempts divided by passes. Runs used four parallel slots.

## Spread across arms

![Task 4 billed cost per run](figures/task4-cost.svg)

The cost chart's right-hand columns give, per arm, the largest value seen in any one run: input, output and cache-read tokens summed over the main agent and all subagents, then the main agent's context peak and the largest subagent context peak (`-` when the arm spawned no subagents). Bars span min to max, dots are single runs (hollow when the run did not pass), the tick is the median, and a lighter tint marks conservative arms.

![Task 4 wall time per run](figures/task4-time.svg)

## Full results

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task4 | 3/3 | 10.36 | 8.20 to 12.12 | 3.91 | 41.8 to 49.6 | 18 to 133 | 5 to 6 | 1 | 0 | 352.5 to 405.4 | 279.8 to 371.6 |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task4 | 3/3 | 6.25 | 5.80 to 6.66 | 0.86 | 40.7 to 44.9 | 4 to 8 | 3 | 1 | 0 | 44.8 to 60.4 | 388.9 to 506.7 |
| A21 | Opus 5.5 high main, Codex GPT-6.1 Sol high subagents, eager | task4 | 3/3 | 7.78 | 7.03 to 8.40 | 1.38 | 47.0 to 65.9 | 11 to 14 | 6 | 1 | 0 | 111.0 to 168.2 | 131.1 to 186.0 |
| A22 | Opus 5.5 high main, Codex GPT-6.1 Sol high, roles + debugger | task4 | 3/3 | 11.22 | 10.34 to 12.89 | 2.56 | 31.6 to 46.8 | 16 to 108 | 1 to 2 | 1 | 0 | 294.1 to 349.7 | 111.6 to 126.9 |
| A23 | Opus 5.5 low main, Codex GPT-6.1 Sol + GPT-6 Luna, tiered | task4 | 3/3 | 2.26 | 1.80 to 2.60 | 0.79 | 30.9 to 77.0 | 4 to 17 | 2 to 5 | 1 to 2 | 0 | 36.4 to 71.4 | 134.4 to 212.3 |

Notes:

- All 15 runs passed; all but one passed in round one. On the original pack nearly every
  run needed a second round; the corrected pack removed the stub defects behind most of
  those misses.
- A23 (Opus 5.5 at low effort, implementer on Codex GPT-6.1 Sol, small implementer and
  tester on GPT-6 Luna) was the cheapest arm by far, at 2.26 USD per pass (1.80 to 2.60),
  about a third of A14's. Codex was 67% of its spend; its Opus main agent cost 0.48 to
  1.01 USD per run. Rep 2 needed a second round (unit-test line coverage of one source file
  at 94.8% against 95%, and a missing limitations list in the report). Rep 3 took 77
  minutes, the slowest run of the task, against 31 and 51 for the other two.
- A14 (Opus 5.5 at low effort directing Sonnet 5.5 and Haiku 5.5) was the cheapest
  Claude-only arm, at 6.25 USD per pass with the tightest spread (5.80 to 6.66 USD). Its main agent
  stayed under 61K tokens of context in every run and delegated nearly all the work.
- A21 (Opus 5.5 main, Codex subagents, eager) was third at 7.78 USD per pass. Codex was
  60% of its spend (14.11 of 23.34 USD over three runs); the Opus main agent cost 2.25 to
  3.87 USD per run.
- A22 (Opus 5.5 main, Codex in fixed roles plus a debugger) spawned only one or two
  subagents and did most of the work in the main agent: 294K to 350K tokens of main
  context and up to 108 turns. Codex was 12% of its spend (3.95 of 33.65 USD), so it cost
  the most, 11.22 USD per pass, but it was also the fastest arm, 31.6 to 46.8 minutes.
- A11 (Sonnet 5.5 main, Haiku 5.5 implementer and tester) cost 10.36 USD per pass, with
  the widest spread (8.20 to 12.12 USD).
- No solo Opus 5.5 (A1) baseline ran on the corrected pack, so Task 4 has no spend ratio
  against A1.
