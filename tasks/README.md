# Tasks

Each task directory holds:

- `spec.md`: the specification the agent receives. It is copied into the
  run's repository as `TASK.md`. Every public name (types, function codes,
  message IDs, event IDs, file paths) is pinned so hidden tests can compile
  and run against any correct implementation.
- `prompt.md`: the one-paragraph prompt passed to `claude -p`.
- `acceptance/`: the hidden acceptance pack. Never copied into the agent's
  repository. See `harness/ACCEPTANCE_CONTRACT.md` for its interface.

| Task | Directory | Kind | Expected duration |
|------|-----------|------|-------------------|
| 1 | task1-utlog-translator | script | 15-30 min |
| 2 | task2-sample-app-status | extend small app | 45-90 min |
| 3 | task3-hk-staleness | extend flight app | 1.5-3 h |
| 4 | task4-stor-app | new app from spec | 3-6 h |
| 5 | task5-system-build | multi-app two-node system from spec | 10-20 h |
