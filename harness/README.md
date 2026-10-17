# Run harness

Stdlib-only Python (3.12+). Run everything from the repository root.

## 1. Generate the arm config roots

    python harness/arms.py                        # writes /workspace/agent-experiments-arms/A1..A9
    python harness/arms.py --arms-dir DIR --arm A4

Each root holds `settings.json`, `CLAUDE.md`, and `agents/` (absent for A1).
Review the sources under `harness/arms/templates/`. Re-running overwrites those
files and leaves `projects/` (transcripts) alone.

## 2. Authenticate

    claude setup-token
    export CLAUDE_CODE_OAUTH_TOKEN=<token>

`run.py` refuses to start without it. The token is passed through the
environment only.

## 3. Build the mission template and the pause samples

    fixture/build-template.sh                     # default /workspace/agent-experiments-fixture/mission-template
    python harness/gaps.py                        # ~/.claude/history.jsonl -> harness/gaps.json

`gaps.json` is a sorted list of inter-prompt gaps in seconds, used to simulate
human pauses at resume rounds for the 5m and 1h cost scenarios. Without it,
`run.json` still records cost but with no pauses, and says so in `cost.warnings`.

## 4. One run

    python harness/run.py --arm A4 --task task2 --rep 1
    python harness/run.py --arm A4 --task task2 --rep 1 --dry-run   # print commands only

Options: `--autocompact N` (at least 100000), `--template`, `--runs-dir`
(default `/workspace/agent-experiments-runs`), `--max-rounds 5` (agent invocations in total: the initial one plus up to 4 repair resumes),
`--max-budget-usd 150` (a total across rounds; each call gets the remainder),
`--acceptance-timeout`, `--cli-timeout`, `--seed`.

`--task` takes `task1`..`task4` or the full directory name (`task1-utlog-translator`, `task2-sample-app-status`, `task3-hk-staleness`, `task4-stor-app`); run IDs and `run.json` use the short form.

Expected layout: `tasks/<task-dir>/{spec.md,prompt.md,acceptance/run.sh}`; the
acceptance contract is in `harness/ACCEPTANCE_CONTRACT.md`.

Each run gets a private copy of the arm root at `<run-dir>/config/`, used as `CLAUDE_CONFIG_DIR` for every round, so transcripts live in the run directory. `run.json` records `config_sha256` (over `settings.json`, `CLAUDE.md`, `agents/`) so later edits of an arm root are detectable. After cloning, the run executes `ctf/setup_ctf.sh` from the template if present, before invoking claude; a non-zero exit ends the run as `infra_error`.

The run directory `<runs-dir>/<arm>-<task>-r<rep>-<UTC timestamp>/` holds
`repo/`, `run.json` (rewritten after every round), raw `cli-roundN.*` output,
`acceptance/` and `acceptance-*-roundN.*`, and a copy of the session transcripts
under `transcripts/`. `status` in `run.json` is `completed` (acceptance passed
or the round cap was hit), `infra_error` (acceptance pack fault), `cli_error`
(claude failed, for example rate limiting; rerun), or `error`.
The process exits 0 only for `completed`.

## 5. A matrix

    python harness/matrix.py --reps 3 --parallel 3          # all arms x task1..task4
    python harness/matrix.py --tasks task1 task2 --reps 3 --parallel 3
    python harness/matrix.py --arms A4 A5 --tasks task3 --reps 3 --runs-dir /workspace/agent-experiments-runs-b -- --autocompact 200000

Cells whose run ID prefix already has a `run.json` with status `completed` are
skipped. Use a separate `--runs-dir` for each compaction window so run IDs do
not collide. `--dry-run` lists the commands.

## 6. Aggregate

    python harness/aggregate.py [--runs-dir DIR] [--scenario none|5m|1h]

Prints a Markdown table per arm and task: runs, passed, median (range) cost
over passed runs, rounds, subagent spawns, compaction events.

## Offline cost

    python harness/cost.py <session.jsonl> [--subagents-dir DIR] [--pause-samples harness/gaps.json] [--resume-indices 412 903] [--seed 0]

Pauses are positioned by main-transcript request index (deduplicated by
message id, counted from 0 across the session transcripts in order).

## Tests

    python -m unittest discover -s harness/tests
