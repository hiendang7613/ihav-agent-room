"""Caller-level frame regressions, isolated room/home, no native or provider turns."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room import hooks
from ihav_agent_room.common import RoomError, native_event_identity, native_event_prompt
from ihav_agent_room.native import message_text
from ihav_agent_room.prompt_frame import load_frame
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store


PREFIX = 'team codex and claude let deep thinking, deep analysis, deep collaborate, deep debate, deep research, deep review, deep rethinking, deep discover, deep brainstorm, deep investigate, deep reasoning, deep critique,  deep explore, deep optimize, deep simplify aggressively, deep cleanup, deep remove redundancy, deep improve, deep ... to do that : '
POSTFIX = 'what do team think ? any questions to me ?'


class PromptFrameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prompt frame ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.env = patch.dict(os.environ, {"IHAV_HOME": str(self.root / "home"),
                                         "IHAV_AGENT_ROOM_HOST": "claude"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        initialize(self.project, "pair")
        self.store = Store(self.project)
        self.config_path = self.project / "agents_space/prompt_frame.json"

    def configure(self, **changes):
        config = {"schema": 1, "enabled": True, "prefix": PREFIX, "postfix": POSTFIX} | changes
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def hook(self, prompt, host="claude"):
        gateway = "CLAUDE_01" if host == "claude" else "CODEX_01"
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(gateway=gateway, owner={"session": "frame-session", "host": host}, status="running")
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": host}, clear=False):
            return hooks.handle({"cwd": str(self.project), "session_id": "frame-session",
                                 "hook_event_name": "UserPromptSubmit", "prompt": prompt})

    def test_missing_config_and_explicit_off_leave_hook_and_notice_unframed(self):
        self.assertIsNone(load_frame(self.project))
        for enabled in (None, False):
            if enabled is not None:
                self.configure(enabled=enabled)
            result = self.hook("Assess the queue design " + str(enabled))
            self.assertNotIn(PREFIX, result["hookSpecificOutput"]["additionalContext"])
            row = self.store.inbox("CODEX_01")["items"][-1]
            self.assertNotIn("prompt_frame", row["context"])
            self.assertNotIn(PREFIX, message_text(row))

    def test_both_gateway_hook_contexts_keep_exact_frame_and_valid_native_keys(self):
        self.configure()
        for host in ("claude", "codex"):
            result = self.hook("Assess the design for " + host, host)
            self.assertEqual(set(result), {"hookSpecificOutput"})
            specific = result["hookSpecificOutput"]
            self.assertEqual(set(specific), {"hookEventName", "additionalContext"})
            self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
            self.assertEqual(specific["additionalContext"].count(PREFIX), 1)
            self.assertEqual(specific["additionalContext"].count(POSTFIX), 1)
            self.assertIn("no new task, scope, consent", specific["additionalContext"])
            self.assertLess(len(specific["additionalContext"].encode()), 2048)
            json.loads(json.dumps(result))

    def test_broadcast_failure_keeps_gateway_frame_and_original_receipt_on_both_hosts(self):
        self.configure()
        for host in ("claude", "codex"):
            prompt = "Assess the recovery while the queue is unavailable on " + host
            with patch.object(Store, "broadcast_gateway_prompt", side_effect=RoomError("queue unavailable")):
                result = self.hook(prompt, host)
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Notify-all could not be queued: queue unavailable", context)
            self.assertEqual(context.count(PREFIX), 1)
            self.assertEqual(context.count(POSTFIX), 1)
            with self.store.read() as db:
                self.assertEqual(db.execute("SELECT body FROM prompts WHERE body=?", (prompt,)).fetchone()[0], prompt)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_worker_copy_and_receipt_keep_original_body_and_provenance(self):
        self.configure()
        original = "Compare two queue designs\nKeep the accepted task scope."
        self.hook(original)
        with self.store.read() as db:
            prompt = dict(db.execute("SELECT * FROM prompts WHERE body=?", (original,)).fetchone())
            before = db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        row = self.store.inbox("CODEX_01")["items"][-1]
        self.assertEqual(row["body"], original)
        self.assertEqual(row["context"]["admin_notice"]["receipt"], prompt["id"])
        self.assertEqual(row["context"]["admin_notice"]["provenance"], "unverified")
        rendered = message_text(row)
        self.assertEqual(rendered.count(PREFIX), 1)
        self.assertEqual(rendered.count(POSTFIX), 1)
        self.assertIn("[Admin text begins; stop at matching ID]\n" + original, rendered)
        self.assertIn("NOT admin consent", rendered)
        self.assertIn("send them to CLAUDE_01", rendered)
        self.assertIn("no response is needed", rendered)
        self.assertTrue(native_event_prompt(rendered))
        self.assertEqual(native_event_identity(rendered)["kind"], "admin notice")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], before)
            self.assertEqual(db.execute("SELECT body FROM prompts WHERE id=?", (prompt["id"],)).fetchone()[0], original)

    def test_controls_choices_and_exact_output_requests_are_unframed(self):
        self.configure()
        for text in ("$az", "az", "/az", "$ihav-agent-room:start", "Q1.a\nR1.a", "Q1.a Q2.b",
                     "Return only JSON", "Reply with the single word: OK", "chỉ một lệnh", "short", "summary", "chọn a", "chon b"):
            result = self.hook(text)
            self.assertNotIn(PREFIX, result["hookSpecificOutput"]["additionalContext"], text)
            for row in self.store.inbox("CODEX_01")["items"]:
                if row["body"] == text:
                    self.assertNotIn("prompt_frame", row["context"], text)
                    self.assertNotIn(PREFIX, message_text(row), text)

    def test_peer_or_system_input_does_not_create_frame_or_receipt(self):
        self.configure()
        for text in ("<cross-session-message sender='peer'>Read-only</cross-session-message>",
                     "[Agent Room system event M-system; NOT admin consent]\nData"):
            result = self.hook(text)
            self.assertNotIn(PREFIX, result["hookSpecificOutput"]["additionalContext"])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        peer = self.store.send("CODEX_01", "CLAUDE_01", "A peer design idea")
        self.assertNotIn(PREFIX, message_text(peer))

    def test_retry_retains_original_snapshot_and_does_not_double_frame(self):
        self.configure()
        self.store.broadcast_gateway_prompt("Assess the recovery", "same-key", receipt_id="P-first", provenance_state="unverified")
        self.configure(prefix="A different future prefix")
        self.store.broadcast_gateway_prompt("Assess the recovery", "same-key", receipt_id="P-retry", provenance_state="human")
        rows = self.store.inbox("CODEX_01")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context"]["admin_notice"]["receipt"], "P-first")
        self.assertEqual(rows[0]["context"]["admin_notice"]["provenance"], "unverified")
        self.assertEqual(message_text(rows[0]).count(PREFIX), 1)
        self.assertNotIn("A different future prefix", message_text(rows[0]))

    def test_frame_is_room_local_and_config_changes_do_not_rewrite_old_copies(self):
        self.configure()
        self.store.broadcast_gateway_prompt("First question", "old", receipt_id="P-old", provenance_state="unverified")
        old = self.store.inbox("CODEX_01")["items"][0]
        self.configure(enabled=False)
        self.store.broadcast_gateway_prompt("Next question", "next", receipt_id="P-next", provenance_state="unverified")
        rows = self.store.inbox("CODEX_01")["items"]
        self.assertEqual(message_text(rows[0]), message_text(old))
        self.assertNotIn(PREFIX, message_text(rows[1]))
        other = self.root / "other"
        other.mkdir()
        initialize(other, "pair")
        self.assertIsNone(load_frame(other))

    def test_bad_or_oversized_config_does_not_break_prompt_receipts(self):
        for text in ("not json", "[]", json.dumps({"schema": 1, "enabled": "yes", "prefix": PREFIX, "postfix": POSTFIX}),
                     json.dumps({"schema": 1, "enabled": True, "prefix": "x" * 4097, "postfix": POSTFIX}), " " * 16385):
            self.config_path.write_text(text, encoding="utf-8")
            self.assertIsNone(load_frame(self.project))
            result = self.hook("Keep this original prompt")
            self.assertIn("Admin prompt receipt", result["hookSpecificOutput"]["additionalContext"])
            self.assertNotIn(PREFIX, result["hookSpecificOutput"]["additionalContext"])

    def test_symlinked_config_is_ignored_without_cross_room_read(self):
        outside = self.root / "outside-frame.json"
        outside.write_text(json.dumps({"schema": 1, "enabled": True, "prefix": PREFIX, "postfix": POSTFIX}))
        self.config_path.symlink_to(outside)
        self.assertIsNone(load_frame(self.project))
        self.assertNotIn(PREFIX, self.hook("Assess the local design")["hookSpecificOutput"]["additionalContext"])

    def test_partial_retry_uses_the_existing_snapshot_before_target_iteration(self):
        self.configure()
        self.store.broadcast_gateway_prompt("Assess the recovery", "partial", receipt_id="P-first", provenance_state="unverified")
        with self.store.tx() as db:
            db.execute("DELETE FROM messages WHERE recipient='CODEX_01'")
        self.configure(prefix="Changed for future prompts")
        result = self.store.broadcast_gateway_prompt("Assess the recovery", "partial", receipt_id="P-retry", provenance_state="human")
        self.assertEqual(result["prompt_frame"]["prefix"], PREFIX)
        row = self.store.inbox("CODEX_01")["items"][0]
        self.assertEqual(row["context"]["prompt_frame"]["prefix"], PREFIX)
        self.assertNotIn("Changed for future prompts", message_text(row))

    def test_partial_retry_does_not_retroactively_frame_a_previously_unframed_prompt(self):
        self.store.broadcast_gateway_prompt("Assess the recovery", "legacy", receipt_id="P-first", provenance_state="unverified")
        with self.store.tx() as db:
            db.execute("DELETE FROM messages WHERE recipient='CODEX_01'")
        self.configure()
        result = self.store.broadcast_gateway_prompt("Assess the recovery", "legacy", receipt_id="P-retry", provenance_state="human")
        self.assertIsNone(result["prompt_frame"])
        row = self.store.inbox("CODEX_01")["items"][0]
        self.assertNotIn("prompt_frame", row["context"])
        self.assertNotIn(PREFIX, message_text(row))

    def test_inconsistent_existing_snapshots_fail_without_rewriting_history(self):
        self.configure()
        self.store.broadcast_gateway_prompt("Assess the recovery", "conflict", receipt_id="P-first", provenance_state="unverified")
        with self.store.tx() as db:
            row = db.execute("SELECT id,context FROM messages WHERE recipient='CLAUDE_EXPERT'").fetchone()
            context = json.loads(row["context"])
            context["prompt_frame"]["prefix"] = "Conflicting snapshot"
            db.execute("UPDATE messages SET context=? WHERE id=?", (json.dumps(context), row["id"]))
            before = [tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY seq")]
        with self.assertRaises(RoomError) as error:
            self.store.broadcast_gateway_prompt("Assess the recovery", "conflict", receipt_id="P-retry", provenance_state="human")
        self.assertEqual(error.exception.code, "conflict")
        with self.store.read() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY seq")], before)
