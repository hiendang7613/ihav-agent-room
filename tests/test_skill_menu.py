"""The distributed menu has six entry points; CLI maintenance remains available."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ihav_agent_room.cli import parser
from ihav_agent_room.common import PLUGIN_ROOT, RoomError
from ihav_agent_room.hooks import handle
from ihav_agent_room.package import build
from ihav_agent_room.package_verifier import verify_archive


class SkillMenuTests(unittest.TestCase):
    def test_archive_exposes_only_six_skills_and_keeps_cli_maintenance(self):
        expected = {"start", "status", "stop", "mode", "effort", "doctor"}
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "plugin.zip"
            build(archive)
            verified = verify_archive(archive)
            self.assertGreater(verified["files_checked"], 0)
            with zipfile.ZipFile(archive) as package:
                skills = {Path(name).parts[2] for name in package.namelist()
                          if name.startswith("ihav-agent-room/skills/") and name.endswith("/SKILL.md")}
                self.assertEqual(skills, expected)
                manifest = json.loads(package.read("ihav-agent-room/PACKAGE-MANIFEST.json"))
                self.assertEqual({name.split("/")[1] for name in manifest["files_sha256"]
                                  if name.startswith("skills/")}, expected)
                for host in ("claude", "codex"):
                    self.assertIn(f"ihav-agent-room/hooks/{'hooks' if host == 'claude' else host}.json",
                                  package.namelist())
            for command in (("init", "--no-start"), ("connect", "--check")):
                parsed = parser().parse_args(list(command))
                self.assertEqual(parsed.command, command[0])

    def test_builder_rejects_an_extra_removed_skill_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete = root / "complete.zip"
            build(complete)
            with zipfile.ZipFile(complete) as package:
                package.extractall(root / "copied")
            source = root / "copied/ihav-agent-room"
            extra = source / "skills/init/SKILL.md"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("---\nname: init\ndescription: Legacy setup\n---\n")
            output = root / "invalid.zip"
            with self.assertRaises(RoomError) as caught:
                build(output, source)
            self.assertEqual(caught.exception.code, "package")
            self.assertEqual(caught.exception.details["unexpected"], ["skills/init/SKILL.md"])
            self.assertFalse(output.exists())

    def test_builder_requires_every_public_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete = root / "complete.zip"
            build(complete)
            with zipfile.ZipFile(complete) as package:
                package.extractall(root / "copied")
            source = root / "copied/ihav-agent-room"
            (source / "skills/effort/SKILL.md").unlink()
            output = root / "incomplete.zip"
            with self.assertRaises(RoomError) as caught:
                build(output, source)
            self.assertEqual(caught.exception.code, "package")
            self.assertIn("skills/effort/SKILL.md", caught.exception.details["missing"])
            self.assertFalse(output.exists())

    def test_fresh_host_hooks_route_to_start_and_do_not_install_a_personal_alias(self):
        for host, entry in (("claude", "/ihav-agent-room:start"), ("codex", "$ihav-agent-room:start")):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": host}, clear=True), \
                    patch("ihav_agent_room.scaffold.install_alias") as install:
                result = handle({"hook_event_name": "SessionStart", "session_id": "new-host", "cwd": directory})
                instruction = result["hookSpecificOutput"]["additionalContext"]
                self.assertIn(entry, instruction)
                self.assertNotIn("ihav-agent-room:init", instruction)
                install.assert_not_called()
                self.assertFalse((Path(directory) / "agents_space").exists())


if __name__ == "__main__":
    unittest.main()
