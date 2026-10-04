"""Silent hooks are reported to the admin through a room notice (admin request R1.a, 2026-10-04)."""

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import time
import unittest

from ihav_agent_room.common import GATEWAY
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store


class HookSilenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hook silence ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        initialize(self.project, "pair")
        self.store = Store(self.project)
        self.supervisor = Supervisor(self.store, "g1")
        self.transcript = self.project / "session.jsonl"
        self.transcript.write_text("{}\n")

    def seen(self, seconds_before_transcript):
        moved = self.transcript.stat().st_mtime
        stamp = datetime.fromtimestamp(moved - seconds_before_transcript, timezone.utc).isoformat()
        self.store.member(GATEWAY, {"hook_seen": stamp, "transcript": str(self.transcript)})
        return stamp

    def notices(self):
        with self.store.read() as db:
            return [row[0] for row in db.execute("SELECT body FROM messages WHERE recipient=?", (GATEWAY,))]

    def test_a_conversation_that_moved_on_without_hooks_gets_one_warning(self):
        self.seen(3600)
        self.assertTrue(self.supervisor.check_hook_silence(now_ts=time.time()))
        self.assertIn("/reload-plugins", self.notices()[0])
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=time.time() + 400))  # Once per silence.
        self.assertEqual(len(self.notices()), 1)

    def test_recent_hooks_or_missing_data_stay_quiet(self):
        self.seen(60)
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=time.time()))
        self.store.member(GATEWAY, {"transcript": str(self.project / "missing.jsonl")})
        self.assertFalse(self.supervisor.check_hook_silence(now_ts=time.time() + 400))
        self.assertEqual(self.notices(), [])


if __name__ == "__main__":
    unittest.main()
