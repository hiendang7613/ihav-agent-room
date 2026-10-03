"""Current references stay available across upgrades without replacing room rules."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ihav_agent_room import __version__
from ihav_agent_room.cli import main, parser, run
from ihav_agent_room.common import PLUGIN_ROOT, RoomError
from ihav_agent_room.package import build
from ihav_agent_room.scaffold import initialize


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent room guides $(literal) ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def call(self, *args):
        return subprocess.run([sys.executable, str(PLUGIN_ROOT / "bin/ihav-agent-room"),
                               "--project", str(self.project), "--json", "guide", *args],
                              cwd=self.root, env=dict(os.environ, PATH="", IHAV_AGENT_ROOM_MEMBER="unbound"),
                              capture_output=True, text=True, timeout=5)

    def test_catalog_needs_no_project_or_resource_read_and_contains_only_pointers(self):
        with patch("ihav_agent_room.guides.PLUGIN_ROOT", self.root / "missing-plugin"), \
                patch("ihav_agent_room.cli.Store", side_effect=AssertionError("Guide opened a room")):
            result = run(parser().parse_args(["--project", str(self.root / "missing-project"), "guide"]))
        self.assertEqual(result["plugin_version"], __version__)
        self.assertEqual({item["topic"] for item in result["topics"]},
                         {"collaboration", "learning", "evidence", "cli", "response-style"})
        self.assertNotIn("content", result)
        self.assertLess(len(json.dumps(result)), 1000)
        self.assertFalse((self.root / "missing-project").exists())
        for item in result["topics"]:
            self.assertEqual(item["read_command"], "ihav-agent-room guide " + item["topic"])

    def test_selected_topic_reads_exact_utf8_bytes_without_requiring_other_guides(self):
        shipped = self.root / "payload/templates/conventions/learning.md"
        shipped.parent.mkdir(parents=True)
        content = "# Học cùng nhau\r\nGiữ điều kiện và phản chứng.\r\n".encode("utf-8")
        shipped.write_bytes(content)
        with patch("ihav_agent_room.guides.PLUGIN_ROOT", self.root / "payload"), \
                patch("ihav_agent_room.cli.Store", side_effect=AssertionError("Guide opened a room")):
            result = run(parser().parse_args(["guide", "learning"]))
        self.assertEqual(result["content"], content.decode("utf-8"))
        self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(result["source"], "templates/conventions/learning.md")
        self.assertEqual(result["topic"], "learning")

    def test_real_cli_reads_every_topic_without_native_tools_or_room(self):
        catalog = self.call()
        self.assertEqual(catalog.returncode, 0, catalog.stderr + catalog.stdout)
        for item in json.loads(catalog.stdout)["data"]["topics"]:
            result = self.call(item["topic"])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            guide = json.loads(result.stdout)["data"]
            self.assertTrue(guide["content"].startswith("# "))
            self.assertEqual(guide["sha256"], hashlib.sha256(guide["content"].encode("utf-8")).hexdigest())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_upgrade_keeps_local_rules_and_reads_current_reference_without_ledger_effects(self):
        initialize(self.project)
        guide = self.project / "agents_space/conventions/collaboration.md"
        guide.write_bytes(b"# Old room guide\r\nOur custom discussion rule stays in force.\r\n")
        local = guide.read_bytes()
        initialize(self.project)
        self.assertEqual(guide.read_bytes(), local)
        before = {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        result = self.call("collaboration")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(result.stdout)["data"]
        self.assertIn("ihav-agent-room note search", data["content"])
        self.assertNotIn("Our custom discussion rule", data["content"])
        self.assertIn("does not replace project-specific instructions", data["rule"])
        self.assertEqual({p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}, before)

    def test_invalid_topics_are_argument_errors_without_path_reads(self):
        for topic in ("../README.md", str(self.root / "secret.txt"), "unknown", ""):
            with self.subTest(topic=topic):
                result = self.call(topic)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], "arguments")
        self.assertEqual(list(self.project.iterdir()), [])

    def test_missing_or_invalid_resource_is_an_error_without_local_fallback(self):
        local = self.project / "agents_space/conventions/learning.md"
        local.parent.mkdir(parents=True)
        local.write_text("# Stale local fallback\n")
        shipped = self.root / "payload/templates/conventions/learning.md"
        shipped.parent.mkdir(parents=True)
        for content in (None, b"\xff"):
            if content is not None:
                shipped.write_bytes(content)
            with patch("ihav_agent_room.guides.PLUGIN_ROOT", self.root / "payload"), redirect_stdout(io.StringIO()) as output:
                code = main(["--project", str(self.project), "--json", "guide", "learning"])
            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "local_error")
            self.assertNotIn("data", result)
        self.assertEqual(local.read_text(), "# Stale local fallback\n")

    def test_package_rejects_a_missing_catalog_resource_before_publishing_an_archive(self):
        archive = self.root / "complete.zip"
        build(archive)
        with zipfile.ZipFile(archive) as package:
            package.extractall(self.root / "package")
        copied = self.root / "package/ihav-agent-room"
        (copied / "templates/conventions/evidence.md").unlink()
        target = self.root / "incomplete.zip"
        with self.assertRaises(RoomError) as caught:
            build(target, copied)
        self.assertEqual(caught.exception.code, "package")
        self.assertIn("templates/conventions/evidence.md", caught.exception.details["missing"])
        self.assertFalse(target.exists())

    def test_every_example_command_in_the_guides_parses_with_the_real_cli(self):
        """A guide that shows a command the CLI no longer accepts costs the operator a failed call and a retry."""
        files = sorted((PLUGIN_ROOT / "templates/conventions").glob("*.md")) + sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
        checked, rejected = 0, []
        for path in files:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                match = re.match(r"^\s{0,8}(ihav-agent-room\s+.+)$", line)
                if not match:
                    continue
                command = re.sub(r"--expected-version N\b", "--expected-version 1", match.group(1).split("#")[0].rstrip())
                if any(mark in command for mark in ("<", "...", "|")) or command.endswith("\\"):
                    continue  # Placeholders, pipelines and continued lines are not complete commands.
                try:
                    argv = shlex.split(command)[1:]
                except ValueError:
                    continue
                checked += 1
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        parser().parse_args(["--project", "/project", *argv])
                    except SystemExit as stop:
                        if stop.code:
                            rejected.append(f"{path.relative_to(PLUGIN_ROOT)}:{number}: {command}")
        self.assertGreaterEqual(checked, 25, "the check must see the guides' examples, not pass vacuously")
        self.assertEqual(rejected, [])


if __name__ == "__main__":
    unittest.main()
