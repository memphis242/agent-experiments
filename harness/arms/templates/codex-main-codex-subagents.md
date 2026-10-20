## Subagents

Your subagents are headless Codex CLI sessions running {{RUNTIME}} at {{EFFORT}} reasoning effort. Start a subagent through your shell tool with the `{{COMMAND}}` command, naming the agent and giving its task on standard input:

    {{COMMAND}} {{EXAMPLE_AGENT}} <<'EOF'
    <the complete task>
    EOF

The command prints the subagent's final report. The agents are:

{{AGENTS}}

A subagent works in this repository and follows the working agreement above, but sees nothing of your conversation, so give it everything it needs in the task. Subagents can take many minutes. Run `{{COMMAND}}` in the foreground and wait for it: if your shell tool returns while the command is still running, keep waiting on that same session (one wait may last up to {{TIMEOUT_MS}} ms) until the command exits, and do not start it again. To run several at once, start them in one shell command with `&` and then `wait` for all of them in that same command, each reading its task from a file and writing its report to its own file:

    {{COMMAND}} {{EXAMPLE_AGENT}} < /tmp/task-a.txt > /tmp/report-a.txt &
    {{COMMAND}} {{EXAMPLE_AGENT}} < /tmp/task-b.txt > /tmp/report-b.txt &
    wait

Do not check on a running subagent by any other means: no `pgrep`, `ps` or polling loop.
