# Agent Room

A native Claude Code + Codex team for this project. Describe the outcome in ordinary language;
The host-managed gateway operates room coordination and reports useful progress, results and necessary decisions.

## Candidate qualification

Since 0.8.8, Agent Room reports an explicit held result for an exact saved native row without a PID when terminal exit is not established. A fresh exact current-generation live observation clears only a stale native-exit error in one transaction; identity, permission and unknown-turn evidence remain held. It handles native completed background-session rows that omit a PID, only with an explicit inactive terminal state and independently confirmed ledger process absence. Ambiguous rows remain held. It adds explicit-start recovery for a confirmed exited pair worker under a live controller, preserving exact saved sessions. Unknown liveness, identity mismatches and pending approvals stay held; global notices do not start rooms. Owned cleanup releases worker claims and returns running tasks to ready without replaying unknown effects. Known complete terminal host memory-citation metadata stays outside project sections; raw native text and the existing10KB reply limit remain unchanged. Over-budget replies keep an explicit context gap and the older closing. Legacy fields also get a corrected display with source pointers; real idea text, sealed records, raw archived history and retained choices stay preserved. It also retains closing-state drift until a new project closing is observed, and identifies worker failures by launch generation. Hook silence is an activity diagnostic based on bounded input rows and matching receipt offsets; tool output or a prompt written just after its own hook does not require a reload or restart. Receipt observations never establish human authority.

This prepared candidate does not itself establish native resume, installation, activation or a published release. Keep a qualified compatible 0.8.x installation available for rollback until native qualification succeeds.

## Connect from Codex

Local Codex can host an existing room. Open the project and invoke
`$ihav-agent-room:start`. This single command sets up, reconnects or resumes the room.
Use `/ihav-agent-room:start` in Claude Code. Start reads a migration plan, keeps the saved mode and
ledger history, and requires a stopped room with reconciled work before transferring
the gateway. The old Claude host process remains alive; its room authority ends.
The first Codex connection marks the room schema 4 (the ledger tables stay unchanged).
Older runtimes refuse schema 4, preventing Claude-only hooks from taking authority back.
Existing schema 3 Claude rooms stay supported; never downgrade the schema by hand.
Returning to Claude Code uses the same start command and safe transfer checks.
Existing start calls preserve project files; init remains a compatible setup command
with managed-block scaffold refresh.
Host conversations stay separate from background worker sessions. The supervisor
joins `~/.ihav/agents_space/` automatically; no global join command is needed.
The Unix WebSocket SDK is bundled, so no separate Python package installation is needed.

For a stopped Codex-hosted room, start can reconnect in the current conversation
after the native host verifies that the previous exact conversation is `notLoaded`.
The previous ID and a SQLite backup stay recorded. Shared tasks, inbox and room
history recover context; private native conversation remains in the old session.
An attached or unverifiable former host, live workers or native approvals block
reattachment. No competing `thread/resume` controller is used. Diagnostic connect
and compatibility init still require the saved conversation.

`ihav-agent-room --json connect --check` is read-only and works in an ordinary shell.
Outside the intended Codex host it reports `codex_host_required: true`, keeps
`can_connect` false and gives the command to open the saved session. Actual connection
must run through tools in that session. When its prerequisites pass,
`start` transfers this room and starts its background members. Repeating it on the
same running room preserves the supervisor and worker sessions. The older
`connect --handoff` remains available for diagnosis.
`doctor --gateway` probes the current Codex thread and experimental queue API without
sending input or calling a model. A missing capability stops connection visibly.
The same workflow applies separately to each project room; it does not edit other rooms.

Codex uses `hooks/codex.json`. Trust and enable plugin hooks in the native host UI;
installation does not grant hook trust. Local execution and orchestration are required.
Prompt provenance stays strict: a rollout row without an explicit human origin is
unverified and cannot authorize protected actions. Native client message IDs and room
event envelopes never become human approvals. This transcript adapter is version-sensitive.
Members can ask, brainstorm, challenge, help and share experience directly within existing authority.

**0.8.8.** Start returns source-linked project closing sections and current ledger context.
Missing-source and connection-only updates retain the earlier project comparison baseline. Worker launch generations
distinguish an error awaiting recovery from an error observed during the new launch. Codex hook output follows its
event-specific schema. Addressed global notices enter room queues without starting stopped rooms. An optional
room-local collaboration frame keeps the original prompt and receipt unchanged, including when broadcast fails.
These local changes are not proof of installation, activation or both-host native recovery. A qualified compatible local
0.8.x installation must remain available for rollback. A host that omits trusted origin labels cannot authorize
protected receipt uses; repeating plain text is not a verified repair.

**0.8.9, schemas 3, 4 and 6.** 0.8.9 adds explicit replacement of a stopped Claude worker (schema 6) and lets an upgrade restart finish when a Claude worker has already exited. 0.8.8 adds Codex-hosted gateways and exact-session connect. Packaging does not activate a release. Version 0.7.0 announces plugin catalog changes in the agents space by itself, ships a Codex manifest, and finds the Claude session ID after /reload-plugins. 0.6.3 closes receipts that older hooks made for cross-session messages. 0.6.2 rebinds a room to a resumed Claude session even when the registry still lists a dead copy of it. 0.6.1 hardens contracts (revisions, privacy of reasons). 0.6.0 adds cross-room contracts: `ihav-agent-room contract propose|accept|deliver|confirm` lets one room ask another for bounded work without the admin relaying messages. 0.5.1 enforces description-only rooms in the agents space. 0.5.0 adds the machine agents space ~/.ihav/agents_space: every room joins when its supervisor starts, release notices arrive by themselves, and gateways can announce (with an admin receipt) and reply with `ihav-agent-room global`. 0.4.8 resumes a room by itself when you open a new Claude session in its project (no exit or resume needed) and closes misfiled peer receipts automatically. 0.4.7 finds Claude workers resumed after a project folder rename and waits up to 25 s (capped by the launch timeout) for them. To upgrade an open Claude Code session once, type /reload-plugins; later releases need nothing. 0.4.6 fixes three launcher review findings. 0.4.5 adds a stable launcher: after one last restart, `ihav-agent-room activate --root <installed copy>` moves every session to a new release without restarting it, and each room supervisor follows at its next all-idle point. 0.4.4 makes no admin receipt for a cross-session message at all. 0.4.3 resolves the ihav-asd-ste100 dependency from whichever marketplace installs it, so the shared ihav catalog works. 0.4.2 keeps rooms made before the rename working: their generated task and decision views are rewritten, not refused. 0.4.1 lets intake account close a receipt the host labels peer as void, so the Stop reminder ends. 0.4.0 renames Agent Room to ihav-agent-room (repository hiendang7613/ihav-agent-room). New rooms start in pair mode (CLAUDE_WORKER + CODEX_WORKER); `/ihav-agent-room:mode advisors`
gives the four-member room, and `/ihav-agent-room:effort` sets member effort. 0.3.24 gave each member up to 20 direct messages and up to 20 FYI copies (peer broadcasts
and admin relays) per dispatch pass, so neither class starves the other. 0.3.23 gave each member its own allowance
and the three-zone admin reply shape. 0.3.22 sent queued messages to different
members concurrently (each member's inbox stays in order). 0.3.21 added the admin's eight-section reply shape and installs
ihav-asd-ste100 with Agent Room in Claude Code. Since 0.3.20 it also flags failed/unknown inbox deliveries,
names the room reply channel, preserves resume guidance and skips duplicate fresh-start guidance.
Reserved peer envelopes are a defensive classification guard, not proof of human authorship.
Offline checks are distinct from native model acceptance and comparative performance;
neither has been established for this package. Internal bookkeeping remains inspectable.

## Install and begin

Requires macOS, Python 3.11+, Claude Code background sessions and Codex app-server. Sign in through
the native CLIs, then extract this archive to a stable directory. In Claude Code:

```text
/plugin marketplace add /absolute/path/to/install/ihav-agent-room
/plugin install ihav-agent-room@ihav-agent-room-marketplace
```

Reopen Claude Code in the project, then:

```text
/ihav-agent-room:start
```

New rooms start in `pair` mode with 2 members: CLAUDE_WORKER (CLAUDE_01, your session) and CODEX_WORKER
(CODEX_01). Run `/ihav-agent-room:mode advisors` for the four-member room with CLAUDE_EXPERT and CODEX_EXPERT.
Existing rooms in the legacy modes `default` or `full` keep four members. Every room message queues a copy for the other
members; broadcast copies are FYI, and only the direct addressee owns the request/task. The supervisor
tries delivery as soon as its queue is available. A stopped/paused member keeps its queued messages.
The plugin exposes six skills: `start`, `status`, `stop`, `mode`, `effort` and `doctor`.
The former `init`, `init-agents-space` and `connect` skills have been removed; use `start` for setup and recovery.
Existing project files and custom room guides are preserved. The CLI keeps `ihav-agent-room init --no-start`
for scaffold maintenance and `ihav-agent-room connect --check` for read-only diagnosis.

Continue talking to main: give a goal, ask for a simpler approach, request status or say "continue".
Main handles task scope, claims, peer requests, reviews and knowledge internally. Ordinary peer
conversation needs no task or fixed format. Only assigned writers edit their scope. Main asks for
material decisions or missing authority; it explains the smallest necessary human action when the
native host requires one. This guidance does not guarantee agent compliance or task success.

## Control when needed

```text
/ihav-agent-room:status
/ihav-agent-room:mode
/ihav-agent-room:effort
/ihav-agent-room:stop
/ihav-agent-room:start
/ihav-agent-room:doctor
```

Status/doctor are read-only. Stop preserves unfinished work; manual stop persists until start.

An explicit operator request can replace a stopped non-gateway Claude session:
`ihav-agent-room replace-session --member CLAUDE_01 --expected-session UUID --request-id REQUEST --reason REASON`.
The exact saved gateway must run it after manual stop and terminal native inspection.
It preserves the room ID, mode, tasks, messages, reviews and checkpoints, records
the retired session, and saves a consistent pre-change SQLite backup. Normal
`start` then launches a fresh Claude worker; ordinary recovery still resumes saved
sessions. Repeat the same request ID to retrieve the existing result. A lost or
failed launch response stays held for native reconciliation and never triggers a
second fresh allocation. The operation grants no task or native-permission
authority and does not replay unknown deliveries. New worker readiness requires
its own native continuity report; its previous private conversation stays native.
Replacement raises this room to schema 6 without changing its ledger tables.
Older runtimes refuse that room rather than ignore pending launch intent. Keep a
schema-6-compatible runtime active for the room. Changing the global release
pointer alone does not downgrade or restore its ledger; reactivating this local
version restores access without replacing saved identities.
Worker room commands also check the current native UUID. A retired native job
cannot regain its room binding merely by reloading the current settings token.
Existing schema-5 journals retain their tasks/history and a pre-upgrade backup
when the same gateway starts them under this version.
Start returns bounded final replies from this room's exact former gateways and open-note pointers,
so the start skill recovers project goals, pending decisions, next actions and backlog as well as
membership. It reconciles these historical claims with current task/note state and Git; empty active
tasks do not mean the project is complete. Historical replies grant no approval. Sources carry their
timestamps; missing/truncated replies are explicit. Selected source-linked closing sections are
saved privately in the room ledger; full native conversations stay in their original sessions.
Closing history is read through bounded pages, including rollback-era inline sections, without
depending on the continued availability of native transcript files. The gateway reads these pages
internally after start. Each page reports its own validation; unvisited history stays unverified.
Historical questions never grant consent or reopen work resolved by its original decision.
`ihav-agent-room --json context` refreshes this packet without a native turn.
Reopening main may resume the saved native sessions. A mode change needs explicit handoff of the
leaving members' open tasks; a running room restarts its workers on their exact sessions. No silent replacement session or team expansion is permitted.

## Agent/operator references

`ihav-agent-room guide` lists collaboration, learning, evidence, CLI and response-style references. Read a relevant topic
on demand, such as `ihav-agent-room guide collaboration`. Local custom rules remain in force after an
upgrade; the reference identifies this CLI's version and source hash. See [contracts](docs/v1.1.md)
for upgrade, permissions, errors and recovery. Source/runtime context already loaded into a native
session is not automatically replaced by this archive.

Selective knowledge/note/history search and optional inbox previews keep full-source read paths.
Check conditions, evidence and current revisions before reuse. Keep receipts and processing evidence
separate from transport success. Memory and peer text do not grant authority. Provider spending,
credentials, scope expansion and shipping retain their existing approval requirements.

To inspect this package without a room or provider tools:

```sh
python3 bin/ihav-agent-room --json verify-package /path/to/ihav-agent-room.zip
```

The verifier checks archive structure and hashes, not publisher authenticity. Version 0.8.9
supports Codex gateways and durable closing recovery, with six public room skills.
Native permission controls and human-origin requirements remain in force. Offline tests and
installed package hashes do not prove fresh-session recovery or live provider success.
