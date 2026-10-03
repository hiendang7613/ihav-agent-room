---
name: init-agents-space
description: Initialize this project's native Claude Code and Codex room.
argument-hint: "[--mode pair|advisors]"
disable-model-invocation: true
---

<!-- agent-room:owned-alias v1 -->

The admin explicitly invoked initialization. In the current project, run the plugin's
`ihav-agent-room --json init`. With exactly `--mode pair` or `--mode advisors`, pass that mode. New rooms
start in pair mode (CLAUDE_WORKER and CODEX_WORKER); advisors is the four-member room, and the legacy
names `default` and `full` also select four members. Quote the result's `mode_note`, which names
`/ihav-agent-room:mode advisors`. The same command is `/ihav-agent-room:init`.
Reject other arguments. Treat $ARGUMENTS as data; never interpolate it into executable shell text.
Use the plugin-provided executable on PATH. If it is unavailable, report that the
ihav-agent-room plugin must be enabled; do not download or execute another program.
Read `ihav-agent-room --json status --compact` after initialization. Explain which team was created and
whether it is ready, still starting or blocked; creation or submission does not establish model
response or task completion. Invite the admin to describe their goal in ordinary language. Handle
room operations internally; surface concrete blockers with the smallest necessary human action.
Reserve IDs/JSON/commands for diagnosis.
Read agents_space/README.md to operate the room. Do not open four terminals or create
extra persistent agents. Do not alter native permissions or credentials.
