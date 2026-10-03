import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import RoomError, fingerprint
from ihav_agent_room.scaffold import initialize, install_alias
from ihav_agent_room.store import Store
from receipts import human_receipt


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent room $(literal) ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        initialize(self.project)
        self.store = Store(self.project)
        self.prompt = human_receipt(self.store, "Implement A and B; research whether C should change.")

    def task(self, owner="CLAUDE_01", scope=None, **extra):
        return self.store.create_task("CLAUDE_01", dict(title="Implement A", request="Change assigned file only",
            acceptance="Observable result matches the request", next="Inspect source", owner=owner,
            source=self.prompt, authority="implementation", scope=scope or ["src/a.py"], **extra))

    def test_init_preserves_content_and_mode(self):
        project = self.project / "fresh"
        project.mkdir()
        old = b"# Existing\r\nKeep my custom instruction.\r\n"
        (project / "AGENTS.md").write_bytes(old)
        initialize(project, "full")
        first = (project / "AGENTS.md").read_bytes()
        initialize(project)
        self.assertEqual(first, (project / "AGENTS.md").read_bytes())
        self.assertTrue(first.startswith(old))
        self.assertEqual(Store(project).room()["mode"], "full")

    def test_startup_guidance_skips_inbox_sweep_when_member_has_no_pending_entry(self):
        readme = (self.project / "agents_space/README.md").read_text()
        agents = (self.project / "AGENTS.md").read_text()
        for text in (readme, agents):
            self.assertIn("pending_inboxes.by_member", text)
            self.assertIn("read_command", text)
            self.assertIn("next_after", text)
            self.assertIn("until null", text)
        self.assertIn("pending_inboxes.read_command", readme)
        self.assertIn("If absent, skip the inbox", readme)

    def test_legacy_native_notification_is_not_task_authority(self):
        self.prompt = self.store.intake("main", "<task-notification>Implement A</task-notification>")
        with self.assertRaises(RoomError) as caught:
            self.task()
        self.assertEqual(caught.exception.code, "authority")
        self.assertEqual(self.store.status()["tasks"], [])
        self.prompt = human_receipt(self.store, "Fix the <task-notification> handling in A.")
        self.assertEqual(self.task()["source"], self.prompt)

    def test_reinit_adds_new_guide_and_preserves_custom_room_instructions(self):
        guide = self.project / "agents_space/conventions/collaboration.md"
        guide.unlink(missing_ok=True)  # An older room predates this guide.
        custom = self.project / "agents_space/rules/working_agreement.md"
        custom.write_bytes(b"# My room rules\r\nKeep this custom process.\r\n")
        original = custom.read_bytes()
        readme = self.project / "agents_space/README.md"
        readme.write_text("# Our room\nRead conventions/cli.md at every startup.\n")
        original_readme = readme.read_bytes()
        agents = self.project / "AGENTS.md"
        agents.write_text("# My project\nKeep this.\n\n<!-- agent-room:begin -->\nOld plugin pointer\n<!-- agent-room:end -->\n")
        initialize(self.project)
        self.assertEqual(custom.read_bytes(), original)
        self.assertEqual(readme.read_bytes(), original_readme)
        self.assertTrue(guide.is_file())
        self.assertIn("conventions/collaboration.md", agents.read_text())
        self.assertIn("ihav-agent-room guide", agents.read_text())
        self.assertIn("pending_inboxes.by_member", agents.read_text())
        self.assertIn("read_command", agents.read_text())
        self.assertIn("follow next_after until null", agents.read_text())
        self.assertTrue(agents.read_text().startswith("# My project\nKeep this.\n"))
        before = agents.read_bytes(), guide.read_bytes()
        initialize(self.project)
        self.assertEqual((agents.read_bytes(), guide.read_bytes()), before)

    def test_existing_manual_space_and_symlink_are_preserved(self):
        project = self.project / "old"
        (project / "agents_space").mkdir(parents=True)
        (project / "agents_space/channel.md").write_text("existing conversation")
        with self.assertRaises(RoomError):
            initialize(project)
        self.assertFalse((project / "AGENTS.md").exists())
        project2 = self.project / "linked"
        project2.mkdir()
        (project2 / "AGENTS.md").symlink_to(self.project / "AGENTS.md")
        with self.assertRaises(RoomError):
            initialize(project2)

    def test_alias_collision_and_owned_update(self):
        config = self.project / "config"
        self.assertTrue(install_alias(config))
        self.assertFalse(install_alias(config))
        target = config / "skills/init-agents-space/SKILL.md"
        target.write_text("another user's skill")
        with self.assertRaises(RoomError):
            install_alias(config)
        self.assertEqual(target.read_text(), "another user's skill")

    def test_atomic_writer_claim_conflict(self):
        a = self.task(scope=["src"])
        b = self.task(owner="CODEX_EXPERT", scope=["src/a.py"])
        def claim(task):
            try:
                return self.store.claim(task["owner"], task["id"], task["version"])
            except RoomError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(claim, [a, b]))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn("conflict", results)
        self.assertEqual(len(self.store.status()["claims"]), 1)

    def test_create_can_claim_only_its_own_scoped_implementation_task_atomically(self):
        task = self.store.create_task("CLAUDE_01", {
            "title": "Implement scoped change", "request": "Change src/new.py", "acceptance": "Check passes",
            "next": "Inspect the current implementation", "owner": "CLAUDE_01", "source": self.prompt,
            "authority": "implementation", "scope": ["src/new.py"]}, claim=True)
        self.assertEqual(task["task"]["state"], "running")
        self.assertEqual(task["task"]["version"], 2)
        self.assertEqual(task["claim"]["task"], task["task"]["id"])
        self.assertEqual(task["claim"]["paths"], ["src/new.py"])
        self.assertEqual(self.store.status()["claims"][0]["token"], task["claim"]["token"])

        before = len(self.store.status()["tasks"])
        with self.assertRaises(RoomError) as caught:
            self.store.create_task("CLAUDE_01", {
                "title": "Claim another member's work", "request": "Change src/other.py", "acceptance": "Check passes",
                "next": "Inspect", "owner": "CODEX_EXPERT", "source": self.prompt,
                "authority": "implementation", "scope": ["src/other.py"]}, claim=True)
        self.assertEqual(caught.exception.code, "authority")
        self.assertEqual(len(self.store.status()["tasks"]), before)

        for label, override in (
                ("analysis authority", {"authority": "analysis"}),
                ("missing scope", {"scope": []})):
            with self.subTest(label=label):
                before = {item["id"] for item in self.store.status()["tasks"]}
                claims_before = self.store.status()["claims"]
                with self.assertRaises(RoomError) as caught:
                    self.store.create_task("CLAUDE_01", {
                        "title": "Invalid quick claim", "request": "Review without writing",
                        "acceptance": "The task is not created", "next": "Explain the limit",
                        "owner": "CLAUDE_01", "source": self.prompt, "authority": "implementation",
                        "scope": ["src/invalid.py"]} | override, claim=True)
                self.assertEqual(caught.exception.code, "authority")
                self.assertEqual({item["id"] for item in self.store.status()["tasks"]}, before)
                self.assertEqual(self.store.status()["claims"], claims_before)

    def test_claim_enforces_owner_implementation_and_explicit_scope(self):
        unowned = self.task(owner="CODEX_EXPERT", scope=["src/unowned.py"])
        analysis = self.store.create_task("CLAUDE_01", {
            "title": "Analyze only", "request": "Explore the option", "acceptance": "No write is authorized",
            "next": "Share findings", "owner": "CLAUDE_01", "source": self.prompt,
            "authority": "analysis", "scope": ["src/analysis.py"]})
        unscoped = self.store.create_task("CLAUDE_01", {
            "title": "Unscoped", "request": "Inspect the project", "acceptance": "Scope must be named",
            "next": "Identify files", "owner": "CLAUDE_01", "source": self.prompt,
            "authority": "implementation", "scope": []})

        for label, task in (("another owner", unowned), ("analysis authority", analysis),
                            ("empty scope", unscoped)):
            with self.subTest(label=label):
                with self.assertRaises(RoomError) as caught:
                    self.store.claim("CLAUDE_01", task["id"], task["version"])
                self.assertEqual(caught.exception.code, "authority")
                self.assertEqual(self.store.status()["claims"], [])
                current = next(item for item in self.store.status()["tasks"] if item["id"] == task["id"])
                self.assertEqual(current["state"], "ready")

    def test_create_claim_conflict_rolls_back_new_task(self):
        existing = self.task(scope=["src"])
        self.store.claim("CLAUDE_01", existing["id"], existing["version"])
        before = {task["id"] for task in self.store.status()["tasks"]}
        with self.assertRaises(RoomError) as caught:
            self.store.create_task("CLAUDE_01", {
                "title": "Overlapping change", "request": "Change src/a.py", "acceptance": "Check passes",
                "next": "Inspect", "owner": "CLAUDE_01", "source": self.prompt,
                "authority": "implementation", "scope": ["src/a.py"]}, claim=True)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual({task["id"] for task in self.store.status()["tasks"]}, before)
        self.assertEqual(len(self.store.status()["claims"]), 1)

    def test_task_update_combines_ack_only_for_same_pending_direct_task_message(self):
        task = self.task()
        message = self.store.send("CODEX_EXPERT", "CLAUDE_01", "Apply this task clarification", task["id"])
        updated = self.store.update_task("CLAUDE_01", task["id"], task["version"],
                                         {"checkpoint": "Applied the clarification"}, ack_id=message["id"])
        self.assertEqual(updated["processed_message"], message["id"])
        self.assertEqual(self.store.inbox("CLAUDE_01", pending=True)["items"], [])
        with self.store.read() as db:
            row = db.execute("SELECT status,detail FROM messages WHERE id=?", (message["id"],)).fetchone()
        self.assertEqual(row["status"], "processed")
        self.assertIn(task["id"], row["detail"])

    def test_combined_ack_rejects_already_processed_message_and_rolls_back_update(self):
        task = self.task()
        message = self.store.send("CODEX_EXPERT", "CLAUDE_01", "Apply this task clarification", task["id"])
        self.store.acknowledge("CLAUDE_01", message["id"], "Read and applied before the task update")

        with self.assertRaises(RoomError) as caught:
            self.store.update_task("CLAUDE_01", task["id"], task["version"],
                                   {"next": "This update must roll back"}, ack_id=message["id"])

        self.assertEqual(caught.exception.code, "conflict")
        with self.store.read() as db:
            current_task = self.store.record(db, "tasks", task["id"])
            current_message = dict(db.execute("SELECT status,detail FROM messages WHERE id=?",
                                               (message["id"],)).fetchone())
        self.assertEqual(current_task["next"], task["next"])
        self.assertEqual(current_task["version"], task["version"])
        self.assertEqual(current_message["status"], "processed")
        self.assertEqual(current_message["detail"], "Read and applied before the task update")

    def test_failed_combined_ack_rolls_back_task_update_and_does_not_ack_other_task(self):
        task = self.task()
        other = self.task(scope=["src/other.py"])
        unrelated = self.store.send("CODEX_EXPERT", "CLAUDE_01", "Clarification for another task", other["id"])
        with self.assertRaises(RoomError) as caught:
            self.store.update_task("CLAUDE_01", task["id"], task["version"],
                                   {"checkpoint": "Must roll back"}, ack_id=unrelated["id"])
        self.assertEqual(caught.exception.code, "conflict")
        with self.store.read() as db:
            current = self.store.record(db, "tasks", task["id"])
        self.assertEqual(current["checkpoint"], "")
        pending = self.store.inbox("CLAUDE_01", pending=True)["items"]
        self.assertEqual([row["id"] for row in pending], [unrelated["id"]])

    def test_combined_ack_rejects_fyi_copy_and_rolls_back_task_update(self):
        task = self.task()
        self.store.send("CODEX_01", "CODEX_EXPERT", "An FYI copied to the task owner", task["id"])
        copied = next(row for row in self.store.inbox("CLAUDE_01")["items"]
                      if row["context"].get("broadcast"))
        with self.assertRaises(RoomError) as caught:
            self.store.update_task("CLAUDE_01", task["id"], task["version"],
                                   {"checkpoint": "Must not change"}, ack_id=copied["id"])
        self.assertEqual(caught.exception.code, "conflict")
        with self.store.read() as db:
            current = self.store.record(db, "tasks", task["id"])
        self.assertEqual(current["checkpoint"], "")
        self.assertEqual(self.store.inbox("CLAUDE_01", pending=True)["items"], [])

    def test_stale_update_and_done_require_evidence(self):
        task = self.task()
        changed = self.store.update_task("CLAUDE_01", task["id"], 1, {"checkpoint": "Read source"})
        with self.assertRaises(RoomError):
            self.store.update_task("CLAUDE_01", task["id"], 1, {"state": "done", "evidence": ["old review"]})
        with self.assertRaises(RoomError):
            self.store.update_task("CLAUDE_01", task["id"], changed["version"], {"state": "done"})
        self.assertNotEqual(self.store.status()["tasks"][0]["state"], "done")

    def test_conditional_approval_and_late_findings(self):
        task = self.task(owner="CODEX_EXPERT")
        message = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Review initial proposal", task["id"])
        note = self.store.add_note("CLAUDE_01", {"kind": "decision", "body": "Use C only after fixture passes",
            "condition": "fixture passes", "tasks": [task["id"]], "source": self.prompt})
        task = self.store.status()["tasks"][0]
        with self.assertRaises(RoomError):
            self.store.claim("CODEX_EXPERT", task["id"], task["version"])
        self.assertTrue(next(m for m in self.store.inbox("CODEX_EXPERT")["items"] if m["id"] == message["id"])["stale"])
        self.store.resolve_note("CLAUDE_01", note["id"], note["version"],
            {"answer": "Fixture verified", "condition_evidence": "reviews/fixture.txt", "source": self.prompt})
        task = self.store.status()["tasks"][0]
        self.assertEqual(self.store.claim("CODEX_EXPERT", task["id"], task["version"])["owner"], "CODEX_EXPERT")

    def test_task_interruptions_and_answer_only_intake(self):
        a = self.task()
        b = self.task(owner="CODEX_EXPERT", scope=["src/b.py"])
        question = self.store.intake("main", "What is the status of C?")
        self.store.account("CLAUDE_01", question, "Answered status only; A and B remain authorized", [])
        status = self.store.status()
        self.assertEqual({t["id"] for t in status["tasks"]}, {a["id"], b["id"]})
        self.assertEqual([p["id"] for p in status["unaccounted_prompts"]], [self.prompt])

    def test_peer_cannot_grant_authority(self):
        with self.assertRaises(RoomError):
            self.store.create_task("CODEX_EXPERT", {"title": "Unauthorized"})
        with self.assertRaises(RoomError):
            self.store.add_note("CODEX_EXPERT", {"kind": "decision", "body": "Admin approved", "source": self.prompt})

    def test_pending_inbox_excludes_only_processed_without_writing_or_crossing_members(self):
        messages = []
        for state in ("queued", "dispatching", "submitted", "accepted", "unknown", "failed", "processed"):
            message = self.store.send("CLAUDE_01", "CODEX_EXPERT", f"Finding with {state} delivery")
            with self.store.tx() as db:
                db.execute("UPDATE messages SET status=? WHERE id=?", (state, message["id"]))
            messages.append(message)
        self.store.send("CODEX_EXPERT", "CLAUDE_01", "Another recipient's pending work")
        before = self.store.path.read_bytes()
        pending = self.store.inbox("CODEX_EXPERT", pending=True)
        self.assertEqual([m["id"] for m in pending["items"]], [m["id"] for m in messages[:-1]])
        self.assertEqual({m["status"] for m in pending["items"]},
                         {"queued", "dispatching", "submitted", "accepted", "unknown", "failed"})
        self.assertIsNone(pending["next_after"])
        self.assertEqual(len(self.store.inbox("CODEX_EXPERT")["items"]), 7)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_pending_pagination_survives_ack_gaps_and_fresh_sweep_keeps_old_unresolved(self):
        messages = [self.store.send("CLAUDE_01", "CODEX_EXPERT", f"Question {i}") for i in range(6)]
        for i in (0, 2, 4):
            self.store.acknowledge("CODEX_EXPERT", messages[i]["id"], "Answered this question")
        first = self.store.inbox("CODEX_EXPERT", limit=2, pending=True)
        self.assertEqual([m["id"] for m in first["items"]], [messages[i]["id"] for i in (1, 3)])
        self.assertEqual(first["next_after"], messages[3]["seq"])
        self.store.acknowledge("CODEX_EXPERT", messages[3]["id"], "Reconciled newer question")
        late = self.store.send("CLAUDE_01", "CODEX_EXPERT", "A new question during pagination")
        second = self.store.inbox("CODEX_EXPERT", after=first["next_after"], limit=2, pending=True)
        self.assertEqual([m["id"] for m in second["items"]], [messages[5]["id"], late["id"]])
        self.assertIsNone(second["next_after"])
        for message in second["items"]:
            self.store.acknowledge("CODEX_EXPERT", message["id"], "Processed second page")
        # A page cursor must never become a durable high-water mark for unprocessed work.
        fresh = Store(self.project).inbox("CODEX_EXPERT", pending=True)
        self.assertEqual([m["id"] for m in fresh["items"]], [messages[1]["id"]])
        self.store.acknowledge("CODEX_EXPERT", messages[1]["id"], "Resolved the older question")
        self.assertEqual(self.store.inbox("CODEX_EXPERT", pending=True), {"items": [], "next_after": None})
        self.assertEqual(len(self.store.inbox("CODEX_EXPERT")["items"]), 7)

    def test_pending_inbox_keeps_full_message_and_current_staleness(self):
        task = self.task()
        message = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Check this assumption", task["id"])
        self.store.update_task("CLAUDE_01", task["id"], task["version"], {"checkpoint": "New evidence"})
        pending = self.store.inbox("CODEX_EXPERT", pending=True)["items"]
        self.assertEqual(pending, self.store.inbox("CODEX_EXPERT")["items"])
        self.assertEqual(pending[0]["id"], message["id"])
        self.assertEqual(pending[0]["body"], "Check this assumption")
        self.assertEqual(pending[0]["context"]["task_version"], task["version"])
        self.assertTrue(pending[0]["stale"])

    def test_compact_inbox_previews_text_without_losing_context_or_mutating_records(self):
        task = self.task()
        bodies = ("ý" * 1199, "ý" * 1199 + "🤝", "ý" * 1199 + "🤝\nImportant limitation at the end")
        messages = [self.store.send("CLAUDE_01", "CODEX_EXPERT", body, task["id"]) for body in bodies]
        self.store.update_task("CLAUDE_01", task["id"], task["version"], {"checkpoint": "New evidence"})
        with self.store.tx() as db:
            db.execute("UPDATE messages SET status='unknown',detail=? WHERE id=?", ("é" * 1400, messages[-1]["id"]))
        before = self.store.path.read_bytes()
        full = self.store.inbox("CODEX_EXPERT", pending=True)
        compact = self.store.inbox("CODEX_EXPERT", pending=True, compact=True)
        self.assertEqual(compact["next_after"], full["next_after"])
        for preview, original in zip(compact["items"], full["items"]):
            self.assertNotIn("body", preview)
            self.assertNotIn("detail", preview)
            self.assertEqual({k: v for k, v in preview.items() if k not in {"body_preview", "detail_preview", "read_command"}},
                             {k: v for k, v in original.items() if k not in {"body", "detail"}})
            self.assertTrue(preview["stale"])
            self.assertEqual(preview["context"]["task_version"], task["version"])
        self.assertEqual([m["body_preview"] for m in compact["items"][:2]], list(bodies[:2]))
        self.assertEqual(compact["items"][-1]["body_preview"], bodies[1] + " [truncated; read full record]")
        self.assertEqual(compact["items"][-1]["detail_preview"], "é" * 1200 + " [truncated; read full record]")
        self.assertIsNone(compact["items"][0]["detail_preview"])
        self.assertEqual(self.store.inbox("CODEX_EXPERT", pending=True), full)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_compact_pending_pagination_keeps_ack_gaps_and_recipient_scope(self):
        messages = []
        for state in ("processed", "queued", "dispatching", "submitted", "accepted", "unknown", "failed"):
            message = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Question with " + state)
            with self.store.tx() as db:
                db.execute("UPDATE messages SET status=? WHERE id=?", (state, message["id"]))
            messages.append(message)
            self.store.send("CODEX_EXPERT", "CLAUDE_01", "A different member's question")
        first = self.store.inbox("CODEX_EXPERT", limit=2, pending=True, compact=True)
        self.assertEqual([m["id"] for m in first["items"]], [m["id"] for m in messages[1:3]])
        self.assertEqual(first["next_after"], messages[2]["seq"])
        self.store.acknowledge("CODEX_EXPERT", messages[2]["id"], "Read full question and reconciled it")
        late = self.store.send("CLAUDE_01", "CODEX_EXPERT", "Arrived during pagination")
        after, remainder = first["next_after"], []
        while after is not None:
            page = self.store.inbox("CODEX_EXPERT", after=after, limit=2, pending=True, compact=True)
            remainder.extend(page["items"])
            after = page["next_after"]
        self.assertEqual([m["id"] for m in remainder], [m["id"] for m in messages[3:]] + [late["id"]])
        self.assertEqual([m["status"] for m in remainder], ["submitted", "accepted", "unknown", "failed", "queued"])
        fresh = Store(self.project).inbox("CODEX_EXPERT", pending=True, compact=True)
        self.assertEqual([m["id"] for m in fresh["items"]], [messages[1]["id"]] + [m["id"] for m in remainder])
        for flags in ({"after": -1}, {"limit": 0}, {"limit": 201}):
            with self.assertRaises(RoomError):
                self.store.inbox("CODEX_EXPERT", pending=True, compact=True, **flags)

    def test_dedup_pagination_and_ack_are_not_messages(self):
        for i in range(3):
            self.store.send("CLAUDE_01", "CODEX_EXPERT", f"Finding {i}", message_id=f"m{i}")
        self.store.send("CLAUDE_01", "CODEX_EXPERT", "Finding 0", message_id="m0")
        with self.assertRaises(RoomError):
            self.store.send("CLAUDE_01", "CODEX_EXPERT", "Different", message_id="m0")
        first = self.store.inbox("CODEX_EXPERT", limit=2)
        second = self.store.inbox("CODEX_EXPERT", after=first["next_after"], limit=2)
        self.assertEqual([m["id"] for m in first["items"] + second["items"]], ["m0", "m1", "m2"])
        with self.assertRaises(RoomError):
            self.store.acknowledge("CLAUDE_01", "m0", "Not recipient")
        self.store.acknowledge("CODEX_EXPERT", "m0", "Read finding and recorded checkpoint")
        self.assertEqual(sum(self.store.status()["message_counts"].values()), 9)
        self.assertEqual(self.store.status()["message_counts"]["processed"], 1)

    def test_reopen_and_source_snapshot(self):
        path = self.project / "code.py"
        path.write_text("a = 1\n")
        task = self.task(scope=["code.py"])
        snapshot = fingerprint(self.project, ["code.py"])
        self.store.update_task("CLAUDE_01", task["id"], 1, {"checkpoint": "Review at a=1", "snapshot": snapshot})
        recovered = Store(self.project)
        path.write_text("a = 2\n")
        with self.assertRaises(RoomError):
            recovered.update_task("CLAUDE_01", task["id"], 2, {"state": "done", "evidence": ["Review at a=1"]})
        self.assertEqual(recovered.status()["tasks"][0]["checkpoint"], "Review at a=1")

    def test_generated_projection_never_overwrites_user_file(self):
        target = self.project / "agents_space/tasks/active.md"
        target.write_text("Concurrent user note")
        with self.assertRaises(RoomError):
            self.store.project_views()
        self.assertEqual(target.read_text(), "Concurrent user note")

    def test_scope_escape_is_rejected(self):
        with self.assertRaises(RoomError):
            self.task(scope=["../outside.py"])
        with self.assertRaises(RoomError):
            self.task(scope=["agents_space/.runtime/room.sqlite3"])

    def test_dependency_completion_notifies_waiting_owner(self):
        a = self.task()
        b = self.task(owner="CODEX_EXPERT", scope=["src/b.py"], dependencies=[a["id"]])
        with self.assertRaises(RoomError):
            self.store.claim("CODEX_EXPERT", b["id"], b["version"])
        before = len(self.store.inbox("CODEX_EXPERT")["items"])
        self.store.update_task("CLAUDE_01", a["id"], a["version"], {"state": "done", "evidence": ["Acceptance check passed"]})
        messages = self.store.inbox("CODEX_EXPERT")["items"]
        self.assertEqual(len(messages), before + 1)
        self.assertEqual(messages[-1]["task"], b["id"])

    def test_cancel_does_not_release_another_live_writer(self):
        task = self.task(owner="CODEX_EXPERT")
        claim = self.store.claim("CODEX_EXPERT", task["id"], task["version"])
        self.store.update_task("CLAUDE_01", task["id"], claim["version"],
            {"state": "cancelled", "source": self.prompt, "next": "Stop writing and acknowledge cancellation"})
        self.assertEqual(len(self.store.status()["claims"]), 1)
        other = self.task(scope=["src/a.py"])
        with self.assertRaises(RoomError):
            self.store.claim("CLAUDE_01", other["id"], other["version"])
        self.store.release("CODEX_EXPERT", task["id"], claim["token"])
        self.assertEqual(self.store.claim("CLAUDE_01", other["id"], other["version"])["owner"], "CLAUDE_01")


if __name__ == "__main__":
    unittest.main()
