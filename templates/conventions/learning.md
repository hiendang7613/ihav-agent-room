# Learning together

Use the room's shared knowledge when experience can help the next decision. Any member can
remember a useful discovery, failed approach, counterexample or tentative preference during
ordinary work. Choose what deserves keeping; routine steps and transcripts usually do not.
There is no compulsory retrospective, minimum number of lessons, or periodic learning turn.
Prefer keeping the reasoning, failed approach or condition a future teammate would otherwise have
to rediscover; link facts already explained in code/docs. Revise a related record instead of adding
a duplicate. Tentative ideas remain welcome with their uncertainty and scope. Members handle the
recording internally; admin need not manage a knowledge taxonomy or approve every ordinary lesson.

## Find before remembering

```sh
ihav-agent-room --json knowledge search 'queue retry'
ihav-agent-room --json knowledge show K-ID
```

Search uses all whitespace-separated terms as literal, case-insensitive substrings across
title, body, tags, evidence, applicability and limits. Use a few concrete terms, not a whole
prompt. Broaden or change terms if relevant experience is missing. This is local text search,
not semantic retrieval. Long previews can start near a matching term, marked `[excerpt]` when
the beginning is omitted. A window may omit other matches or qualifications; follow the read
command for full context. Follow `next_after` with `--after` for further pages. No match means
no matching recorded knowledge, not that the idea has never been tried.

If useful terms are still unclear, optionally browse a small page of current lessons or open ideas:

```sh
ihav-agent-room --json knowledge search --limit 4
ihav-agent-room --json note search --limit 4
```

Results use insertion order; keep `next_after` when paging. Read the full sources and current limits
of a relevant record, then choose terms or ask a peer. Browsing is a discovery option, not a required
startup step or a reason to load every record.

Read a relevant record and its evidence before relying on it. Check `applies_when`, `limits`
and current source. The record's `basis` describes where the claim came from; no label proves
that it is correct. An experiment is evidence for its conditions, not a universal rule.

## Learn from ordinary conversation

When an exchange was never summarized into a note or lesson, find it in this room's history:

```sh
ihav-agent-room --json history --query 'queue retry' --limit 8
ihav-agent-room --json history --kind prompts --query 'concise' --limit 8
```

Matches keep full text, identity, origin/status and cursor. Read surrounding unfiltered history
and current records for corrections or changed scope; a keyword match is not a settled decision.
Use the original message/receipt ID as evidence if the exchange is worth remembering. Check
prompt origin and current instructions before inferring admin style. No match means no matching
text in this room, not that nobody tried the idea. Choose other terms or ask a peer when useful.
This reads the project ledger; it does not scan personal native transcripts or other projects.

## Save what changed your understanding

Write a JSON file, then use `ihav-agent-room knowledge add --input FILE.json`:

```json
{
  "title": "Check the receiver before retrying a message",
  "body": "In the bounded restart experiment, dispatch succeeded while the final native completion was missing. Reconcile the receiver before resending.",
  "basis": "observed",
  "evidence": ["docs/restart-experiment.json: exact run and message IDs"],
  "tags": ["restart", "messaging"],
  "applies_when": "A native dispatch has an unknown outcome",
  "limits": "One local experiment; does not establish transport guarantees"
}
```

Use real evidence, not the example path. `title`, `body` and at least one `evidence` entry are
required. Tags, applicability and limits are free text; there is no fixed domain taxonomy.
Keep titles and lessons concise enough to help the next decision. Link bulky evidence in project
artifacts instead of copying transcripts. These are writing guidelines, not per-field length or
tag/source-count quotas. Search and shared links use bounded previews with full-record commands;
`show` and `history` retain the complete text, including detail beyond a preview.
Default `basis=inferred` keeps a proposed explanation tentative. `observed` records a member's
reported observation. The CLI checks structure, not whether the cited experiment really ran.

The committed record is immediately available to every member in this project. Share it with
a relevant peer when it helps their work:

```sh
ihav-agent-room send --to CLAUDE_01 --knowledge K-ID --body 'This may explain the delay. Do you see a counterexample?'
```

The optional link stores the lesson ID and version current at queue time. Inbox reads compare
that `queued_version` with `current_version` and show `changed`, current state/basis/title, and
commands for the full record and history. Native dispatch includes the same small reference;
the attempt retains what was observed then. Later edits appear in subsequent inbox reads.
An unchanged version can already be `retired`; check both state and version. Read the current
record, evidence and limits before reuse. Metadata alone does not establish that a lesson is
correct, that the sender read it, or that a recipient processed it.

Body/evidence text is not copied into the link. Free-form messages can still mention IDs without
`--knowledge`; these are ordinary text and are not inferred into links. A stable message `--id`
keeps its original reference on repeated sends, even after the lesson changes. Use a new message
for a new follow-up. Saving/revising knowledge does not create a room message. When a lesson merits
immediate discussion, use `send --knowledge K-ID`; that message is queued for every other member.
Peers can challenge a lesson directly; search and share when useful instead of loading all memory each turn.

## Understand admin style without inventing instructions

Any member may infer a tentative preference, tagged for example `admin-style`, with the actual
observations and uncertainty in the record. Main can record a preference explicitly stated by
admin using `basis=admin` and `source=P-ID`, the original admin receipt. Keep the original scope,
conditions and exceptions. This label means main supplied a real receipt, not that software
verified the interpretation. Never turn repeated agent guesses into an admin statement.

Current requests and original decisions take precedence over remembered preferences. Memory
cannot authorize edits, provider spending, permission changes or shipping. Material uncertainty
about intent goes to main; routine reversible choices can use judgment within existing authority.

## Refine, correct and try again

When evidence improves, update the existing record with a small JSON patch:

```sh
ihav-agent-room knowledge update K-ID --expected-version 1 --input correction.json
ihav-agent-room knowledge history K-ID
```

All members may revise ordinary records; main maintains admin-stated ones. Concurrent edits
require rereading and reconciling the current version. The author, latest editor and previous
revisions remain visible. Add disagreements and limits while evidence is unresolved.

When a surprise, recurring friction or disagreement suggests a better approach, search a few
project terms and use `note search [WORDS] --state all` for earlier conclusions or failed attempts.
Use what fits to propose an alternative or invite a peer's counterexample. Read the full current
record first; excerpts may omit context. Search only current revisions; `note history ID` shows
how a discussion changed. Matching text alone does not establish applicability.

When the finding also changes your open question or proposal, use `note resolve` to record the
new conclusion or reopen the discussion. Keep reusable knowledge and the particular discussion
distinct; refer to their IDs when helpful. Ordinary author/main follow-ups need no admin receipt
and notify relevant peers. See [collaboration guide](collaboration.md) and `note history`.

Use `{"state":"retired","limits":"Why this no longer applies"}` to retire a lesson. Default
search excludes it immediately. `show`, `history` and `search --include-retired` still expose
it explicitly; retirement is not deletion of sensitive data. Do not store secrets or unrelated
personal information. Main can retire an inaccurate admin-stated record and replace it with
an inference; peers can raise counterevidence directly.

For self-improvement, let an actual friction point lead to a small authorized experiment.
Compare against the previous behavior, retain useful evidence, and revise or retire the lesson
if the result disagrees. Use the existing task and review controls when code changes require
them. Stop when there is no worthwhile next experiment within scope. This is an opportunity
for agents to improve their practice, not a scheduler, mandatory stage or unbounded model loop.

## Promote durable experience to the right artifact

When a finding could improve future work, first check code, tests, project docs, native skills
and room knowledge/history/notes. Invite a peer's counterexample when it could change the conclusion.

Choose by audience: keep tentative or one-off findings as room knowledge; update a shared convention
when both harnesses need the practice; use a native skill for host-specific behavior. Add an instruction
only for a durable, reusable practice not already clear in current sources. Do not create a skill just
to store a lesson or narrow requested work to make a proposal fit.

An optional advisory proposal states its audience, use, evidence and limits. Keep disagreement and
uncertainty visible. A proposal grants no edit authority: main assigns file scope and the writer claims
it. There is no recurrence threshold or required promotion step.
