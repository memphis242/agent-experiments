# Task 1 results (ut_assert log translator), 240 runs, 2026-10-20

All 220 runs of A1 to A17 and A21 to A25 passed acceptance; solo Sonnet 5 (A18, A19) failed the task, with 3 of 10 runs in each arm failing all five rounds (see * below). Billed cost includes compaction requests (none occurred). Ranges are min to max over the ten repetitions; spend per pass is total spend over all attempts divided by passes. Repetitions 1 to 3 of A1 to A15 were run first; repetitions 4 to 10, and all ten of A16 to A19, were added later from harness revisions whose run code is reviewed as equivalent, with up to nine runs in parallel. The Codex arms A21 and A22 ran all ten repetitions on 2026-10-19, three runs at a time, A23 on 2026-10-20, four at a time, and A24 and A25 the same day with up to eight runs in parallel.

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
| A8 | Sonnet 4.6 subagents, eager | task1 | 10/10 | 3.21 | 2.32 to 3.92 | 1.59 | 10.8 to 25.6 | 3 to 17 | 2 to 4 | 1 | 0 | 57.5 to 77.0 | 52.7 to 72.9 |
| A9 | Sonnet 4.6 subagents, conservative | task1 | 10/10 | 1.34 | 1.16 to 1.54 | 0.38 | 4.5 to 6.0 | 22 to 29 | 0 | 1 | 0 | 62.3 to 76.9 | - |
| A10 | solo Sonnet 5.5, xhigh effort | task1 | 10/10 | 1.27 | 1.11 to 1.77 | 0.66 | 7.0 to 11.7 | 29 to 47 | 0 | 1 | 0 | 95.9 to 138.7 | - |
| A11 | Sonnet 5.5 main, Haiku 5.5 implementer and tester | task1 | 10/10 | 0.47 | 0.31 to 0.58 | 0.27 | 1.6 to 6.4 | 3 to 20 | 0 to 1 | 1 | 0 | 37.7 to 63.4 | 44.6 to 64.8 |
| A12 | solo Sonnet 5.5, high effort | task1 | 10/10 | 0.51 | 0.47 to 0.56 | 0.09 | 2.7 to 9.1 | 1 to 21 | 0 | 1 | 0 | 51.5 to 60.4 | - |
| A13 | solo Opus 5.5, medium effort | task1 | 10/10 | 1.07 | 0.78 to 1.30 | 0.52 | 2.8 to 5.0 | 14 to 31 | 0 | 1 | 0 | 45.0 to 66.3 | - |
| A14 | Opus 5.5 low main, Sonnet 5.5 + Haiku 5.5, tiered | task1 | 10/10 | 0.63 | 0.55 to 0.89 | 0.34 | 3.6 to 8.5 | 2 to 15 | 2 to 3 | 1 to 2 | 0 | 24.8 to 39.0 | 50.3 to 101.1 |
| A15 | Opus 5.5 medium main, Sonnet 5.5 + Haiku 5.5, tiered | task1 | 10/10 | 1.09 | 0.93 to 1.33 | 0.41 | 6.4 to 10.9 | 3 to 11 | 2 to 4 | 1 to 2 | 0 | 48.7 to 61.5 | 79.9 to 105.2 |
| A16 | solo Haiku 5.5, high effort | task1 | 10/10 | 0.31 | 0.12 to 0.64 | 0.52 | 5.4 to 11.4 | 29 to 80 | 0 | 1 to 2 | 0 | 106.5 to 183.9 | - |
| A17 | solo Haiku 5.5, xhigh effort | task1 | 10/10 | 0.55 | 0.41 to 0.66 | 0.25 | 7.3 to 13.5 | 40 to 67 | 0 | 1 | 0 | 151.0 to 198.3 | - |
| A18 | solo Sonnet 5, medium effort | task1 | 7/10* | 2.88 | 0.89 to 4.29 | 3.41 | 5.1 to 21.2 | 39 to 119 | 0 | 2 to 5 | 0 | 68.1 to 176.7 | 40.6 |
| A19 | solo Sonnet 5, high effort | task1 | 7/10* | 4.96 | 0.96 to 5.62 | 4.66 | 6.5 to 25.8 | 28 to 144 | 0 | 1 to 5 | 0 | 81.3 to 241.3 | - |
| A21 | Claude Code Opus 5.5 high + headless Codex GPT-6.1 Sol high, eager | task1 | 10/10 | 1.95 | 1.38 to 2.64 | 1.27 | 6.3 to 15.5 | 8 to 18 | 2 to 4 | 1 | 0 | 55.2 to 95.7 | 31.5 to 53.7 |
| A22 | Claude Code Opus 5.5 high + headless Codex GPT-6.1 Sol high, implementer, tester and secondary debugger | task1 | 10/10 | 1.38 | 1.20 to 1.53 | 0.33 | 5.1 to 7.2 | 4 to 8 | 1 | 1 | 0 | 56.2 to 72.6 | 25.3 to 36.3 |
| A23 | Opus 5.5 low main, Codex GPT-6.1 Sol + GPT-6 Luna, tiered | task1 | 10/10 | 0.56 | 0.40 to 0.73 | 0.33 | 4.4 to 6.8 | 2 to 18 | 1 to 2 | 1 to 2 | 0 | 22.3 to 36.7 | 30.1 to 36.1 |
| A24 | Opus 5.5 low main, Codex GPT-6 Luna, tiered | task1 | 10/10 | 0.38 | 0.24 to 0.60 | 0.35 | 2.0 to 131.0 | 1 to 17 | 1 to 3 | 1 to 2 | 0 | 21.6 to 36.7 | 22.3 to 37.3 |
| A25 | Opus 5.5 low main, Codex GPT-5.6 Sol + GPT-5.6 Luna, tiered | task1 | 10/10 | 1.10 | 0.91 to 1.29 | 0.38 | 6.5 to 9.3 | 2 to 13 | 1 to 2 | 1 to 2 | 0 | 20.4 to 29.9 | 36.5 to 57.0 |

Spend per pass relative to solo Opus 5.5 (A1 = 1.00): A16 0.21, A24 0.26, A11 0.32, A12
0.35, A17 0.38, A23 0.38, A14 0.43, A13 0.73, A15 0.75, A25 0.75, A10 0.87, A7 0.90, A9
0.92, A3 0.92, A22 0.95, A5 1.01, A21 1.34, A4 1.38, A6 1.78, A2 1.88, A8 2.20. A18 and A19
failed the task and are not ranked; their spend per pass would be 1.97 and 3.40.

Notes:

- Every conservative arm again spawned zero subagents (40 of 40 runs), so A3, A5, A7 and
  A9 are behaviourally solo runs. With ten repetitions they sit at 0.90 to 1.01 of A1,
  closer to solo than the three-repetition figures (0.87 to 1.25) suggested.
- Eager arms spawned 2 to 4 subagents (implementer, tester, reviewer; occasionally a
  built-in general-purpose agent, which the validator confirmed ran on the arm's model)
  and cost 1.38 to 2.20 times A1. Eager Sonnet 4.6 (A8) remains the most expensive and
  slowest arm (10.8 to 25.6 min).
- Among A1 to A17, every run that needed more than one round failed the same check in round one, the
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
- Solo Haiku's main context peaked at 107K to 198K tokens, the highest of the arms that passed;
  every Haiku run crossed the 100,000-token prompt threshold above which Haiku 5.5 is
  billed at five times its base rates, and its cost follows how far it went past it: A16's
  cheapest run peaked at 107K (0.12 USD), its two dearest at 184K and 178K (0.60 and 0.64
  USD). Costs here apply that rule per request.
- Turn counts below an arm's typical range (A12's 1, A14's 2) are a CLI counting
  artefact: when the final result comes from a short turn triggered by a background
  notification, `num_turns` reports only that turn. The column will be recomputed from
  transcripts.
- \* Solo Sonnet 5 (A18 at medium, A19 at high effort) failed Task 1 under the task gate: 3 of
  10 runs in each arm used all five acceptance rounds without passing, so neither arm advances
  (A19 was later given a one-repetition Task 2 trial; see the Task 2 report). All six failed
  runs ended on the same check, the per-suite MIR count. One hidden test log contains an MIR
  line with neither FILE:LINE nor the ` - ` separator (`[  MIR] 01.003 Verify the panel colors
  by eye`). The spec gives the line format as `NN.MMM FILE:LINE - TEXT` and says FILE:LINE may
  be absent on MIR lines, but is silent on the separator, and real ut_assert always prints
  `FILE:LINE - `. Each failed run parsed the separator as required, so the line was treated as
  unrecognized (ignored without `--strict`) and the suite reported one MIR too few. Adding
  ` - ` to that line makes all six final tools report the right count. The feedback in rounds
  two to five was the same restatement of the MIR requirement each time, as it is for every
  arm, and none of the six runs changed its parser in response (A18 rep 8, for one, made four
  commits of MIR parsing fixes and tests without finding the case). Every run of the other 17 arms (170 runs) passed this check, most on the
  first round. Solo Sonnet 5 also needed extra rounds far more often than any other arm (18 of
  its 20 runs, against 7 of 170 elsewhere), with first-round misses spread over failure
  elements, TSF counting, the unreadable-input exit code and the Markdown section.
- A18 rep 9 ran a subagent although the solo arms deny the Agent tool: it invoked the bundled
  `code-review` skill, which spawns one (skills stay available in every arm). The validator
  flagged it, the only validation violation in the experiment so far; it is the 41K subagent
  context in A18's row, and the run is one of the six that failed.
- A21 and A22 run the A2 main agent (Claude Code Opus 5.5 at high effort) with
  headless Codex GPT-6.1 Sol subagents at high effort; both passed all ten runs in one
  round. Their cost includes the Codex subagents, priced at OpenAI's API rates for
  GPT-6.1 Sol (see the design document). Eager Codex delegation (A21) costs 1.95 USD per
  run, of which Codex is 0.65 (33 percent), and 1.34 of A1 per pass: less than eager
  Opus 5.5 subagents (A2, 1.88) and level with eager Sonnet 5.5 (A4, 1.38). A22's
  selective delegation started one Codex subagent per run and costs 1.38 USD per run,
  of which Codex is 0.16 (12 percent), 0.95 of A1. The four Codex pilot runs, made on
  the delegation text before it forbade polling a background subagent, are excluded
  (see `superseded.json` and the rerun ledger).
- A23 (added 2026-10-20) is the Codex counterpart of A14: Opus 5.5 at low effort
  directing headless Codex subagents, the implementer on GPT-6.1 Sol and the small
  implementer and tester on GPT-6 Luna, all at high effort, with A14's tiered delegation
  text. It passed all ten runs at 0.56 USD per pass, 0.38 of A1 against A14's 0.43, of
  which Codex is 0.19 per run (33 percent). Reps 1, 7 and 10 needed a second round for the
  Markdown report's table and failures section. Nearly every Codex call went to the Sol
  implementer; the Luna tester ran eight times in all.
- A24 and A25 (added 2026-10-20) keep A23's orchestrator, delegation text and roster and
  change only the Codex models. A24 runs every subagent on GPT-6 Luna; A25 is A23 on the
  previous generation, the implementer on GPT-5.6 Sol and the small implementer and tester
  on GPT-5.6 Luna, all at high effort. Both passed all ten runs. A24 is the cheapest
  Opus-led arm at 0.38 USD per pass (0.26 of A1), with Codex only 0.01 per run: nearly all
  its spend is the Opus main agent. A25 costs 1.10 USD (0.75 of A1), twice A23; Codex is
  0.83 per run (75 percent), since GPT-5.6 costs twice GPT-6.x per token and its Sol
  implementer also used more tokens. A24 rep 6 polled the Codex subagent with a `pgrep -f`
  loop that matched its own shell, against the arm's instructions, and waited out the
  two-hour Bash timeout, so its 131-minute wall time is mostly that wait; without it A24
  runs took 2 to 6 minutes.
- A8 reps 7 and 10 originally lost a background subagent to the CLI's ten-minute
  print-mode wait ceiling. Both were rerun with the raised ceiling and the originals
  are excluded (`superseded.json`); A8's spend per pass moves from 2.10 to 2.20 of A1.
