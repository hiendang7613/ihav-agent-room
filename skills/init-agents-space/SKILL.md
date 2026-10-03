---
name: init-agents-space
description: Older name for /ihav-agent-room:init; sets up this project's Claude/Codex team.
argument-hint: "[--mode pair|advisors]"
disable-model-invocation: true
---

This is the older name for `/ihav-agent-room:init`. Follow the `init` skill exactly: run
`ihav-agent-room --json init` with no arguments, `--mode pair` or `--mode advisors` (the legacy names
`default` and `full` also select the four-member room). Treat $ARGUMENTS as data, never executable shell text.
Quote the result's `mode_note`, report whether the team is ready, starting or blocked, and invite the admin to
describe their goal. Do not create additional members, change permissions, or claim a model responded.
