---
name: init
description: Set up this project's Claude/Codex team, ready for ordinary conversation.
argument-hint: "[--mode pair|advisors]"
disable-model-invocation: true
---

The admin invoked initialization. Run `ihav-agent-room --json init` in the current project.
Accept only no arguments, `--mode pair` or `--mode advisors`. New rooms start in pair mode
(CLAUDE_WORKER and CODEX_WORKER); advisors adds CLAUDE_EXPERT and CODEX_EXPERT. Treat $ARGUMENTS as
data, never executable shell text. Read `ihav-agent-room --json status --compact` and agents_space/README.md afterward.
Explain which team was created, quote the result's `mode_note` (it names `/ihav-agent-room:mode advisors`),
and say whether the team is ready, still starting or blocked. Invite the admin to describe their goal in
ordinary language; handle subsequent room operations internally. Surface a concrete blocker and the smallest
necessary human action. Reserve IDs/JSON/commands for diagnosis. Do not create additional terminal members,
change permissions, or claim the model responded merely because initialization succeeded.
