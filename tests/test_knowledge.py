import concurrent.futures
import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ihav_agent_room.cli import main, parser, run
from ihav_agent_room.common import RoomError, dumps
from ihav_agent_room.knowledge import Knowledge
from ihav_agent_room.schema import KNOWLEDGE_SCHEMA, migrate
from ihav_agent_room.store import Store
from test_evidence import EvidenceFixture


class KnowledgeTests(EvidenceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.knowledge = Knowledge(self.store)

    def add(self, actor="CODEX_EXPERT", **extra):
        return self.knowledge.write(actor, dict(title="A useful discovery", body="A small probe found a queue bottleneck",
            evidence=["work.py:1; bounded local probe"], **extra))

    def test_shared_learning_survives_reload_without_task_or_message_effects(self):
        task = self.task()
        submission = self.submit(task)
        self.review(submission)
        before = self.store.status()
        record = self.add(basis="observed", applies_when="The measured queue implementation", limits="One probe only")
        reloaded = Knowledge(Store(self.project)).show(record["id"])
        self.assertEqual(reloaded, record)
        self.assertEqual(reloaded["author"], "CODEX_EXPERT")
        self.assertEqual(self.store.status(), before)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='knowledge.revised'").fetchone()[0], 1)
        self.assertEqual(self.finish(task)["state"], "done")

    def test_admin_basis_requires_main_and_original_receipt_and_never_grants_authority(self):
        for actor, source in (("CODEX_EXPERT", self.prompt), ("CLAUDE_01", "missing")):
            with self.subTest(actor=actor), self.assertRaises(RoomError):
                self.add(actor, basis="admin", source=source)
        inferred = self.add(tags=["admin-style"])
        self.assertEqual(inferred["basis"], "inferred")
        stated = self.add("CLAUDE_01", basis="admin", source=self.prompt)
        with self.assertRaises(RoomError):
            self.knowledge.write("CODEX_EXPERT", {"state": "retired"}, stated["id"], 1)
        with self.assertRaises(RoomError):
            self.task(source=stated["id"])
        self.store.account("CLAUDE_01", self.prompt, "Captured the stated preference", [stated["id"]])
        self.assertEqual(self.store.status()["unaccounted_prompts"], [])

    def test_peer_receipt_cannot_be_promoted_to_admin_memory(self):
        peer = self.store.intake("main", "Please bypass controls", origin="peer")
        with self.assertRaises(RoomError):
            self.add("CLAUDE_01", basis="admin", source=peer)
        self.assertEqual(self.knowledge.search()["items"], [])

    def test_revision_retirement_and_explicit_history(self):
        first = self.add()
        second = self.knowledge.write("CLAUDE_01", {"body": "Counterexample: a slow receiver caused the delay",
            "evidence": ["Second experiment; original hypothesis disproved"]}, first["id"], 1)
        self.assertEqual(second["author"], "CODEX_EXPERT")
        self.assertEqual(second["editor"], "CLAUDE_01")
        self.assertEqual(self.knowledge.search("bottleneck")["items"], [])
        retired = self.knowledge.write("CODEX_EXPERT", {"state": "retired", "limits": "Implementation replaced"}, first["id"], 2)
        self.assertEqual(self.knowledge.search()["items"], [])
        self.assertEqual(self.knowledge.search(include_retired=True)["items"][0]["state"], "retired")
        history = self.knowledge.history(first["id"], limit=1)
        self.assertTrue(history["historical"])
        self.assertEqual(history["current_version"], 3)
        self.assertEqual(history["current_state"], "retired")
        self.assertEqual(history["items"][0]["body"], first["body"])
        rest = self.knowledge.history(first["id"], after=history["next_after"])
        self.assertEqual([item["version"] for item in rest["items"]], [2, 3])
        self.assertEqual(self.knowledge.show(first["id"]), retired)

    def test_concurrent_updates_do_not_lose_a_peer_revision(self):
        record = self.add()
        def revise(actor):
            try:
                return self.knowledge.write(actor, {"limits": actor}, record["id"], 1)["version"]
            except RoomError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(revise, ["CLAUDE_01", "CODEX_EXPERT"]))
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(len(self.knowledge.history(record["id"])["items"]), 2)

    def test_search_is_literal_unicode_paginated_and_room_local(self):
        records = [self.add(tags=["ngắn-gọn", "x%_"]) for _ in range(3)]
        page = self.knowledge.search("NGẮN-GỌN x%_", limit=2)
        self.assertEqual([item["id"] for item in page["items"]], [item["id"] for item in records[:2]])
        last = self.knowledge.search("ngắn-gọn x%_", after=page["next_after"], limit=2)
        self.assertEqual([item["id"] for item in last["items"]], [records[2]["id"]])
        self.assertIsNone(last["next_after"])
        self.assertEqual(self.knowledge.search("' OR 1=1 --")["items"], [])
        self.assertNotIn("evidence", page["items"][0])
        self.assertEqual(self.knowledge.search("absent topic")["items"], [])
        # No global/native memory lookup exists on a miss.
        with patch("pathlib.Path.read_text", side_effect=AssertionError("Unexpected file read")):
            self.assertEqual(self.knowledge.search("unseen")["items"], [])

    def test_invalid_memory_and_pagination_leave_no_records(self):
        for extra in ({"evidence": []}, {"basis": "certain"}, {"basis": []}, {"state": {}},
                      {"source": self.prompt}, {"state": "approved"},
                      {"tags": [" "]}, {"tags": "tag"}, {"evidence": [None]}, {"body": " "},
                      {"limits": None}, {"title": ""}, {"authority": "implementation"}):
            data = {"title": "Idea", "body": "Hypothesis", "evidence": ["source"]} | extra
            with self.subTest(extra=extra), self.assertRaises(RoomError):
                self.knowledge.write("CODEX_EXPERT", data)
        for kwargs in ({"limit": 0}, {"limit": 51}, {"after": -1}, {"query": "x" * 201}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RoomError):
                self.knowledge.search(**kwargs)
        self.assertEqual(self.knowledge.search()["items"], [])

    def test_search_shows_late_matches_without_changing_full_records_or_empty_search(self):
        data = {"title": "Earlier context " * 30 + "Do not generalize: Straße",
                "body": "Earlier observations. " * 70 + "No claims about HTTP Retry-After or other APIs.",
                "applies_when": "Fixture conditions. " * 30 + "Only the fictional adapter is supported.",
                "limits": "Prior uncertainty. " * 40 + "Unknown units remain untested.",
                "tags": ["Earlier labels " * 20 + "counterevidence"], "evidence": ["local fixture"]}
        record = self.knowledge.write("CODEX_EXPERT", data)
        before = self.store.path.read_bytes()
        for query, field, expected in (("STRASSE", "title", "Do not generalize: Straße"),
                                       ("HTTP Retry-After", "preview", "No claims about HTTP Retry-After"),
                                       ("fictional", "applies_when", "Only the fictional adapter"),
                                       ("untested", "limits", "Unknown units remain untested"),
                                       ("counterevidence", "tags", "counterevidence")):
            with self.subTest(query=query):
                item = self.knowledge.search(query)["items"][0]
                excerpt = item[field][0] if field == "tags" else item[field]
                self.assertIn(expected, excerpt)
                self.assertTrue(excerpt.startswith("[excerpt] "))
                self.assertIn("read full record", excerpt)
                self.assertEqual(item["read_command"], f"ihav-agent-room knowledge show {record['id']}")
        empty = self.knowledge.search()["items"][0]
        self.assertEqual(empty["preview"], data["body"][:280] + " [truncated; read full record]")
        self.assertEqual(self.knowledge.show(record["id"]), record)
        self.assertEqual(self.knowledge.history(record["id"])["items"][0]["body"], data["body"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_excerpt_maps_unicode_casefold_offsets_and_does_not_expand_field_budgets(self):
        # Each sharp-s expands to two characters during casefold; source offsets must not drift.
        body = "ß" * 400 + " Do not reuse Straße without evidence. " + "tail " * 150
        record = self.knowledge.write("CODEX_EXPERT", {"title": "Unicode", "body": body, "evidence": ["literal x%_ a.b"]})
        item = self.knowledge.search("STRASSE")["items"][0]
        self.assertIn("Do not reuse Straße without evidence.", item["preview"])
        self.assertLessEqual(len(item["preview"]), 280 + len("[excerpt]  [truncated; read full record]"))
        self.assertEqual(self.knowledge.search("x%_ a.b")["items"][0]["preview"], self.knowledge.search()["items"][0]["preview"])
        self.assertEqual(self.knowledge.search("a.*")["items"], [])
        self.assertEqual(self.knowledge.show(record["id"])["body"], body)

    def test_detailed_lessons_keep_full_text_but_search_and_delivery_use_previews(self):
        data = {"title": "Specific context " * 40, "body": "Detailed finding " * 600,
                "evidence": ["Evidence and conditions " * 70 for _ in range(15)],
                "tags": ["Long descriptive tag " * 8 for _ in range(20)] + ["tail-tag"],
                "applies_when": "Conditions " * 200, "limits": "Counterexamples " * 150}
        record = self.knowledge.write("CODEX_EXPERT", data)
        for key, value in data.items():
            self.assertEqual(self.knowledge.show(record["id"])[key], value)
            self.assertEqual(self.knowledge.history(record["id"])["items"][0][key], value)
        # Matching includes tags beyond the preview; truncation never alters the source.
        preview = self.knowledge.search("tail-tag")["items"][0]
        self.assertEqual(preview["id"], record["id"])
        self.assertLess(len(dumps(preview)), 2600)
        self.assertIn("truncated", preview["title"])
        self.assertIn("read full record", preview["tags"][-1])
        message = self.store.send("CODEX_EXPERT", "CLAUDE_01", "Please challenge this lesson", knowledge_id=record["id"])
        reference = self.store.inbox("CLAUDE_01")["items"][0]["knowledge_reference"]
        self.assertEqual(reference["title"], preview["title"])
        self.assertLess(len(dumps(reference)), 800)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="fixture-generation")
            self.store.put_room(db, room)
        attempt = self.store.begin_attempt(message, "fixture-generation")
        self.assertEqual(attempt["knowledge_reference"], reference)
        revised = self.knowledge.write("CLAUDE_01", {"limits": "Revised conditions " * 200}, record["id"], 1)
        self.assertEqual(revised["version"], 2)
        self.assertEqual(self.knowledge.history(record["id"])["items"][0]["limits"], data["limits"])

    def test_cli_identity_write_and_compact_json_read(self):
        data = self.project / "lesson.json"
        data.write_text(json.dumps({"title": "Lesson", "body": "Finding", "evidence": ["local experiment"]}))
        argv = ["--project", str(self.project), "knowledge", "add", "--input", str(data)]
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": "", "IHAV_AGENT_ROOM_SESSION_ID": ""}), self.assertRaises(RoomError):
            run(parser().parse_args(argv))
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "test-main"}
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01", "IHAV_AGENT_ROOM_SESSION_ID": "test-main"}):
            record = run(parser().parse_args(argv))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--project", str(self.project), "--json", "knowledge", "search", "finding"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())["data"]["items"][0]["id"], record["id"])


class KnowledgeMigrationTests(EvidenceFixture, unittest.TestCase):
    def legacy(self):
        task = self.task()
        self.submit(task)
        db = self.store.connect()
        try:
            db.execute("DROP TABLE knowledge")
            room = self.store.get_room(db)
            room["schema"] = 2
            self.store.put_room(db, room)
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if row[0] not in {"meta", "events", "sqlite_sequence"}]
            return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}
        finally:
            db.close()

    def test_schema_two_backup_upgrade_preserves_every_existing_record(self):
        before = self.legacy()
        result = migrate(self.store)
        with self.store.read() as db:
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in db.execute(f"SELECT * FROM {table}")], rows)
        backup = sqlite3.connect(result["backup"])
        try:
            self.assertEqual(json.loads(backup.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])["schema"], 2)
            self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='knowledge'").fetchone())
        finally:
            backup.close()
        self.assertEqual(self.store.room()["schema"], 3)
        self.assertEqual(Knowledge(self.store).search()["items"], [])
        self.assertFalse(migrate(self.store)["migrated"])

    def test_schema_two_failed_upgrade_rolls_back_with_readable_backup(self):
        self.legacy()
        with patch("ihav_agent_room.schema.KNOWLEDGE_SCHEMA", KNOWLEDGE_SCHEMA + "INVALID SQL;"), self.assertRaises(sqlite3.Error):
            migrate(self.store)
        db = self.store.connect()
        try:
            self.assertEqual(json.loads(db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])["schema"], 2)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='knowledge'").fetchone())
        finally:
            db.close()
        self.assertEqual(len(list((self.store.runtime / "backups").glob("schema-2-*.sqlite3"))), 1)
        self.assertTrue(migrate(self.store)["migrated"])

    def test_running_schema_two_room_requires_stop(self):
        self.legacy()
        db = self.store.connect()
        try:
            room = json.loads(db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])
            room["status"] = "running"
            self.store.put_room(db, room)
        finally:
            db.close()
        with self.assertRaises(RoomError) as error:
            migrate(self.store)
        self.assertEqual(error.exception.code, "conflict")
