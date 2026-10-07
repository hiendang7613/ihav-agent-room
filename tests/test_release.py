"""Stable launcher and active release pointer: a new release reaches running sessions without a restart."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room import __version__
from ihav_agent_room.common import HOST_GATEWAYS, RoomError
from ihav_agent_room.globalspace import GlobalSpace
from ihav_agent_room.release import activate, active_release, inspect_root, pointer_path
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store

SOURCE = Path(__file__).resolve().parent.parent


class ReleaseFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="release home ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.env = dict(os.environ, HOME=str(self.home))
        self.env.pop("IHAV_HOME", None)
        self.env.pop("IHAV_AGENT_ROOM_PIN", None)
        patcher = patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("IHAV_HOME", None)

    def install(self, version, host=".claude", protocol=1):
        """A copy of this checkout under a host plugin cache, reporting `version`."""
        root = self.home / host / "plugins" / "cache" / "ihav" / "ihav-agent-room" / version
        shutil.copytree(SOURCE / "ihav_agent_room", root / "ihav_agent_room", ignore=shutil.ignore_patterns("__pycache__"))
        for folder in ("bin", "resources", "templates", "skills", "hooks", ".claude-plugin"):
            shutil.copytree(SOURCE / folder, root / folder)
        package = root / "ihav_agent_room" / "__init__.py"
        text = package.read_text().replace(f'__version__ = "{__version__}"', f'__version__ = "{version}"')
        package.write_text(text.replace("LAUNCHER_PROTOCOL = 1", f"LAUNCHER_PROTOCOL = {protocol}"))
        return root

    def version_via(self, root, **env):
        result = subprocess.run([sys.executable, str(root / "bin" / "ihav-agent-room"), "--version"],
                                env=dict(self.env, **env), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


class LauncherTests(ReleaseFixture, unittest.TestCase):
    def test_an_old_installed_launcher_runs_the_activated_release_without_restart(self):
        old, new = self.install("9.0.0"), self.install("9.1.0")
        self.assertEqual(self.version_via(old), "9.0.0")
        activate(str(new))
        self.assertEqual(self.version_via(old), "9.1.0")  # Same launcher path, next call: new code.
        self.assertEqual(self.version_via(old, IHAV_AGENT_ROOM_PIN="1"), "9.0.0")

    def test_rollback_restores_the_previous_release(self):
        old, new = self.install("9.0.0"), self.install("9.1.0")
        activate(str(old))
        activate(str(new))
        result = activate(rollback=True)
        self.assertEqual((result["activated"], self.version_via(new)), ("9.0.0", "9.0.0"))

    def test_repeating_the_current_activation_keeps_the_rollback_target(self):
        old, new = self.install("9.0.0"), self.install("9.1.0")
        activate(str(old))
        activate(str(new))
        self.assertTrue(activate(str(new))["unchanged"])
        self.assertEqual(activate(rollback=True)["activated"], "9.0.0")

    def test_a_non_object_pointer_is_reported_and_can_be_replaced(self):
        new = self.install("9.1.0")
        pointer_path().parent.mkdir(parents=True)
        for content in ('["invalid"]', '{"root": 3, "previous": "x", "launcher_protocol": 1}'):
            with self.subTest(content=content):
                pointer_path().write_text(content)
                shown = activate()
                self.assertEqual((shown["pointer_state"], shown["active"], shown["previous"]), ("invalid", None, None))
                with self.assertRaises(RoomError):
                    activate(rollback=True)
                self.assertEqual(activate(str(new))["activated"], "9.1.0")

    def test_a_development_checkout_ignores_the_pointer(self):
        activate(str(self.install("9.1.0")))
        self.assertEqual(self.version_via(SOURCE), __version__)

    def test_bad_pointers_fall_back_to_the_launchers_own_copy(self):
        old = self.install("9.0.0")
        outside = Path(tempfile.mkdtemp(prefix="outside ")).resolve()
        self.addCleanup(shutil.rmtree, outside)
        shutil.copytree(old, outside / "copy")
        link = self.home / ".claude" / "plugins" / "cache" / "ihav" / "link"
        link.symlink_to(self.install("9.2.0"))
        pointer_path().parent.mkdir(parents=True)
        for label, record in (("not json", None), ("outside caches", {"root": str(outside / "copy")}),
                              ("symlinked root", {"root": str(link)}), ("missing root", {"root": str(self.home / "gone")}),
                              ("other protocol", {"root": str(self.install("9.3.0", protocol=2))})):
            with self.subTest(label):
                pointer_path().write_text("{" if record is None else json.dumps(record | {"launcher_protocol": 1}))
                self.assertEqual(self.version_via(old), "9.0.0")
                self.assertIsNone(active_release())

    def test_activation_refuses_unsafe_or_incompatible_roots_and_keeps_the_pointer(self):
        good = self.install("9.0.0")
        activate(str(good))
        before = pointer_path().read_text()
        for root in (str(SOURCE), str(self.install("9.3.0", protocol=2)), "relative/path"):
            with self.subTest(root=root), self.assertRaises(RoomError):
                activate(root)
        broken = self.install("9.4.0")
        (broken / "ihav_agent_room" / "cli.py").write_text("raise SystemExit(3)\n")
        with self.assertRaises(RoomError):
            activate(str(broken))  # The smoke check runs the copy before any session does.
        self.assertEqual(pointer_path().read_text(), before)
        self.assertEqual(inspect_root(good)["version"], "9.0.0")

    def test_a_codex_cache_copy_is_also_allowed(self):
        activate(str(self.install("9.5.0", host=".codex")))
        self.assertEqual(active_release()["version"], "9.5.0")

    def test_only_the_gateway_may_switch_the_release_through_the_cli(self):
        old, new = self.install("9.0.0"), self.install("9.1.0")
        for host, gateway in HOST_GATEWAYS.items():
            for member in ("CLAUDE_01", "CODEX_01", "CLAUDE_EXPERT", "CODEX_EXPERT"):
                for binding in ("", "worker-binding"):
                    with self.subTest(host=host, member=member, binding=binding):
                        activate(str(old))
                        before = pointer_path().read_text()
                        ok = member == gateway and not binding
                        env = dict(self.env, IHAV_AGENT_ROOM_HOST=host,
                                   IHAV_AGENT_ROOM_MEMBER=member, IHAV_AGENT_ROOM_BINDING=binding)
                        result = subprocess.run(
                            [sys.executable, str(SOURCE / "bin" / "ihav-agent-room"), "--json", "activate", "--root", str(new)],
                            env=env, capture_output=True, text=True, timeout=60)
                        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
                        if ok:  # Both host gateways announce the same qualified activation.
                            entry = json.loads(result.stdout)["data"]["announced"]
                            ledger = GlobalSpace(self.home / ".ihav" / "agents_space")
                            self.assertEqual(ledger.show(entry)["subject"], "ihav-agent-room 9.1.0 is active")
                        else:
                            self.assertEqual(json.loads(result.stdout)["error"]["code"], "authority")
                            self.assertEqual(pointer_path().read_text(), before)

    def test_bound_workers_cannot_roll_back_the_release(self):
        old, new = self.install("9.0.0"), self.install("9.1.0")
        activate(str(old))
        activate(str(new))
        before = pointer_path().read_text()
        for host, member in HOST_GATEWAYS.items():
            with self.subTest(host=host):
                result = subprocess.run(
                    [sys.executable, str(SOURCE / "bin" / "ihav-agent-room"), "--json", "activate", "--rollback"],
                    env=dict(self.env, IHAV_AGENT_ROOM_HOST=host, IHAV_AGENT_ROOM_MEMBER=member,
                             IHAV_AGENT_ROOM_BINDING="worker-binding"), capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], "authority")
                self.assertEqual(pointer_path().read_text(), before)


class SupervisorUpgradeTests(ReleaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / "project"
        self.project.mkdir()
        initialize(self.project, "pair")
        self.store = Store(self.project)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation="g1", manual_stop=False)
            self.store.put_room(db, room)
        self.supervisor = Supervisor(self.store, "g1")
        self.follows = patch("ihav_agent_room.runtime.follows_pointer", return_value=True)  # Act as an installed copy.
        self.follows.start()
        self.addCleanup(patch.stopall)

    def transition(self):
        return self.store.room().get("mode_transition")

    def test_a_newer_active_release_drains_the_room_for_an_exact_session_restart(self):
        activate(str(self.install("9.1.0")))
        self.supervisor.request_upgrade()
        room = self.store.room()
        self.assertTrue(room["restart_requested"])
        self.assertEqual((self.transition()["reason"], self.transition()["release"], self.transition()["to"]), ("upgrade", "9.1.0", "pair"))

    def test_the_same_version_a_manual_stop_or_a_running_transition_changes_nothing(self):
        activate(str(self.install(__version__)))
        self.supervisor.request_upgrade()
        self.assertIsNone(self.transition())
        activate(str(self.install("9.1.0")))
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["manual_stop"] = True
            self.store.put_room(db, room)
        self.supervisor.next_release_check = 0
        self.supervisor.request_upgrade()
        self.assertIsNone(self.transition())

    def test_a_development_or_pinned_supervisor_never_schedules_an_upgrade(self):
        """Its launcher would restart into the same copy, so a drain would loop without progress (review M-1d10513c)."""
        self.follows.stop()
        activate(str(self.install("9.1.0")))
        for env in ({}, {"IHAV_AGENT_ROOM_PIN": "1"}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.supervisor.next_release_check = 0
                self.supervisor.request_upgrade()
                self.assertIsNone(self.transition())
        self.follows.start()

    def test_the_check_is_throttled(self):
        self.supervisor.request_upgrade()
        activate(str(self.install("9.1.0")))
        self.supervisor.request_upgrade()  # Within 30 s of the last check: not read yet.
        self.assertIsNone(self.transition())


if __name__ == "__main__":
    unittest.main()
