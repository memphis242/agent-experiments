# Acceptance pack contract

Each task has a hidden pack at `tasks/<task>/acceptance/`. The harness copies
it to `<run-dir>/acceptance/` (outside the agent's repository) after every
agent round and executes it. `harness/acceptance.py` enforces this contract;
a violation aborts the run with status `infra_error` and is never counted as
an agent failure.

## Invocation

    bash <run-dir>/acceptance/run.sh <repo-path> <out.json>

The working directory is the copied pack. The script must not modify the
repository. It runs under a timeout (`--acceptance-timeout`, default 2400 s). A timeout is recorded as a failed round (spend kept) and the run continues with the generic feedback "The acceptance run did not complete; verify the build, tests and runtime behavior end to end." Packs must catch exceptions per check and report them as failed checks; exit 2 is reserved for faults that precede any use of the agent's code.

## Hidden answer key

The harness copies the pack to `<run-dir>/acceptance/` for the duration of one run, without any `selftest/` directory, and deletes the copy afterwards. The pack's environment carries no `CLAUDE_CODE_OAUTH_TOKEN`.

## Environment

| Variable | Meaning |
|----------|---------|
| `ACCEPT_NO_NAMESPACE` | Set to `1` by the harness; packs must skip any namespace or isolation setup of their own when it is present. The harness already runs the pack inside its isolation wrapper, whose uid-mapped inner namespace has no capabilities. |

## Rounds

`--max-rounds` (default 5) counts agent invocations: the initial session plus up to 4 repair resumes. The pack runs after every invocation; a failure after the last one is recorded as a cap hit.

## Exit status

| Status | Meaning |
|--------|---------|
| 0 | Every check passed |
| 1 | At least one check failed |
| 2 | Infrastructure or integrity error found BEFORE the pack touched the agent's repository; the run aborts as `infra_error` |

Any other status is a contract violation. Status must agree with `passed`.

## Result file

`out.json` lives under `<run-dir>/results/` (outside the agent's view) and is written by the script on exit 0 and 1:

    {
      "passed": false,
      "checks": [
        {
          "id": "status-tlm-packet-defined",
          "passed": false,
          "feedback": "The status telemetry packet declared in TASK.md section 3 is not produced after the app initializes.",
          "detail": "pytest tests/test_status.py::test_packet failed: ..."
        }
      ]
    }

Rules:

- Exactly the keys `passed` and `checks` at top level; exactly `id`, `passed`,
  `feedback`, `detail` in each check. `checks` is non-empty.
- `id` is a stable kebab-case string (`[a-z0-9]+(-[a-z0-9]+)*`), unique in the
  file, and identical for the same fault in every run. It is the sort key for
  feedback, so renaming an id changes the feedback order.
- `passed` (top level) is true exactly when every check passed.
- `feedback` is written in the language of `TASK.md`. It is required and
  non-empty for a failing check, and sent verbatim to the agent. It must never
  mention the pack, test files, or `run.sh` (the harness rejects feedback
  containing `acceptance/`, `test_*.py`, `*_test.py`, or `run.sh`).
- `detail` is free text for the experimenter. It is never sent to the agent.
  Use an empty string when there is nothing to add.

## Feedback message

Failing checks are sorted by `id`. The message is a fixed preamble followed by
a numbered list of each failing check's `feedback`, so identical faults yield
byte-identical feedback in every arm.
