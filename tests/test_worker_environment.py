"""Native children must not inherit their supervisor's plugin identity."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.store import Store


class WorkerEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="worker environment ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        initialize(self.project)
        self.store = Store(self.project)
        self.supervisor = Supervisor(self.store, "worker-env-generation")

    def test_both_hosts_discard_ambient_plugin_roots_without_changing_parent(self):
        for member, host in (("CLAUDE_01", "claude"), ("CODEX_01", "codex")):
            with self.subTest(host=host):
                parent = {"HOME": str(self.project), "PATH": "/fixture/bin",
                          "PLUGIN_ROOT": "/foreign/codex/cache/agent-room/0.8.1",
                          "CLAUDE_PLUGIN_ROOT": "/foreign/claude/cache/another-plugin",
                          "IHAV_AGENT_ROOM_HOST": "parent-host",
                          "TASK_SENTINEL": "keep-this-value"}
                with patch.dict(os.environ, parent, clear=True):
                    child = self.supervisor.worker_env(member)
                    self.assertEqual(dict(os.environ), parent)
                self.assertEqual(child["PLUGIN_ROOT"], "")
                self.assertEqual(child["CLAUDE_PLUGIN_ROOT"], "")
                self.assertEqual(child["IHAV_AGENT_ROOM_HOST"], host)
                self.assertEqual(child["TASK_SENTINEL"], "keep-this-value")
                self.assertTrue(child["PATH"].endswith(os.pathsep + "/fixture/bin"))

    def test_child_binding_keeps_saved_identity_and_permission_state_for_both_hosts(self):
        for member in ("CLAUDE_01", "CODEX_01"):
            with self.subTest(member=member):
                saved = {"native_id": "saved-" + member, "status": "idle",
                         "permission_mode": "default", "pid": None}
                self.store.member(member, saved)
                with patch.dict(os.environ, {"IHAV_AGENT_ROOM_BINDING": "parent-binding",
                                             "IHAV_AGENT_ROOM_SESSION_ID": "parent-session",
                                             "CLAUDE_CODE_SESSION_ID": "parent-claude",
                                             "CODEX_THREAD_ID": "parent-codex"}, clear=True):
                    child = self.supervisor.worker_env(member)
                self.assertEqual(child["IHAV_AGENT_ROOM_MEMBER"], member)
                self.assertEqual(child["IHAV_AGENT_ROOM_PROJECT"], str(self.project))
                self.assertNotEqual(child["IHAV_AGENT_ROOM_BINDING"], "parent-binding")
                self.assertEqual(self.store.member(member)["token_hash"],
                                 hashlib.sha256(child["IHAV_AGENT_ROOM_BINDING"].encode()).hexdigest())
                for key in ("IHAV_AGENT_ROOM_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
                    self.assertNotIn(key, child)
                for key, value in saved.items():
                    self.assertEqual(self.store.member(member)[key], value)

    def test_unrelated_native_configuration_survives_when_plugin_roots_are_absent(self):
        parent = {"CODEX_HOME": str(self.project / "codex-home"),
                  "CLAUDE_CONFIG_DIR": str(self.project / "claude-home"),
                  "IHAV_HOME": str(self.project / "ihav-home"),
                  "TASK_SENTINEL": "unchanged"}
        with patch.dict(os.environ, parent, clear=True):
            child = self.supervisor.worker_env("CODEX_01")
        for key, value in parent.items():
            self.assertEqual(child[key], value)

    def test_explicit_overlay_clears_a_foreign_spares_roots_and_stale_pin(self):
        spare = {"PLUGIN_ROOT": "/foreign/codex/plugin", "CLAUDE_PLUGIN_ROOT": "/foreign/codex/plugin",
                 "IHAV_AGENT_ROOM_PIN": "1", "SPARE_SENTINEL": "native-value"}
        for member, host in (("CLAUDE_01", "claude"), ("CODEX_01", "codex")):
            with self.subTest(host=host), patch.dict(os.environ, {"PATH": "/fixture/bin"}, clear=True):
                child = self.supervisor.worker_env(member)
                effective = spare | child
                for key in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "IHAV_AGENT_ROOM_PIN"):
                    self.assertEqual(effective[key], "")
                self.assertEqual(effective["IHAV_AGENT_ROOM_HOST"], host)
                self.assertEqual(effective["IHAV_AGENT_ROOM_BINDING"], child["IHAV_AGENT_ROOM_BINDING"])
                self.assertEqual(effective["SPARE_SENTINEL"], "native-value")

    def test_deliberate_supervisor_qualification_pin_is_preserved(self):
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_PIN": "1"}, clear=True):
            child = self.supervisor.worker_env("CLAUDE_01")
        self.assertEqual(child["IHAV_AGENT_ROOM_PIN"], "1")


if __name__ == "__main__":
    unittest.main()
