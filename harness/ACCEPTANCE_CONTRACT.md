# Acceptance pack contract

Each task has a hidden pack at `tasks/<task>/acceptance/`. The harness copies
it to `<run-dir>/acceptance/` (outside the agent's repository) after every
agent round and executes it. `harness/acceptance.py` enforces this contract;
a violation aborts the run with status `infra_error` and is never counted as
an agent failure.

## Invocation

    bash <run-dir>/acceptance/run.sh <repo-path> <out.json>

The working directory is the copied pack. The script must not modify the
repository. It runs under a timeout (`--acceptance-timeout`, default 1800 s).

## Exit status

| Status | Meaning |
|--------|---------|
| 0 | Every check passed |
| 1 | At least one check failed |
| 2 | Infrastructure error (pack could not run); the run aborts as `infra_error` |

Any other status is a contract violation. Status must agree with `passed`.

## Result file

`out.json` is written by the script on exit 0 and 1:

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
