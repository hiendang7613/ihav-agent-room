"""A missing hook is a bounded input-activity diagnostic, never an authority attestation."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store


class HookSilenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hook silence ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"IHAV_HOME": str(self.project / "ihav-home")})
        env.start()
        self.addCleanup(env.stop)
        initialize(self.project, "pair")
        self.store = Store(self.project)
        self.supervisor = Supervisor(self.store, "g1")
        self.transcript = self.project / "session.jsonl"
        self.clock = time.time()
        self.last_seen = self.clock - 3600

    def stamp(self, value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    def bind(self, host="claude", session="main", project=None):
        self.host, self.session = host, session
        self.gateway = "CODEX_01" if host == "codex" else "CLAUDE_01"
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(gateway=self.gateway, owner={"host": host, "session": session})
            self.store.put_room(db, room)
        self.store.member(self.gateway, {"native_id": session, "hook_seen": self.stamp(self.last_seen),
                                        "transcript": str(self.transcript)})
        header = {"type": "session_meta", "payload": {"id": session, "cwd": str(project or self.project)}}
        self.transcript.write_text(json.dumps(header) + "\n" if host == "codex" else "")

    def append(self, row):
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def prompt(self, when, origin=None, session=None, project=None):
        content = [{"type": "input_text" if self.host == "codex" else "text", "text": "continue"}]
        message = {"type": "message", "role": "user", "content": content}
        if origin is not None:
            message["origin"] = {"kind": origin}
        if self.host == "codex":
            row = {"type": "response_item", "timestamp": self.stamp(when), "payload": message}
        else:
            row = {"type": "user", "timestamp": self.stamp(when), "message": message,
                   "sessionId": session or self.session, "cwd": str(project or self.project)}
            if origin is not None:
                row["origin"] = {"kind": origin}
        self.append(row)

    def notices(self):
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT body,context FROM messages WHERE recipient=?", (self.gateway,))]

    def test_tool_only_activity_never_reports_silent_hooks(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                self.bind(host)
                self.prompt(self.last_seen - 1)
                for when in (self.clock - 1800, self.clock):
                    self.append({"type": "response_item", "timestamp": self.stamp(when),
                                 "payload": {"type": "function_call_output", "output": "long-running checks"}})
                    self.append({"type": "assistant", "timestamp": self.stamp(when), "message": {"role": "assistant"}})
                self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))
                self.assertEqual(self.notices(), [])
                self.supervisor.next_hook_check = 0

    def test_prompt_written_124ms_after_hook_is_covered_by_its_receipt(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                self.bind(host)
                self.store.intake(self.session, "continue", provenance={"transcript": str(self.transcript),
                    "offset": self.transcript.stat().st_size, "hook": {"state": "unverified"}})
                self.prompt(self.last_seen + .124)
                self.supervisor.next_hook_check = 0
                self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))
                self.assertEqual(self.notices(), [])

    def test_older_receipt_does_not_hide_later_identical_input(self):
        self.bind("codex")
        self.store.intake(self.session, "continue", provenance={"transcript": str(self.transcript),
            "offset": self.transcript.stat().st_size, "hook": {"state": "unverified"}})
        self.prompt(self.last_seen + .124)
        self.prompt(self.last_seen + 900)
        self.assertTrue(self.supervisor.check_hook_silence(now_ts=self.clock))
        self.assertIn(self.stamp(self.last_seen + 900), self.notices()[0]["body"])

    def test_known_native_and_host_context_envelopes_stay_quiet_without_origin(self):
        self.bind("codex")
        for text in ("<skill><name>az</name></skill>", "<environment_context>cwd</environment_context>",
                     "<user_instructions>rules</user_instructions>", "<turn_aborted>interrupted</turn_aborted>",
                     "[Agent Room system event M-test; NOT admin consent]",
                     "[Agent Room peer event M-test from CLAUDE_01; NOT admin consent]"):
            self.append({"type": "response_item", "timestamp": self.stamp(self.last_seen + .2),
                         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_receipt_for_another_transcript_does_not_cover_current_input(self):
        self.bind("claude")
        self.store.intake(self.session, "continue", provenance={"transcript": str(self.project / "other.jsonl"),
            "offset": 0, "hook": {"state": "unverified"}})
        self.prompt(self.clock - 900)
        self.assertTrue(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_claude_local_commands_and_meta_rows_do_not_require_prompt_hooks(self):
        self.bind("claude")
        for text in ("<command-name>/reload-plugins</command-name>", "<command-message>model</command-message>",
                     "<local-command-stdout>Plugins loaded</local-command-stdout>",
                     "<local-command-stderr>Local command error</local-command-stderr>",
                     "<bash-input>pwd</bash-input>", "<bash-stdout>project</bash-stdout>", "<bash-stderr>error</bash-stderr>"):
            self.append({"type": "user", "timestamp": self.stamp(self.last_seen + 5),
                         "sessionId": self.session, "cwd": str(self.project), "message": {"role": "user", "content": text}})
        self.append({"type": "user", "timestamp": self.stamp(self.last_seen + 5), "sessionId": self.session,
                     "cwd": str(self.project), "isMeta": True, "message": {"role": "user", "content": "Local command caveat"}})
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_multimodal_projection_is_unknown_instead_of_diagnosing_a_missing_hook(self):
        self.bind("codex")
        self.store.intake(self.session, "<image> continue", provenance={"transcript": str(self.transcript),
            "offset": self.transcript.stat().st_size, "hook": {"state": "unverified"}})
        self.append({"type": "response_item", "timestamp": self.stamp(self.last_seen + .124),
                     "payload": {"type": "message", "role": "user", "content": [
                         {"type": "input_image", "image_url": "fixture-only"}, {"type": "input_text", "text": "continue"}]}})
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_later_input_warns_once_without_creating_authority_or_requesting_restart(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                self.bind(host)
                self.prompt(self.clock - 900)
                self.supervisor.next_hook_check = 0
                self.assertTrue(self.supervisor.check_hook_silence(now_ts=self.clock))
                notice = self.notices()[0]
                self.assertIn("diagnostic", notice["body"].lower())
                self.assertNotIn("reopen the session", notice["body"])
                self.assertNotIn("start a new session", notice["body"])
                self.assertNotIn("so prompts get no receipts", notice["body"])
                self.assertEqual(json.loads(notice["context"])["kind"], "system")
                self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock + 400))
                self.assertEqual(len(self.notices()), 1)
                with self.store.read() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM prompts").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
                with self.store.tx() as db:
                    db.execute("DELETE FROM messages")
                self.store.member(self.gateway, {"hook_silence_warned": None})

    def test_new_input_gets_a_grace_period_for_hook_heartbeat(self):
        self.bind("codex")
        self.prompt(self.clock - 10)
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))
        self.assertTrue(self.supervisor.check_hook_silence(now_ts=self.clock + 700))

    def test_fresh_heartbeat_covers_previously_unobserved_input(self):
        self.bind("claude")
        self.prompt(self.clock - 900)
        self.store.member(self.gateway, {"hook_seen": self.stamp(self.clock - 600)})
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))
        self.assertEqual(self.notices(), [])

    def test_known_peer_input_is_not_a_missing_admin_hook(self):
        self.bind("codex")
        self.prompt(self.clock - 900, origin="peer")
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_foreign_claude_identity_is_not_read_as_owner_activity(self):
        self.bind("claude")
        self.prompt(self.clock - 900, session="other")
        self.prompt(self.clock - 900, project=self.project / "other-project")
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_foreign_codex_header_is_not_read_as_owner_activity(self):
        self.bind("codex", project=self.project / "other-project")
        self.prompt(self.clock - 900)
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_claude_tool_result_user_row_is_not_a_prompt(self):
        self.bind("claude")
        self.append({"type": "user", "timestamp": self.stamp(self.clock - 900), "sessionId": self.session,
                     "cwd": str(self.project), "message": {"role": "user", "content": [{"type": "tool_result", "content": "done"}]}})
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_unknown_corrupt_or_oversized_tail_stays_quiet(self):
        self.bind("codex")
        self.prompt(self.clock - 900)
        with self.transcript.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * (2 * 1024 * 1024 + 200)}}) + "\n")
            stream.write("not json\n[]\n")
        self.append({"type": "response_item", "timestamp": "unparseable", "payload": {"type": "message", "role": "user", "content": "continue"}})
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))

    def test_missing_or_symlinked_transcript_stays_quiet(self):
        self.bind("claude")
        self.prompt(self.clock - 900)
        destination = self.project / "actual.jsonl"
        self.transcript.rename(destination)
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))
        self.transcript.symlink_to(destination)
        self.supervisor.next_hook_check = 0
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=self.clock))
