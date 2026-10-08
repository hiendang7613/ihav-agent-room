"""Codex owns its existing host thread; the room owns only background workers."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from ihav_agent_room.cli import parser, run
from ihav_agent_room.codex_gateway import CodexGateway
from ihav_agent_room.common import RoomError, acting_member, main_session_id
from ihav_agent_room.hooks import handle
from ihav_agent_room.provenance import assess, transcript_size
from ihav_agent_room.runtime import bind_main, connection_plan, drain_init_handoff, reconnect_codex_host, request_stop, Supervisor
from test_evidence import EvidenceFixture
import test_runtime as runtime_tests


class CodexGatewayTests(EvidenceFixture, unittest.TestCase):
    def codex_owner(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(gateway="CODEX_01", owner={"host": "codex", "session": "host-codex"}, status="running")
            self.store.put_room(db, room)

    def test_codex_host_identity_and_dynamic_authority(self):
        self.codex_owner()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host-codex"}, clear=True):
            self.assertEqual(main_session_id(), "host-codex")
            self.assertEqual(acting_member(), "CODEX_01")
            self.assertEqual(self.store.actor(), "CODEX_01")
            self.store.main_only("CODEX_01")
            with self.assertRaises(RoomError):
                self.store.main_only("CLAUDE_01")
            with patch.dict(os.environ, CODEX_THREAD_ID="different"):
                with self.assertRaises(RoomError):
                    self.store.actor()
            with patch.dict(os.environ, IHAV_AGENT_ROOM_HOST="claude", IHAV_AGENT_ROOM_MEMBER="CODEX_01"):
                with self.assertRaises(RoomError):
                    self.store.actor()

    def test_worker_cannot_become_gateway_by_selecting_codex_host(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "worker-thread", "IHAV_AGENT_ROOM_MEMBER": "CODEX_01",
                                     "IHAV_AGENT_ROOM_BINDING": "worker-token"}, clear=True):
            with patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock) as probe:
                with self.assertRaises(RoomError) as caught:
                    bind_main(self.store, "worker-thread")
                self.assertEqual(caught.exception.code, "authority")
                probe.assert_not_awaited()
                with self.assertRaises(RoomError):
                    run(parser().parse_args(["--project", str(self.project), "init", "--no-start"]))

    def test_connect_from_different_thread_requires_exact_saved_session_without_writes(self):
        self.store.member("CODEX_01", {"native_id": "saved-worker-thread"})
        before = self.store.room()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "new-host-thread"}, clear=True):
            result = run(parser().parse_args(["--project", str(self.project), "connect", "--handoff"]))
        self.assertFalse(result["connected"])
        self.assertTrue(result["resume_required"])
        self.assertEqual(result["saved_codex_session"], "saved-worker-thread")
        self.assertEqual(result["next"]["argv"], ["codex", "--cd", str(self.project.resolve()), "resume", "saved-worker-thread"])
        self.assertEqual(self.store.room(), before)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], "saved-worker-thread")

    def test_init_wrong_thread_leaves_room_and_project_files_unchanged(self):
        self.check_wrong_thread_leaves_room_and_project_files_unchanged("init")

    def test_start_wrong_thread_leaves_room_and_project_files_unchanged(self):
        self.check_wrong_thread_leaves_room_and_project_files_unchanged("start")

    def check_wrong_thread_leaves_room_and_project_files_unchanged(self, command):
        self.store.member("CODEX_01", {"native_id": "saved-worker-thread"})
        before = self.store.room()
        files = {path: path.read_bytes() for path in self.project.rglob("*.md")}
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "different-thread"}, clear=True), \
                patch("ihav_agent_room.cli.doctor", return_value={"ok": True}), \
                patch("ihav_agent_room.cli.probe_codex", new_callable=AsyncMock) as probe, \
                patch("ihav_agent_room.cli.start_room") as start:
            result = run(parser().parse_args(["--project", str(self.project), command]))
        self.assertFalse(result["connected"])
        self.assertTrue(result["resume_required"])
        probe.assert_not_awaited()
        start.assert_not_called()
        self.assertEqual(self.store.room(), before)
        self.assertEqual(files, {path: path.read_bytes() for path in self.project.rglob("*.md")})

    def test_init_missing_host_capability_does_not_scaffold_a_new_room(self):
        self.check_missing_host_capability_does_not_scaffold_a_new_room("init")

    def test_start_missing_host_capability_does_not_scaffold_a_new_room(self):
        self.check_missing_host_capability_does_not_scaffold_a_new_room("start")

    def check_missing_host_capability_does_not_scaffold_a_new_room(self, command):
        project = self.project / "new-project"
        project.mkdir()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host"}, clear=True), \
                patch("ihav_agent_room.cli.doctor", return_value={"ok": True}), \
                patch("ihav_agent_room.cli.probe_codex", new_callable=AsyncMock,
                      side_effect=RoomError("Queue unavailable", "incompatible")):
            with self.assertRaises(RoomError):
                run(parser().parse_args(["--project", str(project), command]))
        self.assertEqual(list(project.iterdir()), [])

    def test_bundled_sdk_loads_without_site_packages(self):
        result = subprocess.run([sys.executable, "-S", "-c",
            "from ihav_agent_room.codex_gateway import unix_connect; import websockets; "
            "assert unix_connect is not None; assert '.whl/' in websockets.__file__"],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_from_ordinary_shell_returns_saved_session_without_native_calls_or_writes(self):
        self.store.member("CODEX_01", {"native_id": "saved-worker-thread"})
        before = self.store.room()
        with patch.dict(os.environ, {}, clear=True), \
                patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock) as probe, \
                patch("ihav_agent_room.cli.start_room") as start:
            result = run(parser().parse_args(["--project", str(self.project), "connect", "--check"]))
        self.assertFalse(result["connected"])
        self.assertFalse(result["can_connect"])
        self.assertTrue(result["codex_host_required"])
        self.assertTrue(result["resume_required"])
        self.assertIsNone(result["current_codex_session"])
        self.assertEqual(result["next"]["argv"], ["codex", "--cd", str(self.project.resolve()), "resume", "saved-worker-thread"])
        probe.assert_not_awaited()
        start.assert_not_called()
        self.assertEqual(self.store.room(), before)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], "saved-worker-thread")

    def test_check_without_saved_session_requires_codex_host(self):
        with patch.dict(os.environ, {}, clear=True):
            result = run(parser().parse_args(["--project", str(self.project), "connect", "--check"]))
        self.assertFalse(result["can_connect"])
        self.assertFalse(result["resume_required"])
        self.assertTrue(result["codex_host_required"])
        self.assertEqual(result["next"]["argv"], ["codex", "--cd", str(self.project.resolve())])

    def test_connect_write_still_rejects_shell_claude_and_worker(self):
        before = self.store.room()
        for env in ({}, {"CLAUDE_CODE_SESSION_ID": "claude-host"},
                    {"CODEX_THREAD_ID": "host", "IHAV_AGENT_ROOM_BINDING": "worker-token"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch("ihav_agent_room.cli.start_room") as start:
                with self.assertRaises(RoomError) as caught:
                    run(parser().parse_args(["--project", str(self.project), "connect", "--handoff"]))
                self.assertEqual(caught.exception.code, "identity")
                start.assert_not_called()
        self.assertEqual(self.store.room(), before)

    def test_start_cannot_overwrite_saved_codex_worker_identity(self):
        self.store.member("CODEX_01", {"native_id": "saved-worker-thread"})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "different"}, clear=True), \
                patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock):
            with self.assertRaises(RoomError) as caught:
                bind_main(self.store, "different", handoff=True)
        self.assertEqual(caught.exception.code, "identity")
        self.assertEqual(self.store.member("CODEX_01")["native_id"], "saved-worker-thread")

    def test_reconnect_uncertain_native_state_leaves_stopped_room_unchanged(self):
        self.codex_owner()
        self.store.member("CODEX_01", {"native_id": "host-codex"})
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["status"] = "stopped"
            self.store.put_room(db, room)
        before = self.store.room()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "new-host"}, clear=True), \
                patch("ihav_agent_room.runtime.process_alive", return_value=False), \
                patch("ihav_agent_room.runtime.probe_detached_codex", new_callable=AsyncMock,
                      side_effect=RoomError("Native read timed out", "outcome_unknown")):
            with self.assertRaises(RoomError) as caught:
                reconnect_codex_host(self.store, "new-host")
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(self.store.room(), before)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], "host-codex")
        self.assertFalse((self.store.runtime / "backups").exists())

    def test_host_identity_survives_switching_back_to_claude(self):
        self.codex_owner()
        self.store.member("CODEX_01", {"native_id": "host-codex"})
        self.store.member("CLAUDE_01", {"native_id": "claude-worker"})
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="stopped", host_sessions={"codex": "host-codex"})
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude-host"}, clear=True), \
                patch("ihav_agent_room.runtime.exact_claude", return_value={"pid": 42}), \
                patch("ihav_agent_room.runtime.process_alive", return_value=False), \
                patch("ihav_agent_room.runtime.process_stamp", return_value="stamp"):
            bind_main(self.store, "claude-host", handoff=True)
        self.assertEqual(self.store.gateway, "CLAUDE_01")
        self.assertIsNone(self.store.member("CODEX_01")["native_id"])
        self.assertEqual(self.store.room()["host_sessions"]["codex"], "host-codex")
        # A later background Codex worker is not the saved user conversation.
        self.store.member("CODEX_01", {"native_id": "new-codex-worker"})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host-codex"}, clear=True), \
                patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock), \
                patch("ihav_agent_room.runtime.process_alive", return_value=False):
            self.assertFalse(connection_plan(self.store, "host-codex")["resume_required"])
            bind_main(self.store, "host-codex", handoff=True)
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], "claude-worker")
        self.assertEqual(self.store.room()["background_sessions"]["CODEX_01"], "new-codex-worker")

    def test_manual_stop_during_init_drain_cancels_transfer(self):
        self.codex_owner()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["supervisor"] = {"pid": 42, "stamp": "stamp", "handoff_protocol": 1}
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude-host"}, clear=True), \
                patch("ihav_agent_room.runtime.exact_claude", return_value={"pid": 43}), \
                patch("ihav_agent_room.runtime.process_alive", return_value=True), \
                patch("ihav_agent_room.runtime.time.sleep", side_effect=lambda _: request_stop(self.store)):
            with self.assertRaises(RoomError) as caught:
                drain_init_handoff(self.store, "claude-host")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertTrue(self.store.room()["manual_stop"])
        self.assertEqual(self.store.gateway, "CODEX_01")
        self.assertEqual(self.store.room()["owner"]["session"], "host-codex")

    def test_saved_session_handoff_preserves_native_ids_history_mode_and_old_host(self):
        self.store.member("CODEX_01", {"native_id": "saved-worker-thread"})
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "old-claude", "pid": 42, "stamp": "stamp"}
            self.store.put_room(db, room)
        mode = self.store.room()["mode"]
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "saved-worker-thread"}, clear=True), \
                patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock), \
                patch("ihav_agent_room.runtime.process_alive", side_effect=lambda pid, stamp: pid == 42):
            bind_main(self.store, "saved-worker-thread", handoff=True)
        self.assertEqual(self.store.gateway, "CODEX_01")
        self.assertEqual(self.store.member("CODEX_01")["native_id"], "saved-worker-thread")
        self.assertEqual(self.store.room()["mode"], mode)
        self.assertEqual(self.store.room()["schema"], 4)
        self.assertIsNone(self.store.member("CLAUDE_01")["native_id"])
        with self.store.read() as db:
            events = [json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE kind='room.gateway_changed'")]
        self.assertEqual(events[0]["former_owner"]["session"], "old-claude")
        self.assertEqual(events[0]["previous_native_ids"]["CODEX_01"], "saved-worker-thread")
        with sqlite3.connect(events[0]["backup"]) as backup:
            old_room = json.loads(backup.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])
            saved_member = json.loads(backup.execute("SELECT data FROM members WHERE name='CODEX_01'").fetchone()[0])
        self.assertEqual(old_room["schema"], 3)
        self.assertEqual(old_room["owner"]["session"], "old-claude")
        self.assertEqual(saved_member["native_id"], "saved-worker-thread")

    def test_check_reports_running_room_and_preserves_it(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["status"] = "running"
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-host"}, clear=True):
            plan = connection_plan(self.store, "codex-host")
        self.assertFalse(plan["can_connect"])
        self.assertTrue(plan["blockers"])
        self.assertEqual(self.store.room()["status"], "running")

    def test_codex_owner_skipped_and_claude_worker_has_own_host_environment(self):
        self.codex_owner()
        supervisor = Supervisor(self.store, "generation")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host-codex", "IHAV_AGENT_ROOM_HOST": "codex",
                                     "CLAUDE_CODE_SESSION_ID": "stale-main"}, clear=True):
            env = supervisor.worker_env("CLAUDE_01")
        self.assertEqual(env["IHAV_AGENT_ROOM_HOST"], "claude")
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        token_hash = hashlib.sha256(env["IHAV_AGENT_ROOM_BINDING"].encode()).hexdigest()
        self.assertEqual(self.store.member("CLAUDE_01")["token_hash"], token_hash)

    def test_unfinished_work_blocks_host_switch_without_changing_state(self):
        self.task()
        before = self.store.room()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host-codex"}, clear=True), \
                patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock):
            with self.assertRaises(RoomError) as caught:
                bind_main(self.store, "host-codex")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(before, self.store.room())

    def test_live_claude_owner_blocks_codex_takeover(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "claude-live", "pid": 42, "stamp": "stamp"}
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host-codex"}, clear=True), \
                patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock), \
                patch("ihav_agent_room.runtime.process_alive", return_value=True):
            with self.assertRaises(RoomError) as caught:
                bind_main(self.store, "host-codex")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.store.gateway, "CLAUDE_01")

    def test_codex_subagent_hook_does_not_bind_parent_or_stop_room(self):
        self.codex_owner()
        before = self.store.room()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host-codex"}, clear=True):
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
                self.assertEqual(handle({"cwd": str(self.project), "session_id": "host-codex",
                    "agent_id": "child", "hook_event_name": event}), {})
        self.assertEqual(self.store.room(), before)

    def test_claude_hook_cannot_reclaim_codex_gateway_or_erase_saved_session(self):
        self.codex_owner()
        self.store.member("CODEX_01", {"native_id": "host-codex"})
        before = self.store.room()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "new-claude"}, clear=True), \
                patch("ihav_agent_room.runtime.exact_claude") as native:
            with self.assertRaises(RoomError) as caught:
                bind_main(self.store, "new-claude")
            self.assertEqual(caught.exception.code, "conflict")
            native.assert_not_called()
        self.assertEqual(self.store.room(), before)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], "host-codex")

    def test_codex_transcript_provenance_never_assumes_human(self):
        path = self.project / "codex.jsonl"
        def row(origin=None, client=None):
            payload = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Approve work"}]}
            if origin:
                payload["origin"] = {"kind": origin}
            if client:
                payload["clientId"] = client
            path.write_text(json.dumps({"type": "response_item", "payload": payload}) + "\n")
            return assess(str(path), transcript_size(str(path)), "Approve work")
        self.assertEqual(row()["state"], "unverified")
        self.assertEqual(row("human")["state"], "human")
        self.assertEqual(row("peer")["state"], "non_human")
        self.assertEqual(row("human", "room-event")["state"], "non_human")

    def test_gateway_thread_wrong_identity_cwd_ephemeral_or_input_capability_rejected(self):
        async def check():
            good = {"id": "host", "cwd": str(self.project), "ephemeral": False, "canAcceptDirectInput": True}
            client = CodexGateway(self.project, "host")
            for bad in ({"id": "other"}, {"cwd": str(self.project.parent)}, {"ephemeral": True},
                        {"canAcceptDirectInput": False}, {"canAcceptDirectInput": None}):
                client.request = AsyncMock(return_value={"thread": good | bad})
                with self.assertRaises(RoomError):
                    await client.thread()
            client.request = AsyncMock(return_value={"thread": good})
            self.assertEqual(await client.thread(), good)
        asyncio.run(check())

    def test_gateway_send_only_queues_and_mismatched_receipt_stays_unknown(self):
        async def check():
            client = CodexGateway(self.project, "host")
            client.thread = AsyncMock()
            client.request = AsyncMock(return_value={"queuedSubmission": {"id": "queue-1", "clientUserMessageId": "M-1"}})
            message = {"id": "M-1", "native_text": "A peer result"}
            self.assertEqual(await client.send(message), "accepted")
            self.assertEqual(client.request.call_args.args[0], "thread/queue/add")
            self.assertEqual(client.request.call_args.args[1]["threadId"], "host")
            client.request.return_value = {"queuedSubmission": {"id": "queue-2", "clientUserMessageId": "other"}}
            with self.assertRaises(RoomError) as caught:
                await client.send(message)
            self.assertEqual(caught.exception.code, "outcome_unknown")
        asyncio.run(check())


class CodexGatewayRuntimeTests(unittest.TestCase):
    # Reuse process-fixture helpers without collecting all its existing test methods twice.
    wait = runtime_tests.RuntimeTests.wait
    call = runtime_tests.RuntimeTests.call
    start = runtime_tests.RuntimeTests.start
    effects = runtime_tests.RuntimeTests.effects
    cleanup_runtime = runtime_tests.RuntimeTests.cleanup_runtime

    def setUp(self):
        runtime_tests.RuntimeTests.setUp(self)
        self.codex_session = str(uuid.uuid4())
        self.codex_home = self.root / "codex-home"
        control = self.codex_home / "app-server-control"
        control.mkdir(parents=True)
        control_path = control / "app-server-control.sock"
        self.host_process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("fake_codex_host.py")),
            str(control_path)], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # Registered after runtime cleanup, so stop it explicitly after the room.
        self.addCleanup(self.cleanup_host)
        self.wait(lambda: Path(str(control_path) + ".ready").exists())
        self.host_file = self.root / "native/codex-host.json"
        self.host_file.write_text(json.dumps({"id": self.codex_session, "cwd": str(self.project),
            "ephemeral": False, "canAcceptDirectInput": True, "status": {"type": "idle"}}))
        self.env.update(CODEX_THREAD_ID=self.codex_session, CODEX_HOME=str(self.codex_home),
                        IHAV_AGENT_ROOM_HOST="codex", IHAV_AGENT_ROOM_MEMBER="CODEX_01")
        self.env.pop("IHAV_AGENT_ROOM_SESSION_ID", None)

    def cleanup_host(self):
        self.cleanup_runtime()
        self.host_process.terminate()
        self.host_process.wait(timeout=5)
        self.host_process.stderr.close()

    def test_codex_host_pair_launch_queue_stop_and_exact_resume(self):
        self.start("pair")
        self.assertEqual(self.store.gateway, "CODEX_01")
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        worker = self.store.member("CLAUDE_01")["native_id"]
        self.assertTrue(worker)
        self.assertEqual(self.main_process.poll(), None)  # Unrelated native session survives.
        starts = self.effects("claude_start")
        self.assertEqual(starts[-1]["member"], "CLAUDE_01")
        self.assertFalse(any(x["data"].get("method") in {"thread/start", "thread/resume", "turn/start", "turn/steer"}
                             for x in self.effects("codex_packet")))
        sent = self.store.send("CLAUDE_01", "CODEX_01", "Concrete peer finding")
        self.wait(lambda: self.effects("codex_gateway_queue"))
        queued = self.effects("codex_gateway_queue")[-1]["data"]
        self.assertEqual(queued["threadId"], self.codex_session)
        self.assertEqual(queued["clientUserMessageId"], sent["id"])
        self.wait(lambda: self.store.inbox("CODEX_01")["items"][0]["status"] == "accepted")
        self.call("ack", sent["id"], "--evidence", "Read the fixture finding")
        self.call("stop")
        self.wait(lambda: self.store.room()["status"] == "stopped")
        self.assertTrue(self.host_file.exists())
        self.assertEqual(self.main_process.poll(), None)
        self.start()
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], worker)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        self.call("stop")
        self.wait(lambda: self.store.room()["status"] == "stopped")

    def test_codex_missing_queue_capability_keeps_room_stopped(self):
        (self.root / "native/queue_unsupported").touch()
        before = self.store.room()
        result = self.call("start", ok=False)
        self.assertEqual(result["error"]["code"], "incompatible")
        self.assertEqual(self.store.room(), before)
        self.assertFalse(self.effects("claude_start"))

    def test_codex_unknown_queue_write_is_not_replayed(self):
        self.start("pair")
        (self.root / "native/queue_crash_after_input").touch()
        sent = self.store.send("CLAUDE_01", "CODEX_01", "Unknown queue write")
        self.wait(lambda: self.store.room()["status"] in {"failed", "stopped"})
        self.assertEqual(len(self.effects("codex_gateway_queue")), 1)
        with self.store.read() as db:
            row = db.execute("SELECT status FROM messages WHERE id=?", (sent["id"],)).fetchone()
        self.assertEqual(row["status"], "unknown")
        self.assertTrue(self.host_file.exists())

    def test_connect_handoff_from_live_claude_preserves_saved_codex_session_and_mode(self):
        self.check_handoff_idempotence_and_resume("init")

    def test_start_handoff_idempotence_and_resume_preserve_room_and_sessions(self):
        self.check_handoff_idempotence_and_resume("start")

    def check_handoff_idempotence_and_resume(self, command):
        self.env["IHAV_AGENT_ROOM_HOST"] = "claude"
        self.env["IHAV_AGENT_ROOM_MEMBER"] = "CLAUDE_01"
        self.env["IHAV_AGENT_ROOM_SESSION_ID"] = self.session
        self.env.pop("CODEX_THREAD_ID", None)
        self.start("pair")
        self.codex_session = self.store.member("CODEX_01")["native_id"]
        self.call("stop")
        self.wait(lambda: self.store.room()["status"] == "stopped")
        self.host_file.write_text(json.dumps({"id": self.codex_session, "cwd": str(self.project),
            "ephemeral": False, "canAcceptDirectInput": True, "status": {"type": "idle"}}))
        self.env.update(IHAV_AGENT_ROOM_HOST="codex", IHAV_AGENT_ROOM_MEMBER="CODEX_01", CODEX_THREAD_ID=self.codex_session)
        self.env.pop("IHAV_AGENT_ROOM_SESSION_ID", None)
        check = self.call("connect", "--check")
        self.assertFalse(check["resume_required"])
        self.assertTrue(check["handoff_required"])
        self.assertFalse(check["room_changed"])
        result = self.call(command)
        self.assertTrue(result["connected"])
        self.wait(lambda: self.store.room()["status"] == "running")
        self.assertEqual(self.store.room()["mode"], "pair")
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        self.assertEqual(self.main_process.poll(), None)
        worker = self.store.member("CLAUDE_01")["native_id"]
        generation = self.store.room()["generation"]
        room_id = self.store.room()["id"]
        starts = len(self.effects("claude_start"))
        repeated = self.call(command)
        self.assertTrue(repeated["connected"])
        self.assertFalse(repeated["started"])
        self.assertEqual(self.store.room()["id"], room_id)
        self.assertEqual(self.store.room()["generation"], generation)
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], worker)
        self.assertEqual(len(self.effects("claude_start")), starts)
        self.call("stop")
        self.wait(lambda: self.store.room()["status"] == "stopped")
        self.call(command)
        self.wait(lambda: self.store.room()["status"] == "running")
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], worker)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        self.assertEqual(self.store.room()["id"], room_id)

    def test_init_round_trip_keeps_host_and_worker_sessions_distinct(self):
        self.check_round_trip_keeps_host_and_worker_sessions_distinct("init")

    def test_start_round_trip_keeps_host_and_worker_sessions_distinct(self):
        self.check_round_trip_keeps_host_and_worker_sessions_distinct("start")

    def check_round_trip_keeps_host_and_worker_sessions_distinct(self, command):
        self.start("pair")
        original_worker = self.store.member("CLAUDE_01")["native_id"]
        room_id = self.store.room()["id"]
        self.env.update(IHAV_AGENT_ROOM_HOST="claude", IHAV_AGENT_ROOM_MEMBER="CLAUDE_01",
                        IHAV_AGENT_ROOM_SESSION_ID=self.session)
        self.env.pop("CODEX_THREAD_ID", None)
        self.call(command)
        self.wait(lambda: self.store.room()["status"] == "running")
        background_codex = self.store.member("CODEX_01")["native_id"]
        self.assertNotEqual(background_codex, self.codex_session)
        self.assertEqual(self.store.room()["host_sessions"]["codex"], self.codex_session)
        self.env.update(IHAV_AGENT_ROOM_HOST="codex", IHAV_AGENT_ROOM_MEMBER="CODEX_01",
                        CODEX_THREAD_ID=self.codex_session)
        self.env.pop("IHAV_AGENT_ROOM_SESSION_ID", None)
        self.call(command)
        self.wait(lambda: self.store.room()["status"] == "running")
        self.assertEqual(self.store.gateway, "CODEX_01")
        self.assertEqual(self.store.room()["id"], room_id)
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], original_worker)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)

    def test_start_creates_new_codex_pair_without_prior_init(self):
        self.project = self.root / "New room $(literal) with spaces"
        self.project.mkdir()
        self.store = runtime_tests.Store(self.project)
        self.host_file.write_text(json.dumps({"id": self.codex_session, "cwd": str(self.project),
            "ephemeral": False, "canAcceptDirectInput": True, "status": {"type": "idle"}}))
        self.assertFalse(self.store.exists())
        result = self.call("start")
        self.assertTrue(result["initialized"])
        self.wait(lambda: self.store.room()["status"] == "running")
        self.assertEqual(self.store.room()["mode"], "pair")
        self.assertEqual(self.store.gateway, "CODEX_01")
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        self.assertTrue(self.store.member("CLAUDE_01")["native_id"])
        self.assertEqual(self.main_process.poll(), None)

    def test_start_preserves_existing_project_instructions(self):
        custom = {
            self.project / "AGENTS.md": "Project-specific bootstrap instructions.\n",
            self.project / "CLAUDE.md": "Keep this project's native instructions.\n",
            self.project / ".gitignore": "project-cache/\n",
        }
        for path, content in custom.items():
            path.write_text(content)
        before = {path: path.read_bytes() for path in custom}
        self.call("start", "--mode", "pair")
        self.wait(lambda: self.store.room()["status"] == "running")
        self.assertEqual(before, {path: path.read_bytes() for path in custom})

    def move_to_new_codex_host(self, detached=True):
        previous = json.loads(self.host_file.read_text())
        if detached:
            previous.pop("canAcceptDirectInput", None)  # Real notLoaded replies omit this field.
            previous["status"] = {"type": "notLoaded"}
        (self.root / "native/codex-previous.json").write_text(json.dumps(previous))
        self.codex_session = str(uuid.uuid4())
        self.host_file.write_text(json.dumps({"id": self.codex_session, "cwd": str(self.project),
            "ephemeral": False, "canAcceptDirectInput": True, "status": {"type": "idle"}}))
        self.env["CODEX_THREAD_ID"] = self.codex_session
        return previous["id"]

    def test_start_reconnects_detached_codex_host_and_keeps_worker_and_backup(self):
        self.start("pair")
        room_id = self.store.room()["id"]
        worker = self.store.member("CLAUDE_01")["native_id"]
        self.call("stop")
        with self.store.tx() as db:
            legacy = self.store.get_room(db)
            legacy.pop("host_sessions", None)
            self.store.put_room(db, legacy)
        previous = self.move_to_new_codex_host()
        result = self.call("start")
        self.assertTrue(result["connected"])
        self.assertTrue(result["reconnected_host"])
        self.assertEqual(result["previous_codex_session"], previous)
        self.wait(lambda: self.store.room()["status"] == "running")
        room = self.store.room()
        self.assertEqual(room["id"], room_id)
        self.assertEqual(room["mode"], "pair")
        self.assertEqual(room["host_sessions"]["codex"], self.codex_session)
        self.assertEqual(room["host_session_history"][-1]["session"], previous)
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], worker)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        with sqlite3.connect(result["recovery_backup"]) as backup:
            saved = json.loads(backup.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])
            self.assertEqual(saved["owner"]["session"], previous)
        self.assertFalse(any(x["data"].get("method") in {"thread/start", "thread/resume", "turn/start", "turn/steer"}
                             for x in self.effects("codex_packet")))
        repeated = self.call("start")
        self.assertFalse(repeated["started"])
        self.assertNotIn("reconnected_host", repeated)
        self.assertEqual(len(self.store.room()["host_session_history"]), 1)

    def test_start_refuses_another_attached_codex_host_without_writes(self):
        self.start("pair")
        self.call("stop")
        previous = self.move_to_new_codex_host(detached=False)
        before = self.store.room()
        result = self.call("start", ok=False)
        self.assertEqual(result["error"]["code"], "conflict")
        self.assertEqual(self.store.room(), before)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], previous)
