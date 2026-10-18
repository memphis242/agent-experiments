# Task 1 results (ut_assert log translator), 27 runs, 2026-10-18

All 27 runs passed acceptance. Billed cost includes compaction requests (none occurred). Ranges are min to max over the three repetitions; spend per pass is total spend over all attempts divided by passes.

| Arm | Description | Task | Passes/attempts | Spend per pass USD | Cost per run USD | Cost spread USD | Wall min per run | Turns per run | Spawns | Rounds | Compactions | Main ctx peak (K tokens) | Subagent ctx peak (K tokens) |
|-----|-------------|------|----------------:|-------------------:|-----------------:|----------------:|-----------------:|--------------:|-------:|-------:|------------:|-------------------------:|-----------------------------:|
| A1 | solo Opus 5.5 | task1 | 3/3 | 1.42 | 1.15 to 1.66 | 0.50 | 4.2 to 6.0 | 25 to 32 | 0 | 1 | 0 | 61.7 to 81.3 | - |
| A2 | Opus 5.5 subagents, eager | task1 | 3/3 | 2.83 | 2.36 to 3.24 | 0.88 | 6.6 to 9.9 | 9 to 13 | 2 | 1 | 0 | 67.8 to 92.6 | 36.6 to 66.7 |
| A3 | Opus 5.5 subagents, conservative | task1 | 3/3 | 1.23 | 1.05 to 1.38 | 0.33 | 4.1 to 4.8 | 20 to 30 | 0 | 1 | 0 | 57.1 to 72.8 | - |
| A4 | Sonnet 5.5 subagents, eager | task1 | 3/3 | 2.00 | 1.66 to 2.60 | 0.94 | 5.2 to 8.9 | 3 to 12 | 2 to 4 | 1 | 0 | 64.3 to 85.3 | 29.9 to 69.3 |
| A5 | Sonnet 5.5 subagents, conservative | task1 | 3/3 | 1.77 | 1.27 to 2.62 | 1.35 | 4.7 to 9.0 | 20 to 55 | 0 | 1 to 4 | 0 | 68.8 to 106.5 | - |
| A6 | Sonnet 5 subagents, eager | task1 | 3/3 | 2.73 | 2.35 to 3.43 | 1.08 | 8.1 to 12.6 | 4 to 7 | 2 to 4 | 1 | 0 | 64.7 to 66.4 | 74.6 to 99.9 |
| A7 | Sonnet 5 subagents, conservative | task1 | 3/3 | 1.30 | 1.19 to 1.37 | 0.19 | 4.4 to 5.3 | 22 to 24 | 0 | 1 | 0 | 61.5 to 71.2 | - |
| A8 | Sonnet 4.6 subagents, eager | task1 | 3/3 | 3.70 | 3.39 to 3.92 | 0.53 | 15.9 to 25.6 | 3 to 17 | 3 to 4 | 1 | 0 | 69.6 to 77.0 | 52.7 to 72.9 |
| A9 | Sonnet 4.6 subagents, conservative | task1 | 3/3 | 1.38 | 1.23 to 1.46 | 0.23 | 5.1 to 5.4 | 25 to 29 | 0 | 1 | 0 | 64.0 to 76.8 | - |

Notes: every conservative arm spawned zero subagents on this task, so A3, A5, A7, A9 are
behaviourally solo runs. One A5 run needed four rounds on the Markdown failures section
check. Eager arms spawned 2 to 4 subagents (implementer, tester, reviewer; occasionally a
built-in general-purpose agent, which the validator confirmed ran on the arm's model).
