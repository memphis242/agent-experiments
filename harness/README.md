# Run harness

Stdlib-only Python (3.12+). Run everything from the repository root.

## 1. Generate the arm config roots

    python harness/arms.py                        # writes /workspace/agent-experiments-arms/A1..A9
    python harness/arms.py --arms-dir DIR --arm A4

Each root holds exactly `settings.json`, `CLAUDE.md`, and `agents/` (absent for A1); a run refuses to start if the root holds anything else. Every arm denies a fixed set of tools (`arms.DENIED_TOOLS`); A2-A9 also set `CLAUDE_CODE_SUBAGENT_MODEL` (with `_FORCE=1`) in `settings.json` `env`, so built-in agent types run on the arm's subagent model. Regenerate the roots after pulling harness changes: matrix and aggregate compare hashes.
Review the sources under `harness/arms/templates/`. Re-running overwrites those
files.

## 2. Authenticate

    claude setup-token
    export CLAUDE_CODE_OAUTH_TOKEN=<token>

`run.py` refuses to start without it (except `--dry-run`). The child environment is built from an allowlist (PATH, HOME, USER, LOGNAME, LANG, LC_ALL, LC_CTYPE, TERM, SHELL, the token, and CLAUDE_CONFIG_DIR, plus a fixed `DISABLE_AUTOUPDATER=1`); `ANTHROPIC_API_KEY` and everything else is not inherited. Acceptance packs get the same environment without the token.

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
(default `/workspace/agent-experiments-runs`), `--max-rounds 5` (agent invocations in total: the initial one plus up to 4 repair resumes). There is no dollar cap: runs are never stopped on cost, and cost is only recorded.
`--acceptance-timeout` (default 2400 s), `--cli-timeout` (default 6 hours), `--seed`.

`--task` takes `task1`..`task4` or the full directory name (`task1-utlog-translator`, `task2-sample-app-status`, `task3-hk-staleness`, `task4-stor-app`); `run.json` uses the short form.

Expected layout: `tasks/<task-dir>/{spec.md,prompt.md,acceptance/run.sh}`; the
acceptance contract is in `harness/ACCEPTANCE_CONTRACT.md`.

Each run gets a private copy of the arm root at `<run-dir>/config/`, used as `CLAUDE_CONFIG_DIR` for every round, so transcripts live in the run directory. `run.json` records `config_sha256` (over `settings.json`, `CLAUDE.md`, `agents/`) so later edits of an arm root are detectable. After cloning, the run executes `ctf/setup_ctf.sh` from the template if present, before invoking claude; a non-zero exit ends the run as `infra_error`.

The run ID is an opaque 8-hex nonce (it is the directory name and the `Run ID:` in the system prompt, so it does not reveal the arm). `run.json` holds the arm, task, and repetition, and `<runs-dir>/runs.tsv` indexes them (`nonce, arm, task, rep, start|end, time, status`; the last line per nonce wins).

The run directory `<runs-dir>/<nonce>/` holds `repo/`, `config/`, `run.json` (rewritten after every round), raw `cli-roundN.*` output, `results/` (acceptance result JSON and logs), and a copy of the session transcripts under `transcripts/`. The pack copy is deleted after each acceptance run and its `selftest/` directory is never copied.

`status` in `run.json`: `completed` (acceptance passed, or the round cap was hit), `usage_limit` (subscription limit; exit code 3), `infra_error` (acceptance pack exit 2 or contract violation, or the CTF setup failed), `cli_error` (claude failed or timed out), `error`. Only `completed` exits 0. An acceptance timeout is a failed round (spend kept, generic feedback, the run continues). `run.json` also records `provenance` (claude version, harness git SHA and dirty flag, spec and pack hashes, template HEAD), `validation` (model, effort, and forbidden-tool violations per transcript; report only), `leak_check` (transcript lines mentioning `acceptance/`, `selftest`, `reference/`, or `agent-experiments-runs`; report only), and `cost`.

Cost: `cost.scenarios.billed` prices the CLI's session-total `modelUsage` token counts (which include compaction summary requests the transcripts omit) with the price table; each model's 5m/1h cache-write split comes from its transcript writes (main model 1h, subagent models 5m when there are none). It is the primary figure (`totals.billed_usd`). `cost.billed_transcript_usd` is the transcript-only figure and `cost.untranscribed` the per-model CLI minus transcript remainder. If `modelUsage` is missing, billed falls back to the transcript with a warning. `none`, `5m`, and `1h` reprice the main agent only; subagent requests keep recorded pricing. `uniform_1h` reprices subagent writes at the 1h price. `cost.reconciliation` compares cumulative billed cost through each round with the CLI's `total_cost_usd` (report only; the CLI figure is kept as `totals.cli_reported_cumulative_usd` (the CLI figure is cumulative per session; per-round values are in `totals.cli_reported_by_round_usd`)).

## Run isolation

cFS binds fixed UDP ports (CI_LAB 1234, TO_LAB 2234) and, with CTF, shares
`/dev/shm` (OSAL's RAM disk at `/dev/shm/osal:RAM`, which CTF deletes between
runs), and CTF finds and kills cFS by command line across the host. Every claude invocation and every acceptance `run.sh` therefore runs in
its own namespaces:

    pasta --config-net --quiet -t none -u none -T none -U none -- sh -c 'unshare -m -i -p -f --mount-proc --kill-child sh -c "mount -t tmpfs tmpfs /dev/shm && <hiding steps> && unshare -U --map-user=<uid> --map-group=<gid> sh -c <command>"'

pasta and unshare must be installed; outbound HTTPS still works. In order:

1. **User + network namespace (pasta).** Gives each run its own loopback, so
   fixed UDP ports do not collide. The four `none` port options disable pasta's
   default loopback port forwarding, which otherwise diverted the namespace's
   UDP traffic to host ports and reached other sessions' cFS instances.
2. **Mount, IPC, and PID namespaces (`unshare -m -i -p -f --mount-proc --kill-child`).** A private tmpfs on `/dev/shm`
   keeps OSAL's RAM disk and shared memory per run; the PID namespace makes
   CTF's `pgrep`/`pkill` see only this run and kills everything left inside when
   the namespace ends. The mounts happen here because only this layer still has
   root capabilities. For claude invocations (not acceptance runs) this layer
   also hides the answer key: this run's `repo/` and `config/` are bind-mounted
   to a staging directory, an empty tmpfs is mounted over each path in
   `run.HIDDEN_PATHS` (`/workspace`, `~/.claude`, `~/projects/agent-experiments`,
   `/tmp`), and the staged directories are bound back at their original paths.
   Nothing else under those paths is visible, and the command starts in `repo/`.
   `claude` itself must live outside the hidden paths.
3. **Nested user namespace (`unshare -U --map-user --map-group`).** Maps the
   harness uid/gid back. As fake root, OSAL skips its unprivileged
   message-queue depth adaptation and cFS fails to boot with `OS_QueueCreate`
   EINVAL. Because the uid is not 0, `IS_SANDBOX` is not needed.

Every wrapped command runs in its own session; on timeout the whole process group is killed.

`run.py` self-checks the full wrapper at startup (harness uid, empty `/dev/shm`, own PID namespace, only this run's `repo/` and `config/` visible, `claude` reachable) and fails clearly otherwise. `--no-netns` disables the
wrapper for debugging; do not run concurrent cFS runs with it.
After each claude
call and each acceptance run, leftover `core-cpu1` processes whose cwd is under
the run directory are killed (found by scanning `/proc`).

## 5. A matrix

    python harness/matrix.py --reps 3 --parallel 3          # all arms x task1..task4
    python harness/matrix.py --tasks task1 task2 --reps 3 --parallel 3      # 3 is the default; needs pasta (see Run isolation)
    python harness/matrix.py --arms A4 A5 --tasks task3 --reps 3 --runs-dir /workspace/agent-experiments-runs-b -- --autocompact 200000

A cell is skipped only if a `completed` run exists for it whose `config_sha256` equals the current arm root's and whose harness SHA equals the current one. Cells that ended in `usage_limit`, `cli_error`, or any other status rerun from scratch (their spend stays recorded in their run directories). Once any run reports a usage limit, no new cell is launched; wait for the limit to reset and rerun the same command. Use a separate `--runs-dir` for each compaction window.

**Use a fresh runs directory for the real matrix**, for example `--runs-dir /workspace/agent-experiments-runs-main`. The default directory may hold smoke runs made with older settings or harness code. `--dry-run` lists the commands.

## 6. Aggregate

    python harness/aggregate.py [--runs-dir DIR] [--scenario billed|none|5m|1h|uniform_1h]

Per arm and task: passes/attempts, spend per pass (total spend over all attempts divided by passes; the primary metric), and min-to-max ranges over all attempts for cost, wall minutes, main-agent turns, spawns, rounds, and compactions. `--task` filters tasks; `--markdown-out FILE` also writes the table to a file. Warnings list runs with an analysis error and runs whose config or harness hash differs from the majority.

## Offline cost

    python harness/cost.py <session.jsonl> [--subagents-dir DIR] [--pause-samples harness/gaps.json] [--resume-indices 412 903] [--seed 0]

Pauses are positioned by main-transcript request index (deduplicated by
message id, counted from 0 across the session transcripts in order).

## Tests

    python -m unittest discover -s harness/tests

## Reanalyze

    python harness/reanalyze.py <runs-dir> [--run ID]

Recomputes `cost`, `behavior`, `validation`, and `totals` of existing `run.json` files from `transcripts/` and `cli-round*.stdout.json`, leaves everything else intact, rewrites atomically, sets `reanalyzed_with` (harness SHA), and prints billed before and after per run.
