"""Mode restart must observe native turn completion before owned cleanup."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import process_stamp
from ihav_agent_room.runtime import Supervisor, change_mode, request_stop
from ihav_agent_room.store import Store


class ModeDrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="room-mode-drain-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.store.initialize("advisors")
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="drain-test", supervisor={
                "pid": os.getpid(), "stamp": process_stamp(os.getpid())})
            self.store.put_room(db, room)

    def test_running_mode_request_does_not_start_cleanup(self):
        self.store.member("CODEX_EXPERT", {"status": "working", "turn_id": "old-turn"})
        result = change_mode(self.store, "pair")
        self.assertTrue(result["restarting"])
        self.assertEqual(self.store.room()["status"], "running")
        self.assertEqual(self.store.status(compact=True)["room"]["mode_transition"]["state"], "draining")
        self.assertEqual(self.store.member("CODEX_EXPERT")["turn_id"], "old-turn")

    def test_real_supervisor_loop_waits_for_turn_completion_before_shutdown(self):
        async def scenario():
            supervisor = Supervisor(self.store, "drain-test")
            class Client:
                turn_id = "old-turn"
            client = Client()
            supervisor.codex["CODEX_EXPERT"] = client
            self.store.member("CODEX_EXPERT", {"status": "working", "turn_id": client.turn_id})
            events = []
            async def launch():
                change_mode(self.store, "pair")
            async def observe():
                events.append("native observation")
                if events.count("native observation") == 2:
                    client.turn_id = None
                    self.store.member("CODEX_EXPERT", {"status": "idle", "turn_id": None})
                    events.append("turn completed")
            async def approvals():
                events.append("approvals checked")
            async def shutdown():
                self.assertIn("turn completed", events)
                self.assertIsNone(client.turn_id)
                events.append("cleanup")
            with patch.object(supervisor, "launch", launch), patch.object(supervisor, "native_events", observe), \
                 patch.object(supervisor, "approvals", approvals), patch.object(supervisor, "refresh_claude"), \
                 patch.object(supervisor, "shutdown", shutdown), patch.object(supervisor, "_dispatch_member_queue") as send, \
                 patch.object(supervisor, "owner_alive", return_value=True):
                await asyncio.wait_for(supervisor.run(), 3)
                send.assert_not_called()
            self.assertGreaterEqual(events.count("approvals checked"), 2)
            self.assertLess(events.index("turn completed"), events.index("cleanup"))
        asyncio.run(scenario())

    def test_claude_waiting_or_unknown_is_not_completion(self):
        async def scenario():
            supervisor = Supervisor(self.store, "drain-test")
            supervisor.claude["CLAUDE_EXPERT"] = "exact-session"
            change_mode(self.store, "pair")
            for status in ("working", "waiting_native_input", "waiting_permission", "unknown", "failed", "stopped"):
                self.store.member("CLAUDE_EXPERT", {"status": status})
                self.assertFalse(supervisor.finish_mode_restart())
                self.assertEqual(self.store.room()["status"], "running")
                self.assertEqual(self.store.room()["mode_transition"]["waiting"], ["CLAUDE_EXPERT"])
            self.store.member("CLAUDE_EXPERT", {"status": "idle"})
            self.assertTrue(supervisor.finish_mode_restart())
            self.assertEqual(self.store.room()["status"], "stopping")
        asyncio.run(scenario())

    def test_codex_unknown_outcome_and_active_turn_do_not_count_as_completion(self):
        supervisor = Supervisor(self.store, "drain-test")
        class Client:
            turn_id = "old-turn"
        client = Client()
        supervisor.codex["CODEX_EXPERT"] = client
        self.store.member("CODEX_EXPERT", {"status": "idle"})
        change_mode(self.store, "pair")
        self.assertFalse(supervisor.finish_mode_restart())
        client.turn_id = None
        self.store.member("CODEX_EXPERT", {"status": "failed"})
        self.assertFalse(supervisor.finish_mode_restart())
        self.store.member("CODEX_EXPERT", {"status": "idle"})
        self.assertTrue(supervisor.finish_mode_restart())

    def test_manual_stop_can_cancel_drain_without_claiming_completion(self):
        change_mode(self.store, "pair")
        request_stop(self.store)
        self.assertFalse(self.store.room()["restart_requested"])
        self.assertNotIn("mode_transition", self.store.room())
        self.assertEqual(self.store.room()["status"], "stopping")

    def test_dispatch_cannot_start_new_work_during_drain(self):
        self.store.member("CODEX_01", {"status": "idle"})
        self.store.send("CLAUDE_01", "CODEX_01", "Keep this queued")
        change_mode(self.store, "pair")
        supervisor = Supervisor(self.store, "drain-test")
        with patch.object(supervisor, "_dispatch_member_queue") as send:
            asyncio.run(supervisor.dispatch())
        send.assert_not_called()
        self.assertEqual(self.store.inbox("CODEX_01")["items"][0]["status"], "queued")

    def test_already_selected_queue_is_not_dispatched_after_mode_request(self):
        message = self.store.send("CLAUDE_01", "CODEX_01", "Selected before the request")
        change_mode(self.store, "pair")
        supervisor = Supervisor(self.store, "drain-test")
        with patch.object(self.store, "begin_attempt", side_effect=AssertionError("Drain started an attempt")) as attempt:
            asyncio.run(supervisor._dispatch_member_queue("CODEX_01", [message], set()))
        attempt.assert_not_called()

    def test_drain_forces_fresh_claude_registry_and_missing_status_is_unknown(self):
        async def scenario():
            supervisor = Supervisor(self.store, "drain-test")
            supervisor.claude["CLAUDE_EXPERT"] = "exact-session"
            change_mode(self.store, "pair")
            record = {"sessionId": "exact-session", "cwd": str(self.store.project), "pid": os.getpid()}
            for native_status, expected in (("busy", "working"), (None, "unknown"), ("done", "idle")):
                if native_status is None:
                    record.pop("status", None)
                else:
                    record["status"] = native_status
                with patch("ihav_agent_room.runtime.claude_agents", return_value=[record]) as lookup:
                    await supervisor.refresh_claude(force=True)
                lookup.assert_called_once()
                self.assertEqual(self.store.member("CLAUDE_EXPERT")["status"], expected)
                self.assertEqual(supervisor.finish_mode_restart(), native_status == "done")
        asyncio.run(scenario())
