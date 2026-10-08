import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import RoomError
from ihav_agent_room.continuity import REPLY_BYTES, recovery_context
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.project = self.project.resolve()
        initialize(self.project)
        self.store = Store(self.project)
        self.environment = patch.dict(os.environ, {
            "CODEX_THREAD_ID": "new-codex", "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"), "IHAV_HOME": str(self.root / "ihav")}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(gateway="CODEX_01", owner={"host": "codex", "session": "new-codex"},
                        host_session_history=[{"host": "codex", "session": "old-codex"}])
            self.store.put_room(db, room)
            self.store.event(db, "room.gateway_changed", {"former_owner": {"session": "old-claude"}})

    def claude_transcript(self, text, cwd=None, session="old-claude"):
        directory = re.sub(r"[^A-Za-z0-9]", "-", str(self.project))
        path = self.root / "claude/projects" / directory / "old-claude.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"type": "assistant", "cwd": str(cwd or self.project), "sessionId": session,
               "timestamp": "2026-10-04T08:49:20Z", "message": {"role": "assistant",
               "content": [{"type": "text", "text": text}]}}
        path.write_text(json.dumps(row) + "\n")
        return path

    def codex_transcript(self, session, text, cwd=None):
        path = self.root / "codex/sessions/2026/10/05" / ("rollout-fixture-" + session + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "session_meta", "payload": {"id": session, "cwd": str(cwd or self.project)}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                 "channel": "final", "content": [{"type": "output_text", "text": text}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "Private user text must not be imported"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                 "channel": "commentary", "content": [{"type": "output_text", "text": "Progress, not final"}]}}]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def test_closed_tasks_do_not_erase_old_goals_and_unapproved_next_action(self):
        old = "L1. Support 11 chatbots. Q1. Approve: read saved conversation once? B1. Gemini."
        self.claude_transcript(old)
        self.codex_transcript("old-codex", "Room connected; no active tasks.")
        self.codex_transcript("new-codex", "Start succeeded.")
        before = self.store.path.read_bytes()
        result = run(parser().parse_args(["--project", str(self.project), "context"]))
        self.assertEqual([reply["host"] for reply in result["historical_replies"]], ["codex", "claude", "codex"])
        self.assertIn(old, [reply["text"] for reply in result["historical_replies"]])
        self.assertNotIn("Private user text", json.dumps(result))
        self.assertNotIn("Progress, not final", json.dumps(result))
        self.assertIn("grant no authority", result["rule"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_wrong_project_or_session_never_imports_a_reply(self):
        self.claude_transcript("Unrelated secret", session="another-session")
        self.codex_transcript("old-codex", "Unrelated secret", cwd=self.root / "another-project")
        result = recovery_context(self.store)
        self.assertEqual(result["historical_replies"], [])
        self.assertEqual(len(result["unavailable_sources"]), 3)

    def test_parent_folder_codex_session_has_an_explicit_cwd_gap_and_no_import(self):
        self.codex_transcript("old-codex", "Unrelated project closing", cwd=self.root)
        result = recovery_context(self.store)
        gap = next(item for item in result["unavailable_sources"] if item["session"] == "old-codex")
        self.assertIn("cwd differs", gap["reason"])
        self.assertNotIn("Unrelated project closing", json.dumps(result))

    def test_exact_claude_id_in_parent_directory_reports_cwd_gap_without_reading_other_sessions(self):
        path = self.claude_transcript("Parent-only private reply", cwd=self.root)
        parent = re.sub(r"[^A-Za-z0-9]", "-", str(self.root))
        moved = self.root / "claude/projects" / parent / path.name
        moved.parent.mkdir(parents=True)
        path.rename(moved)
        result = recovery_context(self.store)
        gap = next(item for item in result["unavailable_sources"] if item["session"] == "old-claude")
        self.assertIn("cwd differs", gap["reason"])
        self.assertEqual(result["historical_replies"], [])
        self.assertNotIn("Parent-only private reply", json.dumps(result))

    def test_clear_environment_fixture_keeps_global_storage_in_its_own_temporary_home(self):
        self.assertEqual(os.environ["IHAV_HOME"], str(self.root / "ihav"))
        self.assertTrue(Path(os.environ["IHAV_HOME"]).is_relative_to(self.root))

    def test_worker_or_wrong_gateway_cannot_read_private_main_context(self):
        self.claude_transcript("Private gateway context")
        with patch.dict(os.environ, CODEX_THREAD_ID="unbound-thread"), \
                patch("ihav_agent_room.continuity.transcript_path") as lookup:
            with self.assertRaises(RoomError):
                recovery_context(self.store)
            lookup.assert_not_called()

    def test_truncated_reply_is_visible_and_missing_sources_are_explicit(self):
        self.claude_transcript("x" * (REPLY_BYTES + 1))
        result = recovery_context(self.store)
        self.assertFalse(result["historical_replies"][0]["complete"])
        self.assertEqual(len(result["historical_replies"][0]["text"].encode()), REPLY_BYTES)
        self.assertEqual(len(result["unavailable_sources"]), 2)

    def test_symlink_transcript_is_not_followed(self):
        path = self.claude_transcript("Source")
        saved = self.root / "outside.jsonl"
        path.rename(saved)
        path.symlink_to(saved)
        self.assertEqual(recovery_context(self.store)["historical_replies"], [])

    def test_start_returns_working_context_without_an_extra_user_command(self):
        self.claude_transcript("Project goal; unanswered Q1.")
        with patch("ihav_agent_room.cli.doctor", return_value={"ok": True}), \
                patch("ihav_agent_room.cli.connection_plan", return_value={"resume_required": False, "blockers": []}), \
                patch("ihav_agent_room.cli.probe_codex"), \
                patch("ihav_agent_room.cli.start_room", return_value={"started": False, "reason": "supervisor already running"}):
            result = run(parser().parse_args(["--project", str(self.project), "start"]))
        self.assertTrue(result["connected"])
        self.assertIn("Project goal", result["working_context"]["historical_replies"][0]["text"])

    def phase_transcript(self, messages):
        path = self.codex_transcript("old-codex", "Replaced fixture")
        header = {"type": "session_meta", "payload": {"id": "old-codex", "cwd": str(self.project)}}
        rows = [header, *({"type": "response_item", "timestamp": timestamp,
                          "payload": {"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": text}], **fields}}
                         for timestamp, text, fields in messages)]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    def test_codex_phase_commentary_never_replaces_final_answer(self):
        closing = "Admin-Zone: L1. Eleven chatbots. Q1. Await approval. R1. sent_unknown. B1. Gemini."
        for fields in ({"phase": "commentary"}, {"phase": "commentary", "channel": None},
                       {"phase": "commentary", "channel": "final"}):
            with self.subTest(fields=fields):
                self.phase_transcript([
                    ("2026-10-05T04:40:00Z", closing, {"phase": "final_answer"}),
                    ("2026-10-05T04:42:34Z", "I will check the context next.", fields)])
                before = self.store.path.read_bytes()
                result = run(parser().parse_args(["--project", str(self.project), "context"]))
                reply = next(item for item in result["historical_replies"] if item["session"] == "old-codex")
                self.assertEqual(reply["text"], closing)
                self.assertEqual(reply["observed_at"], "2026-10-05T04:40:00Z")
                self.assertTrue(reply["complete"])
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_codex_commentary_only_is_an_unavailable_source(self):
        self.phase_transcript([("2026-10-05T04:42:34Z", "Work remains in progress.", {"phase": "commentary"})])
        result = recovery_context(self.store)
        self.assertEqual(result["historical_replies"], [])
        self.assertTrue(any(source["session"] == "old-codex" for source in result["unavailable_sources"]))

    def test_codex_unknown_phase_preserves_legacy_but_rejects_unrecognised_phase(self):
        for fields, expected in (({}, True), ({"phase": None}, True),
                                 ({"phase": None, "channel": "final"}, True),
                                 ({"phase": "future_phase"}, False),
                                 ({"phase": "final_answer", "channel": "commentary"}, False)):
            with self.subTest(fields=fields):
                self.phase_transcript([("2026-10-05T04:40:00Z", "Legacy final evidence.", fields)])
                result = recovery_context(self.store)
                self.assertEqual(bool(result["historical_replies"]), expected)
