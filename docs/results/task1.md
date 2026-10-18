# Task 1 results (ut_assert log translator), 27 runs, 2026-10-18

All 27 runs passed acceptance. Billed cost includes compaction requests (none occurred). Prices: current list prices.

| Arm | Task | Attempts | Passes | Attrition | Spend USD | USD per pass | Conditional USD | Rounds | Spawns | Compactions |
| A1 | task1 | 3 | 3 | none | 4.26 | 1.42 | 1.45 (1.15-1.66) | 1 (1-1) | 0 (0-0) | 0 (0-0) |
| A2 | task1 | 3 | 3 | none | 8.48 | 2.83 | 2.88 (2.36-3.24) | 1 (1-1) | 2 (2-2) | 0 (0-0) |
| A3 | task1 | 3 | 3 | none | 3.68 | 1.23 | 1.25 (1.05-1.38) | 1 (1-1) | 0 (0-0) | 0 (0-0) |
| A4 | task1 | 3 | 3 | none | 5.99 | 2.00 | 1.73 (1.66-2.60) | 1 (1-1) | 3 (2-4) | 0 (0-0) |
| A5 | task1 | 3 | 3 | none | 5.30 | 1.77 | 1.41 (1.27-2.62) | 1 (1-4) | 0 (0-0) | 0 (0-0) |
| A6 | task1 | 3 | 3 | none | 8.18 | 2.73 | 2.40 (2.35-3.43) | 1 (1-1) | 2 (2-4) | 0 (0-0) |
| A7 | task1 | 3 | 3 | none | 3.90 | 1.30 | 1.34 (1.19-1.37) | 1 (1-1) | 0 (0-0) | 0 (0-0) |
| A8 | task1 | 3 | 3 | none | 11.11 | 3.70 | 3.80 (3.39-3.92) | 1 (1-1) | 4 (3-4) | 0 (0-0) |
| A9 | task1 | 3 | 3 | none | 4.15 | 1.38 | 1.46 (1.23-1.46) | 1 (1-1) | 0 (0-0) | 0 (0-0) |

Per-arm medians: wall minutes and main-agent turns

| Arm | Setup | USD per pass | Median wall min | Median main turns | Median spawns |
|---|---|---:|---:|---:|---:|
| A1 | solo Opus 5.5 | 1.42 | 4.7 | 29 | 0 |
| A2 | Opus subagents, eager | 2.83 | 9.1 | 10 | 2 |
| A3 | Opus subagents, conservative | 1.23 | 4.5 | 24 | 0 |
| A4 | Sonnet 5.5 subagents, eager | 2.00 | 6.0 | 4 | 3 |
| A5 | Sonnet 5.5 subagents, conservative | 1.77 | 5.0 | 23 | 0 |
| A6 | Sonnet 5 subagents, eager | 2.73 | 9.0 | 5 | 2 |
| A7 | Sonnet 5 subagents, conservative | 1.30 | 4.7 | 23 | 0 |
| A8 | Sonnet 4.6 subagents, eager | 3.70 | 18.0 | 4 | 4 |
| A9 | Sonnet 4.6 subagents, conservative | 1.38 | 5.3 | 27 | 0 |

Notes: every conservative arm spawned zero subagents on this task, so A3, A5, A7, A9 are
behaviourally solo runs. One A5 run needed four rounds on the Markdown failures section
check. Eager arms spawned 2 to 4 subagents (implementer, tester, reviewer; occasionally a
built-in general-purpose agent, which the validator confirmed ran on the arm's model).
