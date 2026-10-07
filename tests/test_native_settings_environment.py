"""A daemon-hosted Claude resume must receive the explicit root tombstones."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.native import start_claude


SESSION = "11111111-2222-3333-4444-555555555555"


class SuccessfulLaunch:
    returncode = 0

    async def wait(self):
        return 0


class NativeSettingsEnvironmentTests(unittest.TestCase):
    def launch(self, directory, resume, pin):
        project = Path(directory).resolve()
        log = project / "CLAUDE_01.log"
        env = {"PATH": "/usr/bin", "IHAV_AGENT_ROOM_BINDING": "test-binding",
               "IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01", "IHAV_AGENT_ROOM_HOST": "claude",
               "IHAV_AGENT_ROOM_PIN": pin, "PLUGIN_ROOT": "", "CLAUDE_PLUGIN_ROOT": "",
               "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF": "1", "UNRELATED_SETTING": "retained-in-child"}
        captured = {}

        async def launched(*args, **kwargs):
            captured["args"] = args
            captured["child_env"] = kwargs["env"]
            captured["settings"] = json.loads(log.with_suffix(".settings.json").read_text())
            kwargs["stdout"].write(f"backgrounded · 1a2b3c4d session {SESSION}\n")
            kwargs["stdout"].flush()
            return SuccessfulLaunch()

        live = {"sessionId": SESSION, "cwd": str(project), "pid": 4242, "id": "1a2b3c4d"}
        with patch("ihav_agent_room.native.claude_agents", side_effect=[[], [live]]), \
                patch("ihav_agent_room.native.asyncio.create_subprocess_exec", new=launched), \
                patch("ihav_agent_room.native.process_stamp", return_value="stamp"):
            found = asyncio.run(start_claude(project, SESSION if resume else None, resume, env, log,
                member="CLAUDE_01", model="opus", effort="medium"))
        self.assertEqual(found["sessionId"], SESSION)
        self.assertEqual(captured["child_env"], env)
        return captured

    def test_explicit_empty_roots_reach_serialized_settings_on_fresh_and_exact_resume(self):
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as directory:
                captured = self.launch(directory, resume, "")
                settings = captured["settings"]
                self.assertEqual(settings["env"]["PLUGIN_ROOT"], "")
                self.assertEqual(settings["env"]["CLAUDE_PLUGIN_ROOT"], "")
                self.assertEqual(settings["env"]["IHAV_AGENT_ROOM_PIN"], "")
                self.assertEqual(settings["env"]["IHAV_AGENT_ROOM_BINDING"], "test-binding")
                self.assertEqual((settings["model"], settings["effortLevel"]), ("opus", "medium"))
                self.assertEqual(settings["worktree"], {"bgIsolation": "none"})
                self.assertNotIn("UNRELATED_SETTING", settings["env"])
                self.assertNotIn("PATH", settings["env"])
                if resume:
                    self.assertEqual(captured["args"], ("claude", "--bg", "--resume", SESSION))

    def test_deliberate_supervisor_pin_remains_in_resume_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            captured = self.launch(directory, True, "1")
        self.assertEqual(captured["settings"]["env"]["IHAV_AGENT_ROOM_PIN"], "1")
        self.assertEqual(captured["settings"]["env"]["PLUGIN_ROOT"], "")
        self.assertEqual(captured["settings"]["env"]["CLAUDE_PLUGIN_ROOT"], "")
