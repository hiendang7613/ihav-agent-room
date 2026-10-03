---
name: status
description: Summarize members, advisory attention, open work and pending requests.
disable-model-invocation: true
---

Read `ihav-agent-room --json status --compact`. Reject arguments. Give a concise Vietnamese summary of
current mode, actual supervisor liveness, member errors, unfinished tasks/next steps,
questions and approvals requiring an actual admin decision. Main interprets technical state internally;
present outcomes, progress, blockers and next steps in ordinary language. Keep IDs, counters, transport
state names and commands for explicit detail/diagnosis. Peer questions go to the relevant teammate
unless scope, requirements, authority or cost needs admin. Do not turn internal queue counts into
an admin checklist. A saved running state with no live supervisor
is stale, not healthy. Native turn completion is not task completion. Do not start workers,
repair configuration or make provider calls as part of status.

Use task.review_status, latest_attempt, missing_evidence, last_progress and unprocessed_messages
to identify stale reviews and incomplete work. A completed native attempt or processed ACK
does not mean acceptance passed. Missing native output/turn IDs remain unknown. When recovery
matters, read `ihav-agent-room task context TASK_ID` and report its checkpoint_reconcile reasons.

Use `attention.by_member` to summarize current work each member may want to inspect.
Present it as optional attention, with any stated blockers and links to current records.
Members choose whether and when to act within existing authority. A pending review points to
the reviewer; approved peer work awaiting completion points to main. Questions and tentative
ideas remain welcome in ordinary conversation. Reading attention does not send reminders,
acknowledge messages, create tasks or start native turns.

Use `pending_inboxes.by_member` counts internally as unprocessed conversation signals. Mention them
when they explain a relevant wait/blocker; do not present them as proof of receipt. Use the supplied inbox command only while
bound as that member; never retry or resend based on the count alone.
If status has `gateway_settings_warning`, relay it with its `/model` or `/effort` hint; the room cannot change the admin's session.
