"""Small ledgers remain readable without unrelated task or source inspection."""

import unittest
from unittest.mock import patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import dumps
from ihav_agent_room.knowledge import Knowledge
from ihav_agent_room.store import Store
from test_evidence import EvidenceFixture


class SelectiveReadTests(EvidenceFixture, unittest.TestCase):
    def test_blank_search_browses_current_records_without_text_matching_or_effects(self):
        knowledge = Knowledge(self.store)
        first = knowledge.write("CODEX_EXPERT", {"title": "A surprising observation", "body": "Unfiltered context " * 4000,
            "evidence": ["Fixture-only observation"], "limits": "Keep counterexamples"})
        retired = knowledge.write("CODEX_EXPERT", {"title": "Retired hypothesis", "body": "Disproved",
            "evidence": ["Fixture-only counterexample"], "state": "retired"})
        later = knowledge.write("CODEX_EXPERT", {"title": "A different approach", "body": "Try a different explanation",
            "evidence": ["Another fixture-only observation"]})
        note = self.store.add_note("CODEX_EXPERT", {"kind": "question", "body": "Where could this fail? " * 4000})
        answered = self.store.add_note("CODEX_EXPERT", {"kind": "question", "body": "Earlier question"})
        self.store.resolve_note("CODEX_EXPERT", answered["id"], 1, {"state": "answered", "answer": "Counterexample found"})
        before = {str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        with patch("ihav_agent_room.knowledge.matches_terms", side_effect=AssertionError("Browse matched knowledge text")), \
                patch("ihav_agent_room.store.matches_terms", side_effect=AssertionError("Browse matched note text")), \
                patch("ihav_agent_room.cli.matches_terms", side_effect=AssertionError("Unfiltered history matched text")):
            for query in ("", " \t\n "):
                with self.subTest(query=query):
                    page = knowledge.search(query, limit=1)
                    self.assertEqual([item["id"] for item in page["items"]], [first["id"]])
                    self.assertIsNotNone(page["next_after"])
                    rest = knowledge.search(query, after=page["next_after"], limit=1)
                    self.assertEqual([item["id"] for item in rest["items"]], [later["id"]])
                    self.assertIsNone(rest["next_after"])
                    self.assertEqual([item["id"] for item in knowledge.search(query, include_retired=True)["items"]],
                                     [first["id"], retired["id"], later["id"]])
                    self.assertEqual([item["id"] for item in self.store.search_notes(query)["items"]], [note["id"]])
                    self.assertEqual([item["id"] for item in self.store.search_notes(query, state="all")["items"]],
                                     [note["id"], answered["id"]])
                    prompts = run(parser().parse_args(["--project", str(self.project), "history", "--kind", "prompts", "--query", query]))
                    self.assertEqual([item["id"] for item in prompts["items"]], [self.prompt])
        self.assertEqual(knowledge.show(first["id"])["body"], first["body"])
        self.assertEqual({str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}, before)

    def test_approval_and_intake_lists_preserve_output_without_status_or_source_reads(self):
        task = self.task()
        self.review(self.submit(task))
        accounted = self.store.intake("other-session", "Already handled")
        self.store.account("CLAUDE_01", accounted, "Answer only", [])
        other = self.store.intake("other-session", "Still needs a response")
        approvals = [{"id": "A-1", "state": "pending", "request": {"text": "Native request"}},
                     {"id": "A-2", "state": "expired", "source": "original"}]
        with self.store.tx() as db:
            for approval in approvals:
                db.execute("INSERT INTO approvals VALUES (?,?)", (approval["id"], dumps(approval)))
        before = self.store.path.read_bytes()
        with patch.object(Store, "status", side_effect=AssertionError("Unrelated room status read")), \
                patch("ihav_agent_room.store.source_matches", side_effect=AssertionError("Unrelated source read")):
            for noun, expected in (("approval", approvals), ("intake", None)):
                actual = run(parser().parse_args(["--project", str(self.project), noun, "list"]))
                if expected is not None:
                    self.assertEqual(actual, expected)
                else:
                    self.assertEqual([row["id"] for row in actual], [self.prompt, other])
                    self.assertEqual(actual[1]["body"], "Still needs a response")
        self.assertEqual(self.store.path.read_bytes(), before)
