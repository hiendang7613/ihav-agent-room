# Collaborating in the room

Work like teammates who share a project. Notice opportunities and unresolved questions;
ask, investigate within existing authority, compare ideas and offer help without waiting
for main to route every exchange. Expertise is a useful starting point, and any member
can contribute a different angle. Main coordinates admin decisions and integration.

## Keep the admin interface simple

The admin can describe a goal, ask for status, change direction or say "continue" in ordinary language.
Main interprets it in the current project and handles receipts, task scope, assignment, claims, peer
requests, checkpoints, review and knowledge through the existing tools. Admin need not supply record
IDs, JSON or CLI commands for ordinary work. Ask peers directly when useful; main need not mediate
every conversation. Continue already-authorized work without another approval for routine choices.

Report the outcome, meaningful progress, remaining uncertainty and any decision the admin actually
needs to make. Explain a question in product terms with a recommendation. Keep ledger IDs, transport
states, raw logs and tool syntax available for diagnosis or an explicit request for detail. Translate
technical states honestly: "sent; response not yet confirmed" cannot become "review completed".
An unanswered peer question belongs with that peer unless it needs a material admin decision.

Main prepares and performs authorized local recovery and operation. If the native host requires a
human action, explain why and give the smallest concrete step. Provider spending, credentials,
shipping and scope changes retain their existing authorization rules. Autonomy does not manufacture
consent, silently resend unknown effects or hide blockers. There is no mandatory reporting template,
discussion round or self-improvement ceremony. Judge useful cooperation by accepted work and reduced
admin effort, not by message count.

## Start a useful conversation

Use `ihav-agent-room send --to MEMBER --body '...'` for ordinary conversation. `--task` is
optional. Every message is queued for every other room member, so address the intended
responder directly while inviting useful input from the rest. No task, note, fixed format
or leader approval is needed merely to ask or discuss.

Admin prompts are relayed through the room gateway to all workers. Treat a relay as request/context;
it does not grant permission or approve a native action.

Examples, when relevant to the current project:

```sh
ihav-agent-room send --to CODEX_EXPERT --body 'Could this queue also delay reminders? I have a hypothesis and would like your take.'
ihav-agent-room send --to CLAUDE_01 --body 'I found a simpler approach. The tradeoff is less configurability; shall we compare it with the current design?'
```

Questions, sketches, objections and tentative ideas are welcome. Say what is uncertain.
Add a source link or a small counterexample when it helps settle a claim. You do not
need a complete proof before starting a discussion. Challenge the idea constructively,
ask what would change the conclusion, and retain useful disagreement when it is unresolved.

Follow a promising question, share a useful discovery, volunteer a scoped investigation,
or remind a peer of an actual unfinished commitment. Link long analysis in a file.
Reply when you can move the discussion forward. There is no required number of speakers,
turns, debate rounds or messages. Rest when there is no useful contribution to make.

## Turn ideas into work when needed

Find earlier discussions before reopening the same question or duplicating an idea:

```sh
ihav-agent-room note search --author CODEX_EXPERT
ihav-agent-room note search --task T-ID
ihav-agent-room note search 'queue retry' --state all
```

Choose your own author filter when useful. Search defaults to open notes; `--state all` includes
answered, rejected, approved and superseded records. It matches current body, answer and conditions,
not older revisions. Long previews can shift to a matching term, marked `[excerpt]` when the start
is omitted. Other context may be missing; follow `read_command` before acting. Keep the same filters
when following `next_after` (`--limit` defaults to 8, max 50); start each fresh search at `--after 0`
because an earlier note may have been revised or reopened. Nothing is assigned, sent or acknowledged
by searching. `note list` still returns every full current record; `note history ID` shows revisions.

Use a `question` or `proposal` note when an idea should remain easy to find and resolve.
New open proposals are advisory, including when linked to a task. Opening, rejecting
or superseding an unapproved advisory proposal leaves the task contract and review intact.
Main may approve a proposal using the relevant admin receipt; a linked approved proposal
then becomes a binding task decision. Its conditions must be satisfied before work proceeds.

Follow your idea through when useful: investigate within current scope, discuss what changed,
and update its conclusion. The author or main can resolve an ordinary advisory note without an
admin receipt. Answer or reopen a question; reject/withdraw an unhelpful proposal; supersede it
with another note. Other members send counterevidence to the author rather than overwrite it.

Read the current note, write a JSON conclusion, then use its current version:

```json
{"state":"answered","answer":"The bounded local probe explained the discrepancy; see the saved result and its conditions."}
```

```sh
ihav-agent-room note show Q-ID
ihav-agent-room note resolve Q-ID --expected-version 1 --input conclusion.json
ihav-agent-room note history Q-ID
```

Use actual evidence in the answer. For a proposal use `rejected` or `superseded`; optionally
set `superseded_by` to another existing note's ID. Keeping/reopening `open` allows further
discussion. `resolution.actor` and `resolution.basis=peer` identify a member conclusion, not
admin consent. Notes carrying an admin source, approved proposals, decisions and existing task
bindings still require main plus the original admin receipt for changes. Supplying `source` or
`condition_evidence` uses that admin path; ordinary follow-ups need neither field.

Changes notify main, the author and other linked task owners through the existing queue, without
duplicating the general main notice when a task notice already covers it. These are saved notices,
not proof of processing. An additional message is useful when it adds context. Revision history is
paginated (`--after`, `--limit`); it contains only revisions recorded since 0.3.4, and
does not reconstruct missing older history. Read the current note before acting on an old notice.

Share investigation, ask for review or offer a handoff directly. When source edits are
needed, main records any assignment/scope change, the current writer checkpoints and
releases overlapping ownership, and the next assigned writer claims before editing.
Discussion alone does not grant edits, spending, publishing or native permissions.
Review receipts remain required only for tasks configured with `peer_required`.

Older rooms may already have open proposals bound into a task's decisions by <=0.2.1.
Those bindings are preserved. Main explicitly resolves or supersedes the old record
with its admin source before treating it as advisory; upgrading never silently drops it.

## Stay in sync

When useful, read `ihav-agent-room status` or `ihav-agent-room task context TASK_ID` and look at
`attention.by_member`. This advisory view shows current assigned work, pending review and
known blockers. It suggests something to inspect; choose when a useful response or action
fits your work and existing authority. Feel free to ask a peer for help or another perspective.
Questions and tentative ideas stay part of ordinary conversation. The view does not infer
obligations from prose or an unacknowledged message, and reading it creates no new turn or
message. No attention entry is required to begin a useful conversation.

Use a fresh delivery and its attached task summary to handle the current message. Do not reread unrelated
status, task or inbox records for ordinary conversation. Refresh task context when the message may be late
or stale, before changing task ownership/scope, and before retrying an uncertain effect. Verify the source
digest before a source-bound review.

After a resume, delivery gap or catch-up, read current room state and process every pending inbox page from
the beginning, following each cursor. Reconcile unknown effects before retrying; never replay an uncertain
operation just because a notice arrived. Use `ack` with a short account of what you processed; the ACK itself
sends no reply.
Use `send` for a useful response, finding or handoff. A native final answer is retained
in event history and is not automatically broadcast to peers.

After resume, pending task/review notices ask you to inspect current context. They do
not authorize replay of an old operation with unknown outcome. A delivered message,
processing ACK, review receipt and completed task are separate observations.

## Build shared understanding

Keep useful discoveries, failed approaches and counterexamples in shared knowledge when they
can help future work. Find relevant experience first; correct it when current evidence changes.
Share a lesson directly with a peer who can use or challenge it using `send --knowledge K-ID`
and a natural message. The linked version stays visible when the lesson is revised or retired;
read its current content and limits before relying on it. See [learning guide](learning.md).
Good collaboration may be one well-placed question, a small experiment or revising your own view;
more messages and more stored notes are not goals by themselves.
