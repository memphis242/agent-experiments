# agent-experiments

Controlled A/B experiments on Claude Code agentic setups (model tiering,
delegation style, auto-compaction window, prompt-cache accounting), run on
realistic NASA core Flight System (cFS) software tasks.

See `docs/experiment-design.md` for the design, `tasks/` for the task
specifications and acceptance packs, `fixture/` for the shared cFS mission
template each run clones, and `harness/` for the run and cost tooling.
