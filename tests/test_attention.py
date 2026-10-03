import unittest
from unittest.mock import patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import MEMBERS, dumps
from ihav_agent_room.store import Store
from test_evidence import EvidenceFixture


class AttentionTests(EvidenceFixture, unittest.TestCase):
    def view(self, task=None):
        data = self.store.task_context(task["id"]) if task else self.store.status()
        self.assertIn("attention", data)
        view = data["attention"]
        self.assertIs(view["advisory"], True)
        self.assertEqual(set(view["by_member"]), set(MEMBERS))
        return view["by_member"]

    def items(self, member, task=None):
        return self.view(task)[member]

    def only(self, member, task=None):
        items = self.items(member, task)
        self.assertEqual(len(items), 1)
        return items[0]

    def database_dump(self):
        with self.store.read() as db:
            return tuple(db.iterdump())

    def test_empty_room_and_conversation_do_not_invent_commitments(self):
        self.store.send("CLAUDE_01", "CODEX_EXPERT", "Could we try another approach?")
        self.store.add_note("CODEX_EXPERT", {"kind": "question", "body": "Should we investigate?"})
        self.store.add_note("CODEX_EXPERT", {"kind": "proposal", "body": "One tentative idea"})
        self.assertTrue(all(not items for items in self.view().values()))

    def test_cli_reads_preserve_ledger_files_and_native_lifecycle(self):
        task = self.task()
        submission = self.submit(task)
        self.review(submission)
        self.store.add_note("CODEX_EXPERT", {"kind": "proposal", "body": "Consider later", "tasks": [task["id"]]})
        self.store.project_views()
        before_db = self.database_dump()
        before_files = {str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        with patch("ihav_agent_room.cli.start_room", side_effect=AssertionError("Read started a room")), \
                patch("ihav_agent_room.cli.process_alive", return_value=False):
            status = run(parser().parse_args(["--project", str(self.project), "status"]))
            context = run(parser().parse_args(["--project", str(self.project), "task", "context", task["id"]]))
        self.assertIn("attention", status)
        self.assertEqual(status["attention"], context["attention"])
        self.assertEqual(self.database_dump(), before_db)
        self.assertEqual({str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}, before_files)

    def test_pending_review_targets_peer_then_source_drift_returns_to_owner(self):
        task = self.task()
        first = self.submit(task)
        pending = self.only("CODEX_EXPERT")
        self.assertEqual(pending["reason"], "pending_review")
        self.assertEqual(pending["episode"], first["id"])
        self.assertEqual(pending["review"]["source_digest"], first["digest"])
        self.assertEqual(self.items("CLAUDE_01"), [])
        message = self.store.inbox("CODEX_EXPERT")["items"][0]
        self.store.acknowledge("CODEX_EXPERT", message["id"], "Read the request; review remains pending")
        current = self.current(task)
        self.store.update_task("CLAUDE_01", task["id"], current["version"], {"checkpoint": "Waiting for the peer review"})
        self.assertEqual(self.only("CODEX_EXPERT")["episode"], first["id"])
        self.assertEqual(self.only("CODEX_EXPERT")["reason"], "pending_review")
        self.source.write_text("value = 2\n")
        stale = self.only("CLAUDE_01", task)
        self.assertEqual(stale["reason"], "review_stale")
        self.assertEqual(stale["review"]["state"], "stale")
        self.assertEqual(self.items("CODEX_EXPERT", task), [])
        second = self.submit(task)
        current = self.only("CODEX_EXPERT")
        self.assertEqual(current["episode"], second["id"])
        self.assertNotEqual(current["episode"], first["id"])
        self.assertEqual(self.items("CLAUDE_01"), [])

    def test_current_review_findings_and_blockers_target_author(self):
        for verdict, reason in (("changes_requested", "review_changes_requested"), ("blocked", "review_blocked")):
            with self.subTest(verdict=verdict):
                task = self.task()
                submission = self.submit(task)
                receipt = self.review(submission, verdict=verdict, findings=[{"severity": "medium", "summary": "Need a missing acceptance check"}])
                item = self.only("CLAUDE_01", task)
                self.assertEqual(item["reason"], reason)
                self.assertEqual(item["review"]["receipt"], receipt["id"])
                self.assertIn("ihav-agent-room review show " + receipt["id"], item["read_commands"])
                self.assertEqual(self.items("CODEX_EXPERT", task), [])

    def test_peer_approval_points_to_main_for_completion_and_done_disappears(self):
        task = self.task(owner="CODEX_EXPERT", reviewer="CLAUDE_01")
        submission = self.submit(task)
        self.store.record_review("CLAUDE_01", submission["id"], {
            "source_digest": submission["digest"], "verdict": "approve", "summary": "Verified",
            "findings": [], "evidence": ["Read current work.py"],
        })
        self.assertEqual(self.only("CLAUDE_01")["reason"], "review_approved")
        self.assertEqual(self.items("CODEX_EXPERT"), [])
        self.finish(task)
        self.assertTrue(all(not items for items in self.view().values()))
        self.assertTrue(all(not items for items in self.view(task).values()))

    def test_task_identity_survives_progress_and_moves_with_assignment(self):
        task = self.task(review_policy="none", reviewer=None)
        original = self.only("CLAUDE_01")
        self.assertEqual(original["reason"], "unfinished_task")
        self.assertEqual(original["episode"], "contract:1")
        self.store.checkpoint("CLAUDE_01", task["id"], task["version"], {
            "summary": "Read source", "last_safe_action": "Read only", "next": "Continue when ready",
            "unknown_effects": [], "paths": ["work.py"],
        })
        self.assertEqual(self.only("CLAUDE_01")["episode"], original["episode"])
        current = self.current(task)
        reassigned = self.store.update_task("CLAUDE_01", task["id"], current["version"], {
            "owner": "CODEX_EXPERT", "source": self.prompt,
        })
        self.store = Store(self.project)
        self.assertEqual(self.items("CLAUDE_01"), [])
        self.assertEqual(self.only("CODEX_EXPERT")["episode"], "contract:2")
        self.store.update_task("CLAUDE_01", task["id"], reassigned["version"], {
            "state": "cancelled", "source": self.prompt,
        })
        self.assertTrue(all(not items for items in self.view().values()))

    def test_review_without_peer_policy_keeps_completion_with_owner(self):
        task = self.task(owner="CODEX_EXPERT", review_policy="none", reviewer=None)
        self.submit(task)
        self.assertEqual(self.only("CODEX_EXPERT")["reason"], "unfinished_task")
        self.assertEqual(self.items("CLAUDE_01"), [])

    def test_live_dependency_and_decision_blockers_clear_without_task_mutation(self):
        dependency = self.task(review_policy="none", reviewer=None)
        task = self.task(dependencies=[dependency["id"], dependency["id"]])
        note = self.store.add_note("CLAUDE_01", {
            "kind": "decision", "body": "Use the checked fixture", "tasks": [task["id"]],
            "condition": "Fixture checked", "source": self.prompt,
        })
        item = self.only("CLAUDE_01", task)
        self.assertEqual([(b["kind"], b["id"]) for b in item["blockers"]], [
            ("dependency", dependency["id"]), ("decision", note["id"]),
        ])
        self.assertEqual(item["blockers"][0]["state"], "ready")
        self.assertEqual(item["blockers"][1]["reason"], "condition_pending")
        self.store.update_task("CLAUDE_01", dependency["id"], 1, {"state": "done", "evidence": ["Checked"]})
        self.store.resolve_note("CLAUDE_01", note["id"], 1, {
            "state": "approved", "answer": "Verified", "source": self.prompt, "condition_evidence": "Fixture passed",
        })
        before = self.database_dump()
        self.assertEqual(self.only("CLAUDE_01", task)["blockers"], [])
        self.assertEqual(self.database_dump(), before)

    def test_explicit_blocked_reason_does_not_linger_when_ready(self):
        task = self.task()
        changed = self.store.update_task("CLAUDE_01", task["id"], 1, {
            "state": "blocked", "blocked_reason": "Waiting for the sample file",
        })
        item = self.only("CLAUDE_01")
        self.assertEqual(item["reason"], "blocked_task")
        self.assertEqual(item["blockers"][0]["reason"], "Waiting for the sample file")
        self.store.update_task("CLAUDE_01", task["id"], changed["version"], {"state": "ready"})
        self.assertEqual(self.only("CLAUDE_01")["blockers"], [])

    def test_legacy_binding_is_visible_and_new_advisory_proposal_is_not_a_blocker(self):
        task = self.task()
        note = self.store.add_note("CODEX_EXPERT", {"kind": "proposal", "body": "Optional idea", "tasks": [task["id"]]})
        self.assertEqual(self.only("CLAUDE_01")["blockers"], [])
        # Reproduce an open proposal binding persisted by <=0.2.1.
        with self.store.tx() as db:
            current = self.store.record(db, "tasks", task["id"])
            current["decisions"] = {note["id"]: note["version"]}
            current["contract_revision"] += 1
            self.store.save(db, "tasks", current, current["version"])
        blocker = self.only("CLAUDE_01")["blockers"][0]
        self.assertEqual((blocker["id"], blocker["reason"]), (note["id"], "decision_unapproved"))
        self.store.resolve_note("CLAUDE_01", note["id"], 1, {
            "state": "superseded", "answer": "Retire the legacy binding", "source": self.prompt,
        })
        self.assertEqual(self.only("CLAUDE_01")["blockers"], [])

    def test_context_fallback_retains_bounded_attention_and_record_pointers(self):
        task = self.task(request="Detailed context " * 1000, title="Large title " * 1000)
        for i in range(20):
            self.store.add_note("CLAUDE_01", {
                "kind": "decision", "body": "Decision " * 1000, "condition": "Long condition " * 1000,
                "tasks": [task["id"]], "source": self.prompt,
            })
        pack = self.store.task_context(task["id"])
        self.assertIn("attention", pack)
        self.assertTrue(pack["truncated"])
        self.assertLess(len(dumps(pack)), 13000)
        self.assertIn("ihav-agent-room task show " + task["id"], pack["full_record_commands"])
        self.assertIn("ihav-agent-room task context " + task["id"], pack["attention"]["by_member"]["CLAUDE_01"][0]["read_commands"])
