# Task 1 results (ut_assert log translator), 170 runs, 2026-10-18

All 170 runs passed acceptance. Billed cost includes compaction requests (none occurred). Ranges are min to max over the ten repetitions; spend per pass is total spend over all attempts divided by passes. Repetitions 1 to 3 of A1 to A15 were run first; repetitions 4 to 10, and all ten of A16 and A17, were added later from harness revisions whose run code is reviewed as equivalent, with up to nine runs in parallel.

## Spread across arms

![Task 1 billed cost per run](figures/task1-cost.svg)

The cost chart's right-hand columns give, per arm, the largest value seen in any one run: input, output and cache-read tokens summed over the main agent and all subagents, then the main agent's context peak and the largest subagent context peak (`-` when the arm spawned no subagents). Bars span min to max, dots are single runs, the tick is the median, and a lighter tint marks conservative arms.

![Task 1 wall time per run](figures/task1-time.svg)

## Full results

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A1 | solo Opus 5.5 | task1 | 10/10 | 1.46 | 1.15 to 1.66 | 0.50 | 4.2 to 6.6 | 22 to 36 | 0 | 1 | 0 | 61.7 to 81.3 | - |
| A2 | Opus 5.5 subagents, eager | task1 | 10/10 | 2.74 | 1.89 to 3.32 | 1.44 | 4.7 to 11.2 | 3 to 14 | 2 to 4 | 1 | 0 | 44.4 to 92.6 | 32.2 to 66.7 |
| A3 | Opus 5.5 subagents, conservative | task1 | 10/10 | 1.35 | 1.05 to 1.48 | 0.43 | 4.1 to 6.9 | 20 to 30 | 0 | 1 | 0 | 57.1 to 76.3 | - |
| A4 | Sonnet 5.5 subagents, eager | task1 | 10/10 | 2.01 | 1.45 to 2.60 | 1.15 | 5.0 to 9.1 | 2 to 16 | 2 to 4 | 1 | 0 | 56.5 to 85.3 | 29.9 to 75.3 |
| A5 | Sonnet 5.5 subagents, conservative | task1 | 10/10 | 1.48 | 1.17 to 2.62 | 1.45 | 4.7 to 9.0 | 20 to 55 | 0 | 1 to 4 | 0 | 61.9 to 106.5 | - |
| A6 | Sonnet 5 subagents, eager | task1 | 10/10 | 2.60 | 1.88 to 3.43 | 1.56 | 7.0 to 21.7 | 2 to 12 | 2 to 4 | 1 | 0 | 59.7 to 78.9 | 55.5 to 99.9 |
| A7 | Sonnet 5 subagents, conservative | task1 | 10/10 | 1.31 | 1.19 to 1.49 | 0.30 | 4.4 to 6.3 | 21 to 27 | 0 | 1 | 0 | 61.5 to 73.5 | - |
| A8 | Sonnet 4.6 subagents, eager | task1 | 10/10 | 3.07 | 1.74 to 3.92 | 2.18 | 10.8 to 25.6 | 2 to 17 | 2 to 4 | 1 | 0 | 37.9 to 77.0 | 39.0 to 74.8 |
| A9 | Sonnet 4.6 subagents, conservative | task1 | 10/10 | 1.34 | 1.16 to 1.54 | 0.38 | 4.5 to 6.0 | 22 to 29 | 0 | 1 | 0 | 62.3 to 76.9 | - |
| A10 | solo Sonnet 5.5, xhigh effort | task1 | 10/10 | 1.27 | 1.11 to 1.77 | 0.66 | 7.0 to 11.7 | 29 to 47 | 0 | 1 | 0 | 95.9 to 138.7 | - |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task1 | 10/10 | 0.47 | 0.31 to 0.58 | 0.27 | 1.6 to 6.4 | 3 to 20 | 0 to 1 | 1 | 0 | 37.7 to 63.4 | 44.6 to 64.8 |
| A12 | solo Sonnet 5.5, high effort | task1 | 10/10 | 0.51 | 0.47 to 0.56 | 0.09 | 2.7 to 9.1 | 1 to 21 | 0 | 1 | 0 | 51.5 to 60.4 | - |
| A13 | solo Opus 5.5, medium effort | task1 | 10/10 | 1.07 | 0.78 to 1.30 | 0.52 | 2.8 to 5.0 | 14 to 31 | 0 | 1 | 0 | 45.0 to 66.3 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task1 | 10/10 | 0.63 | 0.55 to 0.89 | 0.34 | 3.6 to 8.5 | 2 to 15 | 2 to 3 | 1 to 2 | 0 | 24.8 to 39.0 | 50.3 to 101.1 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered | task1 | 10/10 | 1.09 | 0.93 to 1.33 | 0.41 | 6.4 to 10.9 | 3 to 11 | 2 to 4 | 1 to 2 | 0 | 48.7 to 61.5 | 79.9 to 105.2 |
| A16 | solo Haiku 5.5, high effort | task1 | 10/10 | 0.31 | 0.12 to 0.64 | 0.52 | 5.4 to 11.4 | 29 to 80 | 0 | 1 to 2 | 0 | 106.5 to 183.9 | - |
| A17 | solo Haiku 5.5, xhigh effort | task1 | 10/10 | 0.55 | 0.41 to 0.66 | 0.25 | 7.3 to 13.5 | 40 to 67 | 0 | 1 | 0 | 151.0 to 198.3 | - |

Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A16 0.21, A11 0.32, A12 0.35, A17
0.38, A14 0.43, A13 0.73, A15 0.75, A10 0.87, A7 0.90, A9 0.92, A3 0.92, A5 1.01, A4
1.38, A6 1.78, A2 1.88, A8 2.10.

Notes:

- Every conservative arm again spawned zero subagents (40 of 40 runs), so A3, A5, A7 and
  A9 are behaviourally solo runs. With ten repetitions they sit at 0.90 to 1.01 of A1,
  closer to solo than the three-repetition figures (0.87 to 1.25) suggested.
- Eager arms spawned 2 to 4 subagents (implementer, tester, reviewer; occasionally a
  built-in general-purpose agent, which the validator confirmed ran on the arm's model)
  and cost 1.38 to 2.10 times A1. Eager Sonnet 4.6 (A8) remains the most expensive and
  slowest arm (10.8 to 25.6 min).
- Every run that needed more than one round failed the same check in round one, the
  Markdown failures section: A5 once (four rounds), A14 twice, A15 once and A16 three
  times (two rounds each). The three A16 runs also failed the Markdown N/A column check.
- A10 to A12 run a Sonnet 5.5 main agent. Solo Sonnet 5.5 at high effort (A12) costs 0.35
  of A1 with the tightest spread of any arm (0.09 USD); at xhigh effort (A10) 0.87, with
  wall time roughly doubling. A11 (Sonnet 5.5 main, Haiku 5.5 implementer and tester)
  spawned a Haiku tester in 2 of 10 runs and nothing in the rest, so it is mostly a solo
  Sonnet 5.5 high run; it costs 0.32 of A1.
- A13 (solo Opus 5.5 at medium effort) costs 0.73 of A1 with shorter wall times (2.8 to
  5.0 min) and a lower main context peak (45K to 66K).
- The tiered arms (A14, A15) run an Opus 5.5 orchestrator with a Sonnet 5.5 implementer
  and Haiku 5.5 small-implementer and tester. Both delegated in every run, nearly always
  one implementer and one tester. At low orchestrator effort (A14) they cost 0.43 of A1,
  the cheapest Opus-led arm, with the main context peaking at 25K to 39K; at medium (A15)
  0.75. Every subagent ran on its agent's assigned model (no validation violations).
- A16 and A17 run solo Haiku 5.5. Both passed all ten runs, with no run reaching the
  final round, so both qualify for Task 2 under the task gate. At high effort (A16) solo
  Haiku is the cheapest arm on the task at 0.21 of A1, but with a wide spread (0.12 to
  0.64 USD) and the three second-round runs above. At xhigh (A17) it passed every run in
  one round at 0.38 of A1, about the cost of A12, and took 7.3 to 13.5 minutes, roughly
  twice solo Opus.
- Solo Haiku's main context peaked at 107K to 198K tokens, the highest on this task;
  every Haiku run crossed the 100,000-token prompt threshold above which Haiku 5.5 is
  billed at five times its base rates, and its cost follows how far it went past it: A16's
  cheapest run peaked at 107K (0.12 USD), its two dearest at 184K and 178K (0.60 and 0.64
  USD). Costs here apply that rule per request.
- Turn counts below an arm's typical range (A12's 1, A14's 2) are a CLI counting
  artefact: when the final result comes from a short turn triggered by a background
  notification, `num_turns` reports only that turn. The column will be recomputed from
  transcripts.
