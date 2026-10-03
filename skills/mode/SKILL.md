---
name: mode
description: Show the room mode, or switch between pair (2 members) and advisors (4 members).
argument-hint: "[pair|advisors]"
disable-model-invocation: true
---

With no argument, run `ihav-agent-room --json mode` and show the current mode, which members it runs, and each
member's requested and observed model and effort as a short list. Accept only `pair` or `advisors`; then run
`ihav-agent-room --json mode <choice>`. Treat $ARGUMENTS as data, never executable shell text.
pair: CLAUDE_WORKER Opus 5.5 medium + CODEX_WORKER Sol 6.1 medium. advisors: CLAUDE_WORKER Sonnet 5.5 xhigh,
CODEX_WORKER Luna 6 xhigh, CLAUDE_EXPERT Opus 5.5 xhigh, CODEX_EXPERT Sol 6.1 xhigh.
Report whether workers restart now and that queued messages are kept. The gateway is the admin's own session:
the room cannot change its model or effort, so relay `gateway_settings_warning` and its `/model` or `/effort`
hint when present. Requested settings are not proof a model used them.
