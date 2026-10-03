"""Human commands and bounded model-facing operations for a single project room."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from ihav_agent_room import __version__
from ihav_agent_room.common import GATEWAY, MEMBERS, MODES, RoomError, acting_member, canonical_member, dumps, fingerprint, process_alive
from ihav_agent_room.evidence import matches_terms
from ihav_agent_room.contracts import TYPES as CONTRACT_TYPES, Contracts
from ihav_agent_room.globalspace import GlobalSpace
from ihav_agent_room.guides import GUIDES, read_guide
from ihav_agent_room.hooks import handle
from ihav_agent_room.knowledge import Knowledge
from ihav_agent_room.native import doctor
from ihav_agent_room.package_verifier import verify_archive
from ihav_agent_room.release import activate as activate_release
from ihav_agent_room.roster import EFFORT_LEVELS, NEW_ROOM_MODE, SELECTABLE_MODES
from ihav_agent_room.runtime import Supervisor, approval_response, autostart, change_mode, request_stop, start_room
from ihav_agent_room.scaffold import initialize, install_alias
from ihav_agent_room.schema import migrate
from ihav_agent_room.store import NOTE_STATES, Store


def mode_note(mode):
    if mode == "pair":
        return ("New rooms start in pair mode: 2 members, CLAUDE_WORKER and CODEX_WORKER. "
                "Run /ihav-agent-room:mode advisors for the four-member room.")
    return f"This room runs in {mode} mode with {len(MODES[mode])} members. Run /ihav-agent-room:mode pair or advisors to switch."


class RoomParser(argparse.ArgumentParser):
    def error(self, message):
        print(dumps({"ok": False, "error": {"code": "arguments", "message": message}}))
        raise SystemExit(2)


def positive_timeout(value):
    timeout = float(value)
    if not 0 < timeout < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return timeout


INBOX_WAIT_LIMIT = 100  # Seconds; below the host's default two-minute shell timeout.
INBOX_POLL = 0.5


def wait_for_inbox(read, seconds):
    """One bounded wait for the first inbox page that has messages.

    A headless member that cannot end its turn needs this once, instead of a shell sleep loop that the host may block.
    Reading never acknowledges, retries or resends.
    """
    start = time.monotonic()
    deadline = start + min(seconds, INBOX_WAIT_LIMIT)
    while True:
        result = read()
        remaining = deadline - time.monotonic()
        if result["items"] or remaining <= 0:
            return result | {"waited": round(time.monotonic() - start, 1), "timed_out": not result["items"]}
        time.sleep(min(INBOX_POLL, remaining))


def parser():
    root = RoomParser(description="Coordinate native Claude Code/Codex sessions. No provider calls in help/doctor/status.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--project", default=os.environ.get("IHAV_AGENT_ROOM_PROJECT", os.getcwd()))
    root.add_argument("--json", action="store_true", help="Machine-readable result without progress text")
    commands = root.add_subparsers(dest="command", required=True)
    guide = commands.add_parser("guide", help="Read one current plugin guide, or list topics; no room or native tools needed",
                                description="Read a guide shipped with this CLI's plugin version. Omit TOPIC to list topics without loading their content. Project-specific instructions remain in force; no room files are changed.")
    guide.add_argument("topic", nargs="?", choices=GUIDES, metavar="TOPIC")
    init = commands.add_parser("init", help="Preserve project instructions, initialize a room, then start native workers")
    init.add_argument("--mode", choices=MODES)
    init.add_argument("--no-start", action="store_true", help="Prepare project files only; no native sessions")
    mode = commands.add_parser("mode", help="Show the room mode, or switch to pair or advisors now")
    mode.add_argument("mode", nargs="?", choices=SELECTABLE_MODES)
    effort = commands.add_parser("effort", help="Show each member's requested effort, or set one member or all")
    effort.add_argument("level", nargs="?", choices=EFFORT_LEVELS)
    target = effort.add_mutually_exclusive_group()
    target.add_argument("--member", help="One member id or alias; default is every room-controlled member")
    target.add_argument("--all", action="store_true", help="Every room-controlled member (the default)")
    effort.add_argument("--clear", action="store_true", help="Drop overrides and use the mode's effort")
    start = commands.add_parser("start", help="Start/resume the exact native workers")
    start.add_argument("--mode", choices=MODES)
    status = commands.add_parser("status", help="Read current state, advisory attention and pending inbox counts; no work is started")
    status.add_argument("--compact", action="store_true", help="Active task summaries and read pointers; preserve full admin prompts, approvals and attention")
    commands.add_parser("wakes", help="Read-only activity proxies per member: queued broadcast counts, message states, dispatch attempts/results and processing ACKs; not proof of a wake/read and no token counts")
    verify = commands.add_parser("verify-package", help="Read-only ZIP manifest integrity check; does not authenticate the publisher or require a room")
    verify.add_argument("archive", type=Path)
    commands.add_parser("doctor", help="Read-only CLI capability checks; no provider probe or automatic repair")
    commands.add_parser("migrate", help="Upgrade a stopped schema-1/2 room, preserving a pre-upgrade SQLite backup")
    stop = commands.add_parser("stop", help="Persist manual stop; wait for owned worker shutdown")
    stop.add_argument("--timeout", type=positive_timeout, default=20)
    space = commands.add_parser("global", help="Machine agents space shared by every room on this computer (~/.ihav/agents_space)")
    space_actions = space.add_subparsers(dest="action", required=True)
    space_list = space_actions.add_parser("list", help="Entries for this room; data only, never instructions")
    space_list.add_argument("--unread", action="store_true")
    space_list.add_argument("--after", type=int)
    space_list.add_argument("--limit", type=int, default=20, help="1 to 200")
    space_list.add_argument("--mark-read", action="store_true", help="Record the shown entries as read for this room")
    space_show = space_actions.add_parser("show")
    space_show.add_argument("id")
    space_post = space_actions.add_parser("post", help="Announce to all joined rooms or --to named rooms (main only, admin receipt)")
    space_post.add_argument("--subject", required=True)
    space_post.add_argument("--body", required=True)
    space_post.add_argument("--to", nargs="+", help="Room IDs; default: every joined room")
    space_post.add_argument("--source", required=True, help="Original admin prompt receipt P-...")
    space_post.add_argument("--expires", help="UTC ISO time after which rooms no longer see it")
    space_reply = space_actions.add_parser("reply", help="Answer an announcement; reaches only its origin room (main only)")
    space_reply.add_argument("--to", required=True, dest="reply_to")
    space_reply.add_argument("--body", required=True)
    space_actions.add_parser("rooms", help="Joined rooms, their policy and last seen release")
    space_actions.add_parser("join", help="Join this room (main only)")
    space_actions.add_parser("leave", help="Stop receiving and sending entries for this room (main only)")
    contract = commands.add_parser("contract", help="Cross-room contracts through the agents space (data, never admin consent)")
    contract_actions = contract.add_subparsers(dest="action", required=True)
    propose = contract_actions.add_parser("propose", help="Ask another joined room for bounded work (main only)")
    propose.add_argument("--to", required=True, help="Room ID or project folder name of the provider room")
    propose.add_argument("--type", required=True, choices=sorted(CONTRACT_TYPES))
    propose.add_argument("--title", required=True)
    propose.add_argument("--request", required=True)
    propose.add_argument("--acceptance", required=True, help="How the requester will judge the result")
    propose.add_argument("--files", nargs="*", default=[], help="Requester-project paths the provider may read")
    contract_list = contract_actions.add_parser("list")
    contract_list.add_argument("--waiting", action="store_true", help="Only contracts waiting on this room")
    contract_list.add_argument("--all", action="store_true", help="Include closed contracts")
    contract_show = contract_actions.add_parser("show")
    contract_show.add_argument("id")
    accept = contract_actions.add_parser("accept", help="Provider accepts (main only)")
    accept.add_argument("id")
    accept_basis = accept.add_mutually_exclusive_group(required=True)
    accept_basis.add_argument("--source", help="This room's admin receipt P-... approving the work")
    accept_basis.add_argument("--self-accept", nargs="+", metavar="ATTEST",
                              help="Standing policy: read_named_files_only no_paid_cost one_turn")
    accept.add_argument("--revision", type=int, help="Revision you read; refused if the contract changed")
    for name, needs in (("decline", "--reason"), ("reject", "--reason"), ("deliver", "--result")):
        action = contract_actions.add_parser(name)
        action.add_argument("id")
        action.add_argument(needs, required=True, dest="note")
        action.add_argument("--revision", type=int, required=name == "reject", help="Revision you read")
    for name in ("start", "confirm", "withdraw"):
        action = contract_actions.add_parser(name)
        action.add_argument("id")
        action.add_argument("--revision", type=int, required=name == "confirm", help="Revision you read")
    activate = commands.add_parser("activate", help="Show or switch the release every session's next hook and CLI call runs; no restart")
    activate.add_argument("--root", help="Installed copy under a host plugin cache, for example ~/.claude/plugins/cache/ihav/ihav-agent-room/0.4.5")
    activate.add_argument("--rollback", action="store_true", help="Switch back to the previously active release")
    commands.add_parser("hook", help=argparse.SUPPRESS)
    commands.add_parser("install-alias", help="Install the bare personal init slash command; never overwrite another skill")
    auto = commands.add_parser("_autostart", help=argparse.SUPPRESS)
    auto.add_argument("--session", required=True)
    auto.add_argument("--permission-mode", default="default")
    serve = commands.add_parser("_serve", help=argparse.SUPPRESS)
    serve.add_argument("--generation", required=True)
    tasks = commands.add_parser("task", help="Assigned work, checkpoints, evidence and writer claims").add_subparsers(dest="action", required=True)
    for action in ("create", "update", "submit", "checkpoint"):
        sub = tasks.add_parser(action)
        sub.add_argument("--input", default="-", help="JSON object inline, a JSON file, or - for stdin")
        if action == "create":
            sub.add_argument("--claim", action="store_true",
                             help="Create and claim your own scoped implementation task atomically")
        if action != "create":
            sub.add_argument("id")
            sub.add_argument("--expected-version", type=int, required=True)
        if action in {"update", "submit"}:
            sub.add_argument("--ack", metavar="MESSAGE_ID",
                             help="Process a pending direct message for this task in the same transaction")
    listing = tasks.add_parser("list")
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--owner", type=canonical_member, choices=MEMBERS)
    show = tasks.add_parser("show")
    show.add_argument("id")
    task_context = tasks.add_parser("context", help="Read bounded current context, advisory attention and reconciliation evidence")
    task_context.add_argument("id")
    claim = tasks.add_parser("claim")
    claim.add_argument("id")
    claim.add_argument("--expected-version", type=int, required=True)
    release = tasks.add_parser("release")
    release.add_argument("id")
    release.add_argument("--token", required=True)
    for noun in ("submission", "review", "checkpoint"):
        operations = commands.add_parser(noun, help="Read immutable evidence records" if noun != "review" else "Record an assigned peer's source-bound review").add_subparsers(dest="action", required=True)
        show_record = operations.add_parser("show")
        show_record.add_argument("id")
        if noun == "review":
            record = operations.add_parser("record")
            record.add_argument("id", help="Submission ID to review")
            record.add_argument("--input", default="-")
            record.add_argument("--ack", metavar="MESSAGE_ID",
                                help="Process a pending direct message for this task in the same transaction")
    attempts = commands.add_parser("attempt", help="Native dispatch and output evidence, separate from task completion").add_subparsers(dest="action", required=True)
    attempts_list = attempts.add_parser("list")
    attempts_list.add_argument("--task")
    attempts_list.add_argument("--after", type=int, default=0)
    attempts_list.add_argument("--limit", type=int, default=50)
    attempts_show = attempts.add_parser("show")
    attempts_show.add_argument("id")
    notes = commands.add_parser("note", help="Shared ideas and decisions; authors/main can resolve advisory notes, admin decisions require a receipt").add_subparsers(dest="action", required=True)
    for action in ("add", "resolve"):
        description = ("Open an idea or record an admin decision" if action == "add" else
                       "Author/main follow-up: JSON answer and optional state/superseded_by; approval or admin/bound notes also require main and source")
        sub = notes.add_parser(action, help=description, description=description)
        sub.add_argument("--input", default="-")
        if action == "resolve":
            sub.add_argument("id")
            sub.add_argument("--expected-version", type=int, required=True)
    notes.add_parser("list", help="Read every full current note; use search for bounded selective recall")
    show = notes.add_parser("show")
    show.add_argument("id")
    note_history = notes.add_parser("history", help="Read recorded revisions; older unrecorded history is not reconstructed")
    note_history.add_argument("id")
    note_history.add_argument("--after", type=int, default=0)
    note_history.add_argument("--limit", type=int, default=8)
    note_search = notes.add_parser("search", help="Find current open notes; filter or use --state all for closed discussions",
                                   description="Read bounded current-note previews, without task review, messages or native work. Defaults to open notes; --state all includes closed discussions. Follow read_command for full context.")
    note_search.add_argument("query", nargs="?", default="")
    note_search.add_argument("--state", choices=("all", *sorted(set().union(*NOTE_STATES.values()))), default="open")
    note_search.add_argument("--author", type=canonical_member, choices=MEMBERS)
    note_search.add_argument("--kind", choices=NOTE_STATES)
    note_search.add_argument("--task")
    note_search.add_argument("--after", type=int, default=0)
    note_search.add_argument("--limit", type=int, default=8)
    knowledge = commands.add_parser("knowledge", help="Shared advisory lessons, experiences and tentative preferences").add_subparsers(dest="action", required=True)
    for action in ("add", "update"):
        sub = knowledge.add_parser(action)
        sub.add_argument("--input", default="-")
        if action == "update":
            sub.add_argument("id")
            sub.add_argument("--expected-version", type=int, required=True)
    knowledge.add_parser("show").add_argument("id")
    for action in ("search", "history"):
        sub = knowledge.add_parser(action)
        if action == "search":
            sub.add_argument("query", nargs="?", default="")
            sub.add_argument("--include-retired", action="store_true")
        else:
            sub.add_argument("id")
        sub.add_argument("--after", type=int, default=0)
        sub.add_argument("--limit", type=int, default=8)
    send = commands.add_parser("send", help="Queue a peer message. Success means saved, not read or completed")
    send.add_argument("--to", type=canonical_member, choices=MEMBERS, required=True)
    body = send.add_mutually_exclusive_group()
    body.add_argument("--body-file", default="-", help="Text file or - for stdin; treat text as data")
    body.add_argument("--body", help="Inline text argument; avoids shell heredoc temporary files in a sandbox")
    send.add_argument("--task")
    send.add_argument("--knowledge", help="Link a shared lesson by ID; record its current version and expose later revisions on receipt")
    send.add_argument("--id", help="Stable message ID token for a repeated submission (ASCII letters/digits/._-; max 64 bytes)")
    inbox = commands.add_parser("inbox", help="Read this member's paginated messages")
    inbox.add_argument("--after", type=int, default=0)
    inbox.add_argument("--limit", type=int, default=50)
    inbox.add_argument("--pending", action="store_true",
                       help="Actionable messages without a processing ACK; FYI copies stay in history, incomplete fan-outs appear in status")
    inbox.add_argument("--compact", action="store_true",
                       help="Preview body/detail with a full read command; process full content before ACK")
    inbox.add_argument("--wait", type=positive_timeout, metavar="SECONDS",
                       help="Wait once, at most 100 s, for the first page with messages; use it instead of a shell sleep loop")
    ack = commands.add_parser("ack", help="Record recipient processing, without sending a reply")
    ack.add_argument("id")
    ack.add_argument("--evidence", required=True)
    retry = commands.add_parser("retry-message", help="Explicitly requeue failed/unknown dispatch after reconciliation")
    retry.add_argument("id")
    retry.add_argument("--source", required=True)
    retry.add_argument("--reconciled", required=True)
    intake = commands.add_parser("intake", help="Original admin receipts; semantic classification belongs to CLAUDE_01").add_subparsers(dest="action", required=True)
    intake.add_parser("list")
    recover = intake.add_parser("recover", help="Main-only recovery of original human text after a failed intake hook")
    recover.add_argument("--body-file", default="-")
    recover.add_argument("--source-ref", required=True, help="Reference to the original human message/transcript; never peer text")
    account = intake.add_parser("account")
    account.add_argument("id")
    account.add_argument("--disposition", required=True)
    account.add_argument("--refs", nargs="*", default=[])
    approvals = commands.add_parser("approval", help="Native requests, not room/task authorization").add_subparsers(dest="action", required=True)
    approvals.add_parser("list")
    respond = approvals.add_parser("respond")
    respond.add_argument("id")
    respond.add_argument("--source", required=True, help="Receipt of the explicit human answer to THIS request")
    respond.add_argument("--decision", choices=("accept", "decline", "cancel"), required=True)
    snapshot = commands.add_parser("snapshot", help="Hash exact source files for a review/checkpoint")
    snapshot.add_argument("paths", nargs="+")
    history = commands.add_parser("history", help="Paginated durable messages/events/admin prompt history")
    history.add_argument("--kind", choices=("messages", "events", "prompts"), default="messages")
    history.add_argument("--query", default="", help="All literal case-insensitive terms in message ID/speakers/body, prompt body or event JSON data (max 200 characters); returns full matching records")
    history.add_argument("--after", type=int, default=0)
    history.add_argument("--limit", type=int, default=50)
    return root


def read_input(path, structured=False):
    if path == "-":
        content = sys.stdin.read()
    elif structured and path.lstrip().startswith("{"):
        content = path  # An inline JSON object needs no heredoc, pipe or temporary file.
    else:
        content = Path(path).read_text()
    if structured:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise RoomError("JSON input must be an object")
        return data
    return content


def run(args):
    command = args.command
    if command == "guide":
        return read_guide(args.topic)
    if command == "hook":
        return handle(json.load(sys.stdin))
    if command == "doctor":
        return doctor()
    if command == "install-alias":
        return {"installed": install_alias()}
    if command == "verify-package":
        return verify_archive(args.archive)
    if command == "activate":
        if (args.root or args.rollback) and acting_member() != GATEWAY:
            raise RoomError("Only the main/operator may switch the active release", "authority")
        result = activate_release(args.root, rollback=args.rollback)
        if result.get("activated") and not result.get("unchanged"):
            try:  # Rooms learn about it; their supervisors already follow the pointer on their own.
                result["announced"] = GlobalSpace().post("release", f"ihav-agent-room {result['activated']} is active",
                    f"Active release {result['activated']} at {result['active']['root']}"
                    f"{' (rollback)' if result.get('rolled_back') else ''}. Hooks and CLI calls use it now; each room "
                    "supervisor restarts its workers on their exact sessions at its next all-idle point.")["id"]
            except (RoomError, sqlite3.Error, OSError) as exc:
                result["announce_error"] = str(exc)
        return result
    store = Store(args.project)
    if command == "migrate":
        if acting_member() != GATEWAY:
            raise RoomError("Only the main/operator may migrate an offline room", "authority")
        return migrate(store)
    if command == "init":
        if acting_member() != GATEWAY:
            raise RoomError("Only the admin's main session can initialize a room", "authority")
        if not args.no_start:
            checks = doctor()
            if not checks["ok"]:
                raise RoomError("Dependencies are missing. No project files changed.", "dependency", checks=checks)
            if not os.environ.get("IHAV_AGENT_ROOM_SESSION_ID"):
                raise RoomError("Run /ihav-agent-room:init in Claude Code, or use init --no-start for files only", "identity")
        # New rooms start in pair mode (admin decision 2026-10-03); the library default keeps four members.
        room = initialize(args.project, args.mode or (None if store.exists() else NEW_ROOM_MODE))
        mode_info = {"mode": room["mode"], "members": list(MODES[room["mode"]]), "mode_note": mode_note(room["mode"])}
        if args.no_start:
            return {"initialized": True, "started": False, "room": room, **mode_info}
        return {**start_room(store, os.environ.get("IHAV_AGENT_ROOM_SESSION_ID"),
                             permission_mode=os.environ.get("IHAV_AGENT_ROOM_PERMISSION_MODE", "default")), **mode_info}
    if command == "global":
        return global_command(store, args)
    if command == "contract":
        return contract_command(store, args)
    if command == "_autostart":
        return autostart(store, args.session, args.permission_mode)
    if command == "_serve":
        asyncio.run(Supervisor(store, args.generation).run())
        room = store.room()
        if room["status"] == "failed":
            raise RoomError("Supervisor failed", "native", detail=room["error"])
        return {"stopped": room["status"] == "stopped"}
    if command == "start":
        return start_room(store, os.environ.get("IHAV_AGENT_ROOM_SESSION_ID"), args.mode,
                          os.environ.get("IHAV_AGENT_ROOM_PERMISSION_MODE", "default"))
    if command == "mode":
        if not args.mode:
            status = store.status()
            return {"mode": status["room"]["mode"], "members": [
                        {key: member.get(key) for key in ("name", "requested_model", "requested_effort", "effort_source",
                                                          "observed_model", "observed_effort", "settings_pending_restart")}
                        | {"in_mode": member["name"] in MODES[status["room"]["mode"]]}
                        for member in status["members"]],
                    "gateway_settings_warning": status.get("gateway_settings_warning"),
                    "choices": list(SELECTABLE_MODES)}
        if acting_member() != GATEWAY:
            raise RoomError("Only the admin's main session changes the room mode", "authority")
        return change_mode(store, args.mode)
    if command == "effort":
        if not args.level and not args.clear:
            return store.effort_report()
        if acting_member() != GATEWAY:
            raise RoomError("Only the admin's main session changes member effort", "authority")
        return store.set_effort(args.level, member=args.member, clear=args.clear)
    if command == "wakes":
        return store.activity_report()
    if command == "status":
        status = store.status(compact=args.compact)
        def observed_alive(pid, stamp):
            try:
                return process_alive(pid, stamp)
            except (OSError, subprocess.TimeoutExpired, RoomError):
                status["process_inspection"] = "unavailable; process_alive=null is unknown, not stopped. Room data remains readable; no escalation is needed for status."
                return None
        supervisor = status["room"].get("supervisor") or {}
        status["supervisor_alive"] = observed_alive(supervisor.get("pid"), supervisor.get("stamp"))
        for member in status["members"]:
            member.pop("token_hash", None)
            member["process_alive"] = observed_alive(member.get("pid"), member.get("stamp"))
        return status
    if command == "snapshot":
        return fingerprint(store.project, args.paths)
    if command in {"submission", "review", "checkpoint", "attempt"} and args.action == "show":
        table = {"submission": "submissions", "review": "reviews", "checkpoint": "checkpoints", "attempt": "attempts"}[command]
        with store.read() as db:
            result = store.entry(db, table, args.id)
            if command == "submission":
                result["review_receipts"] = [row[0] for row in db.execute("SELECT id FROM reviews WHERE submission=? ORDER BY rowid", (args.id,))]
            return result
    if command == "attempt":
        return store.attempts(args.task, args.after, args.limit)
    if command == "task" and args.action == "context":
        return store.task_context(args.id)
    if command == "history":
        if not 1 <= args.limit <= 200 or args.after < 0:
            raise RoomError("Use limit 1..200 and after >= 0")
        if len(args.query) > 200:
            raise RoomError("Search query must be text, at most 200 characters")
        terms = args.query.casefold().split()
        field = ("data" if args.kind == "events" else
                 "id || ' ' || sender || ' ' || recipient || ' ' || body || ' ' || context" if args.kind == "messages" else "body")
        selection = f" AND history_matches({field})" if terms else ""
        with store.read() as db:
            db.create_function("history_matches", 1, lambda content: matches_terms(content, terms))
            rows = [dict(r) for r in db.execute(f"SELECT rowid AS cursor,* FROM {args.kind} WHERE rowid>?{selection} ORDER BY rowid LIMIT ?", (args.after, args.limit + 1))]
        return {"items": rows[:args.limit], "next_after": rows[args.limit-1]["cursor"] if len(rows) > args.limit else None}
    if command == "task" and args.action in {"list", "show"}:
        if args.action == "show":
            with store.read() as db:
                return store.record(db, "tasks", args.id)
        return [t for t in store.status()["tasks"] if (args.all or t["state"] not in {"done", "cancelled"}) and (not args.owner or t["owner"] == args.owner)]
    if command == "note" and args.action in {"list", "show", "history", "search"}:
        if args.action == "search":
            return store.search_notes(args.query, state=args.state, author=args.author, kind=args.kind,
                                      task_id=args.task, after=args.after, limit=args.limit)
        if args.action == "history":
            return dict(store.revision_history("notes", args.id, args.after, args.limit),
                        rule="Historical discussion, not current permission. Read note show for current state; revisions before history support may be absent.")
        if args.action == "show":
            with store.read() as db:
                return store.record(db, "notes", args.id)
        return store.list_notes()
    if command in {"approval", "intake"} and args.action == "list":
        with store.read() as db:
            if command == "approval":
                return [json.loads(row[0]) for row in db.execute("SELECT data FROM approvals")]
            return [dict(row) for row in db.execute("SELECT * FROM prompts WHERE accounted IS NULL")]
    if command == "knowledge":
        knowledge = Knowledge(store)
        if args.action == "search":
            return knowledge.search(args.query, args.after, args.limit, args.include_retired)
        if args.action == "history":
            return knowledge.history(args.id, args.after, args.limit)
        if args.action == "show":
            return knowledge.show(args.id)
        return knowledge.write(store.actor(), read_input(args.input, True),
                               getattr(args, "id", None), getattr(args, "expected_version", None))
    actor = store.actor()
    if command == "review":
        return store.record_review(actor, args.id, read_input(args.input, True), ack_id=args.ack)
    if command == "stop":
        store.main_only(actor)
        request_stop(store)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            room = store.room()
            if room["status"] in {"stopped", "failed"}:
                if room["status"] == "failed":
                    raise RoomError("Room failed or cleanup is unconfirmed. Inspect status/logs.", "cleanup", error=room["error"])
                return {"stopped": True}
            supervisor = room.get("supervisor") or {}
            if not process_alive(supervisor.get("pid"), supervisor.get("stamp")):
                # Reconstruct cleanup ownership from persisted native IDs after coordinator failure.
                cleanup = Supervisor(store, room["generation"])
                asyncio.run(cleanup.recover_owned())
                asyncio.run(cleanup.shutdown())
                continue
            time.sleep(.2)
        raise RoomError("Stop requested; process exit not yet confirmed", "pending")
    if command == "task":
        if args.action == "create":
            return store.create_task(actor, read_input(args.input, True), claim=args.claim)
        if args.action == "update":
            return store.update_task(actor, args.id, args.expected_version, read_input(args.input, True), ack_id=args.ack)
        if args.action == "claim":
            return store.claim(actor, args.id, args.expected_version)
        if args.action == "submit":
            return store.submit_task(actor, args.id, args.expected_version, read_input(args.input, True), ack_id=args.ack)
        if args.action == "checkpoint":
            return store.checkpoint(actor, args.id, args.expected_version, read_input(args.input, True))
        return store.release(actor, args.id, args.token)
    if command == "note":
        data = read_input(args.input, True)
        if args.action == "add":
            return store.add_note(actor, data)
        return store.resolve_note(actor, args.id, args.expected_version, data)
    if command == "send":
        return store.send(actor, args.to, args.body if args.body is not None else read_input(args.body_file), args.task, args.id,
                          knowledge_id=args.knowledge)
    if command == "inbox":
        def read():
            return store.inbox(actor, args.after, args.limit, pending=args.pending, compact=args.compact)
        return wait_for_inbox(read, args.wait) if args.wait else read()
    if command == "ack":
        store.acknowledge(actor, args.id, args.evidence)
        return {"processed": args.id}
    if command == "retry-message":
        store.main_only(actor)
        if not args.reconciled.strip():
            raise RoomError("Explain the outcome reconciliation first")
        with store.tx() as db:
            store.source(db, args.source, "message_retry")
            row = db.execute("SELECT * FROM messages WHERE id=?", (args.id,)).fetchone()
            if not row or row["status"] not in {"unknown", "failed"}:
                raise RoomError("Only failed/unknown dispatches can be explicitly retried", "conflict")
            db.execute("UPDATE messages SET status='queued', detail=? WHERE id=?", (args.reconciled, args.id))
        return {"queued": args.id, "processed": False}
    if command == "intake":
        if args.action == "recover":
            store.main_only(actor)
            body = read_input(args.body_file)
            if not body.strip() or not args.source_ref.strip():
                raise RoomError("Recovery requires original human text and its source reference")
            receipt = store.intake(os.environ["IHAV_AGENT_ROOM_SESSION_ID"], body, origin="manual_recovery:" + args.source_ref)
            notification = store.broadcast_gateway_prompt(body, "recovery\0" + args.source_ref,
                                                          receipt_id=receipt, provenance_state="manual_recovery")
            return {"receipt": receipt, "origin": "manual_recovery", "native_permission_approval_eligible": False,
                    "notify_all_queued": notification["members"], "room_status": notification["room_status"]}
        return {"accounted": args.id} | store.account(actor, args.id, args.disposition, args.refs)
    if command == "approval":
        return approval_response(store, actor, args.id, args.source, args.decision)
    raise RoomError("Unsupported command")


def global_command(store, args):
    space, room = GlobalSpace(), store.room()
    if args.action in {"post", "reply", "join", "leave"} and acting_member() != GATEWAY:
        raise RoomError("Only the main/operator may write to the agents space", "authority")
    if args.action == "join":
        return space.register(room["id"], store.project, __version__, enabled=True)
    if args.action == "leave":
        return space.register(room["id"], store.project, __version__, enabled=False)
    if args.action == "rooms":
        return space.rooms()
    if args.action == "show":
        return space.show(args.id)
    if args.action == "list":
        view = space.visible(room["id"], after=args.after, limit=args.limit, unread=args.unread)
        if args.mark_read and view["items"]:
            space.mark_read(room["id"], view["items"][-1]["seq"])
        return view
    if args.action == "post":
        store.authorize_global_post(acting_member(), args.source)
        return space.post("announcement", args.subject, args.body, origin=room["id"], member=GATEWAY,
                          audience=args.to, expires=args.expires)
    return space.post("reply", "Re: " + space.show(args.reply_to)["subject"][:190], args.body, origin=room["id"],
                      member=GATEWAY, reply_to=args.reply_to)


def contract_command(store, args):
    contracts, room = Contracts(), store.room()["id"]
    if args.action == "list":
        return contracts.listing(room, waiting=args.waiting, include_closed=args.all)
    if args.action == "show":
        return contracts.show(args.id)
    if acting_member() != GATEWAY:
        raise RoomError("Only the main/operator acts on contracts for this room", "authority")
    if args.action == "propose":
        return contracts.propose(room, contracts.resolve_room(args.to), args.type, args.title, args.request,
                                 args.acceptance, args.files)
    if args.action == "accept" and args.source:
        store.authorize_contract_accept(acting_member(), args.source)
    return contracts.act(room, args.id, args.action, note=getattr(args, "note", None),
                         source=getattr(args, "source", None), attest=getattr(args, "self_accept", None),
                         expected_revision=getattr(args, "revision", None))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if args.command == "hook":
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command in {"task", "note", "intake", "review", "migrate"} and getattr(args, "action", "") not in {"list", "show", "context", "history", "search"}:
            try:
                Store(args.project).project_views()
            except (OSError, RoomError) as exc:
                # The transaction is already committed; surface this explicitly, never suggest replay.
                print(dumps({"ok": False, "committed": True, "data": result,
                             "error": {"code": "projection", "message": str(exc)}}))
                return 1
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False,
                         indent=None if args.json else 2, separators=(",", ":") if args.json else None))
        return 1 if args.command == "doctor" and not result["ok"] else 0
    except (RoomError, OSError, ValueError, sqlite3.Error) as exc:
        error = {"code": exc.code if isinstance(exc, RoomError) else "local_error", "message": str(exc)}
        if isinstance(exc, RoomError):
            error.update(exc.details)
        if args.command == "hook":
            print(json.dumps({"systemMessage": "Agent Room hook failed: " + str(exc)}))
            return 0  # Visible diagnostic, without swallowing the user's prompt.
        print(dumps({"ok": False, "error": error}))
        return 1
