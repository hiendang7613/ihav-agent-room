"""Members can follow through on advisory ideas without acquiring admin authority."""

import concurrent.futures
import hashlib
import io
import json
import os
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from ihav_agent_room.cli import main
from ihav_agent_room.common import RoomError, dumps
from ihav_agent_room.knowledge import Knowledge
from ihav_agent_room.store import Store
from test_evidence import EvidenceFixture


class NoteFollowupTests(EvidenceFixture, unittest.TestCase):
    def note(self, kind="proposal", **extra):
        return self.store.add_note("CODEX_EXPERT", dict(kind=kind, body="Try a smaller experiment") | extra)

    def records(self, table):
        with self.store.read() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def logical_messages(self):
        return [message for message in self.records("messages")
                if not json.loads(message["context"]).get("broadcast")]

    def test_author_can_answer_correct_and_reopen_question_without_admin_receipt(self):
        question = self.note("question")
        answered = self.store.resolve_note("CODEX_EXPERT", question["id"], 1,
            {"state": "answered", "answer": "The local experiment explained the discrepancy."})
        self.assertEqual(answered["resolution"], {"actor": "CODEX_EXPERT", "basis": "peer"})
        reopened = self.store.resolve_note("CODEX_EXPERT", question["id"], 2,
            {"state": "open", "answer": "Counterexample in work.py:1; investigate the narrower condition."})
        self.assertEqual((reopened["author"], reopened["version"], reopened["state"]), ("CODEX_EXPERT", 3, "open"))
        self.assertNotIn("source", reopened)
        self.assertEqual(self.records("tasks"), [])
        self.assertEqual(self.records("knowledge"), [])

    def test_author_can_retire_and_replace_proposal_without_changing_task_or_review(self):
        task = self.task()
        self.review(self.submit(task))
        original = self.current(task)
        first = self.note(tasks=[task["id"]])
        second = self.note(body="Use the counterexample to narrow the next probe")
        revised = self.store.resolve_note("CODEX_EXPERT", first["id"], 1,
            {"state": "superseded", "superseded_by": second["id"], "answer": "The first hypothesis did not hold."})
        self.assertEqual(revised["superseded_by"], second["id"])
        self.assertEqual(self.current(task), original)
        self.assertEqual(self.store.status()["tasks"][0]["review_status"]["state"], "approved")
        reopened = self.store.resolve_note("CODEX_EXPERT", first["id"], 2,
            {"state": "open", "answer": "A new condition makes the first idea worth revisiting."})
        self.assertNotIn("superseded_by", reopened)
        self.assertEqual(self.store.revision_history("notes", first["id"])["items"][1]["superseded_by"], second["id"])

    def test_one_notice_to_main_when_main_is_also_linked_task_owner(self):
        task = self.task()
        note = self.note(tasks=[task["id"], task["id"]])
        messages = self.logical_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual((messages[0]["recipient"], messages[0]["task"]), ("CLAUDE_01", task["id"]))
        self.assertIn(note["id"], messages[0]["body"])
        self.assertIn("v1", messages[0]["body"])
        self.store.resolve_note("CODEX_EXPERT", note["id"], 1,
            {"state": "rejected", "answer": "The experiment contradicted it."})
        self.assertEqual(len(self.logical_messages()), 2)
        self.assertIn("rejected", self.logical_messages()[-1]["body"])

    def test_peer_cannot_resolve_another_authors_note_or_approve_their_own(self):
        note = self.note()
        before = self.store.path.read_bytes()
        for actor, data in (("CLAUDE_EXPERT", {"state": "rejected", "answer": "My objection"}),
                            ("CODEX_EXPERT", {"state": "approved", "answer": "I approve"}),
                            ("CODEX_EXPERT", {"state": "approved", "answer": "I approve", "source": self.prompt}),
                            ("CODEX_EXPERT", {"state": "rejected", "answer": "Admin said so", "source": self.prompt}),
                            ("CODEX_EXPERT", {"answer": "Evidence", "condition_evidence": "Claimed success"}),
                            ("UNKNOWN", {"state": "rejected", "answer": "No"})):
            with self.subTest(actor=actor, data=data), self.assertRaises(RoomError):
                self.store.resolve_note(actor, note["id"], 1, data)
            self.assertEqual(self.store.path.read_bytes(), before)
        coordinated = self.store.resolve_note("CLAUDE_01", note["id"], 1,
            {"state": "rejected", "answer": "Consolidated with the author's newer idea."})
        self.assertEqual(coordinated["resolution"]["basis"], "peer")

    def test_admin_resolved_and_legacy_bound_notes_keep_their_authority_boundary(self):
        note = self.note()
        approved = self.store.resolve_note("CLAUDE_01", note["id"], 1,
            {"state": "approved", "answer": "Admin accepts this", "source": self.prompt})
        self.assertEqual(approved["resolution"], {"actor": "CLAUDE_01", "basis": "admin"})
        for actor in ("CODEX_EXPERT", "CLAUDE_01"):
            with self.assertRaises(RoomError):
                self.store.resolve_note(actor, note["id"], 2, {"state": "superseded", "answer": "Withdraw"})
        rejected = self.note()
        self.store.resolve_note("CLAUDE_01", rejected["id"], 1,
            {"state": "rejected", "answer": "Admin rejected it", "source": self.prompt})
        with self.assertRaises(RoomError):
            self.store.resolve_note("CODEX_EXPERT", rejected["id"], 2, {"state": "open", "answer": "Reopen"})
        task = self.task(review_policy="none", reviewer=None)
        legacy = self.note(tasks=[task["id"]])
        with self.store.tx() as db:
            current = self.store.record(db, "tasks", task["id"])
            current["decisions"] = {legacy["id"]: 1}
            self.store.save(db, "tasks", current, current["version"])
        before = self.store.path.read_bytes()
        for actor in ("CODEX_EXPERT", "CLAUDE_01"):
            with self.assertRaises(RoomError):
                self.store.resolve_note(actor, legacy["id"], 1, {"state": "rejected", "answer": "Withdraw"})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.resolve_note("CLAUDE_01", legacy["id"], 1,
            {"state": "superseded", "answer": "Admin removes the old constraint", "source": self.prompt})
        self.assertEqual(self.current(task)["decisions"], {})

    def test_revisions_are_durable_paginated_and_separate_from_knowledge(self):
        note = self.note("question")
        Knowledge(self.store).write("CODEX_EXPERT", {"title": "A lesson", "body": "An observation", "evidence": ["work.py:1"]})
        self.store.resolve_note("CODEX_EXPERT", note["id"], 1, {"state": "answered", "answer": "First finding"})
        self.note()
        self.store.resolve_note("CODEX_EXPERT", note["id"], 2, {"answer": "Corrected finding"})
        reloaded = Store(self.project)
        before = self.store.path.read_bytes()
        page = reloaded.revision_history("notes", note["id"], limit=2)
        self.assertEqual([item["version"] for item in page["items"]], [1, 2])
        self.assertEqual((page["current_version"], page["current_state"]), (3, "answered"))
        last = reloaded.revision_history("notes", note["id"], after=page["next_after"], limit=2)
        self.assertEqual([item["version"] for item in last["items"]], [3])
        self.assertEqual(last["items"][0]["answer"], "Corrected finding")
        self.assertIsNone(last["next_after"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_older_notes_have_no_invented_history(self):
        note = self.note()
        with self.store.tx() as db:
            db.execute("DELETE FROM events WHERE kind='note.revised'")
        self.assertEqual(self.store.revision_history("notes", note["id"])["items"], [])
        self.store.resolve_note("CODEX_EXPERT", note["id"], 1, {"state": "rejected", "answer": "No longer useful"})
        self.assertEqual([item["version"] for item in self.store.revision_history("notes", note["id"])["items"]], [2])

    def test_conflicting_writes_have_one_result_and_one_notification(self):
        note = self.note("question")
        def answer(text):
            try:
                Store(self.project).resolve_note("CODEX_EXPERT", note["id"], 1, {"state": "answered", "answer": text})
                return "saved"
            except RoomError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(answer, ["First answer", "Second answer"]))
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(len(self.logical_messages()), 2)
        self.assertEqual(len(self.store.revision_history("notes", note["id"])["items"]), 2)

    def test_history_event_failure_rolls_back_note_and_notice(self):
        note = self.note()
        before = self.store.path.read_bytes()
        with patch.object(self.store, "event", side_effect=OSError("event disk failure")), self.assertRaises(OSError):
            self.store.resolve_note("CODEX_EXPERT", note["id"], 1, {"state": "rejected", "answer": "Withdraw"})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_updates_and_forged_resolution_metadata_have_no_effect(self):
        note = self.note()
        before = self.store.path.read_bytes()
        for data in ({"state": "rejected", "answer": "   "},
                     {"state": "superseded", "answer": "Withdraw", "superseded_by": "N-missing"},
                     {"state": "superseded", "answer": "Withdraw", "superseded_by": note["id"]},
                     {"state": "open", "answer": "Withdraw", "superseded_by": note["id"]},
                     {"answer": "Withdraw", "resolution": {"basis": "admin"}}):
            with self.subTest(data=data), self.assertRaises(RoomError):
                self.store.resolve_note("CODEX_EXPERT", note["id"], 1, data)
            self.assertEqual(self.store.path.read_bytes(), before)
        for extra in ({"source": self.prompt}, {"resolution": {"basis": "admin", "actor": "CLAUDE_01"}}):
            with self.assertRaises(RoomError):
                self.note(**extra)
            self.assertEqual(self.store.path.read_bytes(), before)
        for kwargs in ({"after": -1}, {"limit": 0}, {"limit": 51}):
            with self.assertRaises(RoomError):
                self.store.revision_history("notes", note["id"], **kwargs)

    def test_real_cli_binds_author_and_history_does_not_write_projections(self):
        note = self.note("question")
        patch_file = self.project / "answer.json"
        patch_file.write_text(dumps({"state": "answered", "answer": "The fixture explains it"}))
        argv = ["--project", str(self.project), "--json", "note", "resolve", note["id"],
                "--expected-version", "1", "--input", str(patch_file)]
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["status"] = "running"
            self.store.put_room(db, room)
        token = "fixture-only-binding"
        self.store.member("CODEX_EXPERT", {"token_hash": hashlib.sha256(token.encode()).hexdigest()})
        env = {"IHAV_AGENT_ROOM_MEMBER": "CODEX_EXPERT", "IHAV_AGENT_ROOM_BINDING": token, "IHAV_AGENT_ROOM_SESSION_ID": ""}
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(argv), 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["data"]["resolution"]["basis"], "peer")
        before = self.store.path.read_bytes()
        with patch.object(Store, "project_views", side_effect=AssertionError("History wrote a view")), \
                patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": "", "IHAV_AGENT_ROOM_BINDING": ""}), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--project", str(self.project), "--json", "note", "history", note["id"]]), 0)
        self.assertEqual([item["version"] for item in json.loads(output.getvalue())["data"]["items"]], [1, 2])
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
