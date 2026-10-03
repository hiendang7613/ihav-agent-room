"""Status projections preserve current obligations while leaving details on demand."""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import PLUGIN_ROOT, dumps
from ihav_agent_room.evidence import source_matches
from test_evidence import EvidenceFixture
from receipts import human_receipt

os.environ.pop("CLAUDE_EFFORT", None)  # Hermetic: the host session effort must not leak into room state.


class CompactStatusTests(EvidenceFixture, unittest.TestCase):
    def test_compact_checks_current_source_without_rechecking_closed_reviews(self):
        closed = self.task()
        self.review(self.submit(closed))
        self.finish(closed)
        active = self.task()
        self.review(self.submit(active))
        self.source.write_text("value = 2\n")
        before = self.store.path.read_bytes()
        with patch("ihav_agent_room.store.source_matches", wraps=source_matches) as checked:
            compact = self.store.status(compact=True)
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(compact["task_counts"], {"done": 1, "review": 1})
        self.assertEqual([t["id"] for t in compact["tasks"]], [active["id"]])
        self.assertEqual(compact["tasks"][0]["review_status"]["state"], "stale")
        # Historical inspection still rechecks the source; no cached verdict is reused.
        with patch("ihav_agent_room.store.source_matches", wraps=source_matches) as checked:
            full = self.store.status()
        self.assertEqual(checked.call_count, 2)
        self.assertTrue(all(t["review_status"]["state"] == "stale" for t in full["tasks"]))
        self.assertEqual(compact["attention"], full["attention"])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_status_totals_include_taskless_work_and_progress_from_earlier_attempts(self):
        active = self.task(review_policy="none", reviewer=None)
        closed = self.task(review_policy="none", reviewer=None)
        self.store.update_task("CLAUDE_01", closed["id"], 1, {"state": "done", "evidence": ["Fixture complete"]})
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="fixture")
            self.store.put_room(db, room)
        attempts = []
        for task_id, state, progress in ((active["id"], "completed", "2099-01-02"),
                                         (active["id"], "unknown", "2099-01-01"),
                                         (closed["id"], "failed", None), (None, "accepted", None)):
            message = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Fixture exchange", task_id)
            attempt = self.store.begin_attempt(message, "fixture")
            self.store.finish_dispatch(attempt["id"], state, "Observed fixture", turn_id=attempt["id"])
            with self.store.tx() as db:
                attempt = self.store.entry(db, "attempts", attempt["id"])
                attempt["progress_at"] = progress
                self.store.save_attempt(db, attempt)
            attempts.append(attempt)
            if task_id is None or state == "completed":
                self.store.acknowledge("CODEX_EXPERT", message["id"], "Fixture processed")
        self.store.send("CLAUDE_01", "CODEX_EXPERT", "Queued without an attempt", active["id"])
        before = self.store.path.read_bytes()
        for compact in (False, True):
            result = self.store.status(compact=compact)
            self.assertEqual(result["message_counts"], {"processed": 2, "unknown": 1, "failed": 1, "queued": 11})
            self.assertEqual(result["attempt_counts"], {"completed": 1, "unknown": 1, "failed": 1, "accepted": 1})
            task = result["tasks"][0]
            self.assertEqual(task["id"], active["id"])
            self.assertEqual(task["latest_attempt"]["id"], attempts[1]["id"])
            self.assertEqual(task["last_progress"], "2099-01-02")
            self.assertEqual(task["unprocessed_messages"], 2,
                             "Only unprocessed direct work counts; FYI copies remain visible in message history")
            if not compact:
                self.assertEqual(result["tasks"][1]["latest_attempt"]["id"], attempts[2]["id"])
                self.assertEqual(result["tasks"][1]["unprocessed_messages"], 1)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_pending_inbox_rollup_is_read_only_and_keeps_transport_states_separate(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="inbox-rollup")
            self.store.put_room(db, room)

        self.store.send("CODEX_EXPERT", "CLAUDE_01", "A question for main")
        accepted = self.store.send("CLAUDE_01", "CODEX_EXPERT", "A finding to review")
        accepted_attempt = self.store.begin_attempt(accepted, "inbox-rollup")
        self.store.finish_dispatch(accepted_attempt["id"], "accepted", "Native submission", "turn-1")
        unknown = self.store.send("CLAUDE_01", "CODEX_01", "An uncertain delivery")
        unknown_attempt = self.store.begin_attempt(unknown, "inbox-rollup")
        self.store.finish_dispatch(unknown_attempt["id"], "unknown", "Transport outcome unknown")
        processed = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Already processed")
        self.store.acknowledge("CODEX_EXPERT", processed["id"], "Read and answered")

        before = self.store.path.read_bytes()
        full = self.store.status()
        compact = self.store.status(compact=True)
        expected = {
            "advisory": True,
            "by_member": {
                "CLAUDE_01": {"count": 1, "statuses": {"queued": 1}},
                "CODEX_01": {"count": 1, "statuses": {"unknown": 1}},
                "CODEX_EXPERT": {"count": 1, "statuses": {"accepted": 1}},
            },
            "read_command": "ihav-agent-room inbox --pending --after 0",
        }
        self.assertEqual(full["pending_inboxes"], expected)
        self.assertEqual(compact["pending_inboxes"], expected)
        self.assertEqual(full["message_counts"], {"processed": 1, "queued": 9, "accepted": 1, "unknown": 1})
        self.assertEqual(before, self.store.path.read_bytes())

    def test_compact_status_summarizes_all_incomplete_fanouts_and_links_to_full_details(self):
        self.store.send("CODEX_01", "CLAUDE_01", "First room update")
        self.store.send("CLAUDE_EXPERT", "CODEX_EXPERT", "Second room update")
        full = self.store.status()
        compact = self.store.status(compact=True)

        expected_by_member = {}
        for notice in full["incomplete_notifications"]:
            for member in notice["incomplete_members"]:
                expected_by_member[member] = expected_by_member.get(member, 0) + 1
        self.assertEqual(compact["incomplete_notification_count"], len(full["incomplete_notifications"]))
        self.assertEqual(compact["incomplete_notifications_by_member"], expected_by_member)
        self.assertEqual(compact["incomplete_notifications"], full["incomplete_notifications"][:1])
        self.assertTrue(compact["incomplete_notifications_truncated"])
        self.assertEqual(compact["detail"]["read_incomplete_notifications"], "ihav-agent-room status")

    def test_status_query_count_does_not_grow_for_each_unreviewed_task_or_note(self):
        def query_count(compact):
            queries = []
            connect = self.store.connect
            def traced_connect():
                db = connect()
                db.set_trace_callback(queries.append)
                return db
            with patch.object(self.store, "connect", traced_connect):
                self.store.status(compact=compact)
            return len(queries)

        initial = {compact: query_count(compact) for compact in (False, True)}
        for i in range(20):
            self.task(title=f"Task {i}", review_policy="none", reviewer=None)
            self.store.add_note("CODEX_EXPERT", {"kind": "proposal", "body": f"Idea {i}"})
        for compact in (False, True):
            self.assertLessEqual(query_count(compact), initial[compact])

    def test_all_open_tasks_survive_while_closed_history_is_counted_and_readable(self):
        active = [self.task(title=f"Work {i}", request="Long source rationale " * 300) for i in range(15)]
        closed = self.task(review_policy="none", reviewer=None)
        self.store.update_task("CLAUDE_01", closed["id"], 1, {"state": "done", "evidence": ["Checked"]})
        cancelled = self.task()
        self.store.update_task("CLAUDE_01", cancelled["id"], 1, {"state": "cancelled", "source": self.prompt})
        full = self.store.status()
        before = self.store.path.read_bytes()
        compact = self.store.status(compact=True)
        self.assertEqual({t["id"] for t in compact["tasks"]}, {t["id"] for t in active})
        self.assertEqual(compact["task_counts"], {"ready": 15, "done": 1, "cancelled": 1})
        self.assertEqual(compact["attention"], full["attention"])
        self.assertNotIn("Long source rationale", dumps(compact))
        for item in compact["tasks"]:
            self.assertIn(item["id"], item["read_command"])
        self.assertEqual(full, self.store.status())
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertIn("Long source rationale", self.store.task_context(active[0]["id"])["task"]["request"])
        self.assertLess(len(dumps(compact)), len(dumps(full)) // 2)

    def test_unknown_attempt_is_visible_without_replaying_large_output(self):
        task = self.task()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="fixture")
            self.store.put_room(db, room)
        message = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Investigate", task["id"])
        attempt = self.store.begin_attempt(message, "fixture")
        with self.store.tx() as db:
            attempt.update(state="unknown", outputs=[{"text": "large-result " * 500}])
            self.store.save_attempt(db, attempt)
        full = self.store.status()
        compact = self.store.status(compact=True)
        latest = compact["tasks"][0]["latest_attempt"]
        self.assertEqual(latest["state"], "unknown")
        self.assertEqual(latest["id"], attempt["id"])
        self.assertIn(attempt["id"], latest["read_command"])
        self.assertNotIn("large-result", dumps(compact))
        self.assertIn("large-result", dumps(full))
        self.assertEqual(compact["message_counts"], full["message_counts"])
        self.assertEqual(compact["attempt_counts"], full["attempt_counts"])
        self.assertEqual(compact["tasks"][0]["unprocessed_messages"], 1)

    def test_original_admin_text_approvals_claims_and_attention_are_never_previewed(self):
        task = self.task()
        self.store.claim("CLAUDE_01", task["id"], task["version"])
        human_receipt(self.store, "Keep this full original condition: " + "specific condition " * 400)
        with self.store.tx() as db:
            db.execute("INSERT INTO approvals VALUES (?,?)", ("A-fixture", dumps({"id": "A-fixture", "state": "pending", "params": {"command": "sensitive action " * 100}})))
        full, compact = self.store.status(), self.store.status(compact=True)
        for key in ("unaccounted_prompts", "approvals", "claims", "attention", "pending_inboxes", "room"):
            self.assertEqual(compact[key], full[key], key)
        model_fields = {"requested_model", "requested_effort", "model_label", "settings_application",
                        "observed_model", "observed_effort", "model_observation_source", "model_observed_at",
                        "effort_source", "effort_observed_at", "settings_pending_restart"}
        compact_members = [{key: value for key, value in member.items() if key not in model_fields}
                           for member in full["members"]]
        self.assertEqual(compact["members"], compact_members)
        self.assertEqual(compact["detail"]["read_models"], "ihav-agent-room status")

    def test_note_preview_and_stale_review_keep_current_identity_and_full_read_path(self):
        task = self.task()
        submission = self.submit(task)
        self.review(submission)
        note = self.store.add_note("CODEX_EXPERT", {"kind": "proposal", "body": "Tentative possibility " * 200})
        old = self.store.add_note("CODEX_EXPERT", {"kind": "proposal", "body": "Obsolete proposal"})
        self.store.resolve_note("CLAUDE_01", old["id"], 1, {"state": "rejected", "answer": "No longer relevant", "source": self.prompt})
        question = self.store.add_note("CODEX_EXPERT", {"kind": "question", "body": "Past question"})
        self.store.resolve_note("CLAUDE_01", question["id"], 1, {"state": "answered", "answer": "Recorded answer", "source": self.prompt})
        self.source.write_text("value = 2\n")
        compact = self.store.status(compact=True)
        self.assertEqual(compact["tasks"][0]["review_status"]["state"], "stale")
        self.assertEqual(compact["tasks"][0]["review_status"], self.store.status()["tasks"][0]["review_status"])
        self.assertEqual([n["id"] for n in compact["notes"]], [note["id"]])
        self.assertIn("truncated", compact["notes"][0]["body_preview"])
        self.assertIn(note["id"], compact["notes"][0]["read_command"])
        self.assertEqual(compact["note_counts"], {"open": 1, "rejected": 1, "answered": 1})

    def test_cli_compact_is_read_only_and_retains_unavailable_process_inspection(self):
        self.task()
        before = self.store.path.read_bytes()
        with patch("ihav_agent_room.cli.start_room", side_effect=AssertionError("Started native work")), \
                patch("ihav_agent_room.cli.process_alive", side_effect=PermissionError("inspection unavailable")):
            result = run(parser().parse_args(["--project", str(self.project), "status", "--compact"]))
        self.assertIsNone(result["supervisor_alive"])
        self.assertIn("unknown", result["process_inspection"])
        self.assertEqual(result["detail"]["mode"], "compact")
        self.assertEqual(before, self.store.path.read_bytes())
        command = [sys.executable, str(PLUGIN_ROOT / "bin/ihav-agent-room"), "--project", str(self.project), "--json", "status"]
        env = dict(os.environ, PATH="")
        for flags, compact in (([], False), (["--compact"], True)):
            response = subprocess.run(command + flags, env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(response.returncode, 0, response.stderr + response.stdout)
            data = json.loads(response.stdout)["data"]
            self.assertEqual("detail" in data, compact)
        self.assertEqual(before, self.store.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
