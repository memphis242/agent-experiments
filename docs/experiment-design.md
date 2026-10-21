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

## Experiment A: model tiering (22 arms)

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
| A20 | Sonnet 4.6 (high effort) | none (Agent tool disallowed) | n/a |
| A21 | Opus 5.5 (Claude Code) | GPT-6.1 Sol, high effort, as headless Codex CLI sessions (all five agents) | eager |
| A22 | Opus 5.5 (Claude Code) | GPT-6.1 Sol, high effort, as headless Codex CLI sessions (implementer, tester, debugger) | roles, with a secondary debugger |

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
if that trial passes. A20 (solo Sonnet 4.6 at high effort) runs Tasks 1 and 2
only, all ten repetitions of each whatever its Task 1 gate result, as a
reference point for solo Sonnet 5.

A21 and A22 ("Claude Code Opus 5.5 high + headless Codex GPT-6.1 Sol high") ask
whether another vendor's coding harness works as the subagent tier. The main
agent is the same Claude Code Opus 5.5 at high effort as A2; its subagents are
OpenAI's GPT-6.1 Sol at high reasoning effort, each one a fresh `codex exec`
session started through the Bash tool with the arm's `codex-agent` command
(`harness/codex_agent.py`). The Agent tool is denied, so no Claude subagent can
run. A21 uses A2's eager delegation text and the full roster; A22 uses A11's
selective text with the main agent planning, debugging, reviewing and
integrating, and Sol as implementer, tester and a secondary debugger for a
failure the main agent has already investigated. The delegation paragraph is
followed by a fixed section that explains `codex-agent`, lists the agents with
their Claude Code descriptions, and says to run long subagents with the Bash
tool's `run_in_background` (the main agent is notified on completion, and the
print-mode wait covers background commands as it does background subagents),
not to poll a background subagent with `pgrep`, `ps` or a loop, and to run it
in the foreground with a two-hour Bash timeout when it must wait for one.
Each agent's Claude Code definition body is passed to Codex as developer
instructions, and the working agreement (CLAUDE.md without its delegation
section) as Codex's global AGENTS.md, matching what a Claude Code subagent
receives. Explorer and reviewer, whose Claude Code definitions grant no editing
tool, run in Codex's read-only sandbox; the others run without Codex's sandbox,
as the main agent runs with bypassPermissions. Codex sub-agents, bundled skills,
web search, apps, plugins, memories and fast mode are disabled; `codex-agent`
refuses to run inside a subagent. The two arms run Task 1 to Task 3 with ten
repetitions; Task 4 follows if their Task 3 spend per pass is on par with the
Task 4 finalists.

The no-polling instruction comes from the first Codex pilots (rep 1 of Tasks 1
and 2, 2026-10-19). Three of the four passed in round 1. In the fourth (A22
Task 1, run 4326636d) the main agent started its Codex tester in the
background and then, instead of waiting for the notification, waited in the
foreground on `until ... ! pgrep -f "codex-agent tester"; do sleep 15; done`
with the two-hour timeout. `pgrep -f` matches the loop's own shell, whose
command line contains the pattern, so the loop could never end; the tester
had finished in three minutes. The same self-matching loop appears in the
Claude-only arms while they wait on builds and CTF runs (eight runs
lost 2 to 10 minutes to it), where the ten-minute Bash ceiling bounds the
loss; the Codex arms' two-hour ceiling, needed to wait on a subagent in the
foreground, let it stall the round for two hours. This was treated as an
integration fault of the Codex arms rather than a result: the delegation
section now forbids polling a background subagent, the stalled run was
stopped, and all four pilots are rerun on the corrected instructions (the
originals are kept as pilots and excluded from the results). The Claude-only
arms are unchanged, so their self-matching loops stay in the record as
ordinary behaviour.

A Codex arm's run is void, stopped before acceptance and rerun, when a subagent
call hits the ChatGPT plan's Codex usage limit (status `usage_limit`) or fails
for any other reason (`codex_error`): the main agent would then be measured
working around a missing subagent. A call the main agent (or the CLI) stops
with a signal is not a failure. The launcher holds every Codex cell after such
a run until it has been looked into.

A26 to A29 are solo Codex arms: the main agent is a headless `codex exec`
session on GPT-6.1 Sol (A26), GPT-6 Luna (A27), GPT-5.6 Sol (A28) or GPT-5.6
Luna (A29) at high reasoning effort, with no subagents. They get the same task
prompt, acceptance rounds and feedback as the Claude arms, and the working
agreement (CLAUDE.md without its delegation section) as the run's Codex
AGENTS.md, so A26 to A29 compare to the solo Claude arms on identical
instructions. The first round is `codex exec --json -C <repo> -o <last message>`
with the prompt on standard input; each feedback round is `codex exec resume
<thread id>` with the feedback on standard input, the thread id taken from the
`thread.started` event. Every round passes the model, reasoning effort and a
`Run ID: <id>` developer message (the counterpart of the Claude main agent's
appended system prompt) as `-c` overrides. The run uses the same per-run
CODEX_HOME, login handling, namespaces and prompt check as the Codex-subagent
arms, with the same Codex configuration (web search, apps, plugins, memories,
sub-agents and fast mode disabled). It needs no Claude token, and `--autocompact`
does not apply. A round that fails ends the run as `usage_limit` (the ChatGPT
plan's Codex limit, recognised from Codex's own error text) or `cli_error`.
Cost, turns (model requests), context peak, compactions, tools, model and effort
come from the session log in the run's CODEX_HOME; the validation section flags
a turn on another model or effort, a multi-agent or web-search tool, and
sessions the rounds did not report. `codex exec` reports each thread's cumulative
usage, which `cost.exec_usage_reconciliation` compares with the log. The cache
write TTL scenarios coincide with billed, since Codex has no TTL split and no
pauses are simulated.

A30 to A32 give a Codex GPT-6.1 Sol main agent (low effort) the tiered delegation
of A14, A15 and A24, with subagents at high effort. A30's subagents are built-in
Codex V2 agents on GPT-6 Luna (the roster and role prompts of A24);
A31's are headless Claude Code sessions, the implementer on Opus 5.5 and the
small-implementer and tester on Haiku 5.5 (the inverse of A23); A32's are Claude
Code sessions on Haiku 5.5 throughout (the inverse of A24). The main agent's
AGENTS.md is what the matching Claude main agent's CLAUDE.md is (the working
agreement and the tiered paragraph) followed by the subagents section, rewritten
for the arm's delegation mechanism. A30 uses Codex 0.163's built-in multi-agent
V2 runtime, explicitly enabling `features.multi_agent` and
`features.multi_agent_v2.enabled`. Its `agents.implementer`,
`agents.small-implementer` and `agents.tester` declarations use the same descriptions
as A24, each referencing a role TOML with GPT-6 Luna, high reasoning effort and
exactly the role prompt body A24 passes to `codex-agent`. No concurrency limit is
set, since the tiered arms specify none: V2 defaults to the root plus three resident
children. The main uses `spawn_agent` with `agent_type`, `fork_turns="none"` and a
self-contained assignment, then `wait_agent`. Both
`features.multi_agent_v2.default_wait_timeout_ms` and `max_wait_timeout_ms` are
3600000 (one hour), reducing check-ins as approved by the owner. Role features
disable further delegation. Built-in children inherit global AGENTS.md, including
the tiered delegation section; Codex cannot replace that with a child-only global
agreement. The working agreement and tiered paragraph preceding the subagent
section remain byte-identical to A24.

A30 uses one CODEX_HOME for main and children, with no `codex-agent` launcher,
subagent home or shell CODEX_HOME override. Main invocations never use
`--ephemeral`, since children need persisted rollouts. The root thread is the one
reported by `codex exec` or resume; children are identified by the
`parent_thread_id` ancestry in rollout metadata. Requests are deduplicated within
threads, attributed to main or subagent and priced per model. Exec's cumulative
`turn.completed.usage` is reconciled only against the main thread. Spawn counts
and roles come from child rollout metadata. Main turns must match the main model
and effort; each child must match its configured role's model and effort. Unknown
roles, unexpected threads and models fail validation. Child error events or logs
ending without completion stop the run before acceptance as `codex_error`, or
`usage_limit` for usage-limit errors.

A31 and A32 start `claude-agent` with the task on standard input and report on
standard output, waiting in the foreground or running several with `&` and a
`wait` in the same shell command, never polling with `pgrep`, `ps` or a loop.
Codex's shell tool has no per-command kill timeout: a command that outlives a call
keeps running as a session. These arms set `background_terminal_max_timeout` to
7200000 (two hours) and `features.code_mode.default_exec_yield_time_ms` to 600000
(ten minutes), explicitly enabling Code Mode. The latter is the 0.163 binary's
CodeModeConfigToml field, also used in its tool-description template. Feature
checks support feature tables with `enabled` as well as boolean feature values.
A31 and A32's `claude-agent` (`harness/claude_agent.py`) runs `claude -p` with the agent's
model, its role as an appended system prompt, `--tools` limited to the agent's
definition, the Agent tool and the denied tools denied, and bypassPermissions,
in a CLAUDE_CONFIG_DIR of the run (`config/claude/`) whose settings.json gives the
agent's effort and the 5-minute cache TTL of a Claude Code subagent, and whose
CLAUDE.md is the working agreement. It differs from a native Claude Code subagent
in keeping Claude Code's full default system prompt (the role is appended, not
substituted) and in being a session of its own with its own transcript. The
Claude login reaches it through a file in the namespace's private `/dev/shm`
(written by the namespace wrapper from a variable it then removes, mode 600), so
the Codex main agent's environment never holds it, Codex's shell tool would
withhold a variable named like a secret anyway, and nothing is on disk; the main
agent can read the file if it looks for it. A Claude usage limit in a
`claude-agent` call ends the run as `usage_limit`, any other failed call as
`claude_error`, after the round and before acceptance. Costs: the main agent from
its Codex session log (source main), each built-in Codex subagent from its
descendant thread in the same home, each Claude call from its transcript (source subagent, Claude prices and
cache TTLs as recorded) plus the tokens the call's own `modelUsage` holds beyond
its transcript.

Codex usage is priced at OpenAI's standard API rates for GPT-6.1 Sol (USD per
million tokens: input 2.00, cached input 0.10, cache write 2.50, output 10.00;
above 272K input tokens 4.00, 0.20, 5.00 and 15.00, applied per request, since
the pricing page states the threshold only as input tokens), one request per model
response from Codex's session log, with reasoning tokens billed as output. The
Claude side is priced as in every other arm. Both run on subscriptions (Claude
and ChatGPT), so the figures are API-equivalent, as everywhere else.

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
plus 20 for A20, plus up to 66 for A21 and A22, plus repair rounds.

In print mode the CLI stops background subagents that are still running ten
minutes after the main agent's last turn, which ends the round with their work
unfinished. Runs from 2026-10-19 raise that ceiling to two hours
(`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`). Earlier completed runs in which the CLI
stopped a subagent this way (27, all in delegating arms) are rerun with the same
arm, task and repetition, and the originals are listed in
`docs/results/superseded.json` and excluded from the results. A run in which the
ceiling never fired would have behaved identically under the new setting, so the
other runs stand.

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
- Every arm hides the owner's `Path.home()/".secrets"` directory under an empty
  tmpfs in the agent's mount namespace, alongside the other hidden paths. The
  harness loads credentials before entering the namespace; credential delivery
  into the run is unaffected.
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
- A Codex arm (A21, A22) also gets its own `CODEX_HOME` under the run dir,
  built from the arm's `codex/` config, with the bundled skills disabled and a
  copy of the user's Codex login, removed when the run ends (written back to
  `~/.codex/auth.json` only if Codex refreshed it). Every `codex-agent` call is a
  new Codex session, so no conversation, history, memory or cache state carries
  between subagents, runs or the user's own sessions; `~/.codex` is hidden in
  the agent's namespace except its installed binaries (bound back read-only;
  the acceptance namespace keeps the host view, as in every arm), and the
  run is pinned to the Codex binary resolved at its start. Before the first
  round the harness renders Codex's model-visible prompt
  (`codex-prompt-input.json` in the run dir) and refuses to start if it carries
  skills, app or plugin sections (or multi-agent sections outside A30) or lacks
  the working agreement,
  or if `codex features list` shows a configured feature in the wrong state,
  and loads the final config with `--strict-config` as every call does. The
  login's refresh token is single-use, so two runs refreshing it at once would
  race: no Codex run starts within 12 hours of the login's refresh point (8
  days after its last refresh, or its access token's expiry if sooner), and a
  login copy left by a killed run is written back if newer and removed when
  the next Codex run starts.
  Codex's resolver is statically linked and sends plain DNS to the host's stub
  resolver, which the namespace cannot reach, so these runs add pasta's
  `--dns-forward` and a private `/etc/resolv.conf` naming its address.
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
