"""Transactional room state; Markdown files are projections, never a second ledger."""

from collections import Counter
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import json

from ihav_agent_room.common import (GATEWAY, MEMBERS, MODES, RoomError, acting_member, main_host, canonical_member, atomic_write, dumps, main_session_id,
                               file_lock, fingerprint, native_event_prompt, now, overlaps, scoped_path, uid)
from ihav_agent_room.evidence import bounded, capture, digest, matches_terms, nonempty_strings, source_matches
from ihav_agent_room.provenance import assess_chain
from ihav_agent_room.prompt_frame import frame_for_prompt, load_frame
from ihav_agent_room.roster import EFFORT_LEVELS, ROSTER_BY_NAME, mode_settings, room_gateway
from ihav_agent_room.schema import (EXTENSIONS, HOST_SCHEMA, KNOWLEDGE_SCHEMA, VERSION,
                                   LEGACY_WORKER_SESSION_SCHEMA, WORKER_SESSION_SCHEMA)


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE members (name TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE prompts (id TEXT PRIMARY KEY, session TEXT NOT NULL, body TEXT NOT NULL,
    created TEXT NOT NULL, accounted TEXT, origin TEXT NOT NULL);
CREATE TABLE tasks (id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE claims (task TEXT PRIMARY KEY REFERENCES tasks(id), owner TEXT NOT NULL,
    token TEXT NOT NULL, paths TEXT NOT NULL);
CREATE TABLE notes (id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    sender TEXT NOT NULL, recipient TEXT NOT NULL, task TEXT, body TEXT NOT NULL,
    context TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, detail TEXT);
CREATE TABLE approvals (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    data TEXT NOT NULL, created TEXT NOT NULL);
"""
# DEC-009 (admin, 2026-09-30) and the admin's follow-up answer in the Claude session: a receipt whose host provenance is not
# confirmed as human is refused for these uses. Accounting (bookkeeping) and creating an analysis-only task are recorded
# in prompt.consumed with their provenance but never refused; a host label that marks the prompt non-human refuses every use.
PROTECTED_USES = frozenset({"contract_accept", "global_post", "native_approval", "task_create_implementation", "task_assign", "task_contract",
                            "task_cancel_or_reopen", "note_admin", "knowledge_admin", "message_retry"})
UNPROTECTED_USES = frozenset({"account", "auto_void", "task_create_analysis"})
MAX_MESSAGE_ID_BYTES = 64
MAX_MESSAGE_CHARS = 16000
ADMIN_NOTICE_PROVENANCE = frozenset({"human", "non_human", "unverified", "manual_recovery"})
# Broadcast copies and admin relays are FYI history. They do not create recipient ACK work.
FYI_CONTEXT_SQL = "(json_extract(context, '$.broadcast.id') IS NOT NULL OR COALESCE(json_extract(context, '$.admin_relay'), 0) = 1)"
ACTIONABLE_PENDING_SQL = f"(status != 'processed' AND NOT {FYI_CONTEXT_SQL})"
DELIVERED_STATUSES = frozenset({"accepted", "submitted", "processed"})
# O3 review-packet cap (2026-10-01): 1,500 bytes keeps three paths and eight bounded
# evidence claims useful for common reviews; FYI copies never receive this packet.
MAX_REVIEW_PACKET_BYTES = 1500
TASK_STATES = {"ready", "running", "blocked", "review", "done", "cancelled"}
NOTE_STATES = {
    "question": {"open", "answered", "superseded"},
    "proposal": {"open", "approved", "rejected", "superseded"},
    "decision": {"approved", "superseded"},
}


def validate_fields(data, text=(), lists=()):
    for key in text:
        if key in data and not isinstance(data[key], str):
            raise RoomError(f"{key} must be a string")
    for key in lists:
        if key in data and (not isinstance(data[key], list) or any(not isinstance(x, str) or not x for x in data[key])):
            raise RoomError(f"{key} must be an array of nonempty strings")


LEGACY_PROJECTION_HEADER = "<!-- agent-room generated; update through agent-room CLI -->\n"


class Store:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.space = self.project / "agents_space"
        self.runtime = self.space / ".runtime"
        self.path = self.runtime / "room.sqlite3"
        for path in (self.space, self.runtime, self.path):
            if path.is_symlink():
                raise RoomError(f"Runtime cannot use a symlink: {path}")

    @property
    def gateway(self):
        return room_gateway(self.room()) if self.exists() else GATEWAY

    def exists(self):
        return self.path.is_file()

    def connect(self, *, timeout=10):
        if not self.exists():
            raise RoomError("Room is not initialized. Run /ihav-agent-room:start in Claude Code or $ihav-agent-room:start in Codex.", "not_initialized")
        connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def tx(self, *, timeout=10):
        db = self.connect(timeout=timeout)
        try:
            db.execute("BEGIN IMMEDIATE")
            self.get_room(db)
            yield db
            db.execute("COMMIT")
        except BaseException as error:
            if db.in_transaction:
                # A refused receipt keeps only its own record (source() runs before any other write in these transactions).
                db.execute("COMMIT" if getattr(error, "keep_record", False) else "ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def read(self):
        db = self.connect()
        try:
            db.execute("BEGIN")
            self.get_room(db)
            yield db
        finally:
            db.close()

    def initialize(self, mode):
        if mode not in MODES:
            raise RoomError("Unknown mode")
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        with file_lock(self.runtime / "init.lock"):
            if self.exists():
                room = self.room()
                if room["project"] != str(self.project):
                    raise RoomError("Room was moved; automatic adoption is not supported")
                return room
            temporary = self.runtime / ("init-" + uid() + ".sqlite3")
            db = sqlite3.connect(temporary)
            try:
                db.executescript(SCHEMA + EXTENSIONS + KNOWLEDGE_SCHEMA)
                room = {"schema": VERSION, "id": uid("room-"), "project": str(self.project),
                        "mode": mode, "status": "stopped", "manual_stop": False,
                        "owner": None, "supervisor": None, "generation": None,
                        "created": now(), "error": None}
                db.execute("INSERT INTO meta VALUES ('room', ?)", (dumps(room),))
                for name in MEMBERS:
                    profile = ROSTER_BY_NAME[name]
                    settings = mode_settings(mode, name)
                    db.execute("INSERT INTO members VALUES (?,?)", (name, dumps({
                        "name": name, "native_id": None, "pid": None, "stamp": None,
                        "status": "stopped", "error": None, "turn_id": None,
                        "requested_model": settings["model"], "requested_effort": settings["effort"],
                        "model_label": settings["label"], "effort_source": "mode",
                        "settings_application": "host-managed" if profile["control"] == "host" else "configured; not started",
                        "observed_model": None, "observed_effort": None, "model_observed_at": None,
                    })))
                db.commit()
                db.close()
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                db.close()
                temporary.unlink(missing_ok=True)
            return room

    def apply_mode_settings(self, db, mode):
        """Reset every member's requested model/effort to the mode's settings; manual overrides are cleared."""
        for name in MEMBERS:
            member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
            settings = mode_settings(mode, name)
            member.update(requested_model=settings["model"], requested_effort=settings["effort"],
                          model_label=settings["label"], effort_source="mode")
            if ROSTER_BY_NAME[name]["host"] == "claude" and name != self.gateway and member.get("native_id"):
                member["settings_pending_restart"] = True
            db.execute("UPDATE members SET data=? WHERE name=?", (dumps(member), name))

    def set_effort(self, level, member=None, clear=False):
        """Set requested effort for one member (an override) or every room-controlled member; clear restores the mode."""
        if not clear and level not in EFFORT_LEVELS:
            raise RoomError("Effort must be one of: " + ", ".join(EFFORT_LEVELS))
        with self.tx() as db:
            room = self.get_room(db)
            names = [canonical_member(member)] if member else [name for name in MEMBERS if name != self.gateway]
            for name in names:
                if name not in MEMBERS:
                    raise RoomError("Unknown member")
                if name == self.gateway:
                    raise RoomError("The gateway is your own session; change it in the native host", "authority")
                data = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                if clear:
                    data.update(requested_effort=mode_settings(room["mode"], name)["effort"], effort_source="mode")
                else:
                    data.update(requested_effort=level, effort_source="override")
                if ROSTER_BY_NAME[name]["host"] == "claude" and data.get("native_id"):
                    data["settings_pending_restart"] = True
                db.execute("UPDATE members SET data=? WHERE name=?", (dumps(data), name))
            self.event(db, "settings.effort", {"members": names, "effort": None if clear else level, "clear": clear})
        return self.effort_report()

    def sync_gateway_effort(self, level):
        """Admin decision Q3.b: when the gateway's own effort changes, every room-controlled member follows it and
        manual overrides are cleared. Codex members apply it on their next turn; running Claude workers on resume."""
        if level not in EFFORT_LEVELS:
            return False
        with self.tx() as db:
            room = self.get_room(db)
            gateway = json.loads(db.execute("SELECT data FROM members WHERE name=?", (self.gateway,)).fetchone()[0])
            previous = room.get("synced_effort")
            if previous is None:
                observed = gateway.get("observed_effort")
                previous = observed if observed in EFFORT_LEVELS else None
            gateway.update(observed_effort=level, effort_observed_at=now())
            db.execute("UPDATE members SET data=? WHERE name=?", (dumps(gateway), self.gateway))
            if room.get("synced_effort") == level:
                return False
            baseline = previous is None or previous == level
            room["synced_effort"] = level
            if baseline:
                # No change from the earliest valid observation: keep mode settings and manual overrides.
                self.put_room(db, room)
                self.event(db, "settings.effort_baseline", {"effort": level})
                return False
            self.put_room(db, room)
            for name in MEMBERS:
                if name == self.gateway:
                    continue
                data = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                data.update(requested_effort=level, effort_source="gateway")
                if ROSTER_BY_NAME[name]["host"] == "claude" and data.get("native_id"):
                    data["settings_pending_restart"] = True
                db.execute("UPDATE members SET data=? WHERE name=?", (dumps(data), name))
            self.event(db, "settings.effort_sync", {"effort": level})
            return True

    def effort_report(self):
        with self.read() as db:
            room = self.get_room(db)
            members = []
            for name in MEMBERS:
                data = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                members.append({"name": name, "in_mode": name in MODES[room["mode"]],
                                "requested_effort": data.get("requested_effort"),
                                "source": "your own session (host-managed)" if name == self.gateway else data.get("effort_source", "mode"),
                                "observed_effort": data.get("observed_effort"),
                                "pending_restart": bool(data.get("settings_pending_restart"))})
            return {"mode": room["mode"], "synced_effort": room.get("synced_effort"), "members": members,
                    "note": "Requested settings, not proof the host applied them."}

    def gateway_settings_warning(self, room, gateway):
        """Admin decision R1.a: warn when the gateway's observed model or effort differs from the mode's setting."""
        wanted = mode_settings(room["mode"], self.gateway)
        hints = []
        observed_model = (gateway.get("observed_model") or "").lower()
        if observed_model and wanted["model"] not in observed_model:
            hints.append(f"/model {wanted['model']}")
        if gateway.get("observed_effort") and gateway["observed_effort"] != wanted["effort"]:
            hints.append(f"/effort {wanted['effort']}")
        if not hints:
            return None
        return (f"Your session runs {gateway.get('observed_model') or 'an unknown model'} at "
                f"{gateway.get('observed_effort') or 'unknown'} effort; mode {room['mode']} expects "
                f"{wanted['label']} at {wanted['effort']}. Run " + " and ".join(hints) + ".")

    @staticmethod
    def get_room(db):
        room = json.loads(db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])
        if room["schema"] not in {VERSION, HOST_SCHEMA, LEGACY_WORKER_SESSION_SCHEMA, WORKER_SESSION_SCHEMA}:
            raise RoomError("Unsupported room schema. Stop with the compatible plugin, then run ihav-agent-room migrate for schema 1 or 2.", "incompatible")
        return room

    @staticmethod
    def put_room(db, room):
        db.execute("UPDATE meta SET value=? WHERE key='room'", (dumps(room),))

    def room(self):
        with self.read() as db:
            room = self.get_room(db)
            if room["project"] != str(self.project):
                raise RoomError("Room belongs to another project location", "conflict")
            return room

    @staticmethod
    def event(db, kind, data):
        return db.execute("INSERT INTO events(kind,data,created) VALUES (?,?,?)", (kind, dumps(data), now())).lastrowid

    def member(self, name, changes=None):
        if name not in MEMBERS:
            raise RoomError("Unknown member")
        with self.tx() as db:
            member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
            if changes:
                member.update(changes)
                db.execute("UPDATE members SET data=? WHERE name=?", (dumps(member), name))
            return member

    def actor(self):
        name = acting_member()
        session = main_session_id()
        with self.read() as db:
            room = self.get_room(db)
            if name == self.gateway:
                owner = room.get("owner") or {}
                if (owner.get("session") == session and session
                        and owner.get("host", "claude") == main_host()
                        and not os.environ.get("IHAV_AGENT_ROOM_BINDING")):
                    return name
            elif name in MEMBERS:
                member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                token = os.environ.get("IHAV_AGENT_ROOM_BINDING", "")
                if token and member.get("token_hash") == hashlib.sha256(token.encode()).hexdigest():
                    if member.get("session_replacement"):
                        # Native exact resume reloads a shared settings file. A
                        # retired job might obtain its new token, but never its
                        # new UUID. The initial fresh hook can bind only while
                        # this generation's explicit allocation is pending.
                        records = [item for item in room.get("worker_session_history", [])
                                   if item["id"] == member["session_replacement"] and item["member"] == name]
                        retired = {item["retired_session"] for item in room.get("worker_session_history", [])}
                        native_matches = (bool(session) and session not in retired and len(records) == 1
                                          and (member.get("native_id") == session or
                                               member.get("native_id") is None and member["status"] == "starting"
                                               and records[0]["state"] == "launching"
                                               and records[0]["launch_generation"] == room["generation"]))
                        if not native_matches:
                            raise RoomError("Replacement worker native identity does not match its current binding", "identity")
                    if name in MODES[room["mode"]] and room["status"] in {"starting", "running", "stopping"}:
                        return name
        raise RoomError("This process is not bound to an active room member", "identity")

    def main_only(self, actor):
        if actor != self.gateway:
            raise RoomError(f"Only {self.gateway} records admin intent or changes assignment/authority", "authority")

    @staticmethod
    def source(db, prompt_id, use):
        """Check a receipt before use; only account bookkeeping is outside the protected-use set."""
        if use not in PROTECTED_USES | UNPROTECTED_USES:
            raise RoomError("Unknown receipt use", "invalid")
        row = db.execute("SELECT body,origin FROM prompts WHERE id=?", (prompt_id,)).fetchone() if prompt_id else None
        if row and use in {"account", "auto_void"} and row["origin"] == "hook" and native_event_prompt(row["body"]):
            # Older hooks made receipts for native envelopes (cross-session messages before 0.4.4). Bookkeeping closes
            # them as void; they still never authorize anything (reported by ihav-competitor-search 2026-10-04).
            provenance = {"state": "non_human", "kind": "native_envelope", "reason": "body is a native event envelope"}
            Store.event(db, "prompt.voided", {"receipt": prompt_id, "use": use} | provenance)
            return provenance | {"void": True}
        if not row or row["origin"] == "peer" or native_event_prompt(row["body"]):
            raise RoomError("An original admin prompt ID is required", "authority")
        # The host transcript row exists by now, so the hook-time offset is enough to find it again.
        receipts = [(event[0], json.loads(event[1])) for event in db.execute("SELECT seq, data FROM events WHERE kind='prompt.receipt' ORDER BY seq")]
        mine = next(((seq, data) for seq, data in receipts if data.get("receipt") == prompt_id), None)
        if mine:
            # Earlier receipts with the same text in the same transcript claim their rows first (one row, one receipt).
            offsets = []
            for seq, other in receipts:
                body = db.execute("SELECT body FROM prompts WHERE id=?", (other.get("receipt"),)).fetchone()
                if seq < mine[0] and other.get("transcript") == mine[1].get("transcript") and body and body[0].strip() == row["body"].strip():
                    offsets.append(other.get("offset"))
            provenance = assess_chain(mine[1].get("transcript"), offsets + [mine[1].get("offset")], row["body"])
        else:
            provenance = {"state": "absent", "reason": "no provenance record: receipt predates provenance records or was recovered manually"}
        if use == "auto_void" and provenance["state"] != "non_human":
            raise RoomError("Only a receipt the host labels non-human closes automatically", "skip")  # Nothing recorded.
        if provenance["state"] == "non_human" and use in {"account", "auto_void"}:
            # The hook could not see the label yet; closing the receipt as void keeps the Stop reminder finite.
            Store.event(db, "prompt.voided", {"receipt": prompt_id, "use": use} | provenance)
            return provenance | {"void": True}
        if provenance["state"] == "non_human":
            message = f"The host transcript labels this prompt {provenance['kind']}, not admin; it cannot be a receipt"
        elif use in PROTECTED_USES and provenance["state"] != "human":
            message = (f"Host provenance of this receipt is {provenance['state']} ({provenance['reason']}); this use needs an admin prompt the "
                       "host confirms as human. Recheck the original host row after it is written; if the host omits origin labels, "
                       "this capability is unavailable. Repeating plain text is not a verified recovery path; intake account still works")
        else:
            Store.event(db, "prompt.consumed", {"receipt": prompt_id, "use": use} | provenance)
            return provenance
        Store.event(db, "prompt.refused", {"receipt": prompt_id, "use": use} | provenance)
        refusal = RoomError(message, "authority")
        refusal.keep_record = True
        raise refusal

    @staticmethod
    def record(db, table, record_id):
        row = db.execute(f"SELECT version,data FROM {table} WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise RoomError(f"Unknown {table} record: {record_id}", "not_found")
        return dict(json.loads(row["data"]), id=record_id, version=row["version"])

    def revision_history(self, table, record_id, after=0, limit=8):
        """Read recorded revisions only; never fabricate history for older records."""
        kind = {"notes": "note.revised", "knowledge": "knowledge.revised"}.get(table)
        if not kind or after < 0 or not 1 <= limit <= 50:
            raise RoomError("Use notes/knowledge history, after >= 0 and limit 1..50")
        with self.read() as db:
            current = self.record(db, table, record_id)
            items = []
            for row in db.execute("SELECT seq,data FROM events WHERE kind=? AND seq>? ORDER BY seq", (kind, after)):
                record = json.loads(row["data"])
                if record["id"] == record_id:
                    items.append(dict(record, cursor=row["seq"]))
                if len(items) > limit:
                    break
        return {"current_version": current["version"], "current_state": current["state"], "historical": True,
                "items": items[:limit], "next_after": items[limit - 1]["cursor"] if len(items) > limit else None}

    @staticmethod
    def save(db, table, record, expected):
        if record["version"] != expected:
            raise RoomError("Record changed. Read current state and reconcile before retrying.",
                            "conflict", current_version=record["version"])
        record["version"] += 1
        record["updated"] = now()
        db.execute(f"UPDATE {table} SET version=?,data=? WHERE id=?",
                   (record["version"], dumps(record), record["id"]))

    def require_ready(self, db, task):
        for dependency in task["dependencies"]:
            if self.record(db, "tasks", dependency)["state"] != "done":
                raise RoomError(f"Dependency is not complete: {dependency}", "dependency")
        for note_id, revision in task.get("decisions", {}).items():
            note = self.record(db, "notes", note_id)
            if note["version"] != revision or note["state"] != "approved":
                raise RoomError("Reconcile changed decisions first", "conflict")
            if note.get("condition") and not note.get("condition_evidence"):
                raise RoomError("Approval condition has not been satisfied", "authority")

    def intake(self, session, body, receipt=None, origin="hook", provenance=None):
        receipt = receipt or uid("P-")
        with self.tx() as db:
            existing = db.execute("SELECT * FROM prompts WHERE id=?", (receipt,)).fetchone()
            if existing:
                if existing["session"] != session or existing["body"] != body:
                    raise RoomError("Receipt ID reused with different prompt", "conflict")
            else:
                db.execute("INSERT INTO prompts VALUES (?,?,?,?,NULL,?)", (receipt, session, body, now(), origin))
                if provenance:
                    self.event(db, "prompt.receipt", {"receipt": receipt, "session": session} | provenance)
        return receipt

    def account(self, actor, prompt_id, disposition, refs):
        self.main_only(actor)
        if not disposition.strip():
            raise RoomError("Record how each intent was handled; a status question need not create a task")
        with self.tx() as db:
            provenance = self.source(db, prompt_id, "account")
            if provenance.get("void"):
                if refs:
                    raise RoomError("A prompt the host labels non-admin takes no task or note references", "authority")
                db.execute("UPDATE prompts SET accounted=? WHERE id=?", (dumps(
                    {"disposition": f"void: host labels this prompt {provenance['kind']}, not admin", "requested": disposition, "refs": []}),
                    prompt_id))
                return {"voided": provenance["kind"]}
            for ref in refs:
                if not any(db.execute(f"SELECT 1 FROM {table} WHERE id=?", (ref,)).fetchone()
                           for table in ("tasks", "notes", "knowledge")):
                    raise RoomError(f"Unknown disposition reference: {ref}")
            db.execute("UPDATE prompts SET accounted=? WHERE id=?",
                       (dumps({"disposition": disposition, "refs": refs}), prompt_id))
        return {}

    def authorize_global_post(self, actor, prompt_id):
        """A machine-wide announcement needs the gateway and an admin prompt the host confirms as human."""
        self.main_only(actor)
        with self.tx() as db:
            self.source(db, prompt_id, "global_post")

    def authorize_contract_accept(self, actor, prompt_id):
        """Accepting another room's contract on the provider admin's word needs a human-confirmed receipt."""
        self.main_only(actor)
        with self.tx() as db:
            self.source(db, prompt_id, "contract_accept")

    def auto_void_peer_receipts(self, session):
        """Close, as void, this session's open receipts that the host transcript now labels non-human.

        Older hooks made them before the transcript row existed; closing them needs no admin answer and grants nothing.
        """
        with self.read() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM prompts WHERE session=? AND accounted IS NULL", (session,))]
        closed = []
        for prompt_id in ids:
            try:
                with self.tx() as db:
                    provenance = self.source(db, prompt_id, "auto_void")
                    db.execute("UPDATE prompts SET accounted=? WHERE id=?", (dumps(
                        {"disposition": f"void: host labels this prompt {provenance['kind']}, not admin (automatic)", "refs": []}),
                        prompt_id))
                closed.append(prompt_id)
            except RoomError:
                continue
        return closed

    def create_task(self, actor, data, *, claim=False):
        self.main_only(actor)
        if data.keys() - {"title", "request", "acceptance", "next", "owner", "source", "authority", "scope", "dependencies", "review_policy", "reviewer"}:
            raise RoomError("Unsupported task creation fields")
        validate_fields(data, ("title", "request", "acceptance", "next", "owner", "source", "authority"), ("scope", "dependencies"))
        for key in ("title", "request", "acceptance", "next", "owner", "source"):
            if not data.get(key):
                raise RoomError(f"Task requires {key}")
        authority = data.get("authority", "analysis")
        if authority not in {"analysis", "implementation"}:
            raise RoomError("authority must be analysis or implementation")
        paths = [scoped_path(self.project, path) for path in data.get("scope", [])]
        if claim and (data["owner"] != actor or authority != "implementation" or not paths):
            raise RoomError("--claim requires the creator to own an implementation task with explicit scope", "authority")
        with self.tx() as db:
            self.source(db, data["source"], "task_create_implementation" if authority == "implementation" else "task_create_analysis")
            room = self.get_room(db)
            if data["owner"] not in MODES[room["mode"]]:
                raise RoomError("Task owner must be active in the room mode")
            policy, reviewer = data.get("review_policy", "none"), data.get("reviewer")
            self.validate_review_policy(room, data["owner"], policy, reviewer)
            dependencies = data.get("dependencies", [])
            for dependency in dependencies:
                self.record(db, "tasks", dependency)
            task = dict(data, id=uid("T-"), version=1, authority=authority, scope=paths,
                        dependencies=dependencies, state="ready", checkpoint="", evidence=[],
                        created=now(), updated=now(), decisions={}, review_policy=policy, reviewer=reviewer,
                        submission=None, checkpoint_id=None, contract_revision=1, last_progress=now())
            db.execute("INSERT INTO tasks VALUES (?,?,?)", (task["id"], 1, dumps(task)))
            self.event(db, "task.created", {"id": task["id"], "actor": actor})
            writer_claim = self._claim(db, actor, task, task["version"]) if claim else None
            if task["owner"] != actor:
                self.notify(db, actor, task["owner"], "New assigned task. Read the task and current decisions before acting.", task["id"])
        return {"task": task, "claim": writer_claim} if claim else task

    def update_task(self, actor, task_id, expected, changes, *, ack_id=None):
        validate_fields(changes, ("state", "checkpoint", "next", "blocked_reason", "owner", "authority", "acceptance", "request", "source"), ("evidence", "scope"))
        if "snapshot" in changes:
            snapshot = changes["snapshot"]
            if not isinstance(snapshot, dict) or any(not isinstance(k, str) or (v is not None and not isinstance(v, str)) for k, v in snapshot.items()):
                raise RoomError("snapshot must map relative file names to hashes or null")
        allowed = {"state", "checkpoint", "next", "evidence", "snapshot", "blocked_reason"}
        admin_fields = {"owner", "scope", "authority", "acceptance", "request", "source", "review_policy", "reviewer"}
        if changes.keys() - allowed - admin_fields:
            raise RoomError("Unsupported task update fields")
        with self.tx() as db:
            task = self.record(db, "tasks", task_id)
            if actor != task["owner"] and actor != self.gateway:
                raise RoomError(f"Only the task owner or {self.gateway} can update it", "authority")
            state = changes.get("state", task["state"])
            previous_state = task["state"]
            if state not in TASK_STATES:
                raise RoomError("Invalid task state")
            terminal_transition = ((state == "cancelled" and previous_state != "cancelled")
                                   or (previous_state in {"done", "cancelled"} and state != previous_state))
            if terminal_transition:
                self.main_only(actor)
                self.source(db, changes.get("source"), "task_cancel_or_reopen")
            contract_fields = changes.keys() & (admin_fields - {"source"})
            if contract_fields or ("source" in changes and not terminal_transition):
                self.main_only(actor)
                self.source(db, changes.get("source"), "task_assign" if contract_fields & {"owner", "scope", "authority"} else "task_contract")
                if changes.keys() & {"owner", "scope", "authority"} and db.execute("SELECT 1 FROM claims WHERE task=?", (task_id,)).fetchone():
                    raise RoomError("Release the writer claim before changing assignment/scope", "conflict")
            if "owner" in changes and changes["owner"] not in MODES[self.get_room(db)["mode"]]:
                raise RoomError("Owner is inactive")
            if "scope" in changes:
                changes = dict(changes, scope=[scoped_path(self.project, x) for x in changes["scope"]])
            if changes.get("authority", task["authority"]) not in {"analysis", "implementation"}:
                raise RoomError("Invalid authority")
            self.validate_review_policy(self.get_room(db), changes.get("owner", task["owner"]),
                                        changes.get("review_policy", task["review_policy"]), changes.get("reviewer", task["reviewer"]))
            contract_changed = any(changes[key] != task.get(key) for key in changes.keys() & admin_fields)
            if contract_changed:
                task["contract_revision"] += 1
            if state in {"running", "review", "done"}:
                self.require_ready(db, task)
            if state == "done":
                if not changes.get("evidence", task["evidence"]):
                    raise RoomError("Completion requires acceptance evidence")
                snapshot = changes.get("snapshot", task.get("snapshot", {}))
                if snapshot and fingerprint(self.project, snapshot) != snapshot:
                    raise RoomError("Reviewed source changed; reconcile before completion", "conflict")
                if changes.get("review_policy", task["review_policy"]) == "peer_required":
                    self.main_only(actor)
                    if self.review_status(db, dict(task, **changes))["state"] != "approved":
                        raise RoomError("Completion needs an approved peer receipt for the current submission/source/contract", "review_required")
            task.update(changes)
            if any(key in changes for key in {"checkpoint", "evidence", "state"}):
                task["last_progress"] = now()
            self.save(db, "tasks", task, expected)
            if state in {"done", "cancelled", "blocked", "review"} and actor == task["owner"]:
                db.execute("DELETE FROM claims WHERE task=?", (task_id,))
            if changes.keys() & admin_fields and task["owner"] != actor:
                self.notify(db, actor, task["owner"], "Task assignment or authority changed. Read its current revision.", task_id)
            if state == "done" and previous_state != "done":
                for row in db.execute("SELECT data FROM tasks").fetchall():
                    dependent = json.loads(row[0])
                    if task_id in dependent["dependencies"] and dependent["state"] not in {"done", "cancelled"}:
                        self.notify(db, actor, dependent["owner"], f"Dependency {task_id} completed. Reconcile all remaining dependencies and blockers before continuing.", dependent["id"])
            self.event(db, "task.updated", {"id": task_id, "version": task["version"], "actor": actor})
            if ack_id is not None:
                self._acknowledge(db, actor, ack_id,
                                  f"Processed with successful task update {task_id}.", expected_task=task_id)
                result = dict(task, processed_message=ack_id)
            else:
                result = task
        return result

    def _claim(self, db, actor, task, expected):
        task_id = task["id"]
        if actor != task["owner"] or task["authority"] != "implementation" or not task["scope"]:
            raise RoomError("A writer needs an assigned implementation task with explicit scope", "authority")
        if task["version"] != expected or task["state"] not in {"ready", "running"}:
            raise RoomError("Read the current actionable task before claiming", "conflict")
        self.require_ready(db, task)
        for row in db.execute("SELECT * FROM claims"):
            if row["task"] == task_id:
                return dict(row, paths=json.loads(row["paths"]))
            if any(overlaps(a, b) for a in task["scope"] for b in json.loads(row["paths"])):
                raise RoomError("Another task owns an overlapping path", "conflict", task=row["task"])
        token = uid()
        db.execute("INSERT INTO claims VALUES (?,?,?,?)", (task_id, actor, token, dumps(task["scope"])))
        task["state"] = "running"
        task["last_progress"] = now()
        self.save(db, "tasks", task, expected)
        self.event(db, "writer.claimed", {"task": task_id, "owner": actor})
        return {"task": task_id, "owner": actor, "token": token, "paths": task["scope"], "version": task["version"]}

    def claim(self, actor, task_id, expected):
        with self.tx() as db:
            task = self.record(db, "tasks", task_id)
            return self._claim(db, actor, task, expected)

    def release(self, actor, task_id, token):
        with self.tx() as db:
            row = db.execute("SELECT * FROM claims WHERE task=?", (task_id,)).fetchone()
            if not row:
                return {"released": False}
            if row["owner"] != actor or row["token"] != token:
                raise RoomError("Only the claimant with its token may release this scope", "authority")
            db.execute("DELETE FROM claims WHERE task=?", (task_id,))
            return {"released": True}

    @staticmethod
    def validate_review_policy(room, owner, policy, reviewer):
        if policy not in ("none", "peer_required"):
            raise RoomError("review_policy must be none or peer_required")
        if policy == "peer_required" and (reviewer not in MODES[room["mode"]] or reviewer == owner):
            raise RoomError("peer_required needs an active reviewer different from the task owner")
        if policy == "none" and reviewer is not None:
            raise RoomError("Set reviewer to null when review_policy is none")

    @staticmethod
    def entry(db, table, record_id):
        row = db.execute(f"SELECT data FROM {table} WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise RoomError(f"Unknown {table} record: {record_id}", "not_found")
        return json.loads(row[0])

    def review_status(self, db, task):
        if task["review_policy"] == "none":
            return {"state": "not_required"}
        if not task.get("submission"):
            return {"state": "not_submitted"}
        submission = self.entry(db, "submissions", task["submission"])
        result = {"submission": submission["id"], "source_digest": submission["digest"]}
        if (submission["contract_revision"] != task["contract_revision"] or submission["author"] != task["owner"]
                or submission["reviewer"] != task["reviewer"] or submission["decisions"] != task["decisions"]):
            return dict(result, state="stale", reason="task contract/owner/decisions changed")
        if submission["evidence"] != task["evidence"]:
            return dict(result, state="stale", reason="submission evidence changed")
        if any(self.record(db, "tasks", key)["version"] != version for key, version in submission["dependencies"].items()):
            return dict(result, state="stale", reason="dependency revision changed")
        if not source_matches(self.project, submission["snapshot"]):
            return dict(result, state="stale", reason="source changed or cannot be read")
        receipts = [json.loads(r[0]) for r in db.execute("SELECT data FROM reviews WHERE submission=? ORDER BY rowid", (submission["id"],))]
        if not receipts:
            return dict(result, state="pending", reviewer=submission["reviewer"])
        receipt = receipts[-1]
        return dict(result, state="approved" if receipt["verdict"] == "approve" else receipt["verdict"], receipt=receipt["id"])

    def submit_task(self, actor, task_id, expected, data, *, ack_id=None):
        if data.keys() - {"paths", "evidence", "summary"}:
            raise RoomError("Submission accepts only paths, evidence and summary; the store owns source hashes")
        nonempty_strings(data.get("evidence"), "evidence")
        if not isinstance(data.get("summary"), str) or not data["summary"].strip() or len(data["summary"]) > 4000:
            raise RoomError("Submission requires a nonempty summary of at most 4000 characters")
        with self.tx() as db:
            task = self.record(db, "tasks", task_id)
            if actor != task["owner"]:
                raise RoomError("Only the author/assigned owner can submit their work", "authority")
            if task["version"] != expected or task["state"] not in {"ready", "running", "review", "blocked"}:
                raise RoomError("Read the current unfinished task before submitting", "conflict")
            self.require_ready(db, task)
            snapshot = capture(self.project, data.get("paths"))
            if not any(value is not None for value in snapshot.values()):
                raise RoomError("Submission needs at least one readable source/report file; include a report for deletion-only work")
            submission = {"id": uid("S-"), "task": task_id, "task_version": task["version"],
                          "contract_revision": task["contract_revision"], "author": actor, "reviewer": task["reviewer"],
                          "snapshot": snapshot, "decisions": task["decisions"], "evidence": data["evidence"],
                          "summary": data["summary"], "created": now()}
            submission["previous_submission"] = task["submission"]
            submission["dependencies"] = {key: self.record(db, "tasks", key)["version"] for key in task["dependencies"]}
            submission["attempts"] = [row[0] for row in db.execute("SELECT id FROM attempts WHERE task=? AND member=? ORDER BY rowid DESC LIMIT 20", (task_id, actor))]
            submission["digest"] = digest(submission)
            db.execute("INSERT INTO submissions VALUES (?,?,?)", (submission["id"], task_id, dumps(submission)))
            task.update(submission=submission["id"], state="review", evidence=data["evidence"], last_progress=now())
            self.save(db, "tasks", task, expected)
            db.execute("DELETE FROM claims WHERE task=? AND owner=?", (task_id, actor))
            self.event(db, "task.submitted", {"task": task_id, "submission": submission["id"], "actor": actor})
            if task["reviewer"]:
                self.notify(db, actor, task["reviewer"],
                            f"Review submission {submission['id']} is ready. Inspect the listed files and acceptance; only {task['reviewer']} records its source-bound receipt.",
                            task_id, review_submission=submission["id"])
            result = {"task": task, "submission": submission}
            if ack_id is not None:
                self._acknowledge(db, actor, ack_id,
                                  f"Processed with successful task submission {task_id}.", expected_task=task_id)
                result["processed_message"] = ack_id
            return result

    def record_review(self, actor, submission_id, data, *, ack_id=None):
        if data.keys() - {"source_digest", "verdict", "summary", "findings", "evidence"}:
            raise RoomError("Unsupported review fields")
        nonempty_strings(data.get("evidence"), "review evidence")
        verdict, findings = data.get("verdict"), data.get("findings")
        if verdict not in ("approve", "changes_requested", "blocked"):
            raise RoomError("verdict must be approve, changes_requested or blocked")
        if not isinstance(data.get("summary"), str) or not data["summary"].strip() or len(data["summary"]) > 4000:
            raise RoomError("Review requires a nonempty summary of at most 4000 characters")
        if not isinstance(findings, list) or len(findings) > 100:
            raise RoomError("findings must be an array of at most 100 findings")
        if (verdict == "approve" and findings) or (verdict == "changes_requested" and not findings):
            raise RoomError("Approve needs no unresolved findings; changes_requested needs findings")
        for finding in findings:
            if not isinstance(finding, dict) or finding.keys() - {"summary", "severity", "path", "line"}:
                raise RoomError("Finding accepts summary, severity and optional path/line")
            if not isinstance(finding.get("summary"), str) or not finding["summary"].strip() or len(finding["summary"]) > 4000:
                raise RoomError("Finding requires a bounded nonempty summary")
            if finding.get("severity") not in ("high", "medium", "low"):
                raise RoomError("Finding severity must be high, medium or low")
            if "line" in finding and (type(finding["line"]) is not int or finding["line"] < 1 or "path" not in finding):
                raise RoomError("Finding line requires a path and positive integer")
        with self.tx() as db:
            submission = self.entry(db, "submissions", submission_id)
            task = self.record(db, "tasks", submission["task"])
            if actor == submission["author"] or actor != task["reviewer"] or actor not in MODES[self.get_room(db)["mode"]]:
                raise RoomError("Only the assigned active peer reviewer can record this receipt", "authority")
            if data.get("source_digest") != submission["digest"]:
                raise RoomError("Read and inspect the exact submission digest before recording review", "conflict")
            if task["submission"] != submission_id or task["state"] != "review" or self.review_status(db, task)["state"] == "stale":
                raise RoomError("Submission is stale or task is no longer in review; reconcile and resubmit", "conflict")
            for finding in findings:
                if "path" in finding and (not isinstance(finding["path"], str) or finding["path"] not in submission["snapshot"]):
                    raise RoomError("Finding path must be one of the submitted files")
            old = db.execute("SELECT data FROM reviews WHERE submission=?", (submission_id,)).fetchone()
            if old:
                previous = json.loads(old[0])
                if all(previous.get(key) == data.get(key) for key in ("source_digest", "verdict", "summary", "findings", "evidence")):
                    result = previous
                    if ack_id is not None:
                        self._acknowledge(db, actor, ack_id,
                                          f"Processed with successful review record {submission_id}.",
                                          expected_task=task["id"], allow_identical=True)
                        result = dict(previous, processed_message=ack_id)
                    return result
                raise RoomError("Review receipts are immutable; author must resubmit after reconciling findings", "conflict")
            receipt = dict(data, id=uid("R-"), submission=submission_id, task=task["id"], reviewer=actor, created=now())
            db.execute("INSERT INTO reviews VALUES (?,?,?)", (receipt["id"], submission_id, dumps(receipt)))
            task["last_progress"] = now()
            self.save(db, "tasks", task, task["version"])
            self.event(db, "review.recorded", {"task": task["id"], "review": receipt["id"], "verdict": verdict, "actor": actor})
            for recipient in {task["owner"], self.gateway} - {actor}:
                self.notify(db, actor, recipient, f"Review {receipt['id']}: {verdict}. Read findings and current source before the next action; task completion remains separate.", task["id"])
            if ack_id is not None:
                self._acknowledge(db, actor, ack_id,
                                  f"Processed with successful review record {submission_id}.", expected_task=task["id"])
                return dict(receipt, processed_message=ack_id)
            return receipt

    def checkpoint(self, actor, task_id, expected, data):
        if data.keys() - {"summary", "last_safe_action", "unknown_effects", "next", "paths"}:
            raise RoomError("Unsupported checkpoint fields")
        for key in ("summary", "last_safe_action", "next"):
            if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 4000:
                raise RoomError(f"Checkpoint requires {key}, at most 4000 characters")
        unknown = data.get("unknown_effects", [])
        if unknown:
            nonempty_strings(unknown, "unknown_effects")
        elif not isinstance(unknown, list):
            raise RoomError("unknown_effects must be an array")
        with self.tx() as db:
            task = self.record(db, "tasks", task_id)
            if actor != task["owner"] or task["state"] in {"done", "cancelled"}:
                raise RoomError("Only the current owner can checkpoint unfinished work", "authority")
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM checkpoints WHERE task=?", (task_id,)).fetchone()[0]
            member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (actor,)).fetchone()[0])
            checkpoint = dict(data, id=uid("C-"), task=task_id, sequence=sequence, owner=actor,
                              task_version=task["version"] + 1, contract_revision=task["contract_revision"],
                              generation=self.get_room(db)["generation"], native_id=member["native_id"],
                              decisions=task["decisions"], snapshot=capture(self.project, data.get("paths")),
                              unknown_effects=unknown, created=now())
            checkpoint["digest"] = digest(checkpoint)
            task.update(checkpoint=data["summary"], checkpoint_id=checkpoint["id"], next=data["next"], last_progress=now())
            self.save(db, "tasks", task, expected)
            db.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (checkpoint["id"], task_id, sequence, dumps(checkpoint)))
            self.event(db, "task.checkpoint", {"task": task_id, "checkpoint": checkpoint["id"], "sequence": sequence})
            return checkpoint

    def task_context(self, task_id, compact=False):
        with self.read() as db:
            task = self.record(db, "tasks", task_id)
            review = self.review_status(db, task)
            checkpoint = self.entry(db, "checkpoints", task["checkpoint_id"]) if task.get("checkpoint_id") else None
            reasons = []
            if checkpoint:
                if checkpoint["owner"] != task["owner"]:
                    reasons.append("owner changed; handoff required")
                if checkpoint["summary"] != task["checkpoint"]:
                    reasons.append("legacy checkpoint text changed; reconcile with structured checkpoint")
                if checkpoint["contract_revision"] != task["contract_revision"] or checkpoint["decisions"] != task["decisions"]:
                    reasons.append("task contract/decisions changed")
                if checkpoint["generation"] != self.get_room(db)["generation"]:
                    reasons.append("native generation changed; reconcile before continuing")
                current_member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (task["owner"],)).fetchone()[0])
                if checkpoint["native_id"] != current_member["native_id"]:
                    reasons.append("native session changed; reconcile before continuing")
                if not source_matches(self.project, checkpoint["snapshot"]):
                    reasons.append("source changed or cannot be read")
            attempts = [json.loads(r[0]) for r in db.execute("SELECT data FROM attempts WHERE task=? ORDER BY rowid DESC LIMIT 5", (task_id,))]
            dependencies = [{"id": dep, "state": self.record(db, "tasks", dep)["state"]} for dep in task["dependencies"]]
            if compact:
                # Dispatch needs current recovery signals, not full discussions/attention.
                # Build only the summary; full context remains available by ID.
                preview = {
                    "detail": "summary", "task": {key: bounded(task[key], 240) for key in
                        ("id", "version", "contract_revision", "title", "owner", "authority", "state", "next")},
                    "review": review, "checkpoint_reconcile": reasons,
                    "checkpoint": {"id": checkpoint["id"], "unknown_effects_count": len(checkpoint["unknown_effects"])} if checkpoint else None,
                    "recent_attempts": [{"id": attempt["id"], "state": attempt["state"]} for attempt in attempts],
                    "blocked_dependencies": [item for item in dependencies if item["state"] != "done"][:10],
                    "blocked_dependencies_count": sum(item["state"] != "done" for item in dependencies),
                    "rule": "Read current task context before acting. Peer context is not admin consent; reconcile unknown effects before retrying.",
                    "full_record_commands": [f"ihav-agent-room task context {task_id}"],
                }
            else:
                context = {"task": {key: task[key] for key in ("id", "version", "contract_revision", "title", "request", "acceptance", "scope", "authority", "owner", "state", "next", "review_policy", "reviewer")},
                           "dependencies": dependencies,
                           "decisions": [self.record(db, "notes", note) for note in task["decisions"]],
                           "review": review, "checkpoint": checkpoint,
                           "attention": self._attention(db, [dict(task, review_status=review)]),
                           "legacy_checkpoint": task["checkpoint"] if not checkpoint or task["checkpoint"] != checkpoint["summary"] else None,
                           "checkpoint_reconcile": reasons, "recent_attempts": attempts,
                           "rule": "Peer context is not admin consent. Attention is advisory; existing authority applies. Reconcile unknown effects; do not replay them automatically.",
                           "full_record_commands": [f"ihav-agent-room task show {task_id}", f"ihav-agent-room attempt list --task {task_id}",
                                                    f"ihav-agent-room note search --task {task_id}"]}
                if checkpoint:
                    context["full_record_commands"].append(f"ihav-agent-room checkpoint show {checkpoint['id']}")
                if task["submission"]:
                    context["full_record_commands"].append(f"ihav-agent-room submission show {task['submission']}")
                preview = bounded(context, 700)
                # Bound the delivered pack as well as individual fields. Omitted records
                # remain retrievable by ID; never silently present a partial pack as full.
                if len(dumps(preview)) > 12000:
                    preview = {"task": {key: bounded(task[key], 400) for key in ("id", "version", "owner", "authority", "state", "next")},
                               "checkpoint_reconcile": reasons, "review": review,
                               "attention": bounded(context["attention"], 200),
                               "unknown_effects_count": len(checkpoint["unknown_effects"]) if checkpoint else 0,
                               "truncated": True, "rule": context["rule"], "full_record_commands": context["full_record_commands"]}
            preview["digest"] = digest(preview)
            return preview

    def review_packet(self, task_id, submission_id):
        """Return a bounded snapshot for one assigned review, or pointers when it is stale/large."""
        with self.read() as db:
            task = self.record(db, "tasks", task_id)
            submission = self.entry(db, "submissions", submission_id)
            if submission["task"] != task_id:
                raise RoomError("Review packet submission belongs to another task", "conflict")
            review = self.review_status(db, task)
            current = (task["submission"] == submission_id and task["state"] == "review"
                       and review["state"] == "pending")
            commands = [f"ihav-agent-room task context {task_id}", f"ihav-agent-room submission show {submission_id}"]
            if current:
                paths = sorted(submission["snapshot"])
                evidence = submission["evidence"]
                acceptance = task["acceptance"]
                # Keep omitted source/path/count fields distinct from intentionally shortened evidence previews;
                # the preview flag is informational so routine long claims do not force a full-record read.
                truncated = (len(paths) > 12 or any(len(path) > 80 for path in paths)
                             or len(evidence) > 12 or len(acceptance) > 140)
                packet = {
                    "status": "current", "task": {"id": task_id, "version": task["version"],
                             "owner": task["owner"], "reviewer": task["reviewer"],
                             "acceptance": bounded(acceptance, 140)},
                    "submission": {"id": submission_id, "source_digest": submission["digest"],
                                   "paths": bounded(paths, 80),
                                   "author_evidence_preview": [item[:80] + ("..." if len(item) > 80 else "")
                                                               for item in evidence[:12]],
                                   "author_evidence_preview_truncated": any(len(item) > 80 for item in evidence[:12]),
                                   "evidence_count": len(evidence)},
                }
                if truncated:
                    packet["truncated"] = True
                    packet["full_record_commands"] = commands
            else:
                packet = {
                    "status": "stale", "truncated": True,
                    "task": {"id": task_id, "version": task["version"], "state": task["state"],
                             "reviewer": task["reviewer"]},
                    "submission": {"id": submission_id, "source_digest": submission["digest"]},
                    "full_record_commands": commands,
                }
            packet["digest"] = digest(packet)
            if len(dumps(packet).encode("utf-8")) > MAX_REVIEW_PACKET_BYTES:
                packet = {
                    "status": "truncated", "truncated": True,
                    "task": {"id": task_id, "version": task["version"], "state": task["state"],
                             "reviewer": task["reviewer"]},
                    "submission": {"id": submission_id, "source_digest": submission["digest"]},
                    "full_record_commands": commands,
                }
                packet["digest"] = digest(packet)
            if len(dumps(packet).encode("utf-8")) > MAX_REVIEW_PACKET_BYTES:
                raise RoomError("Bounded review packet exceeds the existing context budget", "invalid")
            return packet

    @staticmethod
    def _note_preview(note, *, terms=()):
        return {**{key: note[key] for key in ("id", "version", "kind", "state", "author")},
                "body_preview": bounded(note["body"], 240, terms=terms),
                "read_command": f"ihav-agent-room note show {note['id']}"}

    def list_notes(self):
        with self.read() as db:
            return [dict(json.loads(row["data"]), id=row["id"], version=row["version"])
                    for row in db.execute("SELECT id,version,data FROM notes ORDER BY rowid")]

    def search_notes(self, query="", *, state="open", author=None, kind=None, task_id=None, after=0, limit=8):
        if after < 0 or not 1 <= limit <= 50:
            raise RoomError("Use after >= 0 and limit 1..50")
        if not isinstance(query, str) or len(query) > 200:
            raise RoomError("Search query must be text, at most 200 characters")
        if state != "all" and not any(state in states for states in NOTE_STATES.values()):
            raise RoomError("Use an existing note state or all")
        if author is not None and author not in MEMBERS:
            raise RoomError("Unknown note author")
        if kind is not None and kind not in NOTE_STATES:
            raise RoomError("Use kind question/proposal/decision")
        terms, items = query.casefold().split(), []
        with self.read() as db:
            if task_id is not None:
                self.record(db, "tasks", task_id)
            for row in db.execute("SELECT rowid AS cursor,id,version,data FROM notes WHERE rowid>? ORDER BY rowid", (after,)):
                note = dict(json.loads(row["data"]), id=row["id"], version=row["version"])
                if ((state != "all" and note["state"] != state) or (author is not None and note["author"] != author)
                        or (kind is not None and note["kind"] != kind)
                        or (task_id is not None and task_id not in note["tasks"])):
                    continue
                if terms and not matches_terms(" ".join(note.get(key, "") for key in ("body", "answer", "condition", "condition_evidence")), terms):
                    continue
                items.append(dict(self._note_preview(note, terms=terms), answer_preview=bounded(note.get("answer", ""), 240, terms=terms), cursor=row["cursor"]))
                if len(items) > limit:
                    break
        return {"items": items[:limit], "next_after": items[limit - 1]["cursor"] if len(items) > limit else None,
                "filters": {"query": query, "state": state, "author": author, "kind": kind, "task": task_id},
                "matching": "All whitespace-separated terms, literal case-insensitive substring in current body, answer and conditions; insertion order.",
                "rule": "Discussion recall is advisory, not a task or permission. Read note show before acting. Keep filters while paging; start each fresh search at after=0 to find revised or reopened notes."}

    def add_note(self, actor, data):
        if actor not in MEMBERS:
            raise RoomError("Unknown member", "identity")
        if "resolution" in data:
            raise RoomError("The room records resolution provenance; do not supply resolution metadata")
        validate_fields(data, ("kind", "body", "state", "condition", "condition_evidence", "source"), ("tasks",))
        kind = data.get("kind")
        if kind not in NOTE_STATES or not data.get("body"):
            raise RoomError("Note requires kind question/proposal/decision and body")
        state = data.get("state", "approved" if kind == "decision" else "open")
        if state not in NOTE_STATES[kind]:
            raise RoomError("Invalid note state")
        with self.tx() as db:
            if kind == "decision" or state != "open" or data.get("source"):
                self.main_only(actor)
                self.source(db, data.get("source"), "note_admin")
            tasks = data.get("tasks", [])
            for task in tasks:
                self.record(db, "tasks", task)
            note = dict(data, id=uid({"question": "Q-", "proposal": "N-", "decision": "D-"}[kind]),
                        version=1, kind=kind, state=state, tasks=tasks, author=actor, created=now(), updated=now())
            db.execute("INSERT INTO notes VALUES (?,?,?)", (note["id"], 1, dumps(note)))
            self._note_changed(db, actor, note)
            self.event(db, "note.revised", note)
            return note

    def resolve_note(self, actor, note_id, expected, data):
        validate_fields(data, ("state", "answer", "source", "condition_evidence", "superseded_by"))
        if data.keys() - {"state", "answer", "source", "condition_evidence", "superseded_by"}:
            raise RoomError("Unsupported note resolution fields")
        with self.tx() as db:
            note = self.record(db, "notes", note_id)
            state = data.get("state", note["state"])
            if state not in NOTE_STATES[note["kind"]]:
                raise RoomError("Invalid resolution state")
            # Author/main may follow up on an ordinary idea. Any admin provenance,
            # approval or existing legacy task binding keeps the admin receipt boundary.
            bound = any(note_id in self.record(db, "tasks", task)["decisions"] for task in note["tasks"])
            admin = (note["kind"] == "decision" or "approved" in {state, note["state"]}
                     or bool(note.get("source")) or bound or "source" in data or "condition_evidence" in data)
            if admin:
                self.main_only(actor)
                self.source(db, data.get("source"), "note_admin")
            elif actor not in MEMBERS or actor not in {note["author"], self.gateway}:
                raise RoomError("Only the author or main may resolve this advisory note; send counterevidence to its author", "authority")
            if not data.get("answer", "").strip():
                raise RoomError("Keep the answer or reason and its scope")
            if data.get("superseded_by"):
                if state != "superseded":
                    raise RoomError("Use state=superseded when naming a replacement note")
                if data["superseded_by"] == note_id:
                    raise RoomError("A note cannot supersede itself")
                self.record(db, "notes", data["superseded_by"])
            note.update(data)
            if state != "superseded":
                note.pop("superseded_by", None)
            note["resolution"] = {"actor": actor, "basis": "admin" if admin else "peer"}
            self.save(db, "notes", note, expected)
            self._note_changed(db, actor, note)
            self.event(db, "note.revised", note)
            return note

    def _note_changed(self, db, actor, note):
        notified = set()
        summary = f"{note['kind']} {note['id']} v{note['version']} is {note['state']}. "
        for task_id in dict.fromkeys(note["tasks"]):
            task = self.record(db, "tasks", task_id)
            # An idea is advisory until main explicitly approves it. Preserve already
            # bound notes (including <=0.2.1 proposals) until an explicit resolution.
            binding = note["kind"] != "question" and (note["state"] == "approved" or note["id"] in task["decisions"])
            if binding:
                task["contract_revision"] += 1
                task["decisions"][note["id"]] = note["version"]
                if note["state"] == "superseded":
                    task["decisions"].pop(note["id"], None)
                self.save(db, "tasks", task, task["version"])
            if task["owner"] != actor:
                instruction = ("Read current decisions and reconcile affected work." if binding else
                               "Read the advisory note; the task contract and authority are unchanged.")
                self.notify(db, actor, task["owner"], summary + instruction, task_id)
                notified.add(task["owner"])
        for recipient in {self.gateway, note["author"]} - notified - {actor}:
            self.notify(db, actor, recipient, summary + f"Read ihav-agent-room note show {note['id']} before acting; this notice grants no authority.")

    def wake_resumed_work(self, generation):
        """Queue current obligations, never replay a previous native attempt."""
        with self.tx() as db:
            room = self.get_room(db)
            if room["generation"] != generation or room["status"] != "starting":
                return
            for row in db.execute("SELECT data FROM tasks").fetchall():
                task = json.loads(row[0])
                recipients = {}
                if task["owner"] != self.gateway and task["state"] in {"ready", "running", "review"}:
                    recipients[task["owner"]] = "Reconcile your checkpoint, current source and decisions before continuing authorized unfinished work."
                if task["state"] == "review" and task.get("reviewer") in MODES[room["mode"]]:
                    review = self.review_status(db, task)
                    if review["state"] == "pending":
                        recipients[task["reviewer"]] = (f"Your review of submission {review['submission']} is still pending. "
                            "Use the review packet, inspect the actual source, and record a receipt only if it is current.")
                for member, body in recipients.items():
                    if member not in MODES[room["mode"]]:
                        continue
                    # A queued event with current task context will already wake this member.
                    # Old-submission or unrelated room messages do not satisfy this obligation.
                    queued = db.execute("SELECT context FROM messages WHERE recipient=? AND task=? AND status='queued'",
                                        (member, task["id"])).fetchall()
                    if any(json.loads(message["context"]).get("task_version") == task["version"] for message in queued):
                        continue
                    self.notify(db, self.gateway, member,
                               "Room resumed. " + body + " Do not replay effects of unknown outcome.", task["id"],
                               broadcast=False,
                               review_submission=review["submission"] if task["state"] == "review" and member == task.get("reviewer") else None)

    def queue(self, db, sender, recipient, body, task_id=None, message_id=None, *, knowledge_id=None, kind=None,
              admin_relay=False, admin_notice=None, broadcast_id=None, broadcast_recipient=None,
              review_submission=None):
        if (sender not in MEMBERS or recipient not in MEMBERS or not isinstance(body, str) or
                (not body.strip() and admin_notice is None)):
            raise RoomError("Message requires a known recipient and nonempty body")
        if len(body) > MAX_MESSAGE_CHARS:
            raise RoomError(f"Keep messages under {MAX_MESSAGE_CHARS} characters; link longer findings")
        message_id = message_id or uid("M-")
        if not isinstance(message_id, str):
            raise RoomError("Message ID must be text")
        expected_kind = "task" if task_id else "peer"
        kind = expected_kind if kind is None else kind
        if kind not in {"peer", "task", "system"} or (kind != "system" and kind != expected_kind):
            raise RoomError("Message kind must match peer/task context or be system")
        old = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if old:
            old_context = json.loads(old["context"])
            old_knowledge = old_context.get("knowledge", {}).get("id")
            old_kind = old_context.get("kind", "task" if old["task"] else "peer")
            old_admin_relay = old_context.get("admin_relay", False)
            old_admin_notice = old_context.get("admin_notice", {})
            old_broadcast = old_context.get("broadcast", {})
            old_review_submission = old_context.get("review_submission")
            wanted_broadcast = ({"id": broadcast_id, "direct_recipient": broadcast_recipient}
                                if broadcast_id else {})
            wanted_admin_notice = admin_notice or {}
            if (old["sender"], old["recipient"], old["task"], old["body"], old_knowledge, old_kind,
                old_admin_relay, old_admin_notice, old_broadcast, old_review_submission) != (sender, recipient, task_id, body,
                    knowledge_id, kind, admin_relay, wanted_admin_notice, wanted_broadcast, review_submission):
                raise RoomError("Message ID reused with different content", "conflict")
            return dict(old)
        try:
            message_id_bytes = message_id.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RoomError(f"Message ID must be an ASCII token of at most {MAX_MESSAGE_ID_BYTES} bytes") from exc
        if (not message_id_bytes or len(message_id_bytes) > MAX_MESSAGE_ID_BYTES or
                not all(char.isalnum() or char in "._-" for char in message_id)):
            raise RoomError(f"Message ID must use letters, digits, dot, underscore or hyphen (max {MAX_MESSAGE_ID_BYTES} bytes)")
        validated_admin_notice = None
        if admin_notice is not None:
            if (not admin_relay or task_id is not None or knowledge_id is not None or broadcast_id is not None or
                    review_submission is not None or not isinstance(admin_notice, dict) or
                    set(admin_notice) != {"receipt", "provenance", "truncated", "original_chars"}):
                raise RoomError("Admin notice metadata requires an admin relay")
            receipt = admin_notice.get("receipt")
            provenance = admin_notice.get("provenance")
            truncated = admin_notice.get("truncated")
            original_chars = admin_notice.get("original_chars")
            if (not isinstance(receipt, str) or not receipt or not receipt.isascii() or len(receipt) > MAX_MESSAGE_ID_BYTES or
                    not isinstance(provenance, str) or provenance not in ADMIN_NOTICE_PROVENANCE or
                    type(truncated) is not bool or type(original_chars) is not int or original_chars < len(body) or
                    truncated != (original_chars > MAX_MESSAGE_CHARS) or len(body) != min(original_chars, MAX_MESSAGE_CHARS)):
                raise RoomError("Admin notice metadata does not match its copied prompt")
            validated_admin_notice = dict(admin_notice)
        context = {}
        if task_id:
            task = self.record(db, "tasks", task_id)
            context = {"task_version": task["version"], "decisions": task["decisions"]}
        if review_submission is not None:
            submission = self.entry(db, "submissions", review_submission)
            if not task_id or submission["task"] != task_id:
                raise RoomError("Review request must reference a submission for its task", "conflict")
            context["review_submission"] = review_submission
        if knowledge_id is not None:
            knowledge = self.record(db, "knowledge", knowledge_id)
            context["knowledge"] = {"id": knowledge["id"], "version": knowledge["version"]}
        if admin_relay:
            context["admin_relay"] = True
        if validated_admin_notice is not None:
            context["admin_notice"] = validated_admin_notice
        if broadcast_id:
            if broadcast_recipient not in MEMBERS:
                raise RoomError("Broadcast copy requires a direct room recipient")
            context["broadcast"] = {"id": broadcast_id, "direct_recipient": broadcast_recipient}
        if kind == "system":
            context["kind"] = kind
        db.execute("INSERT INTO messages(id,sender,recipient,task,body,context,status,created) VALUES (?,?,?,?,?,?,?,?)",
                   (message_id, sender, recipient, task_id, body, dumps(context), "queued", now()))
        return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def queue_global_entries(self, ledger_id, room_id, entries, *, initial_cursor=0, scanned_seq=None):
        """Import data into this room's gateway queue; no fanout, task, receipt or native operation.

        Stable per-ledger/per-entry IDs, messages and scan progress commit together.
        Sender is the local gateway generating a system notice; actual origin stays
        in global_entry metadata. Already dispatched notices are never replayed.
        """
        if not isinstance(ledger_id, str) or not ledger_id or not isinstance(entries, list) or len(entries) > 20:
            raise RoomError("Invalid bounded global queue batch", "invalid")
        cursor_key = "agents_space.queue_cursor:" + ledger_id
        results = []
        with self.tx(timeout=0.05) as db:
            room = self.get_room(db)
            if room["id"] != room_id or room["project"] != str(self.project):
                raise RoomError("Global directory points to another room; no queue written", "conflict")
            gateway = room_gateway(room)
            old_cursor = db.execute("SELECT value FROM meta WHERE key=?", (cursor_key,)).fetchone()
            cursor = int(old_cursor[0]) if old_cursor else max(0, int(initial_cursor))
            # A handoff can happen after the scan cursor advanced. Retarget only
            # unsent notices from this global ledger, even when this scan is empty.
            pending = db.execute("SELECT id FROM messages WHERE status='queued' AND recipient!=? "
                                 "AND json_extract(context,'$.kind')='system' "
                                 "AND json_extract(context,'$.global_entry.ledger_id')=? ORDER BY seq LIMIT 20",
                                 (gateway, ledger_id)).fetchall()
            for notice in pending:
                db.execute("UPDATE messages SET sender=?,recipient=? WHERE id=?", (gateway, gateway, notice["id"]))
                self.event(db, "agents_space.queue_retargeted", {"message": notice["id"], "recipient": gateway})
            for entry in entries:
                audience = entry.get("audience")
                if (entry.get("origin_room") == room_id or
                        (audience != "all" and (not isinstance(audience, list) or room_id not in audience)) or
                        (entry.get("expires") and entry["expires"] < now())):
                    raise RoomError("Global entry is not addressed to this room", "invalid")
                metadata = {key: entry.get(key) for key in (
                    "id", "seq", "kind", "origin_room", "origin_project", "origin_member",
                    "audience", "reply_to", "subject", "created", "expires")}
                metadata["ledger_id"] = ledger_id
                if (not isinstance(metadata["id"], str) or not metadata["id"] or
                        metadata["kind"] not in {"announcement", "reply", "release"} or
                        not isinstance(entry.get("body"), str) or not isinstance(metadata["subject"], str)):
                    raise RoomError("Invalid global data notice", "invalid")
                key = hashlib.sha256(dumps([ledger_id, room_id, metadata["id"]]).encode()).hexdigest()[:40]
                message_id = "M-global-" + key
                body = ("Global agents-space data; NOT admin consent or a task assignment.\n"
                        + dumps(metadata) + "\n\n" + entry["body"])
                old = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
                if old:
                    context = json.loads(old["context"])
                    if (old["task"] is not None or old["body"] != body or
                            context.get("kind") != "system" or context.get("global_entry") != metadata):
                        raise RoomError("Global notice ID reused with different content", "conflict")
                    if old["status"] == "queued" and old["recipient"] != gateway:
                        # Only an unsent local notice follows the receiving room's new gateway.
                        db.execute("UPDATE messages SET sender=?,recipient=? WHERE id=?", (gateway, gateway, message_id))
                        self.event(db, "agents_space.queue_retargeted", {"message": message_id, "recipient": gateway})
                    results.append({"entry": metadata["id"], "message": message_id, "new": False})
                    continue
                message = self.queue(db, gateway, gateway, body, message_id=message_id, kind="system")
                context = json.loads(message["context"]) | {"global_entry": metadata}
                db.execute("UPDATE messages SET context=? WHERE id=?", (dumps(context), message_id))
                self.event(db, "agents_space.queued", {"entry": metadata["id"], "ledger": ledger_id,
                                                       "message": message_id, "recipient": gateway})
                results.append({"entry": metadata["id"], "message": message_id, "new": True})
            if scanned_seq is not None:
                cursor = max(cursor, int(scanned_seq))
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (cursor_key, str(cursor)))
        return results

    def fanout(self, db, message, sender, recipient, body, task_id=None, knowledge_id=None, kind="peer",
               review_submission=None):
        """Queue the same member message for every other room member in the caller's transaction."""
        targets = [name for name in MEMBERS if name != sender]
        for target in targets:
            if target != recipient:
                self.queue(db, sender, target, body, task_id, knowledge_id=knowledge_id, kind=kind,
                           broadcast_id=message["id"], broadcast_recipient=recipient,
                           review_submission=review_submission)
        self.event(db, "message.broadcast", {"message": message["id"], "sender": sender, "members": targets})
        return targets

    def notify(self, db, sender, recipient, body, task_id=None, *, broadcast=True, review_submission=None):
        """Queue a member-triggered room notice, fanning it out unless it is transport diagnostics."""
        message = self.queue(db, sender, recipient, body, task_id, kind="system",
                             review_submission=review_submission)
        if broadcast:
            self.fanout(db, message, sender, recipient, body, task_id, kind="system",
                        review_submission=review_submission)
        return message

    def notice(self, sender, recipient, body):
        with self.tx() as db:
            return self.notify(db, sender, recipient, body, broadcast=False)

    def send(self, actor, recipient, body, task_id=None, message_id=None, *, knowledge_id=None):
        with self.tx() as db:
            existing = bool(message_id and db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone())
            message = self.queue(db, actor, recipient, body, task_id, message_id, knowledge_id=knowledge_id)
            if not existing:
                self.fanout(db, message, actor, recipient, body, task_id, knowledge_id, "task" if task_id else "peer")
            return message

    def broadcast_gateway_prompt(self, body, key, *, receipt_id, provenance_state):
        """Queue gateway prompt text to every non-gateway member; its content never grants worker authority."""
        self.main_only(self.gateway)
        if not isinstance(key, str) or not key or not isinstance(receipt_id, str) or not receipt_id:
            raise RoomError("Admin notification requires a stable prompt key")
        if (not isinstance(body, str) or not body.strip() or not isinstance(provenance_state, str) or
                provenance_state not in ADMIN_NOTICE_PROVENANCE):
            raise RoomError("Admin notification requires prompt text and its observed provenance state")
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        targets = [name for name in MEMBERS if name != self.gateway]
        copied_body = body[:MAX_MESSAGE_CHARS]
        prompt_frame = frame_for_prompt(load_frame(self.project), body)
        admin_notice = {"receipt": receipt_id, "provenance": provenance_state,
                        "truncated": len(body) > MAX_MESSAGE_CHARS, "original_chars": len(body)}
        with self.tx() as db:
            snapshots = []
            for target in targets:
                child = hashlib.sha256(f"{key_hash}\0{target}".encode("utf-8")).hexdigest()[:36]
                old = db.execute("SELECT context FROM messages WHERE id=?", ("M-" + child,)).fetchone()
                if old:
                    snapshots.append(json.loads(old["context"]).get("prompt_frame"))
            if snapshots:
                if any(snapshot != snapshots[0] for snapshot in snapshots):
                    raise RoomError("Admin notice frame snapshots conflict", "conflict")
                prompt_frame = frame_for_prompt(snapshots[0], body)
            inserted = False
            for target in targets:
                child = hashlib.sha256(f"{key_hash}\0{target}".encode("utf-8")).hexdigest()[:36]
                message_id = "M-" + child
                old = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
                if old:
                    old_context = json.loads(old["context"])
                    old_notice = old_context.get("admin_notice")
                    if ((old["sender"], old["recipient"], old["task"], old["body"],
                         old_context.get("admin_relay", False)) != (self.gateway, target, None, copied_body, True) or
                            (old_notice and (old_notice.get("truncated"), old_notice.get("original_chars")) !=
                             (admin_notice["truncated"], admin_notice["original_chars"]))):
                        raise RoomError("Message ID reused with different content", "conflict")
                    # A repeated hook may produce a fresh receipt after the host writes its transcript row.
                    # Keep the first notice's receipt/provenance for this stable prompt key; never rewrite a sent copy.
                    continue
                message = self.queue(db, self.gateway, target, copied_body, message_id=message_id, admin_relay=True,
                                     admin_notice=admin_notice)
                if prompt_frame is not None:
                    # Copy context only. Original body, receipt and observed provenance stay unchanged.
                    context = json.loads(message["context"]) | {"prompt_frame": prompt_frame}
                    db.execute("UPDATE messages SET context=? WHERE id=?", (dumps(context), message_id))
                inserted = True
            if inserted:
                self.event(db, "gateway.message.broadcast", {"key_hash": key_hash, "members": targets})
            room = self.get_room(db)
            status = room["status"]
            member_status = {name: json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])["status"]
                             for name in targets}
            eligible = [name for name in targets if name in MODES[room["mode"]]]
        return {"members": targets, "room_status": status, "member_status": member_status,
                "eligible_members": eligible, "prompt_frame": prompt_frame}

    def knowledge_reference(self, db, context):
        """Compare a queued lesson reference with its current revision in this read."""
        context = json.loads(context) if isinstance(context, str) else context
        reference = context.get("knowledge")
        if not reference:
            return None
        record = self.record(db, "knowledge", reference["id"])
        return {"id": record["id"], "queued_version": reference["version"], "current_version": record["version"],
                "changed": record["version"] != reference["version"], "state": record["state"],
                "basis": record["basis"], "title": bounded(record["title"], 160),
                "read_command": f"ihav-agent-room knowledge show {record['id']}",
                "history_command": f"ihav-agent-room knowledge history {record['id']}"}

    def inbox(self, actor, after=0, limit=50, *, pending=False, compact=False):
        if after < 0 or not 1 <= limit <= 200:
            raise RoomError("Use after >= 0 and limit 1..200")
        # Successful FYI deliveries remain in history without creating ACK work; failed ones are reported separately.
        selection = f" AND {ACTIONABLE_PENDING_SQL}" if pending else ""
        with self.read() as db:
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM messages WHERE recipient=? AND seq>?{selection} ORDER BY seq LIMIT ?", (actor, after, limit+1))]
            for row in rows:
                row["context"] = json.loads(row["context"])
                row["stale"] = bool(row["task"] and self.record(db, "tasks", row["task"])["version"] != row["context"].get("task_version"))
                reference = self.knowledge_reference(db, row["context"])
                if reference:
                    row["knowledge_reference"] = reference
                if compact:
                    for field in ("body", "detail"):
                        row[field + "_preview"] = bounded(row.pop(field))
                    # A processing ACK must not make the full-message pointer skip this row.
                    row["read_command"] = f"ihav-agent-room inbox --after {row['seq'] - 1} --limit 1"
            return {"items": rows[:limit], "next_after": rows[limit-1]["seq"] if len(rows) > limit else None}

    def _acknowledge(self, db, actor, message_id, evidence, *, expected_task=None, allow_identical=False):
        if not evidence.strip():
            raise RoomError("Describe what was processed, not merely transport acceptance")
        row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row or row["recipient"] != actor:
            raise RoomError("Only the recipient may acknowledge this message", "authority")
        if expected_task is not None:
            if row["task"] != expected_task:
                raise RoomError("Combined ACK must be for the same task", "conflict")
            if row["status"] == "processed":
                if allow_identical and row["detail"] == evidence:
                    return
                raise RoomError("Combined ACK message was already processed", "conflict")
            actionable = db.execute(
                f"SELECT 1 FROM messages WHERE id=? AND recipient=? AND {ACTIONABLE_PENDING_SQL}",
                (message_id, actor)).fetchone()
            if not actionable:
                raise RoomError("Combined ACK requires an unprocessed actionable message", "conflict")
        db.execute("UPDATE messages SET status='processed', detail=? WHERE id=?", (evidence, message_id))
        for attempt_row in db.execute("SELECT id,data FROM attempts WHERE message=?", (message_id,)).fetchall():
            attempt = json.loads(attempt_row["data"])
            attempt.update(processed=now(), processing_evidence=evidence)
            self.save_attempt(db, attempt)

    def acknowledge(self, actor, message_id, evidence):
        with self.tx() as db:
            self._acknowledge(db, actor, message_id, evidence)

    @staticmethod
    def save_attempt(db, attempt):
        attempt["updated"] = now()
        db.execute("UPDATE attempts SET data=? WHERE id=?", (dumps(attempt), attempt["id"]))

    def begin_attempt(self, message, generation):
        with self.tx() as db:
            room = self.get_room(db)
            if room["generation"] != generation or room["status"] not in {"starting", "running"}:
                return None
            changed = db.execute("UPDATE messages SET status='dispatching' WHERE id=? AND status='queued'", (message["id"],)).rowcount
            if not changed:
                return None
            member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (message["recipient"],)).fetchone()[0])
            task = self.record(db, "tasks", message["task"]) if message["task"] else None
            attempt = {"id": uid("E-"), "message": message["id"], "task": message["task"], "member": message["recipient"],
                       "generation": generation, "native_id": member["native_id"], "turn_id": None,
                       "task_version": message.get("context_pack", {}).get("task", {}).get("version", task["version"] if task else None),
                       "context_digest": message.get("context_pack", {}).get("digest"), "state": "dispatching", "outputs": [],
                       "output_state": "not_observed", "processed": None, "created": now(), "updated": now()}
            reference = self.knowledge_reference(db, message["context"])
            if reference:
                attempt["knowledge_reference"] = reference
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)", (attempt["id"], message["id"], message["task"], message["recipient"], generation, dumps(attempt)))
            return attempt

    def finish_dispatch(self, attempt_id, state, detail, turn_id=None):
        with self.tx() as db:
            attempt = self.entry(db, "attempts", attempt_id)
            attempt.update(state=state, detail=detail, turn_id=turn_id)
            self.save_attempt(db, attempt)
            # A model can ACK before the RPC response arrives; retain that processing receipt.
            db.execute("UPDATE messages SET status=?,detail=? WHERE id=? AND status='dispatching'", (state, detail, attempt["message"]))

    def activity_report(self):
        """Read-only activity counters per member. They are not proof that a member woke, read or processed anything.

        Enqueued messages, dispatch attempts/results, and processing ACKs are reported separately. Token use is not
        available from this ledger.
        """
        with self.read() as db:
            report = {}
            for name in MEMBERS:
                message_states = {row["status"]: row["count"] for row in db.execute(
                    "SELECT status, count(*) AS count FROM messages WHERE recipient=? GROUP BY status", (name,))}
                attempt_states = {}
                for row in db.execute("SELECT data FROM attempts WHERE member=?", (name,)):
                    state = json.loads(row[0]).get("state", "unknown")
                    attempt_states[state] = attempt_states.get(state, 0) + 1
                fanouts = sum(name in json.loads(row["data"]).get("members", []) for row in db.execute(
                    "SELECT data FROM events WHERE kind IN ('message.broadcast','gateway.message.broadcast')"))
                report[name] = {"messages_enqueued": sum(message_states.values()), "messages_by_status": message_states,
                                "dispatch_attempts": sum(attempt_states.values()), "attempts_by_result": attempt_states,
                                "processed_acks": message_states.get("processed", 0), "broadcasts_enqueued": fanouts}
            return report

    def incomplete_notification_deliveries(self, db):
        """Correlate unavailable FYI recipients with the member message or admin receipt that notified them."""
        reports = []
        member_rows = db.execute(
            "SELECT e.data AS event, m.recipient, m.status "
            "FROM events e "
            "LEFT JOIN messages origin ON origin.id=json_extract(e.data,'$.message') "
            "LEFT JOIN messages m ON m.id=origin.id OR "
            "json_extract(m.context,'$.broadcast.id')=json_extract(e.data,'$.message') "
            "WHERE e.kind='message.broadcast' ORDER BY e.seq,m.seq")
        current = None
        statuses = {}
        for row in member_rows:
            data = json.loads(row["event"])
            message_id = data["message"]
            if current is not None and current["message"] != message_id:
                reports.extend(self._incomplete_member_fanout(current, statuses))
                statuses = {}
            current = data
            if row["recipient"] is not None:
                statuses[row["recipient"]] = row["status"]
        if current is not None:
            reports.extend(self._incomplete_member_fanout(current, statuses))

        admin_groups = {}
        relay_rows = db.execute(
            "SELECT id,recipient,status,context FROM messages "
            "WHERE json_extract(context,'$.admin_relay')=1 ORDER BY seq")
        for row in relay_rows:
            context = json.loads(row["context"])
            notice = context.get("admin_notice")
            if notice:
                key = ("admin", notice["receipt"])
                group = admin_groups.setdefault(key, {"receipt": notice["receipt"], "incomplete_members": {}})
                if row["status"] not in DELIVERED_STATUSES:
                    group["incomplete_members"][row["recipient"]] = row["status"]
            elif row["status"] not in DELIVERED_STATUSES:
                reports.append({"message": row["id"],
                                "incomplete_members": {row["recipient"]: row["status"]}})
        reports.extend(group for group in admin_groups.values() if group["incomplete_members"])
        return reports

    @staticmethod
    def _incomplete_member_fanout(event, statuses):
        incomplete = {member: statuses.get(member, "missing") for member in event["members"]
                      if statuses.get(member) not in DELIVERED_STATUSES}
        return ([{"message": event["message"], "incomplete_members": incomplete}] if incomplete else [])

    def observe_peer_prompt(self, actor, session, message_id, sender):
        """Record prompt-text evidence for a bound session; source text does not prove peer origin."""
        if actor not in MEMBERS or not session:
            return False
        if acting_member() != actor:
            return False
        with self.tx() as db:
            room = self.get_room(db)
            if actor == self.gateway:
                owner = room.get("owner") or {}
                bound = (owner.get("session") == session and owner.get("host", "claude") == main_host()
                         and not os.environ.get("IHAV_AGENT_ROOM_BINDING"))
            else:
                member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (actor,)).fetchone()[0])
                token = os.environ.get("IHAV_AGENT_ROOM_BINDING", "")
                token_matches = bool(token and member.get("token_hash") == hashlib.sha256(token.encode()).hexdigest())
                bound = (token_matches and actor in MODES[room["mode"]] and member.get("native_id") == session
                         and room["status"] in {"starting", "running", "stopping"})
            if not bound:
                return False

            message = db.execute("SELECT * FROM messages WHERE id=? AND recipient=? AND sender=?",
                                 (message_id, actor, sender)).fetchone()
            if not message:
                return False
            if message["status"] not in {"dispatching", "submitted", "accepted", "unknown", "failed", "processed"}:
                return False
            attempt_row = db.execute(
                "SELECT id,data FROM attempts WHERE message=? AND member=? ORDER BY rowid DESC LIMIT 1",
                (message_id, actor)).fetchone()
            if not attempt_row:
                return False
            attempt = json.loads(attempt_row["data"])
            if attempt.get("prompt_observed_at"):
                return True
            attempt.update(prompt_observed_at=now(), prompt_observation_basis="UserPromptSubmit.prompt_text")
            self.save_attempt(db, attempt)
            self.event(db, "native.prompt_envelope_observed", {"member": actor, "session": session,
                       "message": message_id, "sender": sender, "basis": "UserPromptSubmit.prompt_text"})
            return True

    def attempt_event(self, member, generation, method, params):
        with self.tx() as db:
            candidates = [json.loads(r[0]) for r in db.execute("SELECT data FROM attempts WHERE member=? AND generation=?", (member, generation))]
            turn_id = params.get("turnId") or params.get("turn", {}).get("id")
            if not turn_id and method == "item/completed":
                active = {a["turn_id"] for a in candidates if a["turn_id"] and a["state"] in {"accepted", "running", "output_received"}}
                if len(active) == 1:
                    turn_id = active.pop()
            matches = [a for a in candidates if turn_id and a["turn_id"] == turn_id]
            if params.get("threadId"):
                matches = [a for a in matches if a["native_id"] == params["threadId"]]
            event_id = self.event(db, "native.attempt_event", {"member": member, "generation": generation,
                                  "method": method, "params": params, "attempts": [a["id"] for a in matches]})
            for attempt in matches:
                if method == "turn/started" and attempt["state"] == "accepted":
                    attempt["state"] = "running"
                elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                    text = params["item"].get("text", "")
                    nonempty = isinstance(text, str) and bool(text.strip())
                    attempt["outputs"].append({"event_seq": event_id, "item_id": params["item"].get("id"), "nonempty": nonempty})
                    if nonempty or attempt["output_state"] != "received":
                        attempt["output_state"] = "received" if nonempty else "empty"
                    if nonempty:
                        attempt["progress_at"] = now()
                    if attempt["state"] in {"accepted", "running"}:
                        attempt["state"] = "output_received"
                elif method == "turn/completed":
                    status = params["turn"].get("status")
                    attempt["state"] = status if status in {"completed", "failed", "interrupted"} else "unknown"
                    attempt["native_error"] = params["turn"].get("error")
                    attempt["ended"] = now()
                self.save_attempt(db, attempt)

    def interrupt_attempts(self, reason, member=None, generation=None):
        with self.tx() as db:
            for row in db.execute("SELECT data FROM attempts").fetchall():
                attempt = json.loads(row[0])
                if (member and attempt["member"] != member) or (generation and attempt["generation"] != generation):
                    continue
                if attempt["state"] in {"dispatching", "accepted", "running", "output_received", "submitted"}:
                    # ACK evidence survives; it does not prove the native turn completed.
                    attempt.update(state="unknown", detail=reason, ended=now())
                    self.save_attempt(db, attempt)

    def attempts(self, task_id=None, after=0, limit=50):
        if after < 0 or not 1 <= limit <= 200:
            raise RoomError("Use after >= 0 and limit 1..200")
        with self.read() as db:
            rows = db.execute("SELECT rowid,data FROM attempts WHERE rowid>? AND (? IS NULL OR task=?) ORDER BY rowid LIMIT ?",
                              (after, task_id, task_id, limit + 1)).fetchall()
            return {"items": [dict(json.loads(r["data"]), cursor=r["rowid"]) for r in rows[:limit]],
                    "next_after": rows[limit - 1]["rowid"] if len(rows) > limit else None}

    def _attention(self, db, tasks):
        """Derive advisory work from the caller's read snapshot; never enqueue or mutate."""
        view = {"advisory": True, "by_member": {member: [] for member in MEMBERS}}
        for task in tasks:
            if task["state"] in {"done", "cancelled"}:
                continue
            recipient = task["owner"]
            reason = "blocked_task" if task["state"] == "blocked" else "unfinished_task"
            episode = f"contract:{task['contract_revision']}"
            review = task["review_status"]
            commands = [f"ihav-agent-room task context {task['id']}"]
            if task["state"] == "review" and review.get("submission"):
                episode = review["submission"]
                reason = "review_" + review["state"]
                commands.append(f"ihav-agent-room submission show {review['submission']}")
                if review["state"] == "pending":
                    recipient, reason = review["reviewer"], "pending_review"
                elif review["state"] == "approved":
                    # Peer-required completion belongs to main, even for a worker-owned task.
                    recipient = self.gateway
                if review.get("receipt"):
                    commands.append(f"ihav-agent-room review show {review['receipt']}")
            blockers = []
            if task["state"] == "blocked":
                blockers.append({"kind": "task", "id": task["id"],
                                 "reason": bounded(task.get("blocked_reason") or "Task is marked blocked; read current context.", 300)})
            for dependency in dict.fromkeys(task["dependencies"]):
                dep = self.record(db, "tasks", dependency)
                if dep["state"] != "done":
                    blockers.append({"kind": "dependency", "id": dependency, "state": dep["state"],
                                     "reason": "dependency_incomplete", "read_command": f"ihav-agent-room task show {dependency}"})
            for note_id, revision in task["decisions"].items():
                note = self.record(db, "notes", note_id)
                note_reason = None
                if note["version"] != revision:
                    note_reason = "decision_changed"
                elif note["state"] != "approved":
                    note_reason = "decision_unapproved"
                elif note.get("condition") and not note.get("condition_evidence"):
                    note_reason = "condition_pending"
                if note_reason:
                    blockers.append({"kind": "decision", "id": note_id, "state": note["state"],
                                     "reason": note_reason, "condition": bounded(note.get("condition", ""), 300),
                                     "read_command": f"ihav-agent-room note show {note_id}"})
            item = {"task": task["id"], "title": bounded(task["title"], 300), "state": task["state"],
                    "reason": reason, "episode": episode, "blockers": blockers, "read_commands": commands}
            if task["state"] == "review":
                item["review"] = review
            view["by_member"][recipient].append(item)
        return view

    def status(self, *, compact=False):
        with self.read() as db:
            room = self.get_room(db)
            tasks = [dict(json.loads(r["data"]), id=r["id"], version=r["version"])
                     for r in db.execute("SELECT id,version,data FROM tasks ORDER BY rowid")]
            # Closed records still contribute to counts. Compact reads need no review
            # hashing or attempt details for them; full historical inspection still does.
            visible = {t["id"]: t for t in tasks if not compact or t["state"] not in {"done", "cancelled"}}
            for task in visible.values():
                task["review_status"] = self.review_status(db, task)
                task["latest_attempt"] = None
                task["missing_evidence"] = not bool(task["evidence"])
                task["unprocessed_messages"] = 0
            # One scan supplies both global counts (including taskless peer work) and
            # task details, within the same read snapshot. Nothing is cached or ACKed.
            attempt_counts, message_counts = Counter(), Counter()
            pending_inboxes = {member: Counter() for member in MEMBERS}
            for row in db.execute("SELECT task,data FROM attempts ORDER BY rowid"):
                attempt = json.loads(row["data"])
                attempt_counts[attempt["state"]] += 1
                task = visible.get(row["task"])
                if task is not None:
                    task["latest_attempt"] = attempt
                    if attempt.get("progress_at"):
                        task["last_progress"] = max(task["last_progress"], attempt["progress_at"])
            for row in db.execute(
                    f"SELECT task,recipient,status,count(*) AS count, "
                    f"sum(CASE WHEN {ACTIONABLE_PENDING_SQL} THEN 1 ELSE 0 END) AS pending_count "
                    "FROM messages GROUP BY task,recipient,status"):
                message_counts[row["status"]] += row["count"]
                task = visible.get(row["task"])
                if row["pending_count"]:
                    if task is not None:
                        task["unprocessed_messages"] += row["pending_count"]
                    pending_inboxes[row["recipient"]][row["status"]] += row["pending_count"]
            incomplete_notifications = self.incomplete_notification_deliveries(db)
            members = []
            for row in db.execute("SELECT data FROM members"):
                member = json.loads(row[0])
                profile = ROSTER_BY_NAME[member["name"]]
                settings = mode_settings(room["mode"], member["name"])
                member.setdefault("requested_model", settings["model"])
                member.setdefault("requested_effort", settings["effort"])
                member.setdefault("model_label", settings["label"])
                member.setdefault("observed_model", None)
                member.setdefault("observed_effort", None)
                member.setdefault("model_observed_at", None)
                member.setdefault("settings_application", (
                    "host-managed" if member["name"] == self.gateway else
                    "existing session; settings application unknown" if member.get("native_id") else
                    "configured; not started"))
                members.append(member)
            gateway = next(member for member in members if member["name"] == self.gateway)
            result = {"room": {**room, "gateway": room_gateway(room)},
                    "members": members,

                    "tasks": tasks,
                    "attention": self._attention(db, tasks),
                    "pending_inboxes": {
                        "advisory": True,
                        "by_member": {
                            member: {"count": sum(statuses.values()), "statuses": dict(statuses)}
                            for member, statuses in pending_inboxes.items() if statuses
                        },
                        "read_command": "ihav-agent-room inbox --pending --after 0",
                    },
                    "incomplete_notifications": incomplete_notifications,
                    "incomplete_notification_count": len(incomplete_notifications),
                    "incomplete_notifications_by_member": dict(Counter(
                        member for notice in incomplete_notifications for member in notice["incomplete_members"])),
                    "incomplete_notifications_truncated": False,
                    "notes": [dict(json.loads(r["data"]), id=r["id"], version=r["version"])
                              for r in db.execute("SELECT id,version,data FROM notes ORDER BY rowid")],
                    "unaccounted_prompts": [dict(r) for r in db.execute("SELECT * FROM prompts WHERE accounted IS NULL")],
                    "claims": [dict(r) for r in db.execute("SELECT * FROM claims")],
                    "approvals": [json.loads(r[0]) for r in db.execute("SELECT data FROM approvals")],
                    "attempt_counts": dict(attempt_counts), "message_counts": dict(message_counts)}
            warning = self.gateway_settings_warning(room, gateway)
            if warning:
                result["gateway_settings_warning"] = warning
            return self._compact_status(result) if compact else result

    @staticmethod
    def _compact_status(status):
        """One read-only projection. Full records and authority stay in the ledger."""
        tasks, notes = status["tasks"], status["notes"]
        status["task_counts"] = dict(Counter(t["state"] for t in tasks))
        status["note_counts"] = dict(Counter(n["state"] for n in notes))
        status["tasks"] = []
        for task in tasks:
            if task["state"] in {"done", "cancelled"}:
                continue
            item = {key: task[key] for key in ("id", "version", "state", "owner", "review_status",
                                              "last_progress", "missing_evidence", "unprocessed_messages")}
            item.update(title=bounded(task["title"], 160), next=bounded(task["next"], 240),
                        read_command=f"ihav-agent-room task context {task['id']}", latest_attempt=None)
            attempt = task["latest_attempt"]
            if attempt:
                item["latest_attempt"] = {key: attempt.get(key) for key in ("id", "state", "processed", "turn_id")}
                item["latest_attempt"]["read_command"] = f"ihav-agent-room attempt show {attempt['id']}"
            status["tasks"].append(item)
        status["notes"] = [Store._note_preview(note) for note in notes if note["state"] in {"open", "approved"}]
        model_fields = ("requested_model", "requested_effort", "model_label", "settings_application",
                        "observed_model", "observed_effort", "model_observation_source", "model_observed_at",
                        "effort_source", "effort_observed_at", "settings_pending_restart")
        for member in status["members"]:
            for field in model_fields:
                member.pop(field, None)
        # Keep one concrete example; counts cover the complete set and full status has every ID/state.
        status["incomplete_notifications_truncated"] = len(status["incomplete_notifications"]) > 1
        status["incomplete_notifications"] = status["incomplete_notifications"][:1]
        status["detail"] = {"mode": "compact", "read_all_tasks": "ihav-agent-room task list --all",
                            "read_all_notes": "ihav-agent-room note list",
                            "read_models": "ihav-agent-room status",
                            "read_incomplete_notifications": "ihav-agent-room status",
                            "rule": "Previews need full records before acting."}
        # Keep unaccounted admin prompts, native approvals, claims and attention intact.
        return status

    def project_views(self):
        # Serialize capture + writes, so an older projection cannot replace a newer one.
        with file_lock(self.runtime / "projection.lock"):
            status = self.status()
            header = "<!-- ihav-agent-room generated; update through ihav-agent-room CLI -->\n"
            tasks = [header, "# Active tasks\n"]
            for task in status["tasks"]:
                if task["state"] not in {"done", "cancelled"}:
                    tasks.append(f"## {task['id']} — {task['title']}\n\nState: {task['state']}; owner: {task['owner']}; revision: {task['version']}\n\nReview: {task['review_status']['state']}; submission: {task['submission']}\n\nNext: {task['next']}\n\nCheckpoint: {task['checkpoint']}\n\nLast progress: {task['last_progress']}\n")
            tasks.append("\nUse ihav-agent-room task list --all for complete history.\n")
            notes = [header, "# Decisions and open questions\n"]
            for note in status["notes"]:
                if note["state"] not in {"superseded", "rejected"}:
                    notes.append(f"## {note['id']} — {note['state']}\n\n{note['body']}\n\nAnswer: {note.get('answer', '')}\n\nSource: {note.get('source', 'peer proposal; no admin approval')}\n\nCondition: {note.get('condition', '')}\n")
            for relative, content in (("tasks/active.md", tasks), ("state/current_decisions.md", notes)):
                path = self.space / relative
                # Rooms created before 0.4.0 carry the Agent Room header; the next projection rewrites it.
                if path.exists() and not path.read_text().startswith((header, LEGACY_PROJECTION_HEADER)):
                    raise RoomError(f"Projection conflict; preserve and reconcile {path}", "conflict")
                atomic_write(path, "\n".join(content))
