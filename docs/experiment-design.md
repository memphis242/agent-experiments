# Experiment design

Controlled A/B experiments on Claude Code setups, run on realistic NASA core
Flight System (cFS) engineering tasks. Each run varies exactly one factor
against a baseline. Started 2026-10-17.

## Questions under test

1. Model tiering and delegation. Does an Opus 5.5 orchestrator that delegates
   to cheaper subagents beat a single Opus 5.5 agent, or Opus 5.5 subagents,
   on cost per completed task? Does eager delegation beat conservative
   delegation? Is Sonnet 4.6 "smarter and therefore cheaper" than Sonnet 5?
2. Context policy. Does a small auto-compaction window (100K or 200K tokens)
   reduce cost per completed task, or does the re-read of post-compaction
   context plus lost information cost more than it saves?
3. Cache accounting. How much of total cost does prompt caching remove, and
   what does a 1-hour TTL buy over 5 minutes given real inter-turn pauses?

Question 3 needs no runs of its own: per-request token usage is recorded in
the session transcript and cost under each TTL scenario is computed offline.

## Experiment A: model tiering (9 arms)

| Arm | Main agent | Subagents | Delegation |
|-----|-----------|-----------|------------|
| A1 | Opus 5.5 | none (Agent tool disallowed) | n/a |
| A2 | Opus 5.5 | Opus 5.5 | eager |
| A3 | Opus 5.5 | Opus 5.5 | conservative |
| A4 | Opus 5.5 | Sonnet 5.5 | eager |
| A5 | Opus 5.5 | Sonnet 5.5 | conservative |
| A6 | Opus 5.5 | Sonnet 5 | eager |
| A7 | Opus 5.5 | Sonnet 5 | conservative |
| A8 | Opus 5.5 | Sonnet 4.6 | eager |
| A9 | Opus 5.5 | Sonnet 4.6 | conservative |

Fixed across arms: full model IDs (`claude-opus-5-5`, `claude-sonnet-5-5`,
`claude-sonnet-5`, `claude-sonnet-4-6`), effort pinned to `high` for the
main agent and every subagent, default auto-compaction window,
`promptCacheTtl` pinned to `1h` and `subagentPromptCacheTtl` to `5m`, same
subagent roster (explorer, implementer, tester, debugger, reviewer)
differing only in `model:`.

Delegation style is set by the arm's CLAUDE.md text only. Actual spawn
counts per run are recorded as the manipulation check.

Runs: 9 arms x 4 tasks x 3 repetitions = 108 runs, plus repair rounds.
If budget bites, drop A7 and A9 first.

## Experiment B: auto-compaction window

Top one or two arms from A, on tasks 3 and 4 only.

| Window | Setting |
|--------|---------|
| B1 | default (about 967K on 1M-context models) |
| B2 | `--autocompact 200000` |
| B3 | `--autocompact 100000` (the minimum) |

Runs: 3 windows x 2 arms x 2 tasks x 3 repetitions = 36 runs. Record the
number of compaction events per run.

## Tasks

| # | Name | Expected duration | Kind |
|---|------|------------------|------|
| 1 | ut_assert log translator | 15-30 min | script |
| 2 | SAMPLE_APP status telemetry | 45-90 min | extend small app |
| 3 | HK staleness tracking | 1.5-3 h | extend flight app |
| 4 | Storage manager app | 3-6 h | new app from spec |

Each task has an agent-facing `spec.md` with every public name pinned, and
a hidden `acceptance/` pack that is copied into the run directory after the
agent finishes. See `tasks/`.

## Completion protocol (layered)

Layer 1, automated:
1. Clone the mission template into a fresh directory.
2. Invoke `claude -p` with the task prompt. Record the session ID.
3. Copy the acceptance pack in, run it, remove it.
4. If it fails, generate a feedback message from the failures in spec terms
   (never test code or file paths of the pack), resume the same session with
   `claude -p --resume <id>`, and repeat. Cap: 5 rounds.
5. Record a run result regardless of outcome. Cap hit = failure at known cost.

Layer 2, manual: the user reviews each acceptance-passing result blind to its
arm, in shuffled order, and prompts further until satisfied. Cost and rounds
of layer 2 are recorded separately from layer 1.

Feedback generation is deterministic: the same failure always yields the
same message, so every arm receives identical feedback for identical faults.

## Isolation per run

- `CLAUDE_CONFIG_DIR=/workspace/agent-experiments-arms/<arm>` with only that
  arm's `settings.json`, `CLAUDE.md`, and `agents/`. No global CLAUDE.md, no
  user agents, no plugins. Auth via `CLAUDE_CODE_OAUTH_TOKEN` (subscription).
- Fresh clone of the mission template per run under /workspace, never under
  the home directory (ancestor CLAUDE.md files would load).
- Unique run ID appended via `--append-system-prompt` so cache prefixes
  never match across runs.
- `--permission-mode bypassPermissions --permission-prompts none`, inside a
  per-run directory. Runs are never stopped on cost.
- Each claude invocation and each acceptance run executes in its own network
  namespace, because cFS binds fixed UDP ports (CI_LAB 1234, TO_LAB 2234):
  `pasta --config-net -t none -u none -T none -U none` with a nested
  `unshare -U --map-user=<uid>` so the process keeps a normal uid (as fake
  root, OSAL fails to create message queues) and pasta's loopback port
  forwarding is off (it would divert traffic to host ports). Outbound API
  access still works.
- Up to 3 runs in parallel. Rate-limit retries are logged; throttled runs are
  rerun.

## Metrics per run (one JSON record)

- identity: run ID, arm, task, repetition, timestamps, model IDs, settings
- outcome: acceptance pass/fail per round, rounds used, cap hit
- cost: tokens by type (input, output, cache read, cache creation by TTL)
  per model and per query source (main, subagent), computed USD at list
  price under three scenarios: no cache, 5m TTL with simulated pauses, 1h TTL
- behavior: turns, tool calls, subagent spawns by type and model, compaction
  events, wall time, API time
- layer 2: rounds, tokens, cost, user rating

## Cache accounting

List prices (USD per million tokens):

| Model | Input | Cache read | 5m write | 1h write | Output |
|-------|-------|-----------|----------|----------|--------|
| Opus 5.5 | 4.00 | 0.20 | 5.00 | 8.00 | 20.00 |
| Sonnet 5.5 | 2.00 | 0.20 | 2.50 | 4.00 | 10.00 |
| Sonnet 5 | 2.00 | 0.20 | 2.50 | 4.00 | 10.00 |
| Sonnet 4.6 | 3.00 | 0.30 | 3.75 | 6.00 | 15.00 |
| Haiku 5.5 | 0.10 | 0.01 | 0.125 | 0.20 | 0.50 |

Scenarios computed from the transcript's per-request usage:
- none: cache read and cache creation tokens billed at input price.
- 5m: as recorded, but at each simulated human pause longer than 5 minutes
  the next request's cache-read tokens are rebilled as a 5m write.
- 1h: as recorded with 1h write prices; pauses under 60 minutes keep hits.

Simulated pauses are drawn from the user's real inter-prompt gap
distribution (from `~/.claude/history.jsonl`) and inserted at the start of
each resume round, since headless runs have no human pauses of their own.

## Analysis

Primary metric: cost per completed task (layer 1 pass), median and range
over repetitions. Secondary: wall time, rounds, layer 2 cost. Report
spawn counts and compaction counts alongside. Three repetitions is the
minimum; top arms get additional repetitions.
