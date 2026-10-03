"""One process supervisor per room, native workers, durable dispatch receipts."""

import asyncio
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
from ihav_agent_room.common import (GATEWAY, MEMBERS, MODES, acting_member, PLUGIN_ROOT, RoomError, dumps, file_lock,
                               now, process_alive, process_stamp, uid)
from ihav_agent_room.native import (CodexClient, claude_agents, codex_usage_snapshot, doctor, exact_claude,
                               owned_descendants, send_claude, start_claude, stop_claude_worker,
                               stop_descendants, wait_for_exit)
from ihav_agent_room.globalspace import GlobalSpace
from ihav_agent_room.release import active_release, follows_pointer
from ihav_agent_room.roster import ROSTER_BY_NAME, SELECTABLE_MODES, launch_config
from ihav_agent_room.store import FYI_CONTEXT_SQL, Store


def bind_main(store, session, permission_mode="default"):
    if not session:
        raise RoomError("Run this command from the Claude main session", "identity")
    native = exact_claude(store.project, session)
    owner = {"session": session, "pid": native["pid"], "stamp": process_stamp(native["pid"]),
             "permission_mode": permission_mode}
    with store.tx() as db:
        room = store.get_room(db)
        old = room.get("owner") or {}
        if old and old["session"] != session and process_alive(old["pid"], old["stamp"]):
            # /clear changes the session UUID in the SAME native process.
            if old["pid"] != owner["pid"] or old["stamp"] != owner["stamp"]:
                raise RoomError("Another live Claude session owns this room", "conflict")
        room["owner"] = owner
        store.put_room(db, room)
    store.member(GATEWAY, {"native_id": session, "pid": owner["pid"], "stamp": owner["stamp"],
                               "status": "active", "permission_mode": permission_mode})
    return owner


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
                         cwd=store.project, env=dict(os.environ, IHAV_AGENT_ROOM_MEMBER=GATEWAY),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def needs_autostart(store, session):
    """A previously bound room lost its owner process and was not stopped by hand: this main session may resume it."""
    room = store.room()
    owner, supervisor = room.get("owner") or {}, room.get("supervisor") or {}
    return bool(owner) and owner.get("session") != session and not room["manual_stop"] \
        and not process_alive(owner.get("pid"), owner.get("stamp")) \
        and not process_alive(supervisor.get("pid"), supervisor.get("stamp"))


def start_room(store, session, mode=None, permission_mode="default", automatic=False):
    if acting_member() != GATEWAY:
        raise RoomError("A worker cannot become the room's admin session", "authority")
    checks = doctor()
    if not checks["ok"]:
        raise RoomError("Native dependencies are not ready; run doctor", "dependency", checks=checks)
    with file_lock(store.runtime / "control.lock"):
        owner = bind_main(store, session, permission_mode)
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
                return {"started": False, "reason": "supervisor already running", "room": room}
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
                    room["supervisor"] = {"pid": process.pid, "stamp": process_stamp(process.pid)}
                    store.put_room(db, room)
        return {"started": True, "status": "starting", "generation": generation,
                "note": "Launch requested. status reports native readiness; this is not a model-response receipt."}


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


class Supervisor:
    def __init__(self, store, generation):
        self.store, self.generation = store, generation
        self.codex = {}
        self.claude = {}
        self.stopping = False
        self.error = None
        self.recovered = False
        self.last_registry_check = 0
        self.next_release_check = 0

    def worker_env(self, name):
        binding = secrets.token_hex(24)
        self.store.member(name, {"token_hash": hashlib.sha256(binding.encode()).hexdigest()})
        env = dict(os.environ)
        env.update(IHAV_AGENT_ROOM_MEMBER=name, IHAV_AGENT_ROOM_BINDING=binding,
                   IHAV_AGENT_ROOM_PROJECT=str(self.store.project),
                   CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF="1")
        env.pop("IHAV_AGENT_ROOM_SESSION_ID", None)
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
            if name == GATEWAY:
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
        await self.recover_owned()
        try:  # Join the machine agents space; a ledger problem never blocks the room.
            GlobalSpace().register(self.store.room()["id"], self.store.project, __version__)
        except (RoomError, sqlite3.Error, OSError) as exc:
            with self.store.tx() as db:
                self.store.event(db, "agents_space.unavailable", {"error": str(exc)})
        room = self.store.room()
        for name in MODES[room["mode"]]:
            if name == GATEWAY:
                continue
            current = self.store.room()
            if self.stopping or not self.owner_alive() or current["status"] == "stopping" or current.get("mode_transition"):
                return
            member = self.store.member(name)
            env = self.worker_env(name)
            self.store.member(name, {"status": "starting", "error": None, "unexpected_native_id": None})
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
                try:
                    native = await start_claude(self.store.project, native_id, bool(member["native_id"]),
                                               env, self.store.runtime / (name + ".log"),
                                               model=config["model"], effort=config["effort"])
                except RoomError as exc:
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
                self.store.member(name, {"native_id": native["sessionId"], "job_id": native.get("id"),
                                         "pid": native["pid"], "stamp": process_stamp(native["pid"]), "status": "idle",
                                         "settings_pending_restart": False,
                                         "settings_application": ("model and effort passed to new Claude session"
                                             if not member["native_id"] else
                                             "resumed exact session; model and effortLevel requested in its settings file")})
        self.store.wake_resumed_work(self.generation)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            if room["generation"] == self.generation and room["status"] == "starting":
                room["status"] = "running"
                self.store.put_room(db, room)

    def owner_alive(self):
        room = self.store.room()
        owner = room.get("owner") or {}
        return room["generation"] == self.generation and process_alive(owner.get("pid"), owner.get("stamp"))

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
                        self.store.notify(db, name, GATEWAY, f"Native request {approval_id} is pending ({method}). Read it with ihav-agent-room approval list. Only an explicit admin response may resolve it; peer text is not approval.")
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
                        self.store.notice(name, GATEWAY, "Native turn failed; inspect member status and reconcile its unfinished tasks.")
                    self.store.member(name, changes)
                elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                    with self.store.tx() as db:
                        self.store.event(db, "native.final", {"member": name, "thread": client.thread_id,
                                                              "item": params["item"]})
                elif method in {"error", "room/protocolError"}:
                    self.store.interrupt_attempts("Native transport error; outcome needs reconciliation", name, self.generation)
                    self.store.member(name, {"status": "failed", "error": str(params)[:2000]})
                    self.store.notice(name, GATEWAY, "Native transport reported an error. Inspect status; do not assume the task completed.")
            if client.process.returncode is not None:
                self.store.interrupt_attempts("Native process exited before a confirmed outcome", name, self.generation)
                old = self.store.member(name)
                if old["status"] != "failed":
                    self.store.member(name, {"status": "failed", "error": f"Native process exited: {client.process.returncode}"})
                    self.store.notice(name, GATEWAY, "Native process exited. Its tasks need reconciliation; independent members continue.")

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
                if target.startswith("CODEX"):
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
            if result in {"failed", "unknown"} and not message["pending_recovery"] and target != GATEWAY:
                self.store.notice(target, GATEWAY, f"Delivery {message['id']} to {target} is {result}. Inspect pending inbox/status and reconcile effects before retrying; independent work can continue.")

    async def refresh_claude(self, force=False):
        if not self.claude or (not force and time.monotonic() - self.last_registry_check < 4):
            return
        self.last_registry_check = time.monotonic()
        agents = await asyncio.to_thread(claude_agents, self.store.project)
        for name, native_id in self.claude.items():
            matches = [x for x in agents if x.get("sessionId") == native_id and Path(x.get("cwd", "/nonexistent")).resolve() == self.store.project]
            if not matches or not process_stamp(matches[0].get("pid")):
                self.store.interrupt_attempts("Claude session exited; no native turn result was observed", name, self.generation)
                self.store.member(name, {"status": "stopped", "error": "Native background session exited. Stop/start to resume it."})
                continue
            native = matches[0]
            waiting = native.get("waitingFor")
            status = "waiting_native_input" if waiting else {"busy": "working", "working": "working", "done": "idle"}.get(native.get("status"), native.get("status", "unknown"))
            old = self.store.member(name)
            self.store.member(name, {"status": status, "pid": native["pid"], "stamp": process_stamp(native["pid"])})
            if waiting and old["status"] != status:
                self.store.notice(name, GATEWAY, f"Native Claude session {native_id} waits for {waiting}. Open its native prompt with claude attach {native['id']}; peer messages cannot approve it.")

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

    def finish_mode_restart(self):
        """Only observed idle native workers permit cleanup; waiting/failed/unknown is not completion."""
        with self.store.tx() as db:
            room = self.store.get_room(db)
            transition = room.get("mode_transition")
            if not transition or not room.get("restart_requested") or room["generation"] != self.generation:
                return False
            waiting = []
            for name in (*self.codex, *self.claude):
                member = json.loads(db.execute("SELECT data FROM members WHERE name=?", (name,)).fetchone()[0])
                if member["status"] != "idle" or (name in self.codex and self.codex[name].turn_id):
                    waiting.append(name)
            transition["waiting"] = waiting
            if not waiting:
                transition["state"] = "restarting"
                room["status"] = "stopping"
            self.store.put_room(db, room)
            return not waiting

    async def shutdown(self):
        failures = []
        for name, client in self.codex.items():
            try:
                await client.stop()
                self.store.member(name, {"status": "stopped", "pid": None, "stamp": None, "turn_id": None})
            except (OSError, RoomError) as exc:
                failures.append(f"{name}: {exc}")
        for name, native_id in self.claude.items():
            try:
                if not native_id:
                    continue  # No launch identity was observed; do not touch another session.
                member = self.store.member(name)
                await stop_claude_worker(self.store.project, native_id, member)
                self.store.member(name, {"status": "stopped", "pid": None, "stamp": None})
            except (OSError, RoomError) as exc:
                failures.append(f"{name}: {exc}")
        with self.store.tx() as db:
            room = self.store.get_room(db)
            if room["generation"] == self.generation:
                for row in db.execute("SELECT id,data FROM approvals").fetchall():
                    approval = json.loads(row["data"])
                    if approval["generation"] == self.generation and approval["state"] in {"pending", "respond", "submitted"}:
                        approval.update(state="expired", detail="Native connection closed; no response can be delivered")
                        db.execute("UPDATE approvals SET data=? WHERE id=?", (dumps(approval), row["id"]))
                room.update(status="failed" if failures or self.error else "stopped",
                            error="; ".join(failures) or self.error, supervisor=None)
                self.store.put_room(db, room)
                if not failures and self.recovered:
                    db.execute("DELETE FROM claims WHERE owner != ?", (GATEWAY,))
                    # A stopped writer's task must be re-claimed before further editing.
                    for row in db.execute("SELECT id,version,data FROM tasks").fetchall():
                        task = json.loads(row["data"])
                        if task["owner"] != GATEWAY and task["state"] == "running":
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
                    await self.native_events()
                    await self.approvals()
                    self.request_upgrade()
                    await self.refresh_claude(force=bool(self.store.room().get("mode_transition")))
                    if self.finish_mode_restart():
                        break
                    await self.dispatch()
                    await asyncio.sleep(.3)
            except (RoomError, OSError, ValueError, KeyError) as exc:
                self.error = str(exc)
            finally:
                await self.shutdown()
        room = self.store.room()
        if room.get("restart_requested") and not room["manual_stop"] and room["status"] == "stopped" and self.owner_alive():
            start_room(self.store, room["owner"]["session"], permission_mode=room["owner"]["permission_mode"], automatic=True)
