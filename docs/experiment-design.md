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

## Experiment A: model tiering (19 arms)

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
| A10 | Sonnet 5.5 (xhigh effort) | none (Agent tool disallowed) | n/a |
| A11 | Sonnet 5.5 | Haiku 5.5 (implementer and tester only) | roles |
| A12 | Sonnet 5.5 (high effort) | none (Agent tool disallowed) | n/a |
| A13 | Opus 5.5 (medium effort) | none (Agent tool disallowed) | n/a |
| A14 | Opus 5.5 (low effort) | Sonnet 5.5 implementer; Haiku 5.5 small-implementer and tester | tiered |
| A15 | Opus 5.5 (medium effort) | Sonnet 5.5 implementer; Haiku 5.5 small-implementer and tester | tiered |
| A16 | Haiku 5.5 (high effort) | none (Agent tool disallowed) | n/a |
| A17 | Haiku 5.5 (xhigh effort) | none (Agent tool disallowed) | n/a |
| A18 | Sonnet 5 (medium effort) | none (Agent tool disallowed) | n/a |
| A19 | Sonnet 5 (high effort) | none (Agent tool disallowed) | n/a |

A10 asks whether a cheaper frontier model at maximum effort matches solo
Opus. A12 is the same model at high effort, the owner's everyday choice for
small tasks, isolating the effort factor. A11 is the preferred tiered setup: a frontier orchestrator with the
cheapest model doing bounded implementation and testing; its main agent
explores, plans, debugs, reviews and integrates itself.

A13 is the effort-level control for solo Opus: A1 at medium instead of high
effort. A14 and A15 are tiered-delegation arms with an Opus orchestrator at
low (A14) or medium (A15) effort; the only difference between them is the
orchestrator's effort. Their subagents are a Sonnet 5.5 `implementer` (multi-file or
nontrivial work), a Haiku 5.5 `small-implementer` (small, localized,
well-specified changes) and a Haiku 5.5 `tester`, all at high effort; there are
no explorer, debugger or reviewer agents. Because their subagents use more than
one model, these arms cannot use `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`; the
built-in agent types are instead denied with `Agent(<type>)` permission rules,
so only the arm's own agents can be spawned.

A16 and A17 ask how far the cheapest model gets on its own, at high and
xhigh effort; A18 and A19 ask the same of solo Sonnet 5 at medium and high
effort, and at which task tier it stops passing. These four arms advance task by task. An arm runs Task N+1 only once all
of its Task N repetitions are done and it qualified on them: it is
disqualified if any run failed all five acceptance rounds, or if more than
a third of its repetitions needed the fifth round to pass (at most three of
ten, or one of three). Both solo Sonnet 5 arms failed this gate on Task 1, on a check whose
spec wording is ambiguous (see the Task 1 report); as a deliberate exception,
A19 gets a one-repetition Task 2 trial, and all ten Task 2 repetitions only
if that trial passes.

Fixed across arms: full model IDs (`claude-opus-5-5`, `claude-sonnet-5-5`,
`claude-sonnet-5`, `claude-sonnet-4-6`, `claude-haiku-5-5`), effort pinned to `high` for the
main agent and every subagent (except A10's `xhigh`, A13, A15 and A18's `medium`, A14's `low` and A17's `xhigh` main agent), default auto-compaction window,
`promptCacheTtl` pinned to `1h` and `subagentPromptCacheTtl` to `5m`, same
subagent roster (explorer, implementer, tester, debugger, reviewer)
differing only in `model:`. Built-in agents (Explore, Plan,
general-purpose) stay available in A2-A9 and A11 (denied in A14 and A15) and are forced onto the arm's
subagent model with `CLAUDE_CODE_SUBAGENT_MODEL` and
`CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` in the arm's settings. WebSearch,
WebFetch and Skill stay available in every arm, because the experiment is
meant to reflect practical agentic development. Tools no developer would
invoke in this setting are denied everywhere: Workflow, SendMessage,
ListAgents, RemoteTrigger, Cron*, ScheduleWakeup, EnterWorktree,
ExitWorktree, PushNotification, EndConversation. A post-run validator
records every subagent request's model and flags deviations; it never
drops a run.

Delegation style is set by the arm's CLAUDE.md text only. Actual spawn
counts per run are recorded as the manipulation check.

Runs: 10 repetitions per arm on Tasks 1 to 3 and 3 on Task 4, so 33 runs per arm:
495 for A1 to A15, plus up to 132 for A16 to A19 (fewer if one stops advancing),
plus repair rounds.

Task 5 takes the three arms with the lowest Task 4 spend per pass. An arm qualifies only if it also passes
the task gate above on Task 4: no run failing all five rounds, and at most one of
its three repetitions needing the fifth round.
Budget is not a constraint: a run that hits a subscription usage limit is
dropped and rerun after the limit is raised. Run order is the plain
arm x task x repetition product; the experiment is short enough that
calendar drift is accepted.

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

Faults caused by the agent's output never become infrastructure errors: a
pack that crashes on the agent's files reports that check as failed, and an
acceptance timeout is a failed round with its spend kept. Packs exit 2 only
for problems detected before the agent's code is exercised. A usage-limit
interruption records the attempt and its spend with status `usage_limit`
and pauses the matrix.

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
  per model and per query source (main, subagent); `billed` USD at list
  price with every token at the rate it was actually billed (primary
  figure); hypothetical main-agent scenarios no cache, 5m, 1h (subagents
  always as recorded at 5m); a `uniform_1h` counterfactual repricing
  subagent writes at the 1h rate; the difference to the CLI's self-reported
  cost, recorded for information only (the CLI figure is not trusted)
- provenance: `claude --version`, harness commit, spec and pack hashes,
  template commit, arm config hash; leak check of transcripts for pack paths
- behavior: turns, tool calls, subagent spawns by type and model, compaction
  events, wall time, API time
- layer 2: rounds, tokens, cost, user rating

## Cache accounting

List prices (USD per million tokens):

| Model | Input | Cache read | 5m write | 1h write | Output |
|-------|-------|-----------|----------|----------|--------|
| Opus 5.5 | 4.00 | 0.20 | 5.00 | 8.00 | 20.00 |
| Sonnet 5.5 | 2.00 | 0.10 | 2.50 | 4.00 | 10.00 |
| Sonnet 5 | 2.00 | 0.20 | 2.50 | 4.00 | 10.00 |
| Sonnet 4.6 | 3.00 | 0.30 | 3.75 | 6.00 | 15.00 |
| Haiku 5.5 | 0.10 | 0.01 | 0.125 | 0.20 | 0.50 |

Scenarios for the main agent, computed from the transcript's per-request
usage (subagent requests are always priced as recorded, at 5m):
- billed: as recorded (primary).
- none: cache read and cache creation tokens billed at input price.
- 5m and 1h: cache expiry simulated per agent thread from the actual
  request timestamps, so idle stretches during subagent work, builds and
  acceptance runs turn 1h hits into 5m misses and vice versa.

Human pauses are added offline as a probability of exceeding each TTL,
measured from the user's own transcripts as the idle time between the last
assistant entry and the next prompt (not prompt-to-prompt time, which
includes agent work). This offline analysis tool is still to be written;
transcripts with timestamps are archived per run so it can be applied later.

## Analysis

Primary metric: total billed spend over all attempts of an arm x task
(passes, cap hits, usage-limit drops, errors) divided by the number of
passes. Also reported: conditional median and range over passing runs,
attempts, passes and attrition per arm, wall time, rounds, spawn counts by
agent type and observed model, compaction counts, layer 2 cost.

## Deferred items (recorded, not applied)

- Sample size: three repetitions per cell cannot support per-cell
  significance claims. Planned for the next iteration: pre-registered
  contrasts, log billed cost with task fixed effects over the 4 x 2
  (subagent model x delegation) factorial pooled across tasks, bootstrap
  intervals, pass rate as a separate outcome, and more repetitions.
- Run-order randomisation and parallel-slot mixing: declined for this
  iteration; calendar and load drift accepted.
- Server-side feature-flag identity per fresh config dir: accepted as noise.
- Claude Code auto-update during the experiment: accepted; the version is
  recorded per run.
- Pack validation under heavy parallel load: not pursued; three slots on a
  32-thread machine.
