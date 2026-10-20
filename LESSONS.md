# Lessons learned

What Experiment A has taught so far about running coding agents: which setups work, what
patterns repeat across tasks, and where agents (and the harness) fall down. This is a living
summary; the evidence is in [docs/results/](docs/results/) (one report per task, the workbook
and the charts) and the design in [docs/experiment-design.md](docs/experiment-design.md).
Figures are as of 2026-10-20. A24 and A25 are still running and their numbers are preliminary.

All costs are billed USD per passing run, including Codex subagents at OpenAI API rates.
"x A1" is spend per pass relative to solo Opus 5.5 at high effort (A1).

## What works best

| Setup | Arm | T1 | T2 | T3 | T4 USD |
|---|---|---:|---:|---:|---:|
| Opus 5.5 low main, all Codex subagents on GPT-6 Luna, tiered | A24 | 0.25 | 0.19 | 0.17 | 2.76 |
| Opus 5.5 low main, Codex GPT-6.1 Sol implementer + GPT-6 Luna, tiered | A23 | 0.38 | 0.31 | 0.23 | 2.26 |
| Opus 5.5 low main, Sonnet 5.5 implementer + Haiku 5.5, tiered | A14 | 0.43 | 0.37 | 0.41 | 6.25 |
| Opus 5.5 low main, Codex GPT-5.6 Sol + GPT-5.6 Luna, tiered | A25 | 0.75 | 0.98 | 0.88 | - |
| Opus 5.5 high, Sonnet 5 subagents, conservative | A7 | 0.90 | 0.93 | 0.99 | - |
| Solo Opus 5.5, high effort (baseline) | A1 | 1.00 | 1.00 | 1.00 | - |
| Opus 5.5 high, Sonnet 5 subagents, eager | A6 | 1.78 | 1.71 | 1.75 | - |

A1 costs 1.46, 3.95 and 8.51 USD per pass on Tasks 1 to 3. Task 4 was redone on a corrected
pack for the finalists only, so it has no A1 baseline; there A11 costs 10.36, A21 7.78 and
A22 11.22 USD.

- **A cheap orchestrator over cheap implementers wins, and the gap grows with task size.**
  Opus 5.5 at *low* effort planning, reviewing and integrating, with implementation and
  tests handed to subagents, is the cheapest family on every task. Its main context stays
  small (22K to about 110K tokens, even on Task 4) because the orchestrator never reads or writes
  most of the code itself.
- **Codex subagents beat Claude subagents in the same role.** A23 is A14 with the Sonnet 5.5
  and Haiku 5.5 subagents replaced by GPT-6.1 Sol and GPT-6 Luna, and nothing else changed.
  It costs 11 percent less on Task 1, 45 percent less on Task 3 and 64 percent less on Task 4.
- **The cheapest subagent model is often enough.** A24 (every subagent on GPT-6 Luna) is
  the cheapest arm on Tasks 1 to 3. On Task 4, the hardest task, it is where the stronger
  implementer pays off: every A24 run needed two or three acceptance rounds, against one
  or two for A23, and A24 cost 22 percent more and ran longer.
- **The previous model generation is not a cheap substitute.** A25 (A23 on GPT-5.6 Sol and
  Luna) passed every run but costs about solo Opus on Tasks 2 and 3. GPT-5.6 is twice the
  per-token price of GPT-6.x and its Sol implementer also used about twice the tokens for
  the same work. It still costs about half of eager Sonnet 5 delegation (A6) and slightly
  less than conservative Sonnet 5 (A7), the setups available on the same previous-generation
  toolset.
- **Solo Sonnet 5.5 and solo Haiku 5.5 are cheap through Task 3 but fail Task 4.** Solo
  Sonnet 5.5 (A12) and solo Haiku 5.5 (A16, A17) ran at 0.21 to 0.70 x A1 on Tasks 1 to 3, but
  on the original Task 4 pack each had runs that used all five rounds without passing, and
  solo Haiku hit the 1M context window and compacted.

## Patterns across tasks

- **"Conservative" delegation means no delegation.** Told to delegate only when it clearly
  pays, Opus 5.5 at high effort spawned zero subagents in all 100 conservative runs on Tasks
  1 to 3. Those arms are solo runs whatever subagent model they are configured with.
- **Eager delegation to a peer model buys context headroom, not savings.** Eager Opus 5.5
  subagents (A2) cost 1.28 to 1.88 x A1. Eager delegation pays only when the subagent model
  is much cheaper than the orchestrator.
- **Effort level on the orchestrator matters more than it looks.** Tiered delegation with
  Opus 5.5 at medium effort (A15) costs 41 to 74 percent more than at low (A14) for the same
  pass rate. Low effort keeps the orchestrator to planning and review. Solo Opus at medium
  (A13) saves 13 to 27 percent over high.
- **xhigh effort is rarely worth it.** Solo Sonnet 5.5 at xhigh (A10) costs 2.3 times its
  high-effort run (A12) on Task 2 and doubles wall time, with no gain in pass rate.
- **Weaker models do not change direction under repeated feedback.** Solo Sonnet 5 (A18,
  A19) failed Task 1: six runs used all five rounds on one MIR-count edge case and kept the
  same parser through four rounds of identical feedback. Solo Sonnet 5.5 (A12) on the original
  Task 4 pack subscribed every table entry twice and kept that bug for four rounds.
- **Context size tracks how much code the main agent touches.** Solo arms peak at 147K to
  856K tokens on Tasks 2 and 3; solo Haiku reached 966K and auto-compacted on Task 4.
  Tiered orchestrators stay near or under 110K.
- **Haiku 5.5 gets expensive past 100K tokens of prompt,** where it is billed at five times
  its base rates; every solo Haiku run crossed that line, so its cost follows how far past it
  the run went.
- **Delegation widens the spread.** Delegating arms have the shortest best runs and the
  widest cost and wall-time ranges; repeat each cell several times before comparing.

## Agent behaviour to design around

- **Ending the turn while subagents are still working.** In a headless (print-mode) session,
  a main agent that ends its turn with "the test agents are still running; I'll pick up once
  they report" ends the round, and acceptance finds unfinished work. It cost a round in
  eight Task 3 runs and eight Task 4 runs of delegating arms until the CLI's background-wait
  ceiling was raised from ten minutes to two hours.
- **Polling a background subagent instead of waiting for it.** The Codex-arm instructions
  say to start a subagent with the Bash tool's `run_in_background` option and wait for the
  completion notification, or to run it in the foreground with a two-hour timeout, and
  never to poll it with `pgrep`, `ps` or a loop. Opus 5.5 at low effort still did it in 3 of
  about 60 A24 and A25 runs (none of the 99 A21 to A23 runs made after the rule was added polled a subagent by process name). Each time
  it launched `codex-agent ... > file &` in one Bash call and waited in a later one with
  `until ! pgrep -f "codex-agent implementer"`. Two lessons:
  - Instruction following on harness mechanics is weaker at low effort, or when subagents
    are slow enough to tempt the main agent to work in parallel.
  - A narrow shell gap. `pgrep -f PATTERN` inside `bash -c '... PATTERN ...'` matches its
    own parent shell, so the loop can never end; each run sat until the two-hour Bash
    timeout. Bash's `wait` is no alternative across tool calls: every Bash tool call is a new
    shell, and `wait` only reaps children of the current shell ("pid N is not a child of
    this shell"). Working ways to wait on a known PID: `waitpid PID` (util-linux 2.38+,
    event-driven through a pidfd), `tail --pid=PID -f /dev/null`, or
    `while kill -0 PID; do sleep 1; done`. Whether other models share the `pgrep -f` gap is
    untested.
- **Short polling loops on a file marker are harmless.** `until grep -q '^EXIT' build.log;
  do sleep 20; done` ends within one sleep of the build finishing. A loop is only expensive
  when its exit condition can never become true.
- **Skills can bypass a tool deny.** A solo arm with the Agent tool denied still ran a
  subagent through the bundled `code-review` skill. Validate what actually ran, not only the
  configuration.
- **Subagents can fail on output limits.** A GPT-5.6 Luna tester call ended with
  `Incomplete response returned, reason: max_output_tokens`; the harness records any failed
  Codex call as a `codex_error` run and reruns the cell.

## Grading and harness lessons

- **The task pack can decide the result.** On the original Task 4 pack, stubs that differed
  from real cFS (an existing directory treated as an error, table-update return codes,
  duplicate-subscription counting) decided every capped run. All 25 runs on it were
  superseded and Task 4 was redone on a corrected pack. Before trusting a failure, check the
  grader against the real system. An adversarial audit of the pack found what the runs alone
  did not.
- **Feedback must name the failing behaviour.** Group-level feedback that restates a
  requirement the run believes it already meets sends the agent after the wrong thing (A11
  rep 2 on the original Task 4 pack failed all five rounds on one hidden assertion). Name
  the behaviour that fails, as an interactive user would, without revealing test code.
- **Spec silences become failures.** The Task 1 MIR line with no ` - ` separator and the
  Task 3 fresh-event rule each cost rounds only in some model families. Pin every behaviour
  a grader checks; review each spec for ambiguity before running it.
- **Loosen a grader rule only in a way that cannot flip a pass to a fail.** Then the old
  verdicts stand without a regrade (the Task 2 coverage `%` sign fix). Otherwise regrade every
  round of every run on the fixed pack, as for Task 3.
- **Count the whole bill.** Report run cost from the billed scenario including Codex, not
  the Claude CLI's own total, and apply per-request pricing tiers (Haiku above 100K, GPT
  above 272K).
- **Do not trust the CLI's turn count.** When a background notification triggers the final
  short turn, `num_turns` reports only that turn; recount from transcripts.
- **Isolate parallel runs.** cFS binds fixed UDP ports, so each run gets its own network
  namespace (pasta). Six to twelve runs in parallel on one 16-core machine were fine.
- **Mind the subscription windows.** A run cut off by a usage limit is void and rerun from
  scratch; the launcher reads 5-hour and weekly utilisation from rate-limit headers and stops
  launching at 78 percent of the weekly window. Codex usage does not count against it.
- **Restarting the launcher must not kill runs.** Runs start in their own sessions, so the
  launcher's process can be replaced while they finish.

## Open questions

- Does A24's Task 4 weakness hold at ten repetitions, and is there a Luna-only setup (a
  stronger tester, or medium orchestrator effort) that closes it cheaply?
- Do the A24/A25 polling lapses go away at medium orchestrator effort? Do other main-agent
  models share the `pgrep -f` self-match gap?
- Task 5 (whole-system build) has not run yet; the cheap tiered setups are untested at that
  scale.
