"""Selective room history recall preserves full records and has no processing effects."""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import PLUGIN_ROOT
from ihav_agent_room.store import MAX_MESSAGE_ID_BYTES, Store
from test_evidence import EvidenceFixture


class HistorySearchTests(EvidenceFixture, unittest.TestCase):
    def history(self, *args):
        return run(parser().parse_args(["--project", str(self.project), "history", *args]))

    def test_message_history_can_look_up_the_full_id_shown_in_a_room_digest(self):
        message_id = "M-" + "x" * (MAX_MESSAGE_ID_BYTES - 2)
        message = self.store.send("CODEX_EXPERT", "CLAUDE_01", "A finding to inspect", message_id=message_id)
        before = self.store.path.read_bytes()
        result = self.history("--kind", "messages", "--query", message_id)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["items"][0]["id"], message_id)
        self.assertTrue(all(json.loads(row["context"]).get("broadcast", {}).get("id") == message_id
                            for row in result["items"][1:]))
        self.assertEqual(result["items"][0]["body"], message["body"])
        self.assertIsNone(result["next_after"])
        self.assertEqual(self.store.path.read_bytes(), before, "History by ID is read-only")

    def test_literal_unicode_search_filters_before_pagination_across_both_speakers(self):
        wanted = []
        for number, (sender, recipient) in enumerate((("CLAUDE_01", "CODEX_EXPERT"),
                                                     ("CODEX_EXPERT", "CLAUDE_01"),
                                                     ("CLAUDE_01", "CODEX_EXPERT"))):
            self.store.send(sender, recipient, "Unrelated discussion")
            wanted.append(self.store.send(sender, recipient, f"Straße 100% a.b: finding {number}"))
        self.store.acknowledge("CODEX_EXPERT", wanted[0]["id"], "Considered the first finding")
        with self.store.tx() as db:
            db.execute("UPDATE messages SET status='unknown' WHERE id=?", (wanted[1]["id"],))
            wanted_ids = [row["id"] for row in wanted]
            expected = [dict(row) for row in db.execute(
                "SELECT rowid AS cursor,* FROM messages WHERE id IN (?, ?, ?) "
                "OR json_extract(context,'$.broadcast.id') IN (?, ?, ?) ORDER BY rowid",
                (*wanted_ids, *wanted_ids))]
        before = self.store.path.read_bytes()
        first = self.history("--query", " STRASSE\t100% a.b ", "--limit", "2")
        self.assertEqual([r["id"] for r in first["items"]], [r["id"] for r in expected[:2]])
        self.assertEqual([r["status"] for r in first["items"]], [r["status"] for r in expected[:2]])
        pages = list(first["items"])
        after = first["next_after"]
        while after is not None:
            page = self.history("--query", "STRASSE 100% a.b", "--limit", "2", "--after", str(after))
            pages.extend(page["items"])
            after = page["next_after"]
        self.assertEqual([r["id"] for r in pages], [r["id"] for r in expected])
        self.assertEqual([r["status"] for r in pages], [r["status"] for r in expected])
        for query in ("a.*", "STRASSE absent", "' OR 1=1 --"):
            self.assertEqual(self.history("--query", query), {"items": [], "next_after": None})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_full_prompt_provenance_and_event_payloads_remain_unchanged(self):
        text = "Keep wording ngắn gọn. " + "Complete context " * 500 + "source-tail"
        human = self.store.intake("main", text)
        peer = self.store.intake("peer", text, origin="peer")
        self.store.account("CLAUDE_01", human, "Recorded the scoped preference", [])
        with self.store.tx() as db:
            seq = self.store.event(db, "fixture.review", {"finding": "NGẮN GỌN", "evidence": ["one\nsource", "quote: \""]})
        before = self.store.path.read_bytes()
        result = self.history("--kind", "prompts", "--query", "NGẮN GỌN source-tail")
        self.assertEqual([r["id"] for r in result["items"]], [human, peer])
        self.assertEqual([r["origin"] for r in result["items"]], ["hook", "peer"])
        self.assertIsNotNone(result["items"][0]["accounted"])
        self.assertIsNone(result["items"][1]["accounted"])
        self.assertTrue(all(r["body"] == text for r in result["items"]))
        event = self.history("--kind", "events", "--query", "ngắn gọn")["items"]
        self.assertEqual([r["seq"] for r in event], [seq])
        self.assertEqual(json.loads(event[0]["data"]), {"finding": "NGẮN GỌN", "evidence": ["one\nsource", "quote: \""]})
        self.assertEqual(self.history("--kind", "events", "--query", "fixture.review")["items"], [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_empty_query_retains_full_history_and_cursors_for_every_kind(self):
        for i in range(3):
            self.store.send("CODEX_EXPERT", "CLAUDE_01", f"Idea {i}")
            self.store.intake("main", f"Request {i}")
            with self.store.tx() as db:
                self.store.event(db, "fixture", {"number": i})
        for kind in ("messages", "prompts", "events"):
            with self.subTest(kind=kind), self.store.read() as db:
                expected = [dict(r) for r in db.execute(f"SELECT rowid AS cursor,* FROM {kind} WHERE rowid>1 ORDER BY rowid LIMIT 3")]
                page = {"items": expected[:2], "next_after": expected[1]["cursor"] if len(expected) > 2 else None}
                args = ("--kind", kind, "--after", "1", "--limit", "2")
                for query in ((), ("--query", ""), ("--query", " \t\n ")):
                    self.assertEqual(self.history(*args, *query), page)

    def test_public_cli_is_read_only_without_member_or_native_programs(self):
        task = self.task()
        self.review(self.submit(task))
        message = self.store.send("CODEX_EXPERT", "CLAUDE_01", "A taskless alternative to discuss")
        before = {str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        with patch.object(Store, "actor", side_effect=AssertionError("History required a member")), \
                patch.object(Store, "status", side_effect=AssertionError("History rebuilt status")), \
                patch.object(Store, "review_status", side_effect=AssertionError("History read unrelated sources")):
            self.assertEqual(self.history("--query", "taskless alternative")["items"][0]["id"], message["id"])
        env = dict(os.environ, PATH="", IHAV_AGENT_ROOM_MEMBER="", IHAV_AGENT_ROOM_SESSION_ID="", IHAV_AGENT_ROOM_BINDING="")
        command = [sys.executable, str(PLUGIN_ROOT / "bin/ihav-agent-room"), "--project", str(self.project), "--json", "history"]
        for flags, code in ((["--query", "taskless alternative"], 0), (["--query", "x" * 201], 1),
                            (["--query", "missing", "--limit", "0"], 1), (["--after", "-1"], 1),
                            (["--limit", "201"], 1), (["--kind", "invalid"], 2)):
            with self.subTest(flags=flags):
                result = subprocess.run(command + flags, cwd=self.project, env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, code, result.stderr + result.stdout)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                data = json.loads(result.stdout)
                self.assertEqual(data["ok"], code == 0)
                if code == 0:
                    self.assertEqual(data["data"]["items"][0]["id"], message["id"])
        self.assertEqual({str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
