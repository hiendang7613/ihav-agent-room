"""Selected lessons stay traceable as peer discussion outlives their revisions."""

import concurrent.futures
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from ihav_agent_room.common import PLUGIN_ROOT, RoomError
from ihav_agent_room.knowledge import Knowledge
from ihav_agent_room.native import message_text
from test_evidence import EvidenceFixture


class KnowledgeSharingTests(EvidenceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.knowledge = Knowledge(self.store)
        self.lesson = self.knowledge.write("CODEX_EXPERT", {
            "title": "A fixture observation", "body": "Full evidence context " * 150,
            "evidence": ["Fixture experiment only"], "limits": "Verify against the current implementation"})

    def send(self, **extra):
        return self.store.send("CODEX_EXPERT", "CLAUDE_01", "Could this explain our disagreement?",
                               knowledge_id=self.lesson["id"], **extra)

    def inbox(self, message):
        return next(row for row in self.store.inbox("CLAUDE_01")["items"] if row["id"] == message["id"])

    def test_task_free_reference_is_bound_to_queue_time_and_read_only(self):
        message = self.send()
        self.assertIsNone(message["task"])
        self.assertEqual(message["status"], "queued")
        self.assertEqual(json.loads(message["context"]), {"knowledge": {"id": self.lesson["id"], "version": 1}})
        before = self.store.path.read_bytes()
        received = self.inbox(message)
        reference = received["knowledge_reference"]
        self.assertEqual((reference["queued_version"], reference["current_version"], reference["changed"]), (1, 1, False))
        self.assertEqual(reference["read_command"], "ihav-agent-room knowledge show " + self.lesson["id"])
        self.assertEqual(reference["history_command"], "ihav-agent-room knowledge history " + self.lesson["id"])
        self.assertNotIn("body", reference)
        self.assertNotIn("Full evidence context", message_text(received))
        self.assertIn("advisory", message_text(received))
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(self.store.status()["tasks"], [])

    def test_later_revision_and_retirement_are_visible_without_mutating_old_message(self):
        message = self.send()
        self.knowledge.write("CLAUDE_01", {"body": "Counterexample changes the conclusion"}, self.lesson["id"], 1)
        revised = self.inbox(message)["knowledge_reference"]
        self.assertEqual((revised["queued_version"], revised["current_version"], revised["changed"]), (1, 2, True))
        self.knowledge.write("CODEX_EXPERT", {"state": "retired", "limits": "Fixture implementation replaced"}, self.lesson["id"], 2)
        received = self.inbox(message)
        self.assertEqual(received["knowledge_reference"]["state"], "retired")
        self.assertEqual(received["knowledge_reference"]["current_version"], 3)
        self.assertEqual(received["body"], message["body"])
        self.assertFalse(received["stale"], "Task staleness retains its separate meaning")
        with self.store.read() as db:
            persisted = dict(db.execute("SELECT * FROM messages WHERE id=?", (message["id"],)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
        self.assertEqual(persisted, message)

    def test_compact_inbox_keeps_current_knowledge_revision_and_queued_provenance(self):
        message = self.send()
        self.knowledge.write("CODEX_EXPERT", {"state": "retired", "limits": "Counterexample found"}, self.lesson["id"], 1)
        before = self.store.path.read_bytes()
        full = self.inbox(message)
        preview = next(row for row in self.store.inbox("CLAUDE_01", pending=True, compact=True)["items"]
                       if row["id"] == message["id"])
        self.assertEqual(preview["context"], {"knowledge": {"id": self.lesson["id"], "version": 1}})
        self.assertEqual(preview["knowledge_reference"], full["knowledge_reference"])
        reference = preview["knowledge_reference"]
        self.assertEqual((reference["queued_version"], reference["current_version"], reference["state"], reference["changed"]),
                         (1, 2, "retired", True))
        self.assertEqual(preview["body_preview"], message["body"])
        self.assertFalse(preview["stale"])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_message_identity_keeps_original_version_on_duplicate_after_revision(self):
        message = self.send(message_id="same-intent")
        self.knowledge.write("CLAUDE_01", {"state": "retired"}, self.lesson["id"], 1)
        before = self.store.path.read_bytes()
        self.assertEqual(self.send(message_id="same-intent"), message)
        self.assertEqual(before, self.store.path.read_bytes())
        for knowledge_id in (None, "K-another"):
            with self.subTest(knowledge_id=knowledge_id), self.assertRaises(RoomError) as error:
                self.store.send("CODEX_EXPERT", "CLAUDE_01", message["body"], message_id="same-intent", knowledge_id=knowledge_id)
            self.assertEqual(error.exception.code, "conflict")
        plain = self.store.send("CODEX_EXPERT", "CLAUDE_01", "Plain message", message_id="plain-intent")
        with self.assertRaises(RoomError):
            self.store.send("CODEX_EXPERT", "CLAUDE_01", plain["body"], message_id="plain-intent", knowledge_id=self.lesson["id"])

    def test_concurrent_duplicate_has_one_durable_message(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.send(message_id="concurrent-intent"), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.store.inbox("CLAUDE_01")["items"]), 1)

    def test_invalid_reference_and_read_failure_roll_back_the_send(self):
        before = self.store.path.read_bytes()
        for reference in ("K-absent", self.prompt, ""):
            with self.subTest(reference=reference), self.assertRaises(RoomError) as error:
                self.store.send("CODEX_EXPERT", "CLAUDE_01", "A question", knowledge_id=reference)
            self.assertEqual(error.exception.code, "not_found")
        with patch.object(self.store, "record", side_effect=RoomError("Read failed")), self.assertRaises(RoomError):
            self.send()
        self.assertEqual(before, self.store.path.read_bytes())

    def test_dispatch_keeps_observed_revision_while_later_inbox_reads_refresh_it(self):
        message = self.send()
        self.knowledge.write("CLAUDE_01", {"body": "Revised before dispatch"}, self.lesson["id"], 1)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="sharing-fixture")
            self.store.put_room(db, room)
        attempt = self.store.begin_attempt(message, "sharing-fixture")
        self.assertEqual(attempt["knowledge_reference"]["current_version"], 2)
        self.knowledge.write("CODEX_EXPERT", {"state": "retired"}, self.lesson["id"], 2)
        self.assertEqual(self.store.attempts()["items"][0]["knowledge_reference"], attempt["knowledge_reference"])
        self.assertEqual(self.inbox(message)["knowledge_reference"]["current_version"], 3)
        self.assertEqual(self.knowledge.history(self.lesson["id"])["items"][0]["version"], 1)

    def test_reference_failure_does_not_leave_a_partial_dispatch_claim(self):
        message = self.send()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="sharing-fixture")
            self.store.put_room(db, room)
        before = self.store.path.read_bytes()
        with patch.object(self.store, "knowledge_reference", side_effect=RoomError("Read failed")), self.assertRaises(RoomError):
            self.store.begin_attempt(message, "sharing-fixture")
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(self.store.attempts()["items"], [])
        self.assertEqual(self.inbox(message)["status"], "queued")

    def test_sharing_retired_knowledge_is_allowed_and_task_review_is_unchanged(self):
        task = self.task()
        self.review(self.submit(task))
        before = self.current(task)
        self.knowledge.write("CLAUDE_01", {"state": "retired"}, self.lesson["id"], 1)
        message = self.send(task_id=task["id"])
        context = json.loads(message["context"])
        self.assertEqual(context["task_version"], before["version"])
        self.assertEqual(context["knowledge"]["version"], 2)
        reference = self.inbox(message)["knowledge_reference"]
        self.assertEqual((reference["state"], reference["changed"]), ("retired", False))
        self.assertEqual(self.current(task), before)
        self.assertEqual(self.store.task_context(task["id"])["review"]["state"], "approved")
        with self.assertRaises(RoomError) as error:
            self.task(source=self.lesson["id"])
        self.assertEqual(error.exception.code, "authority")

    def test_unlinked_text_keeps_legacy_shape_and_never_infers_a_reference(self):
        message = self.store.send("CODEX_EXPERT", "CLAUDE_01", "Consider " + self.lesson["id"])
        received = self.inbox(message)
        self.assertEqual(received["context"], {})
        self.assertNotIn("knowledge_reference", received)
        self.assertNotIn("Shared knowledge reference", message_text(received))
        self.assertEqual(self.knowledge.show(self.lesson["id"]), self.lesson)

    def test_real_cli_retains_identity_queue_and_json_contracts(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "sharing-main"}
            self.store.put_room(db, room)
        env = dict(os.environ, IHAV_AGENT_ROOM_MEMBER="CLAUDE_01", IHAV_AGENT_ROOM_SESSION_ID="sharing-main", PATH="")
        command = [sys.executable, str(PLUGIN_ROOT / "bin/ihav-agent-room"), "--project", str(self.project), "--json",
                   "send", "--to", "CODEX_EXPERT", "--knowledge", self.lesson["id"], "--body", "Please challenge this lesson."]
        failed = subprocess.run(command, env=env | {"IHAV_AGENT_ROOM_SESSION_ID": "unbound"}, text=True, capture_output=True, timeout=5)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout)["error"]["code"], "identity")
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        message = json.loads(result.stdout)["data"]
        self.assertEqual(message["status"], "queued")
        self.assertEqual(json.loads(message["context"])["knowledge"]["id"], self.lesson["id"])
        received = self.store.inbox("CODEX_EXPERT", pending=True)["items"][0]
        self.assertEqual(received["knowledge_reference"]["current_version"], 1)
        self.assertEqual(self.store.attempts()["items"], [])


if __name__ == "__main__":
    unittest.main()
