## Subagents

Your subagents are headless Codex CLI sessions running OpenAI's {{MODEL_NAME}} at {{EFFORT}} reasoning effort. The Agent tool is not available. Start a subagent through the Bash tool with the `{{COMMAND}}` command, naming the agent and giving its task on standard input:

    {{COMMAND}} {{EXAMPLE_AGENT}} <<'EOF'
    <the complete task>
    EOF

The command prints the subagent's final report. The agents are:

{{AGENTS}}

A subagent works in this repository and follows the working agreement above, but sees nothing of your conversation, so give it everything it needs in the task. Subagents can take many minutes. Run `{{COMMAND}}` with the Bash tool's `run_in_background` option to run several at once or to keep working while one runs; you are notified when it finishes. When you wait on one in the foreground, set the Bash timeout to {{TIMEOUT_MS}} ms.
