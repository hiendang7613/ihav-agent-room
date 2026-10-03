"""A new main session resumes its room without manual exit or resume (admin request 2026-10-03)."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.hooks import handle
from ihav_agent_room.runtime import autostart, needs_autostart
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autostart ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        initialize(self.project, "pair")
        self.store = Store(self.project)

    def set_room(self, **changes):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(changes)
            self.store.put_room(db, room)

    def events(self):
        with self.store.read() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE kind='room.autostart'")]

    def test_retries_while_the_new_session_is_not_yet_registered(self):
        attempts = [RoomError("Exact Claude session is not live in this project", "unavailable"), {"started": True}]
        with patch("ihav_agent_room.runtime.start_room", side_effect=attempts) as start:
            result = autostart(self.store, "new-session", budget=5, pause=0)
        self.assertEqual((result["result"], start.call_count), ("started", 2))
        self.assertEqual(self.events()[-1]["result"], "started")

    def test_refusals_end_at_once_and_are_recorded(self):
        for code in ("conflict", "dependency"):
            with self.subTest(code=code), patch("ihav_agent_room.runtime.start_room", side_effect=RoomError("no", code)) as start:
                result = autostart(self.store, "new-session", budget=5, pause=0)
                self.assertEqual((result["result"], result["code"], start.call_count), ("failed", code, 1))

    def test_only_a_room_whose_owner_and_supervisor_died_without_manual_stop_needs_it(self):
        dead = {"session": "old", "pid": None, "stamp": None}
        self.assertFalse(needs_autostart(self.store, "new"))  # Never bound: init stays an explicit admin step.
        self.set_room(owner=dead, manual_stop=False)
        self.assertTrue(needs_autostart(self.store, "new"))
        self.assertFalse(needs_autostart(self.store, "old"))
        self.set_room(manual_stop=True)
        self.assertFalse(needs_autostart(self.store, "new"))

    def test_hooks_hand_an_unregistered_session_to_the_background(self):
        self.set_room(owner={"session": "old", "pid": None, "stamp": None}, manual_stop=False)
        payload = {"cwd": str(self.project), "session_id": "new", "permission_mode": "default"}
        with patch("ihav_agent_room.hooks.bind_main", side_effect=RoomError("not live", "unavailable")), \
                patch("ihav_agent_room.hooks.spawn_autostart") as spawn, patch.dict("os.environ", {"IHAV_AGENT_ROOM_SKIP_ALIAS": "1"}):
            output = handle(dict(payload, hook_event_name="SessionStart"))
            self.assertIn("continues in the background", json.dumps(output))
            handle(dict(payload, hook_event_name="UserPromptSubmit", prompt="hello"))
        self.assertEqual([call.args[1] for call in spawn.call_args_list], ["new", "new"])


if __name__ == "__main__":
    unittest.main()
