# Agent Room

A native Claude Code + Codex team for this project. Describe the outcome in ordinary language;
CLAUDE_01 operates room coordination and reports useful progress, results and necessary decisions.
Members can ask, brainstorm, challenge, help and share experience directly within existing authority.

**Version 0.4.0, schema 3.** 0.4.0 renames Agent Room to ihav-agent-room (repository hiendang7613/ihav-agent-room). New rooms start in pair mode (CLAUDE_WORKER + CODEX_WORKER); `/ihav-agent-room:mode advisors`
gives the four-member room, and `/ihav-agent-room:effort` sets member effort. 0.3.24 gave each member up to 20 direct messages and up to 20 FYI copies (peer broadcasts
and admin relays) per dispatch pass, so neither class starves the other. 0.3.23 gave each member its own allowance
and the three-zone admin reply shape. 0.3.22 sent queued messages to different
members concurrently (each member's inbox stays in order). 0.3.21 added the admin's eight-section reply shape and installs
i-have-asd-ste100 with Agent Room in Claude Code. Since 0.3.20 it also flags failed/unknown inbox deliveries,
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
/ihav-agent-room:init
```

New rooms start in `pair` mode with 2 members: CLAUDE_WORKER (CLAUDE_01, your session) and CODEX_WORKER
(CODEX_01). Run `/ihav-agent-room:mode advisors` for the four-member room with CLAUDE_EXPERT and CODEX_EXPERT.
Existing rooms in the legacy modes `default` or `full` keep four members. Every room message queues a copy for the other
members; broadcast copies are FYI, and only the direct addressee owns the request/task. The supervisor
tries delivery as soon as its queue is available. A stopped/paused member keeps its queued messages.
The older name `/init-agents-space` still works. Existing
project files and custom room guides are preserved.

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

The verifier checks archive structure and hashes, not publisher authenticity. Default CLI contracts,
schema, native permissions and model loops are unchanged in 0.4.0 apart from the rename.
