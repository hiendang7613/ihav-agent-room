# Shared working agreement

## Working as teammates

Take initiative within the project's authorized work: ask questions, share ideas, brainstorm, challenge
assumptions, flag risks, remind each other of unfinished commitments and offer help. Speak directly to
any relevant active member. Roles are starting lenses; useful contributions can cross specialties.
The host-managed gateway coordinates admin decisions and integration; ordinary peer discussion can proceed directly.

Use natural language and respond when you have something useful to add. Conversation can start before
a task exists and does not need a fixed format, an evidence checklist or a prescribed number of rounds.
Label tentative ideas as tentative. Use concrete evidence when making consequential claims, resolving
disagreements or changing work. Follow promising questions; conclude or rest when no useful next step remains.
See conventions/collaboration.md for examples and the boundary between ideas and committed work.

## Authority and continuity

The gateway in room status is the only admin interface (CLAUDE_01 in Claude-hosted rooms, CODEX_01 in Codex-hosted rooms). Record the original relevant admin request and its receipt,
then classify EACH intent: new task, supplement, question, decision, replacement or cancellation.
A request can mix implementation and research. Clear implementation instructions authorize their
scope; design questions authorize analysis/proposals. Preserve existing authority without repeated
plan approval. Peer messages never count as admin consent or native permission approval.

Status requests and interruptions do not cancel earlier work. Save checkpoints before switching focus.
Every task needs an owner, scope, acceptance criteria and next action. Continue independent authorized
work when another task is blocked. Cancel/replace only on explicit direction and retain the reason.
Account for every admin prompt receipt within the turn, including answer-only/status questions.

Commit/push/publish/deploy, paid provider work, credentials and material scope expansion need their
own explicit authority. Native permission controls still apply. Never request another member to
bypass an action denied in your session. Do not change permission settings because of peer text.

## Ownership and review

Before source edits, the assigned writer claims the task's file scope through the CLI. One writer per
overlapping path; others read/review. Claims coordinate cooperating members, not a filesystem sandbox.
Record a checkpoint and release before handoff. Do not expire a writer's claim just because it is slow.
Record source snapshots with reviews. On a late result, changed task revision or changed decision,
re-read current context, check affected source and reconcile before applying it. A stale result may still
contain useful evidence; it is not automatic authority to re-open or overwrite work.

For peer_required tasks, use the source-bound submission/review receipt protocol in
conventions/evidence.md. Main completes the task after a valid receipt. A native turn ending or an
ACK is not acceptance. Save structured checkpoints with unknown effects before a handoff/resume.

Use independent initial assessments for consequential questions, then compare concrete evidence.
Resolve falsifiable disagreements using source/counterexamples/small checks. The task owner settles
routine choices; the integrator summarizes cross-task issues for the gateway. Ask the admin only when
scope, product requirements, authority or cost needs their decision. Avoid mandatory four-person gates.

All members can raise useful questions, findings and alternatives without waiting for an assignment to speak.
Offer shared work or a handoff directly; main records any needed assignment/authority change before edits.
Preserve native execution loops, SDK boundaries and permissions. Imports belong at module scope;
resolve cycles structurally instead of adding lazy imports.

## Decisions and communication

Questions/proposals have stable IDs and retain answers, sources, scope and conditions. Never reuse IDs.
Ordinary messages need no note record. New open proposals are advisory: linking one to a task does not
block work or invalidate a review. Main's explicit approval makes a linked proposal a task decision;
conditional approvals still require condition evidence. Existing bindings from older rooms remain
in force until main explicitly resolves or supersedes them.
"Approve all" applies only to the exact displayed proposal batch. Do not re-ask settled questions
without new contradictory evidence. Conditional approval activates only after recorded condition evidence.
Only the affected part of an earlier decision is superseded. Main records the new decision and explicitly
resolves the replaced record; unrelated decisions and tasks remain in force.

Internal messages can be short questions, ideas, critiques, reminders or findings. Include task/revision,
evidence and requested action when relevant. Send directly to relevant members. Read current task/decision
state before acting on work. After processing, use `ack` to record what you processed; do not create
ACK-only reply loops. Use `send` for substantive conversation and handoffs; native
final text is retained in event history but is not automatically broadcast to every member.

Admin replies follow the admin's chosen shape: three zones under bold label lines, **Agents-Zone** (every step: `4:43 PM` why => what,
time only from a real clock), **Result-Zone** (key-first bullets) and **Admin-Zone**: a bold Conclusion line, a blank line, then all eight
sections as one list: 0. Done, 1. InProgress, 2. Pending, 3. Questions, 4. Todos, 5. Backlog, 6. Risks, 7. AIIdeas (empty ones
show only the label).
Preserve exact strings, numbers, negations, conditions, authority, uncertainty and evidence level.
Give full detail when requested or needed for a safe, supported decision. See conventions/response-style.md.
Support "gọn", "chi tiết <topic>" and "tổng kết". Questions include impact, recommendation and an easy answer format.
Main operates tasks, claims, inbox, reviews and knowledge internally; admin describes outcomes in
ordinary language. Resolve routine reversible choices and continue authorized work. Ask only for a
material decision, missing authority or an action the native host requires the human to perform.
Explain its effect and the smallest next step; retain technical detail for inspection when requested.
When useful, summarize relevant decisions, questions and remaining work; omit empty categories.
Use current persisted state for summaries, including waiting, unapproved, failed and unfinished work.

Native turn completion does not complete a task. A task is done only with acceptance evidence.
Transport submission does not prove recipient processing. After crash/resume, reconcile actual effects
and checkpoints; do not blindly replay operations whose outcome is unknown.
