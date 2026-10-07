# Changelog

## 0.8.9 — 2026-10-08

First published release after 0.7.0. Versions 0.8.0 to 0.8.8 were local candidates and were never
published; their changes are included here. Details: [docs/v1.1.md](docs/v1.1.md).

### Added

- Codex-hosted rooms. `/ihav-agent-room:start` in Claude Code and `$ihav-agent-room:start` in Codex
  are the single entry command. The room records its gateway (CLAUDE_01 or CODEX_01).
  `connect --check` and `connect --handoff` move a stopped room between hosts. The websockets
  wheel is bundled, so start installs nothing.
- `ihav-agent-room replace-session` retires one stopped Claude worker; the next start launches a
  fresh session and keeps the room, tasks and history.
- Explicit start recovers an exited pair worker while its controller is live, and reattaches a
  stopped Codex-hosted room to a new Codex conversation when the old one is verified not loaded.
- Start returns the project closing sections (goals, decisions, next work) from the room's earlier
  gateway replies, so an empty task list does not erase the plan.
- Optional collaboration frame (`agents_space/prompt_frame.json`) with a fixed prefix and postfix.
- Update plans for selected ihav plugins, with a durable attempt journal. Plans only; nothing is
  installed automatically, and an uncertain attempt is never replayed.

### Changed

- An upgrade restart no longer waits for a Claude worker whose exit is confirmed; shutdown does
  not stop that session again.
- Missing Claude liveness is reported as "needs reconciliation", not as an exit with a
  stop/start hint.
- Host memory-citation blocks stay out of project closing sections; raw text is unchanged.
- Global notices enter room queues without starting stopped rooms.
- Codex hook output follows each event's schema.

### Upgrade notes

- A room that uses `replace-session` moves to schema 6, and runtimes older than 0.8.9 refuse it.
  Codex-hosted rooms use schema 4, which 0.7.0 refuses. Moving the release pointer back does
  not downgrade a ledger, so keep a compatible runtime active for such rooms.
- Prompt provenance stays fail-closed. On a host that does not label prompt origin, uses that
  need a human-confirmed admin prompt are refused.
