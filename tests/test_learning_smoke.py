"""Pilot grader controls; these synthetic peers never execute a provider."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import PLUGIN_ROOT
from ihav_agent_room.knowledge import Knowledge
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store
from scripts.learning_smoke import Deadline, PREFIX, delivery_settled, prompt, result


class LearningGraderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="learning grader ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        initialize(self.project)
        self.store = Store(self.project)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="fixture")
            self.store.put_room(db, room)
        self.knowledge = Knowledge(self.store)
        self.original = self.knowledge.write("CODEX_EXPERT", {
            "title": "Retry delay", "body": "Observed implicit unit is milliseconds",
            "evidence": ["Synthetic pilot outcomes"], "limits": "Only the observed protocol"})

    def test_delivery_settlement_requires_processing_for_work_but_not_fyi(self):
        for context in ({"broadcast": {"id": "M-copy"}}, {"admin_relay": True}):
            with self.subTest(context=context):
                for status in ("accepted", "submitted", "processed"):
                    self.assertTrue(delivery_settled(status, context))
                for status in ("queued", "failed", "unknown"):
                    self.assertFalse(delivery_settled(status, context))

        actionable = {}
        self.assertFalse(delivery_settled("accepted", actionable))
        self.assertTrue(delivery_settled("processed", actionable))

    def process(self, message, ack=True, complete=True):
        attempt = self.store.begin_attempt(message, "fixture")
        turn = message["id"] + "-turn" if message["recipient"] == "CODEX_EXPERT" else None
        self.store.finish_dispatch(attempt["id"], "accepted" if turn else "submitted", "Offline fixture", turn)
        if complete and turn:
            self.store.attempt_event("CODEX_EXPERT", "fixture", "turn/completed", {"turn": {"id": turn, "status": "completed"}})
        if ack:
            self.store.acknowledge(message["recipient"], message["id"], "Synthetic peer processed the fixture")
        return attempt

    def response(self, phase="reuse", record=None, ack=True, complete=True, **override):
        record = record or self.original
        request = self.store.send("CLAUDE_01", "CODEX_EXPERT", prompt(phase))
        self.process(request, complete=complete)
        value = {"phase": phase, "request": request["id"], "summary": "Synthetic finding with limited applicability",
                 "knowledge": [{"id": record["id"], "version": record["version"]}], "delay_seconds": 1.75}
        value.update(override)
        reply = self.store.send("CODEX_EXPERT", "CLAUDE_01", PREFIX + json.dumps(value))
        self.process(reply, ack=ack)
        return request, reply

    def test_real_ledger_grader_accepts_correlated_current_knowledge(self):
        request, reply = self.response()
        checked = result(self.store, "reuse", request["id"], self.original)
        self.assertEqual(checked["peer_message"], reply["id"])
        self.assertEqual(checked["knowledge"], self.original)

    def test_ack_is_required_in_addition_to_correct_result(self):
        request, _ = self.response(ack=False)
        self.assertIsNone(result(self.store, "reuse", request["id"], self.original))

    def test_ack_does_not_prove_native_turn_completion(self):
        request, _ = self.response(complete=False)
        self.assertIsNone(result(self.store, "reuse", request["id"], self.original))

    def test_completed_state_without_turn_identity_does_not_pass(self):
        request, _ = self.response()
        with self.store.tx() as db:
            attempt = json.loads(db.execute("SELECT data FROM attempts WHERE message=?", (request["id"],)).fetchone()[0])
            attempt["turn_id"] = None
            self.store.save_attempt(db, attempt)
        self.assertIsNone(result(self.store, "reuse", request["id"], self.original))

    def test_wrong_answer_fails_even_with_receipts_and_completion(self):
        request, _ = self.response(delay_seconds=1750)
        with self.assertRaisesRegex(RuntimeError, "Incorrect retry delay"):
            result(self.store, "reuse", request["id"], self.original)

    def test_stale_citation_fails(self):
        request, _ = self.response()
        self.knowledge.write("CODEX_EXPERT", {"limits": "New counterevidence"}, self.original["id"], 1)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            result(self.store, "reuse", request["id"], self.original)

    def test_revision_and_replacement_must_address_the_original_lesson(self):
        replacement = self.knowledge.write("CODEX_EXPERT", {
            "title": "New retry units", "body": "Honor explicit seconds", "evidence": ["Counterexample"]})
        request, _ = self.response("revise", record=replacement, delay_seconds=2)
        with self.assertRaisesRegex(RuntimeError, "original lesson"):
            result(self.store, "revise", request["id"], self.original)
        self.knowledge.write("CODEX_EXPERT", {"state": "retired", "limits": "Replaced with explicit-unit rule"}, self.original["id"], 1)
        self.assertEqual(result(self.store, "revise", request["id"], self.original)["knowledge"]["id"], replacement["id"])

    def test_duplicate_correlated_reply_fails(self):
        request, reply = self.response()
        self.store.send("CODEX_EXPERT", "CLAUDE_01", reply["body"])
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            result(self.store, "reuse", request["id"], self.original)

    def test_message_budget_is_checked_before_waiting_for_processing(self):
        request, _ = self.response(ack=False)
        for _ in range(5):
            self.store.send("CODEX_EXPERT", "CLAUDE_01", "Extra fixture discussion")
        with self.assertRaisesRegex(RuntimeError, "message budget"):
            result(self.store, "reuse", request["id"], self.original)


class LearningBudgetTests(unittest.TestCase):
    def test_either_elapsed_clock_exhausts_the_budget(self):
        for wall, monotonic, expired in ((99, 99, False), (100, 1, True), (-100, 100, True), (500, 2, True)):
            with self.subTest(wall=wall, monotonic=monotonic):
                with patch("scripts.learning_smoke.time.time", return_value=1000), patch("scripts.learning_smoke.time.monotonic", return_value=1000):
                    deadline = Deadline(100)
                with patch("scripts.learning_smoke.time.time", return_value=1000 + wall), patch("scripts.learning_smoke.time.monotonic", return_value=1000 + monotonic):
                    if expired:
                        with self.assertRaisesRegex(RuntimeError, "elapsed-time"):
                            deadline.check()
                    else:
                        deadline.check()

    def test_preview_is_effect_free(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "must not be created"
            check = subprocess.run([sys.executable, str(PLUGIN_ROOT / "scripts/native_smoke.py"),
                "--scenario", "learning", "--project", str(project)], capture_output=True, text=True, timeout=10)
            self.assertEqual(check.returncode, 0, check.stderr)
            scope = json.loads(check.stdout)
            self.assertFalse(scope["execute"])
            self.assertEqual(scope["proposed_scope"]["max_room_messages"], 18)
            self.assertEqual(scope["proposed_scope"]["max_persistent_members"], 4)
            self.assertFalse(project.exists())
