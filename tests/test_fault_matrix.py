"""PREREG gate O2: lifecycle and fault matrix on the supervisor, with native effects replaced.

Every fault must surface to a responsible member, never be reported as completed, and never be
replayed automatically. Delivery-flag and notice-once behavior is covered in test_collaboration.py.
"""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import RoomError, dumps
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store
from receipts import human_receipt


class FakeClient:
    def __init__(self):
        self.events = asyncio.Queue()
        self.process = SimpleNamespace(returncode=None, pid=None)
        self.thread_id = "fixture-thread"
        self.turn_id = None
        self.last_sent_turn_id = None
        self.permission_class = "prompting"
        self.sent = []
        self.send_error = None
        self.stop_error = None

    async def send(self, message):
        self.sent.append(message)
        if self.send_error:
            raise self.send_error
        self.turn_id = self.last_sent_turn_id = "fixture-turn"
        return "accepted"

    async def stop(self):
        if self.stop_error:
            raise self.stop_error


class FaultMatrixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="room faults ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        initialize(self.project)
        self.store = Store(self.project)
        self.generation = "fault-generation"
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation=self.generation)
            self.store.put_room(db, room)
        self.store.member("CLAUDE_01", {"status": "active", "native_id": "main"})
        self.store.member("CODEX_EXPERT", {"status": "idle", "native_id": "expert-thread"})
        self.prompt = human_receipt(self.store, "Implement worker.py and main.py")
        self.client = FakeClient()
        self.supervisor = Supervisor(self.store, self.generation)
        self.supervisor.codex["CODEX_EXPERT"] = self.client

    def accepted_attempt(self, turn="t1"):
        message = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Investigate the fixture")
        attempt = self.store.begin_attempt(message, self.generation)
        self.store.finish_dispatch(attempt["id"], "accepted", "Native accepted", turn)
        self.store.member("CODEX_EXPERT", {"status": "working", "turn_id": turn})
        return message, attempt

    def message(self, message_id):
        with self.store.read() as db:
            return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def attempt(self, attempt_id):
        return next(a for a in self.store.attempts(limit=200)["items"] if a["id"] == attempt_id)

    def notices(self):
        with self.store.read() as db:
            return [dict(r) for r in db.execute("SELECT * FROM messages WHERE sender='CODEX_EXPERT' AND recipient='CLAUDE_01'")]

    async def test_delivery_exception_taxonomy_never_reports_acceptance(self):
        cases = ((RoomError("Outcome not known", "outcome_unknown"), "unknown"),
                 (RoomError("Rejected", "native_rejected"), "failed"),
                 (RoomError("No native session", "unavailable"), "failed"),
                 (OSError("Socket closed"), "unknown"),
                 (TimeoutError(), "unknown"))
        for error, expected in cases:
            with self.subTest(error=repr(error)):
                queued = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Question for " + repr(error))
                self.client.send_error = error
                await self.supervisor.dispatch()
                self.assertEqual(self.message(queued["id"])["status"], expected)
                attempt = next(a for a in self.store.attempts(limit=200)["items"] if a["message"] == queued["id"])
                self.assertEqual(attempt["state"], expected)
                self.assertIsNone(attempt["processed"])

    async def test_native_faults_fail_the_member_surface_one_notice_and_never_complete(self):
        faults = (
            ("turn failed", {"method": "turn/completed", "params": {"turn": {"id": "t1", "status": "failed", "error": "boom"}}}, "failed"),
            ("transport error", {"method": "error", "params": {"message": "stream closed"}}, "unknown"),
            ("protocol error", {"method": "room/protocolError", "params": {"error": "bad packet"}}, "unknown"),
            ("process exit", None, "unknown"),
        )
        for label, event, expected in faults:
            with self.subTest(fault=label):
                self.store.member("CODEX_EXPERT", {"status": "idle", "error": None, "turn_id": None})
                before = len(self.notices())
                message, attempt = self.accepted_attempt()
                self.client.process.returncode = None
                if event:
                    self.client.events.put_nowait(event)
                else:
                    self.client.process.returncode = 7
                await self.supervisor.native_events()
                await self.supervisor.native_events()  # A second poll must not duplicate anything.
                self.assertEqual(self.store.member("CODEX_EXPERT")["status"], "failed")
                self.assertEqual(len(self.notices()) - before, 1)
                self.assertEqual(self.attempt(attempt["id"])["state"], expected)
                self.assertEqual(self.message(message["id"])["status"], "accepted")  # Not processed, not re-queued.

    async def test_failed_member_receives_no_automatic_replay(self):
        message, _ = self.accepted_attempt()
        self.client.process.returncode = 7
        await self.supervisor.native_events()
        follow_up = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Follow-up after the fault")
        sent_before = len(self.client.sent)
        for _ in range(3):
            await self.supervisor.dispatch()
        self.assertEqual(len(self.client.sent), sent_before)
        self.assertEqual(self.message(follow_up["id"])["status"], "queued")
        self.assertEqual(self.message(message["id"])["status"], "accepted")

    async def test_explicit_retry_requeues_unknown_dispatch_once_after_human_reconciliation(self):
        message = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Retry this only after reconciling its effect")
        original = self.store.begin_attempt(message, self.generation)
        self.store.finish_dispatch(original["id"], "unknown", "Transport closed before outcome was known")
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "main"}
            self.store.put_room(db, room)
        retry_prompt = human_receipt(
            self.store,
            f"Explicitly retry message {message['id']} only after checking its prior native effect.",
        )

        args = parser().parse_args([
            "--project", str(self.project), "retry-message", message["id"],
            "--source", retry_prompt,
            "--reconciled", "Checked native history and found no matching effect",
        ])
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01", "IHAV_AGENT_ROOM_SESSION_ID": "main"}):
            result = run(args)

        self.assertEqual(result, {"queued": message["id"], "processed": False})
        self.assertEqual(self.message(message["id"])["status"], "queued")
        self.assertEqual(self.message(message["id"])["detail"], "Checked native history and found no matching effect")
        self.assertEqual(self.attempt(original["id"])["state"], "unknown")
        with self.store.read() as db:
            consumption = [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM events WHERE kind='prompt.consumed'")]
        self.assertTrue(any(item["receipt"] == retry_prompt and item["use"] == "message_retry"
                            and item["state"] == "human" for item in consumption))

        await self.supervisor.dispatch()
        await self.supervisor.dispatch()

        attempts = [a for a in self.store.attempts(limit=200)["items"] if a["message"] == message["id"]]
        self.assertEqual(len(attempts), 2)
        self.assertEqual({a["state"] for a in attempts}, {"unknown", "accepted"})
        self.assertEqual(self.message(message["id"])["status"], "accepted")
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0]["id"], message["id"])

    async def test_supervisor_restart_marks_in_flight_work_unknown_and_replays_nothing(self):
        in_flight = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Dispatch was interrupted")
        attempt = self.store.begin_attempt(in_flight, self.generation)  # Message and attempt stay 'dispatching'.
        waiting = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Still queued at restart")
        with self.store.tx() as db:
            for index, state in enumerate(("pending", "respond", "submitted", "resolved")):
                request = {"id": f"A-{state}", "member": "CODEX_EXPERT", "request_id": index, "method": "item/commandExecution/requestApproval",
                           "params": {}, "supported": True, "generation": self.generation, "state": state, "created": "2026-01-01T00:00:00+00:00"}
                db.execute("INSERT INTO approvals VALUES (?,?)", (request["id"], dumps(request)))
        restarted = Supervisor(self.store, "next-generation")
        restarted.codex["CODEX_EXPERT"] = self.client
        await restarted.recover_owned()
        self.assertEqual(self.message(in_flight["id"])["status"], "unknown")
        self.assertIn("Supervisor restarted", self.message(in_flight["id"])["detail"])
        self.assertEqual(self.attempt(attempt["id"])["state"], "unknown")
        self.assertEqual(self.message(waiting["id"])["status"], "queued")
        states = {a["id"]: a["state"] for a in self.store.status()["approvals"]}
        self.assertEqual(states, {"A-pending": "expired", "A-respond": "expired", "A-submitted": "expired", "A-resolved": "resolved"})
        self.assertEqual(self.client.sent, [])  # Recovery itself sends nothing.
        self.assertEqual(self.store.member("CODEX_EXPERT")["status"], "stopped")
        # Reactivate the member: the restarted loop sends only the never-attempted message, never the unknown one.
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="next-generation")
            self.store.put_room(db, room)
        self.store.member("CODEX_EXPERT", {"status": "idle"})
        for _ in range(2):
            await restarted.dispatch()
        self.assertEqual([m["id"] for m in self.client.sent], [waiting["id"]])
        self.assertEqual(self.message(in_flight["id"])["status"], "unknown")
        self.assertEqual(self.message(waiting["id"])["status"], "accepted")

    async def test_shutdown_keeps_writer_claims_until_cleanup_is_confirmed(self):
        def task(owner, scope):
            return self.store.create_task("CLAUDE_01", {"title": "Write " + scope, "request": "Change it", "acceptance": "Done",
                                                        "next": "Edit", "owner": owner, "source": self.prompt,
                                                        "authority": "implementation", "scope": [scope]})
        worker_task, main_task = task("CODEX_EXPERT", "worker.py"), task("CLAUDE_01", "main.py")
        self.store.claim("CODEX_EXPERT", worker_task["id"], worker_task["version"])
        self.store.claim("CLAUDE_01", main_task["id"], main_task["version"])
        self.supervisor.recovered = True

        def claims():
            with self.store.read() as db:
                return {row["task"]: row["owner"] for row in db.execute("SELECT task,owner FROM claims")}

        def state(task_id):
            with self.store.read() as db:
                return self.store.record(db, "tasks", task_id)["state"]

        self.client.stop_error = RoomError("Cannot confirm native worker exit", "cleanup")
        await self.supervisor.shutdown()
        self.assertEqual(self.store.room()["status"], "failed")
        self.assertEqual(claims(), {worker_task["id"]: "CODEX_EXPERT", main_task["id"]: "CLAUDE_01"})
        self.assertEqual(state(worker_task["id"]), "running")
        self.client.stop_error = None
        await self.supervisor.shutdown()
        self.assertEqual(self.store.room()["status"], "stopped")
        self.assertEqual(claims(), {main_task["id"]: "CLAUDE_01"})
        self.assertEqual(state(worker_task["id"]), "ready")
        self.assertEqual(state(main_task["id"]), "running")


if __name__ == "__main__":
    unittest.main()
