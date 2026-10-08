"""Queue-only global routing: isolated ledgers, no native/provider operations."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ihav_agent_room.common import RoomError, native_event_prompt
from ihav_agent_room.globalspace import GlobalSpace
from ihav_agent_room.native import message_text
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store


class GlobalQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="global queue ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.space = GlobalSpace(self.base / "global", timeout=0.05)
        self.stores = {}
        self.rooms = {}
        for name in ("a", "b", "c"):
            project = self.base / name
            project.mkdir()
            initialize(project, "pair")
            store = self.stores[name] = Store(project)
            self.rooms[name] = store.room()["id"]
            self.space.register(self.rooms[name], project, "candidate")

    def post(self, audience=None, body="data"):
        return self.space.post("announcement", "subject", body, origin=self.rooms["a"], audience=audience)

    def messages(self, name):
        with self.stores[name].read() as db:
            return [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY seq")]

    def test_targeted_post_queues_only_receiver_without_start_or_refanout(self):
        store = self.stores["b"]
        with store.tx() as db:
            room = store.get_room(db)
            room["manual_stop"] = True
            store.put_room(db, room)
        before_room = store.room()
        with patch("ihav_agent_room.runtime.start_room") as start, patch("ihav_agent_room.native.start_claude") as native:
            entry = self.post([self.rooms["b"]])
        start.assert_not_called()
        native.assert_not_called()
        self.assertEqual(store.room(), before_room)
        self.assertEqual(self.messages("a"), [])
        self.assertEqual(self.messages("c"), [])
        rows = self.messages("b")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["status"], row["recipient"], row["task"]), ("queued", store.gateway, None))
        self.assertEqual(set(entry["queue"]["enqueued"]), {self.rooms["b"]})
        context = json.loads(row["context"])
        self.assertEqual(context["global_entry"]["origin_room"], self.rooms["a"])
        self.assertNotIn("broadcast", context)
        self.assertNotIn("admin_notice", context)
        self.assertTrue(native_event_prompt(message_text(row)))
        with store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_all_and_reply_obey_existing_audience_rules(self):
        entry = self.post()
        self.assertEqual((len(self.messages("a")), len(self.messages("b")), len(self.messages("c"))), (0, 1, 1))
        self.space.post("reply", "re", "answer", origin=self.rooms["b"], reply_to=entry["id"])
        self.assertEqual((len(self.messages("a")), len(self.messages("b")), len(self.messages("c"))), (1, 1, 1))

    def test_disabled_and_later_joined_rooms_do_not_receive_old_entries(self):
        self.space.register(self.rooms["c"], self.stores["c"].project, "candidate", enabled=False)
        self.post()
        self.assertEqual(self.messages("c"), [])
        self.space.register(self.rooms["c"], self.stores["c"].project, "candidate", enabled=True)
        self.assertEqual(self.space.import_queue(self.stores["c"])["queued"], 1)
        late = self.base / "late"
        late.mkdir()
        initialize(late, "pair")
        store = Store(late)
        self.space.register(store.room()["id"], late, "candidate")
        self.assertEqual(self.space.import_queue(store)["queued"], 0)

    def test_repeated_concurrent_import_is_deduped(self):
        entry = self.post([self.rooms["b"]])
        def again(_):
            return self.space.enqueue_posted(entry["id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(again, range(4)))
        self.space.import_queue(self.stores["b"])
        self.assertEqual(len(self.messages("b")), 1)

    def test_failed_local_queue_retains_committed_global_entry_for_recovery(self):
        locked = self.stores["b"].connect()
        locked.execute("BEGIN IMMEDIATE")
        try:
            entry = self.post([self.rooms["b"]])
            self.assertIn(self.rooms["b"], entry["queue"]["pending"])
            self.assertEqual(self.space.show(entry["id"])["body"], "data")
            self.assertEqual(self.messages("b"), [])
        finally:
            locked.execute("ROLLBACK")
            locked.close()
        self.assertEqual(self.space.import_queue(self.stores["b"])["queued"], 1)
        self.assertEqual(self.space.import_queue(self.stores["b"])["queued"], 0)

    def test_crash_before_fanout_is_recovered_by_bounded_import(self):
        with patch.object(self.space, "enqueue_posted", return_value={"pending": "simulated crash"}):
            self.post([self.rooms["b"]])
        self.assertEqual(self.messages("b"), [])
        self.assertEqual(self.space.import_queue(self.stores["b"])["queued"], 1)
        self.assertEqual(len(self.messages("b")), 1)

    def test_cursor_and_queue_rollback_together(self):
        with patch.object(self.space, "enqueue_posted", return_value={"pending": "deferred"}):
            self.post([self.rooms["b"]])
        store = self.stores["b"]
        event = store.event
        def fail(db, kind, data):
            if kind == "agents_space.queued":
                raise RuntimeError("crash before commit")
            return event(db, kind, data)
        with patch.object(store, "event", side_effect=fail), self.assertRaises(RuntimeError):
            self.space.import_queue(store)
        self.assertEqual(self.messages("b"), [])
        with store.read() as db:
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key LIKE 'agents_space.queue_cursor:%'").fetchone())
        self.assertEqual(self.space.import_queue(store)["queued"], 1)

    def test_read_marker_does_not_skip_after_queue_cursor_exists(self):
        self.space.import_queue(self.stores["b"])
        with patch.object(self.space, "enqueue_posted", return_value={"pending": "deferred"}):
            entry = self.post([self.rooms["b"]])
        self.space.mark_read(self.rooms["b"], entry["seq"])
        self.assertEqual(self.space.import_queue(self.stores["b"])["queued"], 1)

    def test_read_marker_does_not_skip_before_first_successful_queue_import(self):
        with patch.object(self.space, "enqueue_posted", return_value={"pending": "deferred"}):
            entry = self.post([self.rooms["b"]])
        self.space.mark_read(self.rooms["b"], entry["seq"])
        self.assertEqual(self.space.import_queue(self.stores["b"])["queued"], 1)
        self.assertEqual(self.space.import_queue(self.stores["b"])["queued"], 0)
        self.assertEqual(len(self.messages("b")), 1)

    def test_upgrade_keeps_pre_queue_history_readable_without_replaying_it(self):
        db = self.space.connect()
        try:
            db.execute("INSERT INTO entries (id,kind,origin_room,audience,subject,body,created) "
                       "VALUES ('G-legacy','announcement',?,?,'legacy','old data','2026-01-01')",
                       (self.rooms["a"], json.dumps([self.rooms["b"]])))
            db.execute("DELETE FROM meta WHERE key='queue_start_seq'")
        finally:
            db.close()
        self.assertEqual(self.space.import_queue(self.stores["b"])["queued"], 0)
        self.assertEqual(self.space.visible(self.rooms["b"])["items"][0]["id"], "G-legacy")
        self.assertEqual(self.space.enqueue_posted("G-legacy")["enqueued"], {})
        entry = self.post([self.rooms["b"]], body="new data")
        self.assertEqual(len(self.messages("b")), 1)
        self.assertEqual(json.loads(self.messages("b")[0]["context"])["global_entry"]["id"], entry["id"])

    def test_directory_mismatch_does_not_write_another_room(self):
        db = self.space.connect()
        db.execute("UPDATE rooms SET project=? WHERE room_id=?", (str(self.stores["c"].project), self.rooms["b"]))
        db.close()
        entry = self.post([self.rooms["b"]])
        self.assertIn(self.rooms["b"], entry["queue"]["pending"])
        self.assertEqual(self.messages("b"), [])
        self.assertEqual(self.messages("c"), [])

    def test_import_refuses_a_registry_project_mismatch(self):
        db = self.space.connect()
        db.execute("UPDATE rooms SET project=? WHERE room_id=?", (str(self.stores["c"].project), self.rooms["b"]))
        db.close()
        with self.assertRaises(RoomError) as error:
            self.space.import_queue(self.stores["b"])
        self.assertEqual(error.exception.code, "conflict")
        self.assertEqual(self.messages("b"), [])

    def test_copied_room_id_does_not_authorize_a_different_project_directory(self):
        original = self.stores["b"].connect()
        copied = self.stores["c"].connect()
        try:
            original.backup(copied)
        finally:
            original.close()
            copied.close()
        db = self.space.connect()
        db.execute("UPDATE rooms SET project=? WHERE room_id=?", (str(self.stores["c"].project), self.rooms["b"]))
        db.close()
        entry = self.post([self.rooms["b"]])
        self.assertIn(self.rooms["b"], entry["queue"]["pending"])
        self.assertEqual(self.messages("b"), [])
        self.assertEqual(self.messages("c"), [])

    def test_only_unsent_notices_follow_a_new_gateway_after_cursor_advances(self):
        self.post([self.rooms["b"]], body="unsent")
        self.post([self.rooms["b"]], body="already submitted")
        store = self.stores["b"]
        self.space.import_queue(store)
        old_gateway = store.gateway
        new_gateway = "CODEX_01" if old_gateway == "CLAUDE_01" else "CLAUDE_01"
        second_id = self.messages("b")[1]["id"]
        with store.tx() as db:
            room = store.get_room(db)
            room["gateway"] = new_gateway
            room["owner"] = {"host": "codex" if new_gateway == "CODEX_01" else "claude",
                             "session": "fixture-gateway"}
            store.put_room(db, room)
            db.execute("UPDATE messages SET status='submitted' WHERE id=?", (second_id,))
        self.assertEqual(self.space.import_queue(store)["queued"], 0)
        first, second = self.messages("b")
        self.assertEqual((first["recipient"], first["status"]), (new_gateway, "queued"))
        self.assertEqual((second["recipient"], second["status"]), (old_gateway, "submitted"))

    def test_supervisor_import_failure_does_not_start_or_stop_a_room(self):
        store = self.stores["b"]
        before = store.room()
        supervisor = Supervisor(store, "fixture")
        with patch("ihav_agent_room.runtime.GlobalSpace") as space, patch("ihav_agent_room.runtime.start_room") as start:
            space.return_value.import_queue.side_effect = sqlite3.OperationalError("busy")
            result = asyncio.run(supervisor.import_global_queue())
        start.assert_not_called()
        self.assertEqual(store.room(), before)
        self.assertEqual(result["queued"], 0)
        self.assertIn("busy", result["error"])
