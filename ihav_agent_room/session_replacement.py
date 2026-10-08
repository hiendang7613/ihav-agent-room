"""Explicit Claude worker rotation; ordinary start keeps every saved identity.

This is an operator lifecycle command, like start/stop, not a worker permission
or an admin-receipt capability. It never changes task authority or replays an
unknown message. The ledger holds launch intent before any native allocation.
"""

from contextlib import ExitStack
import json
from pathlib import Path
import re

from ihav_agent_room.common import (MODES, RoomError, dumps, file_lock,
                                    main_host, main_session_id, now,
                                    process_alive, process_stamp)
from ihav_agent_room.native import claude_agents
from ihav_agent_room.roster import ROSTER_BY_NAME
from ihav_agent_room.schema import WORKER_SESSION_SCHEMA, gateway_backup


TERMINAL = frozenset({"stopped", "failed", "done"})
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _member(db, name):
    return json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])


def _record(room, request_id):
    records = [item for item in room.get("worker_session_history", []) if item["id"] == request_id]
    if len(records) != 1:
        raise RoomError("Replacement record is missing or ambiguous; inspect room history", "conflict")
    return records[0]


def _operator(store, room):
    store.main_only(store.actor())
    owner = room.get("owner") or {}
    if (owner.get("host", "claude"), owner.get("session")) != (main_host(), main_session_id()):
        raise RoomError("Only the exact saved gateway can replace a worker session", "identity")


def _absent(process):
    if process_alive(process.get("pid"), process.get("stamp")) is not False:
        raise RoomError("Process absence is not confirmed; inspect status before replacement", "conflict")
    pid = process.get("pid")
    if pid is not None:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 2 or process_stamp(pid) is not None:
            raise RoomError("Process metadata is invalid or its PID is still live", "conflict")


def _stopped_room(db, room):
    if (room["status"] not in {"stopped", "failed"} or not room.get("manual_stop")
            or room.get("mode_transition") or room.get("restart_requested")):
        raise RoomError("Stop and reconcile this room before explicit session replacement", "conflict")
    _absent(room.get("supervisor") or {})
    if any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
           for row in db.execute("SELECT data FROM approvals")):
        raise RoomError("Resolve native approvals before session replacement", "conflict")


def _retired_registry(project, session):
    """Fresh all-job evidence; missing PID metadata alone is not an exit."""
    observations = []
    for native in claude_agents(project, scoped=False):
        if native.get("sessionId") != session:
            continue
        cwd = native.get("cwd")
        if not isinstance(cwd, str) or not cwd or Path(cwd).resolve() != project:
            raise RoomError("Retired native session has mismatched project identity", "identity")
        _absent(native)
        state, status = native.get("state"), native.get("status")
        if (native.get("kind") != "background" or
                not (state in TERMINAL and status in TERMINAL | {None}
                     or state is None and status in TERMINAL)):
            raise RoomError("Retired native session is not affirmatively terminal", "conflict")
        observations.append({key: native.get(key) for key in ("sessionId", "id", "kind", "state", "status")})
    return observations


def prepare_claude_replacement(store, member, expected_session, request_id, reason):
    """Retire exactly one stopped worker, preserving its whole ledger and backup.

    Repeating the same request ID returns its record even after native launch;
    it can never clear the resulting fresh identity for another allocation.
    """
    if member not in ROSTER_BY_NAME or ROSTER_BY_NAME[member]["host"] != "claude":
        raise RoomError("Explicit replacement supports Claude workers only", "invalid")
    if not isinstance(expected_session, str) or not expected_session.strip():
        raise RoomError("An exact expected saved session is required", "invalid")
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        raise RoomError("request-id must be 1 to 128 letters, numbers, dots, colons, hyphens or underscores", "invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise RoomError("An explicit replacement reason of 1 to 4000 characters is required", "invalid")
    _operator(store, store.room())
    with file_lock(store.runtime / "control.lock", blocking=False), ExitStack() as locks:
        with store.tx() as db:
            room = store.get_room(db)
            _operator(store, room)
            if member == room["gateway"] or member not in MODES[room["mode"]]:
                raise RoomError("Replacement requires an enabled non-gateway Claude worker", "authority")
            existing = [item for item in room.get("worker_session_history", []) if item["id"] == request_id]
            if existing:
                record = _record(room, request_id)
                if (record["member"], record["retired_session"], record["reason"]) != (member, expected_session, reason):
                    raise RoomError("This request-id already names a different replacement", "conflict")
                return {"prepared": False, "unchanged": True, "replacement": record}
            locks.enter_context(file_lock(store.runtime / "supervisor.lock", blocking=False))
            _stopped_room(db, room)
            worker = _member(db, member)
            if (worker.get("native_id") != expected_session or worker["status"] not in {"stopped", "failed"}
                    or worker.get("turn_id") or worker.get("unexpected_native_id")):
                raise RoomError("Saved worker identity or terminal state changed; read current status", "conflict")
            if expected_session in (room.get("host_sessions") or {}).values():
                raise RoomError("A saved host session cannot be retired as a worker", "identity")
            if worker.get("session_replacement"):
                previous = _record(room, worker["session_replacement"])
                if previous["state"] != "completed":
                    raise RoomError("Reconcile the pending replacement before requesting another", "outcome_unknown")
            for row in db.execute("SELECT name,data FROM members WHERE name != ?", (room["gateway"],)):
                _absent(json.loads(row["data"]))
            registry = _retired_registry(store.project, expected_session)
            backup = gateway_backup(store)
            _absent(worker)
            _absent(room.get("supervisor") or {})
            record = {"id": request_id, "member": member, "retired_session": expected_session,
                      "retired_job": worker.get("job_id"), "reason": reason,
                      "requested_by": {"host": main_host(), "session": main_session_id()},
                      "at": now(), "backup": backup, "state": "prepared", "registry": registry}
            room["schema"] = WORKER_SESSION_SCHEMA
            room.setdefault("worker_session_history", []).append(record)
            if (room.get("background_sessions") or {}).get(member) == expected_session:
                room["background_sessions"].pop(member)
            worker.update(native_id=None, job_id=None, pid=None, stamp=None, turn_id=None,
                          token_hash=None, status="stopped", error=None, unexpected_native_id=None,
                          launch_generation=None, session_replacement=request_id,
                          observed_model=None, observed_effort=None, model_observed_at=None,
                          model_observation_source=None, hook_seen=None, hook_version=None, transcript=None,
                          settings_application="fresh Claude session explicitly prepared; not started")
            db.execute("UPDATE members SET data=? WHERE name=?", (dumps(worker), member))
            store.put_room(db, room)
            store.event(db, "room.worker_session_replacement_prepared", record)
    return {"prepared": True, "replacement": record,
            "next": "Start the preserved room through its normal start entry; prepared is not native readiness."}


def check_replacement_start(store):
    """Never allocate again after a launch whose result has not been reconciled."""
    with store.read() as db:
        room = store.get_room(db)
        for row in db.execute("SELECT data FROM members"):
            worker = json.loads(row[0])
            if not worker.get("session_replacement"):
                continue
            record = _record(room, worker["session_replacement"])
            if record["state"] in {"launching", "unknown"}:
                raise RoomError("Claude replacement launch needs reconciliation; no new session will be allocated",
                                "outcome_unknown", replacement=record["id"], member=worker["name"],
                                observed_session=worker.get("native_id"))
            expected = record.get("native_id") if record["state"] == "completed" else None
            if record["state"] not in {"prepared", "completed"} or worker.get("native_id") != expected:
                raise RoomError("Replacement identity differs from its durable record", "identity")


def begin_replacement_launch(store, member, generation):
    with store.tx() as db:
        worker = _member(db, member)
        if not worker.get("session_replacement"):
            return None
        room = store.get_room(db)
        record = _record(room, worker["session_replacement"])
        if record["state"] == "completed":
            return None  # Subsequent ordinary starts resume this exact saved session.
        if (record["state"] != "prepared" or worker.get("native_id") is not None
                or room["generation"] != generation or room["status"] != "starting"):
            raise RoomError("Replacement launch is already pending or its identity changed", "outcome_unknown")
        record.update(state="launching", launch_generation=generation, launch_started=now(),
                      binding_hash=worker.get("token_hash"))
        store.put_room(db, room)
        store.event(db, "room.worker_session_replacement_launching", record)
        return record["id"]


def fail_replacement_launch(store, request_id, error):
    if not request_id:
        return
    with store.tx() as db:
        room = store.get_room(db)
        record = _record(room, request_id)
        if record["state"] != "launching":
            return
        worker = _member(db, record["member"])
        record.update(state="unknown", error=str(error), error_code=getattr(error, "code", "unknown"),
                      observed_session=worker.get("native_id"))
        store.put_room(db, room)
        store.event(db, "room.worker_session_replacement_unknown", record)


def complete_replacement_launch(store, request_id, generation, changes):
    with store.tx() as db:
        room = store.get_room(db)
        record = _record(room, request_id)
        worker = _member(db, record["member"])
        session = changes.get("native_id")
        reserved = set((room.get("host_sessions") or {}).values())
        reserved.update(item["retired_session"] for item in room.get("worker_session_history", []))
        reserved.update(json.loads(row[0]).get("native_id") for row in db.execute(
            "SELECT data FROM members WHERE name != ?", (record["member"],)))
        if (record["state"] != "launching" or record["launch_generation"] != generation
                or room["generation"] != generation or not session or session in reserved
                or worker.get("native_id") not in {None, session} or worker.get("unexpected_native_id")
                or worker.get("token_hash") != record.get("binding_hash")):
            raise RoomError("Fresh native identity does not match the pending replacement", "identity")
        worker.update(changes)
        record.update(state="completed", native_id=session, job_id=changes.get("job_id"), completed=now())
        db.execute("UPDATE members SET data=? WHERE name=?", (dumps(worker), record["member"]))
        store.put_room(db, room)
        store.event(db, "room.worker_session_replacement_completed", record)
