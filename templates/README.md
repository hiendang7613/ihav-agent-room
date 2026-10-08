# Agent Room

This project uses native Claude Code and Codex sessions and one shared room ledger.
At startup/resume, read this page and the [working agreement](rules/working_agreement.md),
then refresh `ihav-agent-room --json status --compact`. Status contains current tasks, decisions,
waiting work and `pending_inboxes`. If your member has an entry in `pending_inboxes.by_member`,
run `pending_inboxes.read_command` and follow `next_after` until null. If absent, skip the inbox
sweep for this snapshot; process later native messages normally. Generated
[task](tasks/active.md) and [decision](state/current_decisions.md) files are optional views.
Preserve any additional reading required by this project's own instructions.

Open guides when useful for the work at hand: [CLI operations](conventions/cli.md),
[review/checkpoints](conventions/evidence.md), [collaboration](conventions/collaboration.md),
[shared learning](conventions/learning.md), or [admin-facing response style](conventions/response-style.md).
Use subcommand `--help` for arguments.
After a plugin upgrade, `ihav-agent-room guide` lists current shipped references by topic; for example,
`ihav-agent-room guide collaboration`. Existing room guides stay preserved and may contain custom rules.
The reference reports its CLI's plugin version; project-specific instructions still apply.

Members are encouraged to ask, share ideas, challenge, remind and help each other directly.
Ordinary discussion needs no task or prescribed rounds. Use formal tasks to track committed work;
keep file ownership, admin authority and native permissions clear when acting on an idea.

## Roles

Admin describes goals in ordinary language; main operates the room tools and reports useful outcomes.
See [collaboration](conventions/collaboration.md) for the interface and peer freedom. After upgrading,
`ihav-agent-room guide collaboration` serves the current reference without replacing custom project rules.

- New rooms start in `pair` mode: CLAUDE_01 (CLAUDE_WORKER) and CODEX_01 (CODEX_WORKER). The session that starts the room is its gateway: CLAUDE_01 in Claude Code, CODEX_01 in Codex.
  `advisors` adds CLAUDE_EXPERT and CODEX_EXPERT. The legacy modes `default` and `full` keep all four.
  Only members of the current mode run; address experts only when status lists them as running.
- A logical room message queues deliveries for all other members. The direct addressee owns its
  request/task; other copies are FYI, but still wake their recipients. Paused/stopped members keep
  messages queued until delivery is available.
- Each mode requests a model and effort per member (`ihav-agent-room mode`, `ihav-agent-room effort`). Codex members
  use them on their next turn; Claude workers on launch or resume. The gateway is host-managed. These are
  requests, not proof a model used them.

Use `ihav-agent-room --json status --compact` for current mode, native IDs, errors and waiting work.
For a pending sweep, follow `next_after` through every page; `read_command` starts fresh sweeps
at `--after 0`. A pagination cursor is not a saved read position.
Only a processing ACK removes a message from this view. Failed/unknown deliveries still need
reconciliation; a pending message does not authorize replay. Use `inbox` without the flag for history.
For long or already-delivered text, `inbox --pending --compact` offers previews and full read commands;
see the [CLI guide](conventions/cli.md). Process full content before acting or ACK.
Use `ihav-agent-room --json history` for the durable room conversation and `history --kind events`
for native final responses and state events. Native private conversation histories stay native.

The SQLite database in .runtime is authoritative for tasks/decisions/messages. Generated active
task and decision Markdown are views; do not edit them. Long reviews and evidence can be ordinary
files under reviews/. Checkpoints and metadata are local/private by default, not automatically published.

Only the admin's main session initializes, starts or changes the mode of the room. A mode change needs
handoff of the leaving members' open tasks and reviews first. Native
subagents require existing task/host authorization and remain their parent's responsibility.
