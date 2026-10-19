## Subagents

Your subagents are headless Codex CLI sessions running OpenAI's {{MODEL_NAME}} at {{EFFORT}} reasoning effort. The Agent tool is not available. Start a subagent through the Bash tool with the `{{COMMAND}}` command, naming the agent and giving its task on standard input:

    {{COMMAND}} {{EXAMPLE_AGENT}} <<'EOF'
    <the complete task>
    EOF

The command prints the subagent's final report. The agents are:

{{AGENTS}}

A subagent works in this repository and follows the working agreement above, but sees nothing of your conversation, so give it everything it needs in the task. Subagents can take many minutes. Run `{{COMMAND}}` with the Bash tool's `run_in_background` option to run several at once or to keep working while one runs. You are notified when a background subagent finishes, and the notification carries its report or names the file that holds it, so do not check on it yourself: no `pgrep`, `ps` or polling loop. To wait for one before doing anything else, run it in the foreground instead, with the Bash timeout set to {{TIMEOUT_MS}} ms.
