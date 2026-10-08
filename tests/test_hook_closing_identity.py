"""Native hook payload identity must survive missing or stale shell exports."""

import hashlib
import json
import os
import unittest
from unittest.mock import patch

import test_continuity
from test_closing import CLOSING
from ihav_agent_room.closing import KEY, capture, collect, recover
from ihav_agent_room.common import RoomError
from ihav_agent_room.hooks import handle


class HookClosingIdentityTests(unittest.TestCase):
    setUp = test_continuity.ContinuityTests.setUp
    claude_transcript = test_continuity.ContinuityTests.claude_transcript

    def prepare(self, host="codex"):
        session = "new-codex" if host == "codex" else "old-claude"
        os.environ.update(IHAV_AGENT_ROOM_HOST=host,
                          IHAV_AGENT_ROOM_MEMBER="CODEX_01" if host == "codex" else "CLAUDE_01",
                          IHAV_AGENT_ROOM_SESSION_ID=session)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(gateway=os.environ["IHAV_AGENT_ROOM_MEMBER"],
                        owner={"host": host, "session": session})
            self.store.put_room(db, room)
        path = self.claude_transcript(CLOSING)
        self.assertTrue(capture(self.store)["saved"])
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T12:00:00Z"
        row["message"]["content"][0]["text"] = CLOSING.replace("Support 11 chatbots", "Keep the latest project goal")
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        return session

    def persisted(self):
        with self.store.read() as db:
            return db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]

    def exercise(self, host, environment):
        session = self.prepare(host)
        baseline = self.persisted()
        changes = {"IHAV_AGENT_ROOM_SESSION_ID": session}
        if environment == "missing":
            changes.update({name: "" for name in ("IHAV_AGENT_ROOM_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                                                   "AGENT_ROOM_SESSION_ID", "CODEX_THREAD_ID")})
        elif environment == "stale":
            changes["IHAV_AGENT_ROOM_SESSION_ID"] = "another-session"
        with patch.dict(os.environ, changes):
            original = dict(os.environ)
            result = handle({"hook_event_name": "Stop", "cwd": str(self.project), "session_id": session})
            self.assertEqual(dict(os.environ), original)
        self.assertEqual(result, {})
        self.assertNotEqual(self.persisted(), baseline)
        state = recover(self.store)
        self.assertEqual(state["revision"], 2)
        self.assertIn("Keep the latest project goal", state["fields"]["Goals"]["text"])
        self.assertEqual(state["authority"], "historical_data_only")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM prompts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='context.capture_failed'").fetchone()[0], 0)

    def test_codex_missing_environment_uses_exact_owner_payload(self):
        self.exercise("codex", "missing")

    def test_codex_stale_environment_uses_exact_owner_payload(self):
        self.exercise("codex", "stale")

    def test_codex_matching_environment_still_captures(self):
        self.exercise("codex", "matching")

    def test_claude_missing_environment_uses_exact_owner_payload(self):
        self.exercise("claude", "missing")

    def test_claude_stale_environment_uses_exact_owner_payload(self):
        self.exercise("claude", "stale")

    def test_claude_matching_environment_still_captures(self):
        self.exercise("claude", "matching")

    def stale_member(self, host):
        session = self.prepare(host)
        baseline = self.persisted()
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": "CLAUDE_EXPERT"}):
            original = dict(os.environ)
            self.assertEqual(handle({"hook_event_name": "Stop", "cwd": str(self.project),
                                     "session_id": session}), {})
            self.assertEqual(dict(os.environ), original)
        self.assertEqual(self.persisted(), baseline)
        with self.store.read() as db:
            rows = db.execute("SELECT data FROM events WHERE kind='context.capture_skipped'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0]["data"]),
                             {"host": host, "session": session, "member": "CLAUDE_EXPERT",
                              "reason": "member_mismatch"})
            self.assertEqual(db.execute("SELECT count(*) FROM prompts").fetchone()[0], 0)

    def test_codex_stale_member_reports_skip_without_bypassing_identity(self):
        self.stale_member("codex")

    def test_claude_stale_member_reports_skip_without_bypassing_identity(self):
        self.stale_member("claude")

    def test_stale_payload_wrong_host_subagent_and_recursive_stop_cannot_capture(self):
        session = self.prepare()
        baseline = self.persisted()
        cases = ({"session_id": "former-owner"}, {"agent_id": "subagent"}, {"stop_hook_active": True})
        for changes in cases:
            with self.subTest(changes=changes):
                result = handle({"hook_event_name": "Stop", "cwd": str(self.project),
                                 "session_id": session, **changes})
                self.assertEqual(result, {})
                self.assertEqual(self.persisted(), baseline)
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": "claude", "IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01"}):
            self.assertEqual(handle({"hook_event_name": "Stop", "cwd": str(self.project), "session_id": session}), {})
        self.assertEqual(self.persisted(), baseline)

    def test_bound_worker_cannot_present_the_gateway_payload(self):
        session = self.prepare()
        baseline = self.persisted()
        token = "fixture-only-worker-binding"
        self.store.member("CLAUDE_01", {"token_hash": hashlib.sha256(token.encode()).hexdigest()})
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_BINDING": token, "IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01"}), \
                patch("ihav_agent_room.hooks.capture_closing", wraps=capture) as invoked:
            self.assertEqual(handle({"hook_event_name": "Stop", "cwd": str(self.project), "session_id": session}), {})
            invoked.assert_not_called()
        self.assertEqual(self.persisted(), baseline)

    def test_ordinary_cli_capture_keeps_the_environment_identity_guard(self):
        self.prepare()
        baseline = self.persisted()
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_SESSION_ID": "former-owner", "CODEX_THREAD_ID": ""}):
            with self.assertRaises(RoomError):
                capture(self.store)
        self.assertEqual(self.persisted(), baseline)

    def test_explicit_hook_context_rechecks_owner_and_refuses_bindings(self):
        session = self.prepare()
        baseline = self.persisted()
        for context in ({"host": "codex", "session": "former-owner"},
                        {"host": "claude", "session": session}, {"host": "codex", "session": ""}):
            with self.subTest(context=context), self.assertRaises(RoomError):
                capture(self.store, hook_owner=context)
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_BINDING": "untrusted-worker-token"}), self.assertRaises(RoomError):
            capture(self.store, hook_owner={"host": "codex", "session": session})
        self.assertEqual(self.persisted(), baseline)

    def test_gateway_change_during_hook_capture_preserves_the_previous_snapshot(self):
        session = self.prepare()
        baseline = self.persisted()
        collected = collect(self.store)

        def transfer(_store):
            with self.store.tx() as db:
                room = self.store.get_room(db)
                room.update(owner={"host": "codex", "session": "next-owner"}, generation="next-generation")
                self.store.put_room(db, room)
            return collected

        with patch("ihav_agent_room.closing.collect", side_effect=transfer):
            result = capture(self.store, hook_owner={"host": "codex", "session": session})
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "Gateway changed during capture")
        self.assertEqual(self.persisted(), baseline)
