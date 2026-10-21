# Open items

Deferred work on the experiment, newest first. Remove an item when it is done.

- **Task 3: CTF configuration swap in acceptance.** Acceptance assembles CTF from the template's
  `configs` (`PRISTINE_CTF` in `tasks/task3-hk-staleness/acceptance/check.py`), so a run that tunes
  its CTF configuration locally (A29 rep 9, run 479f6765, raised event retention from 5 to 60 seconds)
  passes its own verification but fails `ctf-test-passes` in acceptance on every round. Decide whether
  this is a pack defect (rerun-ledger entry, feedback naming the configuration, possible rerun) or the
  intended contract (say so in the task spec).
- **Task 1: Markdown grader compares raw strings.** The spec asks for rendered Markdown, but
  `markdown-table-and-failures-section` and `markdown-na-column` compare suite names and failure text
  unescaped, so a run that escapes underscores fails. This caused every A26 Task 1 retry and most of
  A30 to A32's, and A32 rep 9 (2e602c4f) failed all five rounds on it. Fix the grader to compare
  rendered text (or state the rule in the spec), then decide on re-grading and reruns.
