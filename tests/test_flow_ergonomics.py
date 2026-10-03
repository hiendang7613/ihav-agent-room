"""PREREG gate O4: the canonical room flow needs no heredoc, pipe, sleep loop or temporary copy.

Pilot evidence (2026-09-30, 9 permission callbacks): 4 were plugin flow shapes, among them a heredoc piped to
`task create` and inbox polling with sleep loops. A structured command now accepts an inline JSON object, and
the member guide says not to poll. These checks cover the CLI contract and the guide text, not host behavior.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import ihav_agent_room.cli as cli_module
from ihav_agent_room.cli import main, parser, run
from ihav_agent_room.common import PLUGIN_ROOT
from test_evidence import EvidenceFixture


class FlowErgonomicsTests(EvidenceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "main"}
            self.store.put_room(db, room)
        env = patch.dict(os.environ, IHAV_AGENT_ROOM_MEMBER="CLAUDE_01", IHAV_AGENT_ROOM_SESSION_ID="main")
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *args):
        return run(parser().parse_args(["--project", str(self.project), *args]))

    def test_structured_commands_accept_an_inline_json_object(self):
        created = self.cli("task", "create", "--input", json.dumps({
            "title": "Inline task", "request": "Quote 'apostrophes' and \"quotes\"", "acceptance": "Done", "next": "Start",
            "owner": "CLAUDE_01", "source": self.prompt}))
        self.assertEqual(created["request"], "Quote 'apostrophes' and \"quotes\"")
        updated = self.cli("task", "update", created["id"], "--expected-version", "1",
                           "--input", json.dumps({"next": "Continue inline"}))
        self.assertEqual(updated["next"], "Continue inline")
        note = self.cli("note", "add", "--input", '  {"kind": "question", "body": "Is inline input enough?"}')
        self.assertEqual(note["state"], "open")
        self.assertEqual(self.cli("task", "show", created["id"])["next"], "Continue inline")

    def test_file_and_stdin_input_still_work(self):
        path = self.project / "task.json"
        path.write_text(json.dumps({"title": "From file", "request": "r", "acceptance": "a", "next": "n",
                                    "owner": "CLAUDE_01", "source": self.prompt}))
        self.assertEqual(self.cli("task", "create", "--input", str(path))["title"], "From file")
        stdin = io.StringIO(json.dumps({"kind": "question", "body": "From stdin"}))
        with patch("sys.stdin", stdin):
            self.assertEqual(self.cli("note", "add")["body"], "From stdin")

    def test_bad_inline_json_is_a_clean_error_and_changes_nothing(self):
        before = len(self.store.status()["tasks"])
        for bad in ('{"title": "unterminated', '{"title": 1,}'):
            with self.subTest(bad=bad):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["--project", str(self.project), "task", "create", "--input", bad])
                result = json.loads(output.getvalue())
                self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual(len(self.store.status()["tasks"]), before)

    def test_member_guide_steers_away_from_shell_constructs_the_host_prompts_on(self):
        guide = (PLUGIN_ROOT / "templates/conventions/cli.md").read_text()
        self.assertNotIn("quoted heredoc", guide)
        self.assertIn("--input '{", guide)
        self.assertIn("Do not loop on the inbox", guide)
        self.assertIn("`inbox --pending --wait 90` once", guide)
        self.assertIn("avoid heredocs, pipes, `sleep` loops and copies into /tmp", guide)

    # `inbox --wait`: one bounded blocking read replaces the shell sleep loop (pilot callbacks 4 and 5).
    def send_to_main(self, body="Peer finding", delay=0.0):
        def put():
            time.sleep(delay)
            with self.store.tx() as db:
                self.store.queue(db, "CODEX_EXPERT", "CLAUDE_01", body)
        return put

    def messages(self):
        with self.store.read() as db:
            return [tuple(r) for r in db.execute("SELECT id, status, detail FROM messages ORDER BY seq")]

    def test_wait_returns_at_once_when_a_message_is_already_pending(self):
        self.send_to_main()()
        started = time.monotonic()
        result = self.cli("inbox", "--pending", "--after", "0", "--wait", "30")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((len(result["items"]), result["timed_out"]), (1, False))
        self.assertLess(result["waited"], 1)

    def test_wait_returns_the_message_that_arrives_while_waiting(self):
        sender = threading.Thread(target=self.send_to_main("Late finding", delay=0.7))
        sender.start()
        self.addCleanup(sender.join)
        started = time.monotonic()
        result = self.cli("inbox", "--pending", "--after", "0", "--wait", "20")
        elapsed = time.monotonic() - started
        self.assertEqual([item["body"] for item in result["items"]], ["Late finding"])
        self.assertFalse(result["timed_out"])
        self.assertTrue(0.5 < elapsed < 8, elapsed)

    def test_wait_times_out_with_an_empty_page_and_changes_nothing(self):
        before = self.messages()
        started = time.monotonic()
        result = self.cli("inbox", "--pending", "--after", "0", "--wait", "0.6")
        self.assertTrue(0.5 < time.monotonic() - started < 5)
        self.assertEqual((result["items"], result["timed_out"]), ([], True))
        self.assertEqual(self.messages(), before)

    def test_reading_after_a_wait_never_acknowledges(self):
        self.send_to_main()()
        self.cli("inbox", "--pending", "--after", "0", "--wait", "5")
        self.assertEqual([row[1] for row in self.messages()], ["queued"])
        self.assertEqual(len(self.cli("inbox", "--pending", "--after", "0")["items"]), 1)  # Still pending: no ACK happened.

    def test_a_long_request_is_clamped_to_the_documented_limit(self):
        with patch.object(cli_module, "INBOX_WAIT_LIMIT", 0.6):
            started = time.monotonic()
            result = self.cli("inbox", "--pending", "--after", "0", "--wait", "30")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result["timed_out"])
        self.assertEqual(cli_module.INBOX_WAIT_LIMIT, 100)  # Below the host's two-minute shell timeout.

    def test_wait_rejects_zero_negative_and_non_numbers_and_is_optional(self):
        for bad in ("0", "-1", "inf", "soon"):
            with self.subTest(wait=bad), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    parser().parse_args(["--project", str(self.project), "inbox", "--wait", bad])
        self.send_to_main()()
        self.assertNotIn("waited", self.cli("inbox", "--pending", "--after", "0"))  # Without --wait the output is unchanged.


if __name__ == "__main__":
    unittest.main()
