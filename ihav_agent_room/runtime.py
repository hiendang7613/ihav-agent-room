"""One process supervisor per room, native workers, durable dispatch receipts."""

import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import time

from ihav_agent_room import __version__
from ihav_agent_room.common import (GATEWAY, MEMBERS, MODES, acting_member, main_host, PLUGIN_ROOT, RoomError, dumps, file_lock,
                               native_event_prompt, now, process_alive, process_stamp, uid)
from ihav_agent_room.native import (CodexClient, claude_agents, codex_usage_snapshot, doctor, exact_claude,
                               owned_descendants, send_claude, message_text, start_claude, stop_claude_worker,
                               stop_descendants, wait_for_exit)
from ihav_agent_room.catalogwatch import check_catalogs
from ihav_agent_room.continuity import TRANSCRIPT_WINDOW
from ihav_agent_room.globalspace import GlobalSpace
from ihav_agent_room.provenance import NON_HUMAN_ORIGINS, WINDOW_MARGIN, gap, row_text
from ihav_agent_room.codex_gateway import CodexGateway, probe_codex, probe_detached_codex
from ihav_agent_room.release import active_release, follows_pointer
from ihav_agent_room.roster import HOST_GATEWAYS, ROSTER_BY_NAME, SELECTABLE_MODES, launch_config
from ihav_agent_room.store import FYI_CONTEXT_SQL, Store
from ihav_agent_room.schema import HOST_SCHEMA, WORKER_SESSION_SCHEMA, gateway_backup
from ihav_agent_room.session_replacement import (begin_replacement_launch, check_replacement_start,
                                                complete_replacement_launch, fail_replacement_launch)


CLAUDE_EXIT_ERROR = "Native background session exited. Stop/start to resume it."
CLAUDE_LIVENESS_ERROR = "Native Claude liveness is unavailable; native registry requires reconciliation before recovery."


def saved_codex_session(store):
    """Host identity survives switches without becoming a background worker ID."""
    room = store.room()
    return (room.get("host_sessions") or {}).get("codex") or store.member(HOST_GATEWAYS["codex"]).get("native_id")


def bind_main(store, session, permission_mode="default", host=None, handoff=False):
    host = host or main_host()
    gateway = HOST_GATEWAYS[host]
    if acting_member() != gateway or os.environ.get("IHAV_AGENT_ROOM_BINDING"):
        raise RoomError("A worker cannot become the room's admin session", "authority")
    if not session:
        raise RoomError("Run this command from the main Claude or Codex session", "identity")
    if host != "codex" and store.gateway == HOST_GATEWAYS["codex"] and not handoff:
        raise RoomError("This room belongs to its saved Codex gateway; connect from that session", "conflict")
    if host == "codex":
        saved = saved_codex_session(store)
        if saved and saved != session:
            raise RoomError("Resume the room's saved Codex session before connecting; its identity will not be replaced", "identity",
                            saved_session=saved, current_session=session)
    if host == "codex":
        asyncio.run(probe_codex(store.project, session))
        owner = {"host": host, "session": session, "pid": None, "stamp": None, "permission_mode": permission_mode}
    else:
        native = exact_claude(store.project, session)
        owner = {"host": host, "session": session, "pid": native["pid"], "stamp": process_stamp(native["pid"]),
                 "permission_mode": permission_mode}
    with store.tx() as db:
        room = store.get_room(db)
        old = room.get("owner") or {}
        previous_gateway = store.gateway
        target = json.loads(db.execute("SELECT data FROM members WHERE name=?", (gateway,)).fetchone()[0])
        saved = (room.get("host_sessions") or {}).get("codex") or target.get("native_id")
        if host == "codex" and saved and saved != session:
            raise RoomError("Resume the room's saved Codex session before connecting; its identity will not be replaced", "identity",
                            saved_session=saved, current_session=session)
        changed = old and (old.get("session"), old.get("host", "claude")) != (session, host)
        if changed and (host == "codex" or old.get("host") == "codex") and room["status"] not in {"stopped", "failed"}:
            raise RoomError("Stop the room before changing its Codex owner session", "conflict")
        if changed and old.get("host", "claude") == "codex" and not handoff:
            try:
                asyncio.run(probe_codex(store.project, old["session"]))
            except RoomError as exc:
                if exc.code != "unavailable":
                    raise  # Unknown liveness must never permit a takeover.
            else:
                raise RoomError("Another live Codex session owns this room", "conflict")
        if changed and old.get("host", "claude") == "claude" and process_alive(old.get("pid"), old.get("stamp")):
            # /clear changes the session UUID in the SAME native process.
            if not handoff and (old["pid"] != owner["pid"] or old["stamp"] != owner["stamp"]):
                raise RoomError("Another live Claude session owns this room", "conflict")
        if previous_gateway != gateway:
            supervisor = room.get("supervisor") or {}
            if room["status"] not in {"stopped", "failed"} or process_alive(supervisor.get("pid"), supervisor.get("stamp")):
                raise RoomError("Stop the room before switching its gateway host", "conflict")
            if db.execute("SELECT 1 FROM claims LIMIT 1").fetchone() or any(
                    json.loads(row[0])["state"] not in {"done", "cancelled"} for row in db.execute("SELECT data FROM tasks")):
                raise RoomError("Reconcile unfinished tasks and claims before switching the gateway host", "conflict")
            if any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
                   for row in db.execute("SELECT data FROM approvals")):
                raise RoomError("Resolve native approvals before switching the gateway host", "conflict")
            if process_alive(target.get("pid"), target.get("stamp")):
                raise RoomError("The proposed gateway still has a live room worker", "conflict")
            room["gateway_backup"] = gateway_backup(store)
            host_sessions = dict(room.get("host_sessions") or {})
            background_sessions = dict(room.get("background_sessions") or {})
            if old.get("session"):
                host_sessions[old.get("host", "claude")] = old["session"]
            if target.get("native_id") and target["native_id"] != session:
                background_sessions[gateway] = target["native_id"]
            # An old host session must never be resumed as a background worker.
            former = json.loads(db.execute("SELECT data FROM members WHERE name=?", (previous_gateway,)).fetchone()[0])
            background = background_sessions.get(previous_gateway)
            if background in host_sessions.values():
                background = None
            former.update(native_id=background, pid=None, stamp=None, turn_id=None, status="stopped", token_hash=None,
                          settings_application="configured; not started")
            room["background_sessions"] = background_sessions
            room["host_sessions"] = host_sessions
            db.execute("UPDATE members SET data=? WHERE name=?", (dumps(former), previous_gateway))
            store.event(db, "room.gateway_changed", {"from": previous_gateway, "to": gateway, "host": host,
                "backup": room["gateway_backup"], "former_owner": old,
                "previous_native_ids": {previous_gateway: (old or {}).get("session"), gateway: target.get("native_id")}})
        room["owner"] = owner
        room.setdefault("host_sessions", {})[host] = session
        room["gateway"] = gateway
        if room.get("worker_session_history") and room["schema"] < WORKER_SESSION_SCHEMA:
            backup = gateway_backup(store)
            previous_schema = room["schema"]
            room.update(schema=WORKER_SESSION_SCHEMA, worker_identity_schema_backup=backup)
            store.event(db, "room.worker_identity_schema_upgraded", {"from": previous_schema,
                        "to": WORKER_SESSION_SCHEMA, "backup": backup})
        if host == "codex":
            # Version 0.7.0 and older hardcode a Claude gateway. Their get_room()
            # rejects schema 4, preventing old hooks/CLIs from reassigning our owner.
            room["schema"] = max(room["schema"], HOST_SCHEMA)  # Preserve newer worker lifecycle semantics.
        store.put_room(db, room)
    store.member(gateway, {"native_id": session, "pid": owner["pid"], "stamp": owner["stamp"],
                           "status": "active", "permission_mode": permission_mode, "token_hash": None,
                           "turn_id": None, "settings_application": "host-managed"})
    return owner


def connection_plan(store, session, require_host=True, allow_drain=False):
    """Read-only handoff plan for the current project. Other rooms are never changed."""
    host_ready = bool(main_host() == "codex" and session
                      and acting_member() == HOST_GATEWAYS["codex"]
                      and not os.environ.get("IHAV_AGENT_ROOM_BINDING"))
    if require_host and not host_ready:
        raise RoomError("Connect from the intended Codex host, not a room worker", "identity")
    if not host_ready:
        session = None  # A shell, Claude session or worker is not the intended Codex host.
    room = store.room()
    saved = saved_codex_session(store)
    resume_required = bool(saved and saved != session)
    blockers = []
    if room["status"] not in {"stopped", "failed"} and not allow_drain:
        owner = room.get("owner") or {}
        if owner.get("host") != "codex" or owner.get("session") != session:
            blockers.append("Stop this room from its current gateway before connecting from Codex")
    with store.read() as db:
        if db.execute("SELECT 1 FROM claims LIMIT 1").fetchone() or any(
                json.loads(row[0])["state"] not in {"done", "cancelled"} for row in db.execute("SELECT data FROM tasks")):
            if store.gateway != HOST_GATEWAYS["codex"]:
                blockers.append("Reconcile unfinished tasks and claims before changing the gateway host")
        if store.gateway != HOST_GATEWAYS["codex"] and any(
                json.loads(row[0])["state"] in {"pending", "respond", "submitted"} for row in db.execute("SELECT data FROM approvals")):
            blockers.append("Resolve native approvals before changing the gateway host")
    plan = {"project": str(store.project), "room": room["id"], "mode": room["mode"], "gateway": store.gateway,
            "saved_codex_session": saved, "current_codex_session": session, "resume_required": resume_required,
            "can_connect": host_ready and not blockers and not resume_required, "blockers": blockers,
            "codex_host_required": not host_ready,
            "handoff_required": store.gateway != HOST_GATEWAYS["codex"], "input_sent": False, "room_changed": False}
    if resume_required:
        plan["next"] = {"action": "Open the saved Codex session in this project, then run connect again",
                        "argv": ["codex", "--cd", str(store.project), "resume", saved]}
    elif not host_ready:
        plan["next"] = {"action": "Open this project in a Codex host, then run connect there",
                        "argv": ["codex", "--cd", str(store.project)]}
    return plan


def await_detached_shutdown(store, session, budget=10):
    """An explicit start waits for owned cleanup after native proof the old host closed.

    No forced stop, permission response, state rewrite, or native operation replay.
    A deadline leaves ownership intact and reports recovery_pending for the skill.
    """
    room = store.room()
    owner = room.get("owner") or {}
    previous = saved_codex_session(store)
    if (store.gateway != HOST_GATEWAYS["codex"] or owner.get("host") != "codex"
            or owner.get("session") != previous or not previous or previous == session):
        return
    supervisor = room.get("supervisor") or {}
    if room["status"] in {"stopped", "failed"} and not process_alive(supervisor.get("pid"), supervisor.get("stamp")):
        return
    asyncio.run(probe_detached_codex(store.project, session, previous))
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        current = store.room()
        if current.get("owner") != owner or current.get("generation") != room.get("generation"):
            raise RoomError("Room ownership changed during recovery; read current state", "conflict")
        supervisor = current.get("supervisor") or {}
        if current["status"] in {"stopped", "failed"} and not process_alive(supervisor.get("pid"), supervisor.get("stamp")):
            return
        time.sleep(.1)
    raise RoomError("Previous Codex host is confirmed closed, but owned cleanup is still pending; "
                    "track status internally without forcing shutdown", "recovery_pending", previous_session=previous)


def reconnect_codex_host(store, session):
    """Explicit start can recover a stopped Codex room in a new attached host.

    Preserve the old native conversation and ledger; do not claim its private
    context was loaded into this conversation. Liveness uncertainty blocks recovery.
    """
    if main_host() != "codex" or acting_member() != HOST_GATEWAYS["codex"] or os.environ.get("IHAV_AGENT_ROOM_BINDING"):
        raise RoomError("Only the current Codex main session can reconnect its room", "authority")
    await_detached_shutdown(store, session)
    with file_lock(store.runtime / "control.lock"), file_lock(store.runtime / "supervisor.lock", blocking=False):
        with store.tx() as db:
            room = store.get_room(db)
            owner = room.get("owner") or {}
            members = [json.loads(row[0]) for row in db.execute("SELECT data FROM members")]
            member = next(member for member in members if member["name"] == HOST_GATEWAYS["codex"])
            previous = (room.get("host_sessions") or {}).get("codex") or member.get("native_id")
            if previous == session:
                return {}
            if (store.gateway != HOST_GATEWAYS["codex"] or owner.get("host") != "codex"
                    or not previous or owner.get("session") != previous):
                raise RoomError("Reconnect requires this room's recorded Codex host; worker identities stay unchanged", "identity")
            supervisor = room.get("supervisor") or {}
            if room["status"] not in {"stopped", "failed"} or process_alive(supervisor.get("pid"), supervisor.get("stamp")):
                raise RoomError("The previous room supervisor must finish stopping before reconnect", "conflict")
            if any(process_alive(member.get("pid"), member.get("stamp")) for member in members):
                raise RoomError("A previous room worker is still live; reconnect will not interrupt it", "conflict")
            if any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
                   for row in db.execute("SELECT data FROM approvals")):
                raise RoomError("Resolve native approvals before reconnecting in another Codex conversation", "conflict")
            asyncio.run(probe_detached_codex(store.project, session, previous))
            backup = gateway_backup(store)
            history = list(room.get("host_session_history") or [])
            history.append({"host": "codex", "session": previous, "replaced_by": session, "at": now(), "backup": backup})
            room.update(host_session_history=history, gateway_backup=backup)
            room.setdefault("host_sessions", {})["codex"] = session
            room["owner"] = owner | {"session": session, "pid": None, "stamp": None}
            store.put_room(db, room)
            member.update(native_id=session, pid=None, stamp=None, turn_id=None, token_hash=None, status="active")
            db.execute("UPDATE members SET data=? WHERE name=?", (dumps(member), HOST_GATEWAYS["codex"]))
            store.event(db, "room.codex_reconnected", {"previous_session": previous, "session": session, "backup": backup})
    return {"reconnected_host": True, "previous_codex_session": previous, "recovery_backup": backup,
            "context_recovery": "Read current status, task context, pending inbox and room history; private native conversation stays in the previous session."}


def drain_init_handoff(store, session, budget=20):
    """Pause new dispatch and await native idle before an explicit init transfer."""
    host = main_host()
    if host == "codex":
        saved = saved_codex_session(store)
        if saved and saved != session:
            raise RoomError("Resume the room's saved Codex session before connecting", "identity", saved_session=saved)
        asyncio.run(probe_codex(store.project, session))
    else:
        exact_claude(store.project, session)
    with file_lock(store.runtime / "control.lock"):
        with store.tx() as db:
            room = store.get_room(db)
            owner = room.get("owner") or {}
            if store.gateway == HOST_GATEWAYS[host] and (owner.get("host", "claude"), owner.get("session")) == (host, session):
                return None  # An identical concurrent init already completed.
            if room["status"] in {"stopped", "failed"}:
                return None
            supervisor = room.get("supervisor") or {}
            if supervisor.get("handoff_protocol") != 1 or not process_alive(supervisor.get("pid"), supervisor.get("stamp")):
                raise RoomError("The current supervisor cannot safely drain for init; reconcile its stopped state before host transfer", "conflict")
            if db.execute("SELECT 1 FROM claims LIMIT 1").fetchone() or any(
                    json.loads(row[0])["state"] not in {"done", "cancelled"} for row in db.execute("SELECT data FROM tasks")):
                raise RoomError("Reconcile unfinished tasks and claims before switching the gateway host", "conflict")
            if any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"} for row in db.execute("SELECT data FROM approvals")):
                raise RoomError("Resolve native approvals before switching the gateway host", "conflict")
            transition = room.get("mode_transition")
            requester = {"host": host, "session": session}
            if transition and (transition.get("reason") != "handoff" or transition.get("requester") != requester):
                raise RoomError("Another room transition is already pending", "conflict")
            if not transition:
                room.update(restart_requested=True, mode_transition={"from": room["mode"], "to": room["mode"],
                    "state": "draining", "reason": "handoff", "requester": requester})
                store.put_room(db, room)
                store.event(db, "room.handoff_requested", requester)
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        room = store.room()
        owner = room.get("owner") or {}
        if store.gateway == HOST_GATEWAYS[host] and (owner.get("host", "claude"), owner.get("session")) == (host, session):
            return None
        transition = room.get("mode_transition") or {}
        if transition.get("reason") != "handoff" or transition.get("requester") != requester:
            raise RoomError("The init handoff was cancelled or superseded; no ownership was changed", "conflict")
        if room["status"] in {"stopped", "failed"} and not room.get("supervisor"):
            return None
        time.sleep(.1)
    return {"started": False, "handoff_pending": True, "status": room["status"],
            "note": "Native turns are still draining; init can finish internally when the room stops. No turn was forced or approval answered."}


def autostart(store, session, permission_mode="default", budget=60, pause=3):
    """Bind this main session and resume the room in the background.

    The host may list a new session in its registry only after SessionStart returns, so binding retries while the
    session is not yet live. Another live owner, a manual stop or any other refusal ends the attempt; the outcome is
    recorded as a room event instead of being guessed.
    """
    deadline = time.monotonic() + budget
    try:
        with file_lock(store.runtime / "autostart.lock", blocking=False):
            while True:
                try:
                    result = start_room(store, session, permission_mode=permission_mode, automatic=True)
                    outcome = {"session": session, "result": "started" if result.get("started") else "not_started",
                               "reason": result.get("reason")}
                    break
                except RoomError as exc:
                    if exc.code != "unavailable" or time.monotonic() >= deadline:
                        outcome = {"session": session, "result": "failed", "code": exc.code, "reason": str(exc)}
                        break
                    time.sleep(pause)
    except RoomError as exc:  # Another autostart already runs for this room.
        return {"session": session, "result": "skipped", "reason": str(exc)}
    with store.tx() as db:
        store.event(db, "room.autostart", outcome)
    return outcome


def spawn_autostart(store, session, permission_mode="default"):
    """Run autostart detached, so a hook returns at once."""
    store.runtime.mkdir(parents=True, exist_ok=True)
    with open(store.runtime / "autostart.log", "a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, str(PLUGIN_ROOT / "bin" / "ihav-agent-room"), "--project", str(store.project),
                          "_autostart", "--session", session, "--permission-mode", permission_mode],
                         cwd=store.project, env=dict(os.environ, IHAV_AGENT_ROOM_MEMBER=HOST_GATEWAYS[main_host()]),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def needs_autostart(store, session):
    """A previously bound room lost its owner process and was not stopped by hand: this main session may resume it."""
    room = store.room()
    owner, supervisor = room.get("owner") or {}, room.get("supervisor") or {}
    return bool(owner) and owner.get("session") != session and not room["manual_stop"] \
        and not process_alive(owner.get("pid"), owner.get("stamp")) \
        and not process_alive(supervisor.get("pid"), supervisor.get("stamp"))


def recover_exited_pair(store, session, automatic=False, budget=10):
    """Explicit start can clean a live controller whose sole saved worker exited.

    No live native turn is interrupted: an exact owner, terminal worker state,
    absent process and (for Claude) fresh registry absence are all required.
    The existing supervisor owns cleanup and exact-session relaunch. Unknown
    effects remain in the ledger; this request never queues their replay.
    """
    if automatic:
        return None
    with file_lock(store.runtime / "control.lock"):
        room = store.room()
        owner, supervisor = room.get("owner") or {}, room.get("supervisor") or {}
        host = main_host()
        if (room["mode"] != "pair" or room["status"] != "running" or room.get("mode_transition")
                or room["gateway"] != HOST_GATEWAYS[host]
                or (owner.get("host", "claude"), owner.get("session")) != (host, session)
                or supervisor.get("handoff_protocol") != 1
                or process_alive(supervisor.get("pid"), supervisor.get("stamp")) is not True):
            return None
        worker = next(name for name in MODES["pair"] if name != room["gateway"])
        member = store.member(worker)
        saved = member.get("native_id")
        if (member["status"] not in {"stopped", "failed"} or not saved
                or saved in (room.get("host_sessions") or {}).values()
                or member.get("unexpected_native_id") or member.get("turn_id")
                or process_alive(member.get("pid"), member.get("stamp")) is not False):
            return None
        worker_pid = member.get("pid")
        has_pid = isinstance(worker_pid, int) and not isinstance(worker_pid, bool) and worker_pid >= 2
        if (worker.startswith("CODEX") and (not has_pid or not isinstance(member.get("stamp"), str)
                                            or not member["stamp"])):
            return None  # Missing process metadata is not affirmative absence.
        if has_pid and process_stamp(worker_pid) is not None:
            return None  # A reused live PID also requires reconciliation.
        store.main_only(store.actor())
        with store.read() as db:
            if any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
                   for row in db.execute("SELECT data FROM approvals")):
                return None
        if worker.startswith("CLAUDE"):
            # exact_claude's unavailable also covers multiple live matches;
            # count registry evidence directly rather than treating it as death.
            for native in claude_agents(store.project, scoped=False):
                if native.get("sessionId") != saved:
                    continue
                pid, cwd = native.get("pid"), native.get("cwd")
                if not isinstance(cwd, str) or not cwd or Path(cwd).resolve() != store.project:
                    return None
                if pid is None:
                    # --all retains completed native background jobs without
                    # a PID. Require their explicit terminal state, no active
                    # status, and the independently absent ledger process above.
                    if (native.get("kind") == "background"
                            and native.get("state") in {"stopped", "failed", "done"}
                            and native.get("status") is None):
                        continue
                    return {"requested": False, "held": True, "worker": worker, "native_id": saved,
                            "reason": "native_registry_requires_reconciliation",
                            "native_observation": {key: native.get(key) for key in ("kind", "state", "status")},
                            "next": "Inspect the exact saved native job and current approvals; "
                                    "terminal exit is not established."}
                if (not isinstance(pid, int) or isinstance(pid, bool) or pid < 2
                        or process_stamp(pid) is not None):
                    return None
        backup = gateway_backup(store)
        with store.tx() as db:
            current = store.get_room(db)
            observed = json.loads(db.execute("SELECT data FROM members WHERE name=?", (worker,)).fetchone()[0])
            if current != room or observed != member:
                raise RoomError("Pair state changed during recovery inspection; read current status", "conflict")
            if any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
                   for row in db.execute("SELECT data FROM approvals")):
                return None
            if (process_alive(member.get("pid"), member.get("stamp")) is not False
                    or (has_pid and process_stamp(worker_pid) is not None)):
                raise RoomError("Saved worker liveness changed during recovery inspection", "conflict")
            current.update(status="stopping", manual_stop=False, restart_requested=True,
                           pair_recovery_backup=backup,
                           mode_transition={"from": "pair", "to": "pair", "state": "restarting",
                                            "reason": "pair_recovery", "worker": worker, "native_id": saved})
            store.put_room(db, current)
            store.event(db, "room.pair_recovery_requested", {"session": session, "host": host,
                "worker": worker, "native_id": saved, "generation": room["generation"], "backup": backup})
    recovery = {"requested": True, "worker": worker, "native_id": saved,
                "previous_generation": room["generation"], "backup": backup}
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        current = store.room()
        current_owner = current.get("owner") or {}
        if (current["gateway"] != room["gateway"] or current["mode"] != "pair"
                or (current_owner.get("host", "claude"), current_owner.get("session")) != (host, session)
                or store.member(worker).get("native_id") != saved):
            raise RoomError("Pair identity changed during cleanup; read current state", "conflict")
        if current["generation"] != room["generation"]:
            return recovery  # The owned supervisor completed its normal restart.
        current_supervisor = current.get("supervisor") or {}
        if (process_alive(current_supervisor.get("pid"), current_supervisor.get("stamp")) is False
                and process_alive(supervisor.get("pid"), supervisor.get("stamp")) is False):
            if current["status"] == "failed":
                raise RoomError("Owned pair cleanup failed; inspect status before retrying", "cleanup")
            if current["status"] == "stopped":
                return recovery
        time.sleep(.1)
    return {"started": False, "recovery_pending": True, "status": current["status"],
            "pair_recovery": recovery,
            "note": "Exact-session pair recovery requested; owned cleanup remains unconfirmed. "
                    "Track status internally; no competing controller or native outcome replay was started."}


def start_room(store, session, mode=None, permission_mode="default", automatic=False, handoff=False):
    if acting_member() != HOST_GATEWAYS[main_host()] or os.environ.get("IHAV_AGENT_ROOM_BINDING"):
        raise RoomError("A worker cannot become the room's admin session", "authority")
    check_replacement_start(store)
    checks = doctor()
    if not checks["ok"]:
        raise RoomError("Native dependencies are not ready; run doctor", "dependency", checks=checks)
    if handoff and store.gateway != HOST_GATEWAYS[main_host()]:
        pending = drain_init_handoff(store, session)
        if pending:
            return pending
    recovery = (recover_exited_pair(store, session, automatic=automatic)
                if mode in {None, "pair"} else None)
    if recovery and recovery.get("held"):
        return {"started": False, "reason": "Exact native pair recovery is held for reconciliation",
                "pair_recovery": recovery}
    if recovery and recovery.get("recovery_pending"):
        return recovery
    recovery_info = {"pair_recovery": recovery} if recovery else {}
    with file_lock(store.runtime / "control.lock"):
        check_replacement_start(store)  # A concurrent native launch may have failed during dependency checks.
        owner = bind_main(store, session, permission_mode, handoff=handoff)
        with store.tx() as db:
            room = store.get_room(db)
            supervisor = room.get("supervisor") or {}
            if automatic and room["manual_stop"]:
                return {"started": False, "reason": "manual stop persists until /ihav-agent-room:start"}
            if process_alive(supervisor.get("pid"), supervisor.get("stamp")):
                if mode and MODES.get(mode) != MODES.get(room["mode"]):
                    raise RoomError("Stop the room before changing mode", "conflict")
                if room["status"] == "stopping":
                    room["restart_requested"] = True
                    room["manual_stop"] = False
                    store.put_room(db, room)
                    return {"started": False, "reason": "Exact-session restart scheduled after owned cleanup completes"}
                return {"started": False, "reason": "supervisor already running", "room": room, **recovery_info}
            target_mode = mode or room["mode"]
            if target_mode not in MODES:
                raise RoomError("Unknown mode")
            if target_mode != room["mode"]:
                if room["status"] not in {"stopped", "failed"}:
                    raise RoomError("Stop and reconcile the room before changing mode", "conflict")
                check_mode_handoff(db, target_mode)
                store.apply_mode_settings(db, target_mode)
            generation = uid()
            room.update(mode=target_mode, status="starting", generation=generation, manual_stop=False, restart_requested=False, error=None)
            room.pop("mode_transition", None)
            store.put_room(db, room)
        env = dict(os.environ)
        env.pop("IHAV_AGENT_ROOM_MEMBER_TOKEN", None)
        with open(store.runtime / "supervisor.log", "a", encoding="utf-8") as log:
            try:
                process = subprocess.Popen([sys.executable, str(PLUGIN_ROOT / "bin" / "ihav-agent-room"),
                    "--project", str(store.project), "_serve", "--generation", generation],
                    cwd=store.project, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True)
            except OSError as exc:
                with store.tx() as db:
                    room = store.get_room(db)
                    room.update(status="failed", error="Could not launch supervisor")
                    store.put_room(db, room)
                raise RoomError("Could not launch supervisor", "native") from exc
            with store.tx() as db:
                room = store.get_room(db)
                if room["generation"] == generation:
                    room["supervisor"] = {"pid": process.pid, "stamp": process_stamp(process.pid), "handoff_protocol": 1}
                    store.put_room(db, room)
        return {"started": True, "status": "starting", "generation": generation,
                "note": "Launch requested. status reports native readiness; this is not a model-response receipt.",
                **recovery_info}


def requested_config(member, name):
    """The member's current requested settings (mode, override or gateway sync), else the roster default."""
    default = launch_config(name) or {}
    return {"model": member.get("requested_model") or default.get("model"),
            "effort": member.get("requested_effort") or default.get("effort")}


def check_mode_handoff(db, target_mode):
    for row in db.execute("SELECT data FROM tasks"):
        task = json.loads(row[0])
        if task["owner"] not in MODES[target_mode] and task["state"] not in {"done", "cancelled"}:
            raise RoomError("Hand off open tasks of members being disabled before changing mode", "conflict", task=task["id"])
        if task.get("reviewer") and task["reviewer"] not in MODES[target_mode] and task["state"] not in {"done", "cancelled"}:
            raise RoomError("Reassign pending reviewers before disabling their member", "conflict", task=task["id"])


def change_mode(store, mode):
    """Request the new preset now; drain current turns before exact-session restart."""
    if mode not in SELECTABLE_MODES:
        raise RoomError("Mode must be pair or advisors")
    with file_lock(store.runtime / "control.lock"):
        with store.tx() as db:
            room = store.get_room(db)
            previous = room["mode"]
            check_mode_handoff(db, mode)
            room["mode"] = mode
            store.apply_mode_settings(db, mode)
            supervisor = room.get("supervisor") or {}
            restart = room["status"] in {"starting", "running"} and process_alive(supervisor.get("pid"), supervisor.get("stamp"))
            if restart:
                room.update(restart_requested=True, manual_stop=False,
                            mode_transition={"from": previous, "to": mode, "state": "draining"})
            else:
                room.pop("mode_transition", None)
            store.put_room(db, room)
            store.event(db, "room.mode", {"from": previous, "to": mode, "restart": restart})
    return {"mode": mode, "previous": previous, "members": list(MODES[mode]), "restarting": restart,
            "note": ("Current turns finish before exact-session restart; new deliveries wait and queued messages are kept."
                     if restart else "Applies when the room starts.")}


def request_stop(store, manual=True, session=None):
    with store.tx() as db:
        room = store.get_room(db)
        if session and (room.get("owner") or {}).get("session") != session:
            return {"requested": False, "reason": "not owner"}
        room["manual_stop"] = manual or room["manual_stop"]
        if manual:
            room["restart_requested"] = False
        room.pop("mode_transition", None)
        if room["status"] not in {"stopped", "failed"}:
            room["status"] = "stopping"
        store.put_room(db, room)
        return {"requested": True, "status": room["status"]}


def approval_response(store, actor, approval_id, source, decision):
    store.main_only(actor)
    if decision not in {"accept", "decline", "cancel"}:
        raise RoomError("V1 supports one-request accept, decline or cancel; no persistent policy changes")
    with store.tx() as db:
        store.source(db, source, "native_approval")
        prompt = db.execute("SELECT origin FROM prompts WHERE id=?", (source,)).fetchone()
        if prompt["origin"] != "hook":
            raise RoomError("Native approval requires a fresh human prompt receipt; manual recovery is not eligible", "authority")
        row = db.execute("SELECT data FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            raise RoomError("Unknown native request")
        data = json.loads(row[0])
        if not data.get("supported") or data["state"] != "pending":
            raise RoomError("Request is unsupported or no longer pending; use its native UI", "conflict")
        if data["generation"] != store.get_room(db)["generation"]:
            raise RoomError("Request belongs to an earlier native process", "conflict")
        data.update(state="respond", response={"decision": decision}, source=source)
        db.execute("UPDATE approvals SET data=? WHERE id=?", (dumps(data), approval_id))
        return data


def hook_input_observations(store, session, transcript):
    """Recorded hook text/offset pairs, never a verdict about sender authority."""
    observed = []
    with store.read() as db:
        for row in db.execute("SELECT data FROM events WHERE kind='prompt.receipt' ORDER BY seq DESC LIMIT 100"):
            try:
                data = json.loads(row[0])
                if (data.get("session") != session or data.get("transcript") != transcript
                        or type(data.get("offset")) is not int or data["offset"] < 0):
                    continue
                prompt = db.execute("SELECT body,session,origin FROM prompts WHERE id=?", (data["receipt"],)).fetchone()
                if prompt and prompt["session"] == session and prompt["origin"] == "hook":
                    observed.append({"body": prompt["body"].strip(), "offset": data["offset"]})
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    return list(reversed(observed))  # Earlier receipts claim their exact row first.


def unobserved_input_timestamp(path, project, host, session, seen, now_ts, quiet, observed_inputs=()):
    """Bounded activity hint only; user-shaped rows never attest human authority.

    Tool output also changes transcript mtime. Unknown formats or source identity
    stay quiet; protected receipt decisions remain in the provenance owner.
    """
    path = Path(path)
    if not session or host not in {"claude", "codex"} or path.suffix != ".jsonl" or path.is_symlink():
        return None
    try:
        with path.open("rb") as stream:
            if host == "codex":
                header = json.loads(stream.readline(65536))
                payload = header.get("payload") if isinstance(header, dict) else None
                if (header.get("type") != "session_meta" or not isinstance(payload, dict)
                        or payload.get("id") != session or payload.get("cwd") != str(project)):
                    return None
            stream.seek(0, 2)
            start = max(0, stream.tell() - TRANSCRIPT_WINDOW)
            stream.seek(max(0, start - 1))
            boundary = stream.read(1) if start else b"\n"
            tail = stream.read(TRANSCRIPT_WINDOW)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    lines = tail.splitlines(keepends=True)
    cursor = start
    if start and boundary != b"\n":
        cursor += len(lines[0]) if lines else 0
        lines = lines[1:]  # Ignore a partial row without reading past the tail budget.
    inputs = []
    for line in lines:
        position, cursor = cursor, cursor + len(line)
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            if host == "claude" and (row.get("type") != "user" or row.get("sessionId") != session
                                      or row.get("cwd") != str(project)):
                continue
            if host == "codex" and row.get("type") != "response_item":
                continue
            message = row.get("message") if host == "claude" else row.get("payload")
            if not isinstance(message, dict):
                continue
            blocks = message.get("content")
            if isinstance(blocks, list) and any(not isinstance(block, dict) or block.get("type") not in {"input_text", "text"}
                                                for block in blocks):
                continue  # A mixed/image/tool projection cannot prove missing prompt hooks.
            text, origin = row_text(row)
            if not text or not text.strip() or (isinstance(origin, dict) and origin.get("kind") in NON_HUMAN_ORIGINS):
                continue
            if (native_event_prompt(text) or text.lstrip().startswith(("<environment_context>", "<skill>",
                                                                     "<user_instructions>", "<turn_aborted>"))):
                continue  # Known native/context envelopes are not an admin input-activity signal.
            if host == "claude" and (row.get("isMeta") is True or text.lstrip().startswith((
                    "<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-stderr>",
                    "<bash-input>", "<bash-stdout>", "<bash-stderr>"))):
                continue  # Local host commands have their own lifecycle, not a model prompt.
            stamp = datetime.fromisoformat(row["timestamp"])
            if stamp.tzinfo is None:
                continue
            inputs.append({"start": position, "end": cursor, "text": text.strip(), "stamp": stamp})
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    covered = set()
    for observation in observed_inputs:
        matches = [row for row in inputs if row["text"] == observation["body"] and row["start"] not in covered]
        if matches:
            nearest = min(matches, key=lambda row: gap(row, observation["offset"]))
            if gap(nearest, observation["offset"])[0] <= WINDOW_MARGIN + 2 * len(observation["body"].encode("utf-8")):
                covered.add(nearest["start"])
    for row in inputs:
        if row["start"] not in covered and seen < row["stamp"].timestamp() <= now_ts - quiet:
            return row["stamp"].isoformat()
    return None


class Supervisor:
    def __init__(self, store, generation):
        self.store, self.generation = store, generation
        self.codex = {}
        self.claude = {}
        self.terminal_upgrade_members = {}
        self.stopping = False
        self.error = None
        self.recovered = False
        self.last_registry_check = 0
        self.next_release_check = 0
        self.next_catalog_check = 0
        self.next_global_queue_check = 0
        self.next_hook_check = 0
        self.gateway_client = None
        self.gateway_live = False
        self.next_gateway_check = 0

    def worker_env(self, name):
        binding = secrets.token_hex(24)
        self.store.member(name, {"token_hash": hashlib.sha256(binding.encode()).hexdigest()})
        env = dict(os.environ)
        # Explicit empty values also clear a pre-spawned Claude spare's env:
        # omitted keys survive its native overlay. Each host supplies the
        # correct plugin roots when invoking that plugin's hooks.
        for key in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
            env[key] = ""
        # Clear a spare's stale pin, preserving a deliberate supervisor pin.
        env["IHAV_AGENT_ROOM_PIN"] = os.environ.get("IHAV_AGENT_ROOM_PIN", "")
        env.update(IHAV_AGENT_ROOM_MEMBER=name, IHAV_AGENT_ROOM_BINDING=binding,
                   IHAV_AGENT_ROOM_PROJECT=str(self.store.project),
                   IHAV_AGENT_ROOM_HOST=ROSTER_BY_NAME[name]["host"],
                   CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF="1")
        for key in ("IHAV_AGENT_ROOM_SESSION_ID", "AGENT_ROOM_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            env.pop(key, None)
        env.pop("CLAUDE_CODE_MESSAGING_TOKEN", None)
        env.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
        env.pop("CLAUDE_ENV_FILE", None)
        env["PATH"] = str(PLUGIN_ROOT / "bin") + os.pathsep + env.get("PATH", "")
        return env

    async def recover_owned(self):
        # A stale coordinator cannot authorize replay of an unknown dispatch.
        self.store.interrupt_attempts("Supervisor restarted; reconcile before retrying unknown effects")
        with self.store.tx() as db:
            db.execute("UPDATE messages SET status='unknown',detail='Supervisor restarted during dispatch; reconcile before retry' WHERE status='dispatching'")
            for row in db.execute("SELECT id,data FROM approvals").fetchall():
                data = json.loads(row["data"])
                if data["state"] in {"pending", "respond", "submitted"}:
                    data.update(state="expired", detail="Native connection ended; await a new request")
                    db.execute("UPDATE approvals SET data=? WHERE id=?", (dumps(data), row["id"]))
        for name in MEMBERS:
            if name == self.store.gateway:
                continue
            member = self.store.member(name)
            if name.startswith("CLAUDE") and member["native_id"]:
                # Stop by exact session identity, never by name or global daemon.
                try:
                    live = await asyncio.to_thread(exact_claude, self.store.project, member["native_id"])
                except RoomError as exc:
                    if exc.code != "unavailable":
                        raise
                    live = None
                if live and (live["pid"] != member.get("pid") or not process_alive(member.get("pid"), member.get("stamp"))):
                    raise RoomError("Saved Claude session is running outside the recorded worker process; close its other controller before starting", "identity")
                await stop_claude_worker(self.store.project, member["native_id"], member)
            elif process_alive(member.get("pid"), member.get("stamp")):
                children = owned_descendants(member["pid"])
                if os.getpgid(member["pid"]) != member["pid"]:
                    raise RoomError("Stale native process group cannot be attributed safely", "identity")
                os.killpg(member["pid"], signal.SIGTERM)
                owned = {member["pid"]: member["stamp"]}
                if not await wait_for_exit(owned):
                    os.killpg(member["pid"], signal.SIGKILL)
                await stop_descendants(children)
                if not await wait_for_exit(owned):
                    raise RoomError("Cannot confirm stale Codex worker exit", "cleanup")
            self.store.member(name, {"status": "stopped", "pid": None, "stamp": None, "turn_id": None})
        self.recovered = True

    async def launch(self):
        owner = self.store.room().get("owner") or {}
        if owner.get("host") == "codex":
            self.gateway_client = CodexGateway(self.store.project, owner["session"])
            await self.gateway_client.start()
            self.gateway_live = True
        await self.recover_owned()
        try:  # Join the machine agents space; a ledger problem never blocks the room.
            GlobalSpace().register(self.store.room()["id"], self.store.project, __version__)
        except (RoomError, sqlite3.Error, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            with self.store.tx() as db:  # Malformed shared data must never stop local workers from launching.
                self.store.event(db, "agents_space.unavailable", {"error": f"{type(exc).__name__}: {exc}"})
        room = self.store.room()
        for name in MODES[room["mode"]]:
            if name == self.store.gateway:
                continue
            current = self.store.room()
            if self.stopping or not self.owner_alive() or current["status"] == "stopping" or current.get("mode_transition"):
                return
            member = self.store.member(name)
            env = self.worker_env(name)
            self.store.member(name, {"status": "starting", "error": None, "unexpected_native_id": None,
                                     "launch_generation": self.generation})
            profile = ROSTER_BY_NAME[name]
            config = requested_config(member, name)
            if profile["host"] == "codex":
                client = CodexClient(self.store.project, name, env, self.store.runtime / (name + ".log"))
                client.model_config = config
                self.codex[name] = client  # Own cleanup even when initialization fails.
                await client.start(member["native_id"])
                settings_application = ("model requested at thread start; model and effort requested on new turns"
                                        if not member["native_id"] else
                                        "model requested at thread resume; active turn unchanged; effort requested on each new turn")
                self.store.member(name, {"native_id": client.thread_id, "pid": client.process.pid,
                    "stamp": client.stamp, "status": "idle", "turn_id": client.turn_id,
                    "permission_class": client.permission_class,
                    "settings_pending_restart": False,
                    "settings_application": settings_application})
            else:
                native_id = member["native_id"]
                self.claude[name] = native_id
                replacement = begin_replacement_launch(self.store, name, self.generation)
                try:
                    native = await start_claude(self.store.project, native_id, bool(member["native_id"]),
                                               env, self.store.runtime / (name + ".log"),
                                               member=name,
                                               model=config["model"], effort=config["effort"])
                except RoomError as exc:
                    fail_replacement_launch(self.store, replacement, exc)
                    reported = exc.details.get("reported_new_ids", [])
                    registered = self.store.member(name)
                    observed = registered.get("unexpected_native_id") or (registered["native_id"] if not native_id else None)
                    if observed or len(reported) == 1:
                        created_id = observed or reported[0]
                        created = await asyncio.to_thread(exact_claude, self.store.project, created_id)
                        self.claude[name] = created_id  # Owned copy is cleaned, never adopted as the saved ID.
                        self.store.member(name, {"pid": created["pid"], "stamp": process_stamp(created["pid"]),
                                                 "unexpected_native_id": created_id})
                    raise
                self.claude[name] = native["sessionId"]
                changes = {"native_id": native["sessionId"], "job_id": native.get("id"),
                                         "pid": native["pid"], "stamp": process_stamp(native["pid"]), "status": "idle",
                                         "settings_pending_restart": False,
                                         "settings_application": ("model and effort passed to new Claude session"
                                             if not member["native_id"] else
                                             "resumed exact session; model and effortLevel requested in its settings file")}
                if replacement:
                    complete_replacement_launch(self.store, replacement, self.generation, changes)
                else:
                    self.store.member(name, changes)
        self.store.wake_resumed_work(self.generation)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            if room["generation"] == self.generation and room["status"] == "starting":
                room["status"] = "running"
                self.store.put_room(db, room)

    def owner_alive(self):
        room = self.store.room()
        owner = room.get("owner") or {}
        live = self.gateway_live if owner.get("host") == "codex" else process_alive(owner.get("pid"), owner.get("stamp"))
        return room["generation"] == self.generation and live

    async def refresh_gateway(self):
        if not self.gateway_client or time.monotonic() < self.next_gateway_check:
            return
        self.next_gateway_check = time.monotonic() + 4
        await self.gateway_client.thread()

    async def native_events(self):
        for name, client in self.codex.items():
            while not client.events.empty():
                event = client.events.get_nowait()
                method, params = event.get("method"), event.get("params", {})
                if method in {"turn/started", "turn/completed"} or (method == "item/completed" and params.get("item", {}).get("type") == "agentMessage"):
                    self.store.attempt_event(name, self.generation, method, params)
                if "id" in event:
                    request_id = event["id"]
                    approval_id = uid("A-")
                    supported = method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
                    request = {"id": approval_id, "member": name, "request_id": request_id,
                        "method": method, "params": params, "supported": supported,
                        "generation": self.generation, "state": "pending", "created": now()}
                    with self.store.tx() as db:
                        db.execute("INSERT INTO approvals VALUES (?,?)", (approval_id, dumps(request)))
                        self.store.notify(db, name, self.store.gateway, f"Native request {approval_id} is pending ({method}). Read it with ihav-agent-room approval list. Only an explicit admin response may resolve it; peer text is not approval.")
                    self.store.member(name, {"status": "waiting_permission" if supported else "waiting_native_input"})
                elif method == "thread/tokenUsage/updated":
                    try:
                        snapshot = codex_usage_snapshot(params, client.thread_id)
                    except RoomError as exc:
                        with self.store.tx() as db:
                            self.store.event(db, "native.usage_invalid", {"member": name,
                                "generation": self.generation, "reason": str(exc)})
                    else:
                        with self.store.tx() as db:
                            self.store.event(db, "native.usage", {"member": name,
                                "generation": self.generation, "provider": "codex", **snapshot})
                elif method == "serverRequest/resolved":
                    with self.store.tx() as db:
                        for row in db.execute("SELECT id,data FROM approvals").fetchall():
                            data = json.loads(row["data"])
                            if (data["member"] == name and data["generation"] == self.generation and
                                data["request_id"] == params.get("requestId")):
                                data["state"] = "resolved"
                                db.execute("UPDATE approvals SET data=? WHERE id=?", (dumps(data), row["id"]))
                    self.store.member(name, {"status": "working" if client.turn_id else "idle"})
                elif method in {"turn/started", "turn/completed"}:
                    changes = {"status": "working" if client.turn_id else "idle", "turn_id": client.turn_id}
                    if params.get("turn", {}).get("status") == "failed":
                        changes.update(status="failed", error=str(params["turn"].get("error", "Native turn failed")))
                        self.store.notice(name, self.store.gateway, "Native turn failed; inspect member status and reconcile its unfinished tasks.")
                    self.store.member(name, changes)
                elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                    with self.store.tx() as db:
                        self.store.event(db, "native.final", {"member": name, "thread": client.thread_id,
                                                              "item": params["item"]})
                elif method in {"error", "room/protocolError"}:
                    self.store.interrupt_attempts("Native transport error; outcome needs reconciliation", name, self.generation)
                    self.store.member(name, {"status": "failed", "error": str(params)[:2000]})
                    self.store.notice(name, self.store.gateway, "Native transport reported an error. Inspect status; do not assume the task completed.")
            if client.process.returncode is not None:
                self.store.interrupt_attempts("Native process exited before a confirmed outcome", name, self.generation)
                old = self.store.member(name)
                if old["status"] != "failed":
                    self.store.member(name, {"status": "failed", "error": f"Native process exited: {client.process.returncode}"})
                    self.store.notice(name, self.store.gateway, "Native process exited. Its tasks need reconciliation; independent members continue.")

    async def approvals(self):
        with self.store.read() as db:
            requests = [json.loads(row[0]) for row in db.execute("SELECT data FROM approvals")]
        for request in requests:
            if request["state"] != "respond" or request["generation"] != self.generation:
                continue
            client = self.codex.get(request["member"])
            if not client:
                continue
            # Persist uncertainty before sending; never replay approval on a new connection.
            with self.store.tx() as db:
                current = json.loads(db.execute("SELECT data FROM approvals WHERE id=?", (request["id"],)).fetchone()[0])
                if current["state"] != "respond":
                    continue
                current["state"] = "submitted"
                db.execute("UPDATE approvals SET data=? WHERE id=?", (dumps(current), request["id"]))
            await client.respond(request["request_id"], request["response"])

    async def dispatch(self):
        room = self.store.room()
        if room.get("mode_transition"):
            return  # Drain already-running turns; no new starts or steering during this restart.
        paused = {"waiting_permission", "waiting_native_input", "failed", "stopped"}
        queues = {}
        with self.store.read() as db:
            for name in MODES[room["mode"]]:
                member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                if member["status"] not in paused:
                    direct = [dict(row) for row in db.execute(
                        f"SELECT * FROM messages WHERE status='queued' AND recipient=? "
                        f"AND NOT {FYI_CONTEXT_SQL} ORDER BY seq LIMIT 20", (name,))]
                    fyis = [dict(row) for row in db.execute(
                        f"SELECT * FROM messages WHERE status='queued' AND recipient=? "
                        f"AND {FYI_CONTEXT_SQL} ORDER BY seq LIMIT 20", (name,))]
                    # Bound each delivery class per inbox. Keep direct work first,
                    # but do not let a full direct batch starve required room FYIs.
                    queues[name] = direct + fyis
        queues = {target: messages for target, messages in queues.items() if messages}

        # Keep direct priority and FIFO within each delivery class, but start
        # every selected recipient queue together. A slow direct recipient
        # cannot hold other members' FYI notifications behind a phase barrier.
        outcomes = await asyncio.gather(
            *(self._dispatch_member_queue(target, queue, paused) for target, queue in queues.items()),
            return_exceptions=True,
        )
        # Wait for every selected recipient queue before surfacing an
        # unexpected failure; unfinished attempts remain recoverable as unknown.
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

    async def _dispatch_member_queue(self, target, messages, paused):
        """Dispatch one recipient's ordered queue without overlapping its native turns."""
        for message in messages:
            if self.store.room().get("mode_transition"):
                return
            member = self.store.member(target)
            if member["status"] in paused:
                continue
            context = json.loads(message["context"])
            fyi = bool(context.get("broadcast") or context.get("admin_relay"))
            # Refresh per actionable delivery: an earlier failed FYI is not an unknown side effect.
            message["pending_recovery"] = False
            if not fyi:
                with self.store.read() as db:
                    message["pending_recovery"] = bool(db.execute(
                        f"SELECT 1 FROM messages WHERE recipient=? AND status IN ('failed','unknown') "
                        f"AND NOT {FYI_CONTEXT_SQL} LIMIT 1", (target,)).fetchone())
            if message["task"] and not fyi:
                review_submission = context.get("review_submission")
                message["context_pack"] = (self.store.review_packet(message["task"], review_submission)
                                            if review_submission else
                                            self.store.task_context(message["task"], compact=True))
            attempt = self.store.begin_attempt(message, self.generation)
            if not attempt:
                continue
            if "knowledge_reference" in attempt:
                message["knowledge_reference"] = attempt["knowledge_reference"]
            turn_id = None
            try:
                if target == self.store.gateway and self.gateway_client:
                    result = await self.gateway_client.send(message | {"native_text": message_text(message)})
                elif target.startswith("CODEX"):
                    # Mode, effort override or gateway sync takes effect on the next Codex turn.
                    self.codex[target].model_config = requested_config(member, target)
                    result = await self.codex[target].send(message)
                    turn_id = self.codex[target].last_sent_turn_id
                    self.store.member(target, {"status": "working", "turn_id": self.codex[target].turn_id})
                else:
                    sender = self.store.member(message["sender"])
                    mode = sender.get("permission_class", "bypass" if sender.get("permission_mode") in {"bypassPermissions", "plan"} else "prompting")
                    result = await asyncio.to_thread(send_claude, self.store.project, member["native_id"], message, mode)
                detail = "Native submission only; recipient processing remains unconfirmed"
            except RoomError as exc:
                result = "unknown" if exc.code == "outcome_unknown" else "failed"
                detail = str(exc)
            except OSError as exc:
                result, detail = "unknown", str(exc)
            self.store.finish_dispatch(attempt["id"], result, detail, turn_id)
            # One main notice per recipient failure episode, never a notice-about-notice loop.
            if result in {"failed", "unknown"} and not message["pending_recovery"] and target != self.store.gateway:
                self.store.notice(target, self.store.gateway, f"Delivery {message['id']} to {target} is {result}. Inspect pending inbox/status and reconcile effects before retrying; independent work can continue.")

    async def refresh_claude(self, force=False):
        if not self.claude or (not force and time.monotonic() - self.last_registry_check < 4):
            return
        self.last_registry_check = time.monotonic()
        agents = await asyncio.to_thread(claude_agents, self.store.project)
        for name, native_id in self.claude.items():
            matches = [x for x in agents if x.get("sessionId") == native_id and Path(x.get("cwd", "/nonexistent")).resolve() == self.store.project]
            # A retained dead background row can accompany the live interactive
            # row for one saved session. Inspect every exact row before reporting
            # exit, and keep the stamp from that one liveness observation.
            inspected, pid_stamps = [], {}
            for row in matches:
                pid = row.get("pid")
                valid_pid = type(pid) is int and pid >= 2
                if valid_pid and pid not in pid_stamps:
                    pid_stamps[pid] = process_stamp(pid)
                row_stamp = pid_stamps.get(pid) if valid_pid else None
                terminal_row = (row.get("kind") == "background"
                                and row.get("state") in {"stopped", "failed", "done"}
                                and row.get("status") is None
                                and (pid is None or valid_pid))
                inspected.append((row, row_stamp, terminal_row))
            live = [(row, row_stamp) for row, row_stamp, _ in inspected if row_stamp]
            # Only affirmative inactive terminal rows may be dismissed beside
            # the sole live row; another blocked/unverifiable row remains held.
            single_live = len(live) == 1 and all(row_stamp or terminal_row for _, row_stamp, terminal_row in inspected)
            native = (live[0][0] if single_live else
                      next((row for row, row_stamp, terminal_row in inspected if not row_stamp and not terminal_row),
                           matches[0] if matches else None))
            stamp = live[0][1] if single_live else None
            observation = {key: native.get(key) if native else None for key in ("kind", "state", "status")}
            if len(matches) > 1:
                observation.update(matching_rows=len(matches), live_rows=len(live))
            if not stamp:
                # A public blocked row can mean assistant-reported needs or a
                # native prompt. Missing liveness does not prove terminal exit.
                terminal = bool(inspected) and not live and all(terminal_row for _, _, terminal_row in inspected)
                observation["reason"] = "native_terminal_observed" if terminal else "native_registry_requires_reconciliation"
                detail = "Claude session exited" if terminal else "Claude session liveness unavailable"
                self.store.interrupt_attempts(f"{detail}; no native turn result was observed", name, self.generation)
                with self.store.tx() as db:
                    old = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                    changes = {"status": "stopped", "native_observation": observation}
                    if old.get("error") in {None, CLAUDE_EXIT_ERROR, CLAUDE_LIVENESS_ERROR}:
                        changes["error"] = CLAUDE_EXIT_ERROR if terminal else CLAUDE_LIVENESS_ERROR
                    if not terminal and old.get("error") == CLAUDE_EXIT_ERROR:
                        changes["previous_native_exit_error"] = CLAUDE_EXIT_ERROR
                    db.execute("UPDATE members SET data=? WHERE name=?", (dumps(dict(old, **changes)), name))
                continue
            waiting = native.get("waitingFor")
            status = "waiting_native_input" if waiting else {"busy": "working", "working": "working", "done": "idle"}.get(native.get("status"), native.get("status", "unknown"))
            with self.store.tx() as db:
                old = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                observation["reason"] = "native_live_observed"
                changes = {"status": status, "pid": native["pid"], "stamp": stamp,
                           "native_observation": observation}
                # Registry loss can be transient during a native job's PID handover.
                # Read and clear the specific error in one transaction so a new
                # identity/permission error cannot be overwritten by a stale read.
                if (old.get("error") in {CLAUDE_EXIT_ERROR, CLAUDE_LIVENESS_ERROR}
                        and old.get("native_id") == native_id
                        and old.get("launch_generation") == self.generation
                        and not old.get("unexpected_native_id")
                        and status in {"idle", "working", "waiting_native_input"}):
                    changes["error"] = None
                db.execute("UPDATE members SET data=? WHERE name=?", (dumps(dict(old, **changes)), name))
            if waiting and old["status"] != status:
                self.store.notice(name, self.store.gateway, f"Native Claude session {native_id} waits for {waiting}. Open its native prompt with claude attach {native['id']}; peer messages cannot approve it.")

    def request_upgrade(self):
        """A newly activated release replaces this supervisor and its workers at the next all-idle point.

        It reuses the mode-change drain: no new dispatch, running turns and native prompts finish, then exact-session
        restart through the stable launcher, which now resolves to the activated release.
        """
        if time.monotonic() < self.next_release_check:
            return
        self.next_release_check = time.monotonic() + 30
        if not follows_pointer():
            return  # A development or pinned copy would restart into itself and loop.
        target = active_release()
        if not target or target["version"] == __version__:
            return
        with self.store.tx() as db:
            room = self.store.get_room(db)
            if room.get("mode_transition") or room["generation"] != self.generation or room["manual_stop"]:
                return
            room.update(restart_requested=True, mode_transition={"from": room["mode"], "to": room["mode"], "state": "draining",
                                                                 "reason": "upgrade", "release": target["version"]})
            self.store.put_room(db, room)
            self.store.event(db, "room.upgrade", {"from": __version__, "to": target["version"], "root": target["root"]})

    def check_hook_silence(self, now_ts=None, quiet=600):
        """Warn once for later input lacking a heartbeat, with a grace period.

        A long tool turn alone is not a missing hook. This notice is diagnostic
        evidence, never proof that a prompt is human or that a restart is needed.
        """
        now_ts = now_ts if now_ts is not None else time.time()
        if now_ts < self.next_hook_check:
            return False
        self.next_hook_check = now_ts + 300
        member = self.store.member(self.store.gateway)
        transcript, seen = member.get("transcript"), member.get("hook_seen")
        if not transcript or not seen or member.get("hook_silence_warned") == seen:
            return False
        try:
            stamp = datetime.fromisoformat(seen)
            if stamp.tzinfo is None:
                return False
            last = stamp.timestamp()
        except (ValueError, TypeError):
            return False
        if now_ts - last < quiet:
            return False
        owner = self.store.room().get("owner") or {}
        host, session = owner.get("host", "claude"), owner.get("session")
        if member.get("native_id") != session:
            return False
        try:
            recorded = hook_input_observations(self.store, session, transcript)
            observed = unobserved_input_timestamp(transcript, self.store.project, host, session, last, now_ts, quiet, recorded)
        except (RoomError, sqlite3.Error, OSError):
            return False  # Hook diagnostics cannot block room work when their evidence is unavailable.
        if not observed or self.store.member(self.store.gateway).get("hook_seen") != seen:
            return False  # A hook may have arrived while the bounded tail was inspected.
        self.store.member(self.store.gateway, {"hook_silence_warned": seen})
        self.store.notice(self.store.gateway, self.store.gateway,
                          f"Hook activity diagnostic: a later input row at {observed} has no newer recorded room hook after {seen}. "
                          f"Check this {host.title()} session's trusted/enabled hooks and hook errors before choosing recovery.")
        return True

    async def import_global_queue(self):
        """Import data for this already running room, never start a recipient room."""
        if time.monotonic() < self.next_global_queue_check:
            return
        self.next_global_queue_check = time.monotonic() + 2
        try:
            return await asyncio.to_thread(GlobalSpace(timeout=0.05).import_queue, self.store)
        except (RoomError, sqlite3.Error, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            self.next_global_queue_check = time.monotonic() + 30
            try:
                with self.store.tx(timeout=0.05) as db:
                    self.store.event(db, "agents_space.queue_unavailable", {"error": f"{type(exc).__name__}: {exc}"})
            except (RoomError, sqlite3.Error, OSError):
                pass  # A locked local ledger cannot record a diagnostic yet.
            return {"queued": 0, "error": str(exc)}

    async def watch_catalogs(self):
        """Ask the shared ledger whether this supervisor should look at plugin catalogs now (plan N-ac8dc5ab)."""
        if time.monotonic() < self.next_catalog_check:
            return
        self.next_catalog_check = time.monotonic() + 60
        try:
            await asyncio.to_thread(check_catalogs)
        except (RoomError, sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
            with self.store.tx() as db:  # Catalog news is optional; it never disturbs the room.
                self.store.event(db, "agents_space.catalog_check_failed", {"error": f"{type(exc).__name__}: {exc}"})

    def finish_mode_restart(self):
        """Drain idle workers and confirmed terminal Claude jobs during an upgrade.

        A stopped ledger state alone is insufficient. The preceding forced native
        refresh must prove terminal exit; identity, process and approvals stay held.
        This permits owned cleanup, without claiming a task or native result passed.
        """
        self.terminal_upgrade_members = {}
        with self.store.tx() as db:
            room = self.store.get_room(db)
            transition = room.get("mode_transition")
            if (not transition or not room.get("restart_requested") or room["generation"] != self.generation
                    or room.get("manual_stop")):
                return False
            pending_approvals = any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
                                    for row in db.execute("SELECT data FROM approvals"))
            waiting, terminal_members = [], {}
            for name in (*self.codex, *self.claude):
                member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                exited_claude = False
                if (transition.get("reason") == "upgrade" and name in self.claude
                        and member["status"] == "stopped" and not pending_approvals
                        and member.get("native_id") == self.claude[name]
                        and member.get("launch_generation") == self.generation
                        and not member.get("unexpected_native_id") and not member.get("turn_id")):
                    observed = member.get("native_observation") or {}
                    pid = member.get("pid")
                    valid_pid = type(pid) is int and pid >= 2
                    exited_claude = (
                        observed.get("reason") == "native_terminal_observed"
                        and observed.get("kind") == "background"
                        and observed.get("state") in {"stopped", "failed", "done"}
                        and observed.get("status") is None
                        and (pid is None or valid_pid)
                        and process_alive(pid, member.get("stamp")) is False
                        and (pid is None or process_stamp(pid) is None))
                    if exited_claude:
                        terminal_members[name] = {key: member.get(key) for key in
                                                  ("native_id", "launch_generation", "pid", "stamp")}
                if ((member["status"] != "idle" and not exited_claude)
                        or (name in self.codex and self.codex[name].turn_id)):
                    waiting.append(name)
            transition["waiting"] = waiting
            if not waiting:
                transition["state"] = "restarting"
                room["status"] = "stopping"
                self.terminal_upgrade_members = terminal_members
            self.store.put_room(db, room)
            return not waiting

    async def shutdown(self):
        failures = []
        terminal_cleanup_error, terminal_cleanup_held = None, False
        if self.terminal_upgrade_members:
            try:
                await self.refresh_claude(force=True)
            except (OSError, RoomError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, AttributeError) as exc:
                terminal_cleanup_error = str(exc)
        if self.gateway_client:
            try:
                await self.gateway_client.close()
            except (OSError, RoomError) as exc:
                failures.append(f"Gateway proxy: {exc}")
        for name, client in self.codex.items():
            try:
                await client.stop()
                self.store.member(name, {"status": "stopped", "pid": None, "stamp": None, "turn_id": None})
            except (OSError, RoomError) as exc:
                failures.append(f"{name}: {exc}")
        for name, native_id in self.claude.items():
            admitted = self.terminal_upgrade_members.get(name)
            try:
                if not native_id:
                    continue  # No launch identity was observed; do not touch another session.
                if admitted:
                    # The recorded process has already exited. Never resolve and
                    # stop this native session again: another controller may own it.
                    if terminal_cleanup_error:
                        raise RoomError(terminal_cleanup_error, "cleanup")
                    with self.store.tx() as db:
                        room = self.store.get_room(db)
                        member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                        observed = member.get("native_observation") or {}
                        pending = any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
                                      for row in db.execute("SELECT data FROM approvals"))
                        if (room["generation"] != self.generation or pending
                                or any(member.get(key) != value for key, value in admitted.items())
                                or member["status"] != "stopped" or member.get("turn_id")
                                or member.get("unexpected_native_id")
                                or observed.get("reason") != "native_terminal_observed"
                                or observed.get("kind") != "background"
                                or observed.get("state") not in {"stopped", "failed", "done"}
                                or observed.get("status") is not None
                                or process_alive(admitted["pid"], admitted["stamp"]) is not False
                                or (admitted["pid"] is not None and process_stamp(admitted["pid"]) is not None)):
                            raise RoomError("Saved session, process or approval changed after terminal admission", "cleanup")
                        db.execute("UPDATE members SET data=? WHERE name=?",
                                   (dumps(dict(member, status="stopped", pid=None, stamp=None)), name))
                    continue
                member = self.store.member(name)
                await stop_claude_worker(self.store.project, native_id, member)
                self.store.member(name, {"status": "stopped", "pid": None, "stamp": None})
            except (OSError, RoomError, subprocess.TimeoutExpired) as exc:
                if admitted:
                    terminal_cleanup_held = True
                    failures.append(f"{name}: Terminal upgrade cleanup held: {exc}")
                else:
                    failures.append(f"{name}: {exc}")
        with self.store.tx() as db:
            room = self.store.get_room(db)
            if room["generation"] == self.generation:
                if (self.terminal_upgrade_members and not terminal_cleanup_held
                        and any(json.loads(row[0])["state"] in {"pending", "respond", "submitted"}
                                for row in db.execute("SELECT data FROM approvals"))):
                    terminal_cleanup_held = True
                    failures.append("Terminal upgrade cleanup held: Native approval arrived before shutdown commit")
                for row in ([] if terminal_cleanup_held else db.execute("SELECT id,data FROM approvals").fetchall()):
                    approval = json.loads(row["data"])
                    if approval["generation"] == self.generation and approval["state"] in {"pending", "respond", "submitted"}:
                        approval.update(state="expired", detail="Native connection closed; no response can be delivered")
                        db.execute("UPDATE approvals SET data=? WHERE id=?", (dumps(approval), row["id"]))
                room.update(status="failed" if failures or self.error else "stopped",
                            error="; ".join(failures) or self.error, supervisor=None)
                self.store.put_room(db, room)
                if not failures and self.recovered:
                    db.execute("DELETE FROM claims WHERE owner != ?", (self.store.gateway,))
                    # A stopped writer's task must be re-claimed before further editing.
                    for row in db.execute("SELECT id,version,data FROM tasks").fetchall():
                        task = json.loads(row["data"])
                        if task["owner"] != self.store.gateway and task["state"] == "running":
                            task["state"] = "ready"
                            self.store.save(db, "tasks", task, row["version"])
        self.store.interrupt_attempts("Room stopped without a confirmed native result; reconcile before retry", generation=self.generation)
        self.store.project_views()

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: setattr(self, "stopping", True))
        with file_lock(self.store.runtime / "supervisor.lock", blocking=False):
            try:
                await self.launch()
                while not self.stopping and self.owner_alive() and self.store.room()["status"] != "stopping":
                    await self.refresh_gateway()
                    await self.native_events()
                    await self.approvals()
                    self.request_upgrade()
                    await self.watch_catalogs()
                    self.check_hook_silence()
                    await self.refresh_claude(force=bool(self.store.room().get("mode_transition")))
                    if self.finish_mode_restart():
                        break
                    await self.import_global_queue()
                    await self.dispatch()
                    await asyncio.sleep(.3)
            except (RoomError, OSError, ValueError, KeyError) as exc:
                self.error = str(exc)
            finally:
                await self.shutdown()
        room = self.store.room()
        if (room.get("restart_requested") and not room["manual_stop"] and room["status"] == "stopped" and self.owner_alive()
                and (room.get("mode_transition") or {}).get("reason") != "handoff"):
            await asyncio.to_thread(start_room, self.store, room["owner"]["session"], permission_mode=room["owner"]["permission_mode"], automatic=True)
