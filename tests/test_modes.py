"""Room modes (admin decisions 2026-10-03): pair/advisors, mode switch, per-member effort, gateway effort sync."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.pop("CLAUDE_EFFORT", None)  # Hermetic: the host session effort must not leak into room state.

from ihav_agent_room.common import GATEWAY, MODES, RoomError, process_stamp
from ihav_agent_room.hooks import session_effort
from ihav_agent_room.native import start_claude
from ihav_agent_room.roster import MODE_SETTINGS, NEW_ROOM_MODE, mode_settings
from ihav_agent_room.runtime import Supervisor, change_mode
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store

CLI = Path(__file__).resolve().parents[1] / "bin" / "ihav-agent-room"


class ModeRosterTests(unittest.TestCase):
    def test_pair_and_advisors_members_and_settings(self):
        self.assertEqual(NEW_ROOM_MODE, "pair")
        self.assertEqual(MODES["pair"], ("CLAUDE_01", "CODEX_01"))
        self.assertEqual(MODES["advisors"], ("CLAUDE_01", "CODEX_01", "CLAUDE_EXPERT", "CODEX_EXPERT"))
        self.assertEqual((mode_settings("pair", "CLAUDE_WORKER")["model"], mode_settings("pair", "CLAUDE_01")["effort"]),
                         ("opus", "medium"))
        self.assertEqual((mode_settings("pair", "CODEX_WORKER")["model"], mode_settings("pair", "CODEX_01")["effort"]),
                         ("gpt-6.1-sol", "medium"))
        expected = {"CLAUDE_01": ("sonnet", "xhigh"), "CODEX_01": ("gpt-6-luna", "xhigh"),
                    "CLAUDE_EXPERT": ("opus", "xhigh"), "CODEX_EXPERT": ("gpt-6.1-sol", "xhigh")}
        for name, (model, effort) in expected.items():
            with self.subTest(name=name):
                self.assertEqual((mode_settings("advisors", name)["model"], mode_settings("advisors", name)["effort"]),
                                 (model, effort))

    def test_legacy_modes_keep_four_members_and_their_settings(self):
        for legacy in ("default", "full"):
            with self.subTest(mode=legacy):
                self.assertEqual(MODES[legacy], MODES["advisors"])
                for name in MODES[legacy]:
                    self.assertEqual(mode_settings(legacy, name), MODE_SETTINGS["advisors"][name])


class ModeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="room modes ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)

    def store(self, mode):
        initialize(self.project, mode)
        return Store(self.project)

    def test_new_pair_room_requests_pair_settings(self):
        store = self.store("pair")
        self.assertEqual((store.member("CODEX_01")["requested_model"], store.member("CODEX_01")["requested_effort"]),
                         ("gpt-6.1-sol", "medium"))
        self.assertEqual((store.member(GATEWAY)["requested_model"], store.member(GATEWAY)["effort_source"]), ("opus", "mode"))

    def test_effort_override_clear_and_gateway_rule(self):
        store = self.store("advisors")
        store.set_effort("low", member="CODEX_WORKER")
        self.assertEqual((store.member("CODEX_01")["requested_effort"], store.member("CODEX_01")["effort_source"]),
                         ("low", "override"))
        self.assertEqual(store.member("CODEX_EXPERT")["requested_effort"], "xhigh")
        store.set_effort(None, member="CODEX_01", clear=True)
        self.assertEqual((store.member("CODEX_01")["requested_effort"], store.member("CODEX_01")["effort_source"]),
                         ("xhigh", "mode"))
        store.set_effort("high")
        self.assertTrue(all(store.member(name)["requested_effort"] == "high" for name in MODES["advisors"] if name != GATEWAY))
        with self.assertRaises(RoomError):
            store.set_effort("high", member=GATEWAY)
        with self.assertRaises(RoomError):
            store.set_effort("extreme")

    def test_gateway_effort_sync_moves_everyone_and_clears_overrides(self):
        store = self.store("advisors")
        store.member("CLAUDE_EXPERT", {"native_id": "existing-session"})
        store.set_effort("low", member="CODEX_01")
        # The first observed session effort is only a baseline: mode settings and overrides stay.
        self.assertFalse(store.sync_gateway_effort("high"))
        self.assertEqual(store.member("CODEX_EXPERT")["requested_effort"], "xhigh")
        self.assertEqual(store.member("CODEX_01")["requested_effort"], "low")
        self.assertTrue(store.sync_gateway_effort("max"))
        for name in ("CODEX_01", "CLAUDE_EXPERT", "CODEX_EXPERT"):
            with self.subTest(name=name):
                self.assertEqual((store.member(name)["requested_effort"], store.member(name)["effort_source"]), ("max", "gateway"))
        self.assertTrue(store.member("CLAUDE_EXPERT")["settings_pending_restart"])
        self.assertFalse(store.member("CODEX_EXPERT").get("settings_pending_restart"))
        self.assertEqual(store.member(GATEWAY)["observed_effort"], "max")
        self.assertEqual(store.member(GATEWAY)["requested_effort"], "xhigh")
        store.set_effort("low", member="CODEX_01")
        self.assertFalse(store.sync_gateway_effort("max"))  # Same session effort: nothing to sync.
        self.assertEqual(store.member("CODEX_01")["requested_effort"], "low")
        self.assertTrue(store.sync_gateway_effort("medium"))
        self.assertEqual(store.member("CODEX_01")["requested_effort"], "medium")
        self.assertFalse(store.sync_gateway_effort("turbo"))
        report = store.effort_report()
        self.assertEqual(report["synced_effort"], "medium")

    def test_status_warns_when_the_gateway_differs_from_the_mode(self):
        store = self.store("pair")
        self.assertNotIn("gateway_settings_warning", store.status())
        store.member(GATEWAY, {"observed_model": "claude-sonnet-5-5", "observed_effort": "xhigh"})
        warning = store.status()["gateway_settings_warning"]
        self.assertIn("/model opus", warning)
        self.assertIn("/effort medium", warning)
        self.assertIn("gateway_settings_warning", store.status(compact=True))
        store.member(GATEWAY, {"observed_model": "claude-opus-5-5", "observed_effort": "medium"})
        self.assertNotIn("gateway_settings_warning", store.status())

    def test_stopped_room_switches_mode_and_blocks_open_work_of_leaving_members(self):
        store = self.store("default")
        result = change_mode(store, "pair")
        self.assertEqual((result["mode"], result["restarting"]), ("pair", False))
        self.assertEqual(store.room()["mode"], "pair")
        self.assertEqual(store.member("CODEX_01")["requested_model"], "gpt-6.1-sol")
        change_mode(store, "advisors")
        self.assertEqual(store.member("CODEX_01")["requested_model"], "gpt-6-luna")
        with self.assertRaises(RoomError):
            change_mode(store, "full")
        for field in ("owner", "reviewer"):
            with self.subTest(field=field):
                task = {"id": "T-" + field, "owner": "CODEX_01", "state": "in_progress", field: "CODEX_EXPERT"}
                with store.tx() as db:
                    db.execute("INSERT INTO tasks VALUES (?,?,?)", (task["id"], 1, json.dumps(task)))
                with self.assertRaises(RoomError) as caught:
                    change_mode(store, "pair")
                self.assertEqual(caught.exception.code, "conflict")
                self.assertEqual(store.room()["mode"], "advisors")
                with store.tx() as db:
                    db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))

    def test_running_room_restarts_on_exact_sessions(self):
        store = self.store("default")
        with store.tx() as db:
            room = store.get_room(db)
            room.update(status="running", supervisor={"pid": os.getpid(), "stamp": process_stamp(os.getpid())})
            store.put_room(db, room)
        result = change_mode(store, "pair")
        room = store.room()
        self.assertTrue(result["restarting"])
        self.assertEqual((room["mode"], room["status"], room["restart_requested"], room["manual_stop"]),
                         ("pair", "running", True, False))


class ModeCliTests(unittest.TestCase):
    def test_init_defaults_to_pair_and_names_the_switch_command(self):
        with tempfile.TemporaryDirectory(prefix="room init mode ") as directory:
            env = dict(os.environ, IHAV_AGENT_ROOM_MEMBER="CLAUDE_01", IHAV_AGENT_ROOM_SKIP_ALIAS="1")
            result = subprocess.run([sys.executable, str(CLI), "--project", directory, "--json", "init", "--no-start"],
                                    env=env, capture_output=True, text=True, timeout=30)
            data = json.loads(result.stdout)["data"]
            self.assertEqual(data["mode"], "pair")
            self.assertEqual(data["members"], ["CLAUDE_01", "CODEX_01"])
            self.assertIn("pair mode", data["mode_note"])
            self.assertIn("/ihav-agent-room:mode advisors", data["mode_note"])
            shown = json.loads(subprocess.run([sys.executable, str(CLI), "--project", directory, "--json", "mode"],
                                              env=env, capture_output=True, text=True, timeout=30).stdout)["data"]
            self.assertEqual((shown["mode"], shown["choices"]), ("pair", ["pair", "advisors"]))
            self.assertEqual([member["name"] for member in shown["members"] if member["in_mode"]], ["CLAUDE_01", "CODEX_01"])


class ModeCliWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="room cli modes ")
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, IHAV_AGENT_ROOM_MEMBER="CLAUDE_01", IHAV_AGENT_ROOM_SKIP_ALIAS="1")

    def call(self, *args, member=None, ok=True):
        env = dict(self.env, IHAV_AGENT_ROOM_MEMBER=member) if member else self.env
        result = subprocess.run([sys.executable, str(CLI), "--project", self.temp.name, "--json", *args],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        return data.get("data", data)

    def test_reinit_keeps_a_legacy_room_and_names_its_mode(self):
        self.call("init", "--no-start", "--mode", "default")
        again = self.call("init", "--no-start")
        self.assertEqual((again["mode"], len(again["members"])), ("default", 4))
        self.assertIn("/ihav-agent-room:mode", again["mode_note"])

    def test_mode_and_effort_writes_through_the_cli(self):
        self.call("init", "--no-start")
        self.assertEqual(self.call("mode", "advisors")["mode"], "advisors")
        members = {member["name"]: member for member in self.call("effort", "low", "--member", "CODEX_01")["members"]}
        self.assertEqual((members["CODEX_01"]["requested_effort"], members["CODEX_01"]["source"]), ("low", "override"))
        members = {member["name"]: member for member in self.call("effort", "--clear", "--member", "CODEX_01")["members"]}
        self.assertEqual((members["CODEX_01"]["requested_effort"], members["CODEX_01"]["source"]), ("xhigh", "mode"))
        members = {member["name"]: member for member in self.call("effort", "max", "--all")["members"]}
        self.assertEqual({members[name]["requested_effort"] for name in ("CODEX_01", "CLAUDE_EXPERT", "CODEX_EXPERT")}, {"max"})
        for args in (("mode", "pair"), ("effort", "low")):
            with self.subTest(args=args):
                self.assertEqual(self.call(*args, member="CODEX_01", ok=False)["error"]["code"], "authority")
        self.assertEqual(self.call("mode")["mode"], "advisors")
        self.assertEqual(self.call("effort", "--member", "CLAUDE_01", "low", ok=False)["error"]["code"], "authority")


class ModeNativeTests(unittest.TestCase):
    def test_session_effort_reads_the_hook_field_then_the_environment(self):
        self.assertEqual(session_effort({"effort": {"level": "High"}}), "high")
        with patch.dict(os.environ, {"CLAUDE_EFFORT": "max"}):
            self.assertEqual(session_effort({}), "max")
            self.assertEqual(session_effort({"effort": {"level": "low"}}), "low")
        self.assertIsNone(session_effort({"effort": "nonsense"}))

    def test_exact_resume_requests_model_and_effort_in_the_settings_file(self):
        with tempfile.TemporaryDirectory(prefix="native resume settings ") as directory:
            project = Path(directory)

            async def failed_launch(*args, **kwargs):
                raise OSError("no native launch in tests")

            with patch("ihav_agent_room.native.claude_agents", return_value=[]), \
                    patch("ihav_agent_room.native.asyncio.create_subprocess_exec", new=failed_launch):
                with self.assertRaises(OSError):
                    asyncio.run(start_claude(project, "existing-session", True, {"PATH": "/usr/bin"},
                        project / "CLAUDE_EXPERT.log", member="CLAUDE_EXPERT", model="opus", effort="low"))
            settings = json.loads((project / "CLAUDE_EXPERT.settings.json").read_text())
            self.assertEqual((settings["model"], settings["effortLevel"]), ("opus", "low"))

    def test_codex_turn_uses_the_member_current_requested_settings(self):
        with tempfile.TemporaryDirectory(prefix="room codex settings ") as directory:
            project = Path(directory)
            initialize(project, "advisors")
            store = Store(project)
            for name in MODES["advisors"]:
                store.member(name, {"status": "idle", "native_id": name.lower()})
            with store.tx() as db:
                room = store.get_room(db)
                room.update(status="running", generation="mode-generation")
                store.put_room(db, room)
            store.set_effort("low", member="CODEX_EXPERT")

            class Client:
                def __init__(self):
                    self.model_config, self.sent, self.turn_id, self.last_sent_turn_id = None, [], None, None

                async def send(self, message):
                    self.sent.append(dict(self.model_config))
                    return "accepted"

            supervisor = Supervisor(store, "mode-generation")
            supervisor.codex["CODEX_EXPERT"] = Client()
            with store.tx() as db:
                store.queue(db, "CLAUDE_01", "CODEX_EXPERT", "Check the contract")
            asyncio.run(supervisor._dispatch_member_queue(
                "CODEX_EXPERT", [dict(row) for row in store.connect().execute(
                    "SELECT * FROM messages WHERE recipient='CODEX_EXPERT'")], set()))
            self.assertEqual(supervisor.codex["CODEX_EXPERT"].sent, [{"model": "gpt-6.1-sol", "effort": "low"}])


if __name__ == "__main__":
    unittest.main()
