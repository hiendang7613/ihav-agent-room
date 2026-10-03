"""Selective discussion recall stays read-only and separate from task obligations."""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ihav_agent_room.cli import main, parser, run
from ihav_agent_room.common import PLUGIN_ROOT, RoomError, dumps
from ihav_agent_room.store import Store
from test_evidence import EvidenceFixture


class NoteSearchTests(EvidenceFixture, unittest.TestCase):
    def note(self, actor="CODEX_EXPERT", **extra):
        return self.store.add_note(actor, dict(kind="question", body="An open question") | extra)

    def search(self, query="", **filters):
        return self.store.search_notes(query, **filters)

    def test_note_list_keeps_full_output_without_reading_unrelated_review_sources(self):
        task = self.task()
        self.review(self.submit(task))
        self.note(tasks=[task["id"]])
        expected = self.store.status()["notes"]
        before = self.store.path.read_bytes()
        with patch.object(Store, "status", side_effect=AssertionError("Note read rebuilt room status")), \
                patch.object(Store, "review_status", side_effect=AssertionError("Note read hashed task source")):
            actual = run(parser().parse_args(["--project", str(self.project), "note", "list"]))
        self.assertEqual(actual, expected)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_open_search_combines_author_kind_and_exact_task_filters(self):
        first, second = self.task(), self.task()
        wanted = self.note(body="Could CACHE reuse a saved result?", tasks=[first["id"]])
        self.note(body="Could cache reuse work elsewhere?", tasks=[second["id"]])
        self.note("CLAUDE_01", body="Could cache reuse work?", tasks=[first["id"]])
        self.note(kind="proposal", body="Cache reuse is an idea", tasks=[first["id"]])
        self.note(body="Cache only", tasks=[first["id"]])
        closed = self.note(body="Cache reuse was investigated", tasks=[first["id"]])
        self.store.resolve_note("CODEX_EXPERT", closed["id"], 1, {"state": "answered", "answer": "Only in the fixture"})
        page = self.search("  cache\tREUSE ", author="CODEX_EXPERT", kind="question", task_id=first["id"])
        self.assertEqual([item["id"] for item in page["items"]], [wanted["id"]])
        self.assertEqual(page["filters"], {"query": "  cache\tREUSE ", "state": "open", "author": "CODEX_EXPERT",
                                         "kind": "question", "task": first["id"]})
        self.assertIsNone(page["next_after"])
        self.assertIn("advisory", page["rule"].lower())

    def test_current_answers_are_searchable_and_closed_history_is_explicit(self):
        question = self.note(body="What changed?")
        self.store.resolve_note("CODEX_EXPERT", question["id"], 1,
                                {"state": "answered", "answer": "First hypothesis: timeout"})
        self.assertEqual(self.search("timeout")["items"], [])
        self.assertEqual(self.search("timeout", state="all")["items"][0]["id"], question["id"])
        self.store.resolve_note("CODEX_EXPERT", question["id"], 2,
                                {"state": "open", "answer": "Counterexample: a connection reset"})
        self.assertEqual(self.search("timeout", state="all")["items"], [])
        current = self.search("reset")["items"][0]
        self.assertEqual((current["id"], current["version"], current["state"]), (question["id"], 3, "open"))
        self.assertIn("reset", current["answer_preview"])
        self.assertEqual(len(self.store.revision_history("notes", question["id"])["items"]), 3)
        decision = self.note("CLAUDE_01", kind="decision", body="Use the reset fixture", source=self.prompt)
        self.assertEqual(self.search("reset", kind="decision")["items"], [])
        self.assertEqual(self.search("reset", kind="decision", state="approved")["items"][0]["id"], decision["id"])

    def test_pagination_filters_before_limit_and_fresh_sweeps_find_reopened_notes(self):
        earlier = self.note(body="needle previously settled")
        self.store.resolve_note("CODEX_EXPERT", earlier["id"], 1, {"state": "answered", "answer": "Initial result"})
        wanted = []
        for i in range(3):
            self.note(body="Unrelated discussion")
            wanted.append(self.note(body=f"needle question {i}"))
        first = self.search("needle", limit=2)
        self.assertEqual([item["id"] for item in first["items"]], [item["id"] for item in wanted[:2]])
        self.assertIsNotNone(first["next_after"])
        second = Store(self.project).search_notes("needle", after=first["next_after"], limit=2)
        self.assertEqual([item["id"] for item in second["items"]], [wanted[2]["id"]])
        self.assertIsNone(second["next_after"])
        self.store.resolve_note("CODEX_EXPERT", earlier["id"], 2, {"state": "open", "answer": "New evidence"})
        self.assertEqual(self.search("needle")["items"][0]["id"], earlier["id"])
        empty = self.search("needle", after=second["items"][0]["cursor"])
        self.assertEqual(empty["items"], [])
        self.assertIsNone(empty["next_after"])

    def test_previews_are_bounded_and_full_records_remain_readable(self):
        question = self.note(body="Long rationale " * 600 + "rare-tail")
        self.store.resolve_note("CODEX_EXPERT", question["id"], 1, {"answer": "Long answer " * 600 + "counterexample"})
        item = self.search("rare-tail counterexample")["items"][0]
        self.assertLess(len(dumps(item)), 1000)
        self.assertEqual(item["author"], "CODEX_EXPERT")
        self.assertEqual(item["read_command"], f"ihav-agent-room note show {question['id']}")
        self.assertIn("truncated", item["body_preview"])
        full = run(parser().parse_args(["--project", str(self.project), *item["read_command"].split()[1:]]))
        self.assertTrue(full["body"].endswith("rare-tail"))
        self.assertTrue(full["answer"].endswith("counterexample"))
        compact = self.store.status(compact=True)["notes"][0]
        self.assertEqual(compact["author"], "CODEX_EXPERT")
        self.assertNotIn("answer_preview", compact)

    def test_literal_matching_and_invalid_filters_do_not_broaden_or_mutate(self):
        question = self.note(body="Literal 100% a.b and Straße")
        self.assertEqual(self.search("100% a.b STRASSE")["items"][0]["id"], question["id"])
        self.assertEqual(self.search("a.*")["items"], [])
        before = self.store.path.read_bytes()
        for query, filters in ((None, {}), ("x" * 201, {}), ("", {"after": -1}),
                               ("", {"limit": 0}), ("", {"limit": 51}), ("", {"state": "pending"}),
                               ("", {"author": "UNKNOWN"}), ("", {"kind": "task"}),
                               ("", {"task_id": "T-absent"}), ("", {"task_id": ""})):
            with self.subTest(query=query, filters=filters), self.assertRaises(RoomError):
                self.search(query, **filters)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_search_excerpts_surface_late_scope_and_counterevidence_without_changing_status(self):
        body = "Earlier proposal context. " * 40 + "No immediate implementation is requested."
        answer = "Earlier observations. " * 45 + "Counterexample: Straße remains unsupported."
        question = self.note(body=body)
        self.store.resolve_note("CODEX_EXPERT", question["id"], 1, {"answer": answer})
        before = self.store.path.read_bytes()
        page = self.search("immediate STRASSE")
        item = page["items"][0]
        self.assertIn("No immediate implementation is requested.", item["body_preview"])
        self.assertIn("Counterexample: Straße remains unsupported.", item["answer_preview"])
        for field in ("body_preview", "answer_preview"):
            self.assertTrue(item[field].startswith("[excerpt] "))
            self.assertLessEqual(len(item[field]), 240 + len("[excerpt]  [truncated; read full record]"))
        self.assertEqual((item["id"], item["version"], item["state"]), (question["id"], 2, "open"))
        prefix = body[:240] + " [truncated; read full record]"
        self.assertEqual(self.search()["items"][0]["body_preview"], prefix)
        self.assertEqual(self.store.status(compact=True)["notes"][0]["body_preview"], prefix)
        full = self.store.list_notes()[0]
        self.assertEqual((full["body"], full["answer"]), (body, answer))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_real_cli_search_needs_no_member_and_has_no_projection_or_native_effects(self):
        question = self.note(body="Resume the useful discussion")
        self.store.project_views()
        before = {str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        args = ["--project", str(self.project), "--json", "note", "search", "resume", "--author", "CODEX_EXPERT"]
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": "", "IHAV_AGENT_ROOM_BINDING": ""}), \
                patch.object(Store, "actor", side_effect=AssertionError("Read requested a member binding")), \
                patch.object(Store, "status", side_effect=AssertionError("Read rebuilt room status")), \
                patch.object(Store, "project_views", side_effect=AssertionError("Read changed projections")), \
                patch("ihav_agent_room.cli.start_room", side_effect=AssertionError("Read started native work")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["data"]["items"][0]["id"], question["id"])
        command = [sys.executable, str(PLUGIN_ROOT / "bin/ihav-agent-room"), *args]
        env = dict(os.environ, PATH="", IHAV_AGENT_ROOM_MEMBER="", IHAV_AGENT_ROOM_BINDING="")
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(json.loads(result.stdout)["data"]["items"][0]["id"], question["id"])
        for flags, expected in ((["--limit", "0"], 1), (["--state", "pending"], 2)):
            result = subprocess.run(command + flags, env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected)
            self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual({str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}, before)

    def test_task_context_links_discussion_without_inventing_obligations(self):
        task = self.task()
        submission = self.submit(task)
        self.review(submission)
        before_task = self.current(task)
        before_attention = self.store.task_context(task["id"])["attention"]
        self.note(kind="proposal", body="Try another idea later", tasks=[task["id"]])
        pack = self.store.task_context(task["id"])
        self.assertIn(f"ihav-agent-room note search --task {task['id']}", pack["full_record_commands"])
        self.assertEqual(pack["attention"], before_attention)
        self.assertEqual(self.current(task), before_task)
        self.assertEqual(pack["review"]["state"], "approved")


if __name__ == "__main__":
    unittest.main()
