"""Room scheduling/recovery through real persistence, with native effects replaced."""

from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.native import message_text
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import MAX_REVIEW_PACKET_BYTES, Store
from receipts import human_receipt


class NativeRecorder:
    def __init__(self, *args):
        self.process = SimpleNamespace(pid=None)
        self.stamp = None
        self.thread_id = "fixture-thread"
        self.turn_id = None
        self.last_sent_turn_id = None
        self.permission_class = "prompting"
        self.sent = []

    async def start(self, native_id):
        self.thread_id = native_id or self.thread_id

    async def send(self, message):
        self.sent.append(message)
        self.turn_id = self.last_sent_turn_id = "fixture-turn"
        return "accepted"


class CollaborationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="room collaboration ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        initialize(self.project)
        self.store = Store(self.project)
        self.generation = "first-generation"
        self.set_room("running", self.generation)
        self.store.member("CLAUDE_01", {"status": "active", "native_id": "main"})
        self.store.member("CODEX_01", {"status": "stopped"})
        self.store.member("CLAUDE_EXPERT", {"status": "stopped"})
        self.store.member("CODEX_EXPERT", {"status": "idle", "native_id": "expert-thread"})
        self.prompt = human_receipt(self.store, "Implement work.py and have the expert review it")
        (self.project / "work.py").write_text("value = 1\n")

    def set_room(self, status, generation, mode="default"):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status=status, generation=generation, mode=mode)
            self.store.put_room(db, room)

    def current(self, task):
        with self.store.read() as db:
            return self.store.record(db, "tasks", task["id"])

    def messages(self):
        with self.store.read() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY seq")]
        return [row for row in rows if not json.loads(row["context"]).get("broadcast")]

    def logical_message_counts(self):
        counts = {}
        for message in self.messages():
            counts[message["status"]] = counts.get(message["status"], 0) + 1
        return counts

    @staticmethod
    def direct_client_messages(client):
        direct = []
        for message in client.sent:
            context = message["context"]
            context = json.loads(context) if isinstance(context, str) else context
            if not context.get("broadcast"):
                direct.append(message)
        return direct

    def submission(self, owner="CLAUDE_01", reviewer="CODEX_EXPERT"):
        task = self.store.create_task("CLAUDE_01", {
            "title": "Review work", "request": "Implement work.py", "acceptance": "Value is 1",
            "next": "Read source", "owner": owner, "source": self.prompt, "authority": "implementation",
            "scope": ["work.py"], "review_policy": "peer_required", "reviewer": reviewer})
        submitted = self.store.submit_task(owner, task["id"], task["version"], {
            "paths": ["work.py"], "summary": "Ready", "evidence": ["Value checked"]})
        return submitted["task"], submitted["submission"]

    def accept_queued(self):
        for message in self.messages():
            if message["status"] == "queued":
                attempt = self.store.begin_attempt(message, self.store.room()["generation"])
                self.store.finish_dispatch(attempt["id"], "accepted", "Fixture accepted input", "old-turn")

    async def resume(self, generation):
        self.set_room("starting", generation)
        self.store = Store(self.project)  # Reconstruct from the SQLite ledger.
        supervisor = Supervisor(self.store, generation)
        async def fake_start_claude(project, native_id, resume, env, log, **kwargs):
            return {"sessionId": native_id or "claude-fixture-session", "pid": None, "id": "claude-fixture-job"}

        async def fake_stop_claude_worker(*args, **kwargs):
            return None

        with patch("ihav_agent_room.runtime.CodexClient", NativeRecorder), \
                patch("ihav_agent_room.runtime.start_claude", fake_start_claude), \
                patch("ihav_agent_room.runtime.exact_claude", side_effect=RoomError("No fake session", "unavailable")), \
                patch("ihav_agent_room.runtime.stop_claude_worker", fake_stop_claude_worker), \
                patch.object(supervisor, "owner_alive", return_value=True):
            await supervisor.launch()
        return supervisor

    async def test_note_followups_notify_main_and_author_through_existing_dispatch(self):
        note = self.store.add_note("CODEX_EXPERT", {"kind": "question", "body": "Could a smaller probe settle this?"})
        supervisor = Supervisor(self.store, self.generation)
        client = supervisor.codex["CODEX_EXPERT"] = NativeRecorder()
        main_messages = []

        def send_claude(project, native_id, message, mode):
            main_messages.append(message)
            return "submitted"

        with patch("ihav_agent_room.runtime.send_claude", send_claude):
            await supervisor.dispatch()
            self.assertEqual(len(main_messages), 1)
            self.store.acknowledge("CLAUDE_01", main_messages[0]["id"], "Considered the question")
            self.store.resolve_note("CLAUDE_01", note["id"], 1,
                {"answer": "Check the counterexample in the existing fixture."})
            await supervisor.dispatch()
            self.assertEqual(len(client.sent), 1)
            self.assertIn(note["id"], client.sent[0]["body"])
            self.assertIn("v2", client.sent[0]["body"])
            self.assertIsNone(client.sent[0]["task"])
            self.assertEqual(self.logical_message_counts(), {"processed": 1, "accepted": 1})
            self.store.acknowledge("CODEX_EXPERT", client.sent[0]["id"], "Read the follow-up and fixture")
            self.store.resolve_note("CODEX_EXPERT", note["id"], 2,
                {"state": "answered", "answer": "The smaller probe distinguishes the hypotheses."})
            await supervisor.dispatch()
            self.assertEqual(len(main_messages), 2)
            self.assertIn("answered", main_messages[-1]["body"])
            await supervisor.dispatch()
        self.assertEqual((len(main_messages), len(client.sent)), (2, 1))
        self.assertEqual(self.store.status()["tasks"], [])
        self.assertEqual(self.logical_message_counts(), {"processed": 2, "submitted": 1})

    async def test_dispatch_attaches_current_compact_context_and_records_its_digest(self):
        task, submission = self.submission()
        supervisor = Supervisor(self.store, self.generation)
        client = NativeRecorder()
        supervisor.codex["CODEX_EXPERT"] = client
        await supervisor.dispatch()
        self.assertEqual(len(client.sent), 1)
        pack = client.sent[0]["context_pack"]
        self.assertEqual(pack["status"], "current")
        self.assertEqual(pack["submission"]["id"], submission["id"])
        self.assertEqual(pack["task"]["version"], self.current(task)["version"])
        attempts = self.store.attempts(task["id"])["items"]
        self.assertEqual(attempts[0]["context_digest"], pack["digest"])
        self.assertEqual(attempts[0]["state"], "accepted")

    async def test_review_packet_is_submission_bound_and_only_sent_to_assigned_reviewer(self):
        task, submission = self.submission()
        with self.store.read() as db:
            messages = [dict(row) for row in db.execute("SELECT * FROM messages WHERE task=? ORDER BY seq", (task["id"],))]
        self.assertEqual({message["recipient"] for message in messages}, {"CODEX_01", "CLAUDE_EXPERT", "CODEX_EXPERT"})
        for message in messages:
            context = json.loads(message["context"])
            self.assertEqual(context["review_submission"], submission["id"])
            self.assertEqual("broadcast" in context, message["recipient"] != "CODEX_EXPERT")

        packet = self.store.review_packet(task["id"], submission["id"])
        self.assertEqual(packet["status"], "current")
        self.assertEqual(packet["submission"]["source_digest"], submission["digest"])
        self.assertEqual(packet["submission"]["paths"], ["work.py"])
        self.assertEqual(packet["submission"]["author_evidence_preview"], ["Value checked"])
        self.assertEqual(packet["submission"]["evidence_count"], 1)
        self.assertFalse(packet["submission"]["author_evidence_preview_truncated"])
        self.assertEqual(packet["task"]["acceptance"], "Value is 1")
        self.assertNotIn("summary", packet["submission"], "Do not lead reviewers with the author's conclusion")
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()),
                             MAX_REVIEW_PACKET_BYTES)

        self.store.member("CODEX_01", {"status": "idle"})
        self.store.member("CLAUDE_EXPERT", {"status": "idle"})
        supervisor = Supervisor(self.store, self.generation)
        direct_reviewer = supervisor.codex["CODEX_EXPERT"] = NativeRecorder()
        copied_codex = supervisor.codex["CODEX_01"] = NativeRecorder()
        copied_claude = []
        with patch("ihav_agent_room.runtime.send_claude", lambda *args: copied_claude.append(args[2]) or "submitted"):
            await supervisor.dispatch()

        self.assertEqual(len(direct_reviewer.sent), 1)
        self.assertEqual(direct_reviewer.sent[0]["context_pack"]["submission"]["id"], submission["id"])
        review_text = message_text(direct_reviewer.sent[0])
        self.assertIn("evidence are author claims", review_text)
        self.assertIn("author_evidence_preview", review_text)
        self.assertEqual(len(copied_codex.sent), 1)
        self.assertNotIn("context_pack", copied_codex.sent[0])
        self.assertEqual(len(copied_claude), 1)
        self.assertNotIn("context_pack", copied_claude[0])

    async def test_direct_request_is_not_buried_behind_roomwide_fyi_backlog(self):
        self.store.member("CODEX_01", {"status": "stopped"})
        self.store.member("CLAUDE_EXPERT", {"status": "stopped"})
        for index in range(25):
            self.store.send("CLAUDE_01", "CODEX_01", f"Room FYI {index}")
        urgent = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Please review the release blocker")
        supervisor = Supervisor(self.store, self.generation)
        client = supervisor.codex["CODEX_EXPERT"] = NativeRecorder()

        await supervisor.dispatch()

        self.assertEqual(client.sent[0]["id"], urgent["id"])
        self.assertEqual([message["id"] for message in self.direct_client_messages(client)], [urgent["id"]])

    async def test_failed_and_unknown_delivery_flags_refresh_within_batch_and_clear_after_ack(self):
        for error, state in ((RoomError("Wrong active turn"), "failed"), (OSError("Socket closed"), "unknown")):
            with self.subTest(state=state):
                class RejectFirst(NativeRecorder):
                    async def send(self, message):
                        if not self.sent:
                            self.sent.append(message)
                            raise error
                        return await super().send(message)

                first = self.store.send("CLAUDE_01", "CODEX_EXPERT", "First question")
                second = self.store.send("CLAUDE_01", "CODEX_EXPERT", "A useful follow-up")
                supervisor = Supervisor(self.store, self.generation)
                client = supervisor.codex["CODEX_EXPERT"] = RejectFirst()
                with patch("ihav_agent_room.runtime.send_claude", return_value="submitted"):
                    await supervisor.dispatch()
                self.assertEqual([m["id"] for m in client.sent], [first["id"], second["id"]])
                self.assertFalse(client.sent[0]["pending_recovery"])
                self.assertTrue(client.sent[1]["pending_recovery"])
                text = message_text(client.sent[1])
                self.assertIn("Earlier delivery failed or is unknown", text)
                self.assertIn("inbox --pending from --after 0", text)
                self.assertIn("reconcile effects before related actions or retries", text)
                self.assertEqual(next(m for m in self.messages() if m["id"] == first["id"])["status"], state)
                self.store.acknowledge("CODEX_EXPERT", first["id"], "Inspected outcome; no effect replayed")
                third = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Another ordinary question")
                with patch("ihav_agent_room.runtime.send_claude", return_value="submitted"):
                    await supervisor.dispatch()
                self.assertEqual(client.sent[-1]["id"], third["id"])
                self.assertFalse(client.sent[-1]["pending_recovery"])
                self.assertNotIn("Earlier delivery failed or is unknown", message_text(client.sent[-1]))

    async def test_failure_notice_is_once_per_episode_and_main_failure_does_not_recurse(self):
        class RejectAll(NativeRecorder):
            async def send(self, message):
                self.sent.append(message)
                raise RoomError("Wrong active turn")

        supervisor = Supervisor(self.store, self.generation)
        supervisor.codex["CODEX_EXPERT"] = RejectAll()
        with patch("ihav_agent_room.runtime.send_claude", side_effect=RoomError("Main unavailable")):
            failed = self.store.send("CLAUDE_01", "CODEX_EXPERT", "First question")
            await supervisor.dispatch()
            notices = [m for m in self.messages() if m["recipient"] == "CLAUDE_01"]
            self.assertEqual(len(notices), 1)
            self.assertIn(failed["id"], notices[0]["body"])
            self.store.send("CLAUDE_01", "CODEX_EXPERT", "Another question")
            await supervisor.dispatch()
            self.assertEqual(len([m for m in self.messages() if m["recipient"] == "CLAUDE_01"]), 1)
            direct = self.store.send("CODEX_EXPERT", "CLAUDE_01", "A direct finding")
            await supervisor.dispatch()
            self.assertEqual(len(self.messages()), 4)
            self.assertEqual(next(m for m in self.messages() if m["id"] == direct["id"])["status"], "failed")

    async def test_other_recipient_failure_does_not_force_healthy_inbox_sweep(self):
        failed = self.store.send("CODEX_EXPERT", "CLAUDE_01", "A finding")
        attempt = self.store.begin_attempt(failed, self.generation)
        self.store.finish_dispatch(attempt["id"], "failed", "Main unavailable")
        healthy = self.store.send("CLAUDE_01", "CODEX_EXPERT", "A different question")
        supervisor = Supervisor(self.store, self.generation)
        client = supervisor.codex["CODEX_EXPERT"] = NativeRecorder()
        await supervisor.dispatch()
        self.assertEqual([m["id"] for m in client.sent], [healthy["id"]])
        self.assertFalse(client.sent[0]["pending_recovery"])

    async def test_blocked_and_inactive_backlogs_do_not_starve_eligible_member(self):
        # Inactive recipients are valid history but must not consume this mode's dispatch budget.
        for i in range(20):
            self.store.send("CLAUDE_01", "CODEX_01", f"Inactive message {i}")
        self.store.member("CODEX_EXPERT", {"status": "waiting_permission"})
        blocked = [self.store.send("CLAUDE_01", "CODEX_EXPERT", f"Blocked message {i}") for i in range(20)]
        healthy = self.store.send("CODEX_EXPERT", "CLAUDE_01", "A useful direct finding")
        supervisor = Supervisor(self.store, self.generation)
        delivered = []

        def send_claude(project, native_id, message, mode):
            delivered.append(message["id"])
            return "submitted"

        with patch("ihav_agent_room.runtime.send_claude", send_claude):
            await supervisor.dispatch()
        self.assertEqual(delivered, [healthy["id"]])
        self.assertEqual(self.logical_message_counts(), {"queued": 40, "submitted": 1})
        self.store.member("CODEX_EXPERT", {"status": "idle"})
        client = supervisor.codex["CODEX_EXPERT"] = NativeRecorder()
        await supervisor.dispatch()
        self.assertEqual([m["id"] for m in self.direct_client_messages(client)], [m["id"] for m in blocked])
        self.assertEqual(self.logical_message_counts(), {"queued": 20, "submitted": 1, "accepted": 20})

    async def test_dispatch_is_fair_and_fifo_with_two_busy_eligible_recipients(self):
        self.set_room("running", self.generation, "full")
        self.store.member("CODEX_01", {"status": "working"})
        self.store.member("CLAUDE_EXPERT", {"status": "stopped"})
        clients = {name: NativeRecorder() for name in ("CODEX_01", "CODEX_EXPERT")}
        queued = {name: [self.store.send("CLAUDE_01", name, f"Finding {i}") for i in range(25)] for name in clients}
        supervisor = Supervisor(self.store, self.generation)
        supervisor.codex = clients
        await supervisor.dispatch()
        self.assertEqual([len(client.sent) for client in clients.values()], [40, 40])
        for client in clients.values():
            first_batch_contexts = [json.loads(message["context"]) for message in client.sent]
            self.assertTrue(all("broadcast" not in context for context in first_batch_contexts[:20]))
            self.assertTrue(all("broadcast" in context for context in first_batch_contexts[20:40]))
        for _ in range(4):
            await supervisor.dispatch()
        for name, client in clients.items():
            self.assertEqual([m["id"] for m in self.direct_client_messages(client)], [m["id"] for m in queued[name]])
            self.assertEqual(len(client.sent), 50)
        self.assertEqual(len(self.store.attempts(limit=200)["items"]), 100)

    async def test_pending_reviewer_wakes_after_resume_without_replaying_old_attempt(self):
        task, submission = self.submission()
        old = self.messages()[0]
        self.accept_queued()
        self.store.send("CLAUDE_01", "CODEX_EXPERT", "Unrelated room discussion")
        await self.resume("second-generation")
        wakes = [m for m in self.messages() if m["task"] == task["id"] and m["status"] == "queued"]
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["recipient"], "CODEX_EXPERT")
        self.assertIn(submission["id"], wakes[0]["body"])
        self.assertNotEqual(wakes[0]["id"], old["id"])
        self.assertEqual(self.store.member("CODEX_EXPERT")["native_id"], "expert-thread")
        self.assertEqual(self.store.attempts(task["id"])["items"][0]["state"], "unknown")
        await self.resume("third-generation")
        self.assertEqual([m["id"] for m in self.messages() if m["task"] == task["id"] and m["status"] == "queued"], [wakes[0]["id"]])
        self.assertEqual(self.store.status()["tasks"][0]["review_status"]["state"], "pending")
        self.assertEqual(len(self.store.attempts(task["id"])["items"]), 1)

    async def test_current_queued_review_is_not_duplicated_but_old_submission_does_not_hide_new_one(self):
        task, first = self.submission()
        original = self.messages()[0]
        await self.resume("second-generation")
        self.assertEqual([m["id"] for m in self.messages()], [original["id"]])
        current = self.current(task)
        second = self.store.submit_task("CLAUDE_01", task["id"], current["version"], {
            "paths": ["work.py"], "summary": "New evidence", "evidence": ["Additional check"]})["submission"]
        latest = self.messages()[-1]
        attempt = self.store.begin_attempt(latest, "second-generation")
        self.store.finish_dispatch(attempt["id"], "accepted", "Fixture accepted latest input", "old-turn")
        await self.resume("third-generation")
        reminders = [m for m in self.messages() if m["status"] == "queued" and m["id"] != original["id"]]
        self.assertEqual(len(reminders), 1)
        self.assertIn(second["id"], reminders[0]["body"])
        self.assertNotIn(first["id"], reminders[0]["body"])

    async def test_only_current_pending_reviews_are_woken(self):
        for outcome in ("approve", "changes_requested", "blocked", "stale", "cancelled", "ready"):
            with self.subTest(outcome=outcome):
                task, submission = self.submission()
                self.accept_queued()
                if outcome in {"approve", "changes_requested", "blocked"}:
                    self.store.record_review("CODEX_EXPERT", submission["id"], {
                        "source_digest": submission["digest"], "verdict": outcome, "summary": "Fixture review",
                        "findings": [{"summary": "Needs another check", "severity": "medium"}] if outcome == "changes_requested" else [],
                        "evidence": ["Inspected source"]})
                elif outcome == "stale":
                    current = self.current(task)
                    self.store.update_task("CLAUDE_01", task["id"], current["version"], {
                        "acceptance": "A revised requirement", "source": self.prompt})
                else:
                    current = self.current(task)
                    self.store.update_task("CLAUDE_01", task["id"], current["version"], {
                        "state": outcome, "source": self.prompt, "checkpoint": "Fixture state transition"})
                await self.resume("after-" + outcome)
                self.assertEqual([m for m in self.messages() if m["task"] == task["id"] and m["recipient"] == "CODEX_EXPERT" and m["status"] == "queued"], [])

    async def test_main_can_also_resume_an_assigned_review(self):
        task, submission = self.submission(owner="CODEX_EXPERT", reviewer="CLAUDE_01")
        self.accept_queued()
        await self.resume("second-generation")
        wakes = [m for m in self.messages() if m["task"] == task["id"] and m["status"] == "queued"]
        self.assertEqual({m["recipient"] for m in wakes}, {"CLAUDE_01", "CODEX_EXPERT"})
        reviewer_wake = next(m for m in wakes if m["recipient"] == "CLAUDE_01")
        self.assertIn(submission["id"], reviewer_wake["body"])


if __name__ == "__main__":
    unittest.main()
