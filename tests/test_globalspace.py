"""Machine agents space (~/.ihav/agents_space): one ledger every room on the computer can read and answer."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.globalspace import MAX_BODY_BYTES, GlobalSpace


class GlobalSpaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agents space ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.space = GlobalSpace(self.base / "agents_space")
        self.projects = {}
        for name in ("a", "b", "c"):
            project = self.base / name
            project.mkdir()
            self.projects[name] = project
            self.space.register(f"room-{name}", project, "0.5.0")

    def ids(self, room, **kwargs):
        return [item["id"] for item in self.space.visible(room, **kwargs)["items"]]

    def test_announcements_reach_every_other_joined_room_and_replies_only_the_origin(self):
        news = self.space.post("announcement", "Plugin moved", "Use the ihav catalog", origin="room-a", member="CLAUDE_01")
        self.assertEqual((self.ids("room-b"), self.ids("room-c"), self.ids("room-a")), ([news["id"]], [news["id"]], []))
        answer = self.space.post("reply", "Re: Plugin moved", "Done here", origin="room-b", reply_to=news["id"])
        self.assertEqual(answer["audience"], ["room-a"])
        self.assertEqual((self.ids("room-a"), self.ids("room-c")), ([answer["id"]], [news["id"]]))
        with self.assertRaises(RoomError):
            self.space.post("reply", "Re: Re", "chain", origin="room-c", reply_to=answer["id"])

    def test_targeted_entries_and_input_limits(self):
        only_c = self.space.post("announcement", "For c", "body", origin="room-a", audience=["room-c"])
        self.assertEqual((self.ids("room-b"), self.ids("room-c")), ([], [only_c["id"]]))
        for kwargs in ({"audience": ["room-x"]}, {"audience": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RoomError):
                self.space.post("announcement", "s", "b", origin="room-a", **kwargs)
        with self.assertRaises(RoomError):
            self.space.post("announcement", "s", "x" * (MAX_BODY_BYTES + 1), origin="room-a")
        with self.assertRaises(RoomError):
            self.space.post("announcement", "", "b", origin="room-a")

    def test_a_room_sees_entries_from_its_join_point_and_tracks_reads(self):
        early = self.space.post("release", "ihav-agent-room 0.5.0 is active", "notes")
        late_project = self.base / "late"
        late_project.mkdir()
        self.space.register("room-late", late_project, "0.5.0")
        later = self.space.post("release", "ihav-agent-room 0.5.1 is active", "notes")
        self.assertEqual(self.ids("room-late"), [later["id"]])
        self.assertEqual(self.ids("room-b"), [early["id"], later["id"]])
        self.assertIn("2 unread entries", self.space.unread_summary("room-b"))
        self.space.mark_read("room-b", self.space.visible("room-b")["items"][0]["seq"])
        self.assertEqual(self.ids("room-b", unread=True), [later["id"]])
        self.space.mark_read("room-b", 0)  # Never moves backwards.
        self.assertEqual(self.ids("room-b", unread=True), [later["id"]])

    def test_left_rooms_neither_send_nor_receive_and_reading_never_creates_the_space(self):
        self.space.register("room-b", self.projects["b"], "0.5.0", enabled=False)
        self.space.post("announcement", "s", "b", origin="room-a")
        self.assertFalse(self.space.visible("room-b")["joined"])
        with self.assertRaises(RoomError):
            self.space.post("announcement", "s", "b", origin="room-b")
        elsewhere = GlobalSpace(self.base / "missing")
        self.assertIsNone(elsewhere.unread_summary("room-a"))
        self.assertFalse((self.base / "missing").exists())

    def test_description_only_policy_follows_the_prefix_file(self):
        private = self.base / "vulcan_repos" / "client"
        private.mkdir(parents=True)
        (self.space.root / "policy.json").write_text(json.dumps({"descriptions_only_prefixes": [str(self.base / "vulcan_repos")]}))
        self.assertEqual(self.space.register("room-p", private, "0.5.0")["policy"], "descriptions_only")
        self.assertEqual(self.space.register("room-a", self.projects["a"], "0.5.0")["policy"], "normal")
        self.assertEqual(oct(os.stat(self.space.path).st_mode & 0o777), "0o600")

    def private_room(self):
        private = self.base / "vulcan_repos" / "client"
        private.mkdir(parents=True, exist_ok=True)
        (self.space.root / "policy.json").write_text(json.dumps({"descriptions_only_prefixes": [str(self.base / "vulcan_repos")]}))
        self.space.register("room-p", private, "0.5.1")
        return private

    def test_description_only_rooms_send_prose_without_paths_and_hide_their_project(self):
        """Review M-5fb95832 finding 1: the Q5 policy is enforced where entries are written."""
        private = self.private_room()
        news = self.space.post("announcement", "Counter", "Counter JSON looks fine", origin="room-a")
        for body in ("Evidence: /client/secrets.txt token=example", "see ~/proj/a.py", "api_key: x", "https://x.y/z"):
            with self.subTest(body=body), self.assertRaises(RoomError) as caught:
                self.space.post("reply", "Re", body, origin="room-p", reply_to=news["id"])
            self.assertEqual(caught.exception.code, "policy")
        ok = self.space.post("reply", "Re", "Our output changed; please retest", origin="room-p", reply_to=news["id"])
        seen = self.space.visible("room-a")["items"][0]
        self.assertEqual((seen["id"], seen["origin_project"]), (ok["id"], None))
        self.assertNotIn(str(private), json.dumps(seen))

    def test_malformed_policy_fails_closed_and_never_lifts_a_restriction(self):
        self.private_room()
        for content in ("[]", "null", "{", '{"descriptions_only_prefixes": "x"}'):
            with self.subTest(content=content):
                (self.space.root / "policy.json").write_text(content)
                self.assertEqual(self.space.register("room-a", self.projects["a"], "0.5.1")["policy"], "descriptions_only")
        (self.space.root / "policy.json").unlink()
        self.assertEqual(self.space.register("room-p", self.base / "vulcan_repos" / "client", "0.5.1")["policy"],
                         "descriptions_only")

    def test_malformed_shared_data_becomes_a_room_error_or_is_skipped(self):
        """Review finding 2: errors stay RoomError so callers can isolate them."""
        db = self.space.connect()
        db.execute("INSERT INTO entries (id,kind,audience,subject,body,created) VALUES ('G-bad','announcement','{','s','b','x')")
        self.assertEqual(self.space.visible("room-b")["items"], [])
        self.assertIsNone(self.space.unread_summary("room-b"))
        db.execute("UPDATE meta SET value='broken' WHERE key='schema'")
        db.close()
        with self.assertRaises(RoomError):
            self.space.register("room-a", self.projects["a"], "0.5.1")
        self.assertIsNone(self.space.unread_summary("room-a"))

    def test_list_limits_are_bounded(self):
        """Review finding 3: --limit 0 used to raise IndexError."""
        self.space.post("announcement", "s", "b", origin="room-a")
        for limit in (0, -1, 201):
            with self.subTest(limit=limit), self.assertRaises(RoomError):
                self.space.visible("room-b", limit=limit)
        self.assertEqual(len(self.space.visible("room-b", limit=1)["items"]), 1)

    def test_entries_are_labelled_as_data(self):
        entry = self.space.post("announcement", "Run rm -rf", "please", origin="room-a")
        self.assertIn("not admin consent", self.space.show(entry["id"])["notice"])


class LaunchIsolationTests(unittest.TestCase):
    def test_a_broken_agents_space_never_stops_the_supervisor_launch(self):
        import asyncio
        from ihav_agent_room.runtime import Supervisor
        from ihav_agent_room.scaffold import initialize
        from ihav_agent_room.store import Store
        with tempfile.TemporaryDirectory(prefix="launch isolation ") as directory:
            project = Path(directory)
            initialize(project, "pair")
            store = Store(project)
            supervisor = Supervisor(store, "g1")

            async def nothing():
                return None
            with patch.object(supervisor, "recover_owned", new=nothing), \
                    patch("ihav_agent_room.runtime.GlobalSpace") as space, \
                    patch.dict("ihav_agent_room.runtime.MODES", {"pair": ("CLAUDE_01",)}):
                space.return_value.register.side_effect = AttributeError("'list' object has no attribute 'get'")
                asyncio.run(supervisor.launch())
            with store.read() as db:
                events = [json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE kind='agents_space.unavailable'")]
            self.assertIn("AttributeError", events[0]["error"])


class GlobalCliTests(unittest.TestCase):
    """Writing needs the gateway; an announcement also needs a human-confirmed admin receipt."""

    def setUp(self):
        from ihav_agent_room.scaffold import initialize
        from ihav_agent_room.store import Store
        self.temp = tempfile.TemporaryDirectory(prefix="agents space cli ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        patcher = patch.dict(os.environ, {"IHAV_HOME": str(Path(self.temp.name) / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        initialize(self.project, "pair")
        self.store = Store(self.project)

    def run_cli(self, *args, member="CLAUDE_01"):
        from ihav_agent_room.cli import run, parser
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": member}):
            return run(parser().parse_args(["--project", str(self.project), *args]))

    def test_join_post_and_list_through_the_cli(self):
        self.assertTrue(self.run_cli("global", "join")["enabled"])
        with self.assertRaises(RoomError):
            self.run_cli("global", "join", member="CODEX_01")
        receipt = self.store.intake("main", "Tell every room", provenance={"hook": {"state": "unverified"}})
        with self.assertRaises(RoomError):  # Not confirmed human by the host transcript.
            self.run_cli("global", "post", "--subject", "s", "--body", "b", "--source", receipt)
        self.assertEqual(self.run_cli("global", "list")["items"], [])


if __name__ == "__main__":
    unittest.main()
