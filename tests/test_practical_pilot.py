"""Local preparation checks; no model or native CLI is executed."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import PLUGIN_ROOT
from scripts.prepare_practical_pilot import EDIT_SCOPE, SCOPE, prepare, selected_files, transfer_probes


class PracticalPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="practical pilot ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_preview_from_roomless_cwd_without_native_programs_has_no_effect(self):
        result = subprocess.run([sys.executable, str(PLUGIN_ROOT / "scripts/prepare_practical_pilot.py")],
                                cwd=self.root, env=dict(os.environ, PATH=""),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["prepared"])
        self.assertFalse(report["proposed_scope"]["native_executed"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_preparation_copies_exact_inputs_preserves_source_and_freezes_probes_outside_project(self):
        before = selected_files(PLUGIN_ROOT)
        result = prepare(self.root / "coding space")
        self.assertEqual(result["scope"], SCOPE)
        base = Path(result["destination"])
        record = json.loads(Path(result["record"]).read_text())
        self.assertEqual(record["native_status"], "not executed")
        self.assertIsNone(record["task_id"])
        self.assertEqual(record["scope"]["source_edit_scope"], EDIT_SCOPE)
        self.assertEqual(selected_files(PLUGIN_ROOT), before)
        for name, expected in record["output_sha256"].items():
            self.assertEqual(hashlib.sha256((base / name).read_bytes()).hexdigest(), expected, name)
        actual = {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()}
        self.assertEqual(actual, record["output_sha256"].keys() | {"preparation.json"})
        self.assertFalse((base / "project/agents_space").exists())
        self.assertFalse((base / "project/observer").exists())
        self.assertFalse((base / "project/transfer").exists())
        self.assertFalse((base / "project/ref_repos").exists())
        self.assertFalse((base / "project/pilots").exists())
        self.assertEqual((base / "project/reference/verify_package.py").read_bytes(),
                         before["pilots/zip_manifest_audit/verify_package.py"][0])
        self.assertEqual(stat.S_IMODE((base / "project/bin/ihav-agent-room").stat().st_mode),
                         stat.S_IMODE((PLUGIN_ROOT / "bin/ihav-agent-room").stat().st_mode))

    def test_existing_empty_nonempty_and_symlink_destinations_are_preserved(self):
        empty = self.root / "empty"
        empty.mkdir()
        full = self.root / "full"
        full.mkdir()
        (full / "keep").write_bytes(b"untouched")
        link = self.root / "link"
        link.symlink_to(self.root / "absent")
        for destination in (empty, full, link):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "must be new"):
                prepare(destination)
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual((full / "keep").read_bytes(), b"untouched")
        self.assertTrue(link.is_symlink())

    def source_copy(self):
        source = self.root / "source"
        source.mkdir()
        for name, (content, mode) in selected_files(PLUGIN_ROOT).items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(mode)
        return source

    def test_missing_reference_fails_before_creating_destination(self):
        source = self.source_copy()
        (source / "pilots/zip_manifest_audit/verify_package.py").unlink()
        destination = self.root / "new"
        with self.assertRaisesRegex(ValueError, "regular file"):
            prepare(destination, source)
        self.assertFalse(destination.exists())

    def test_symlink_input_and_parent_are_rejected_before_any_output(self):
        source = self.source_copy()
        input_file = source / "resources/distribution-readme.md"
        input_file.unlink()
        input_file.symlink_to(PLUGIN_ROOT / "resources/distribution-readme.md")
        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare(self.root / "new", source)
        self.assertFalse((self.root / "new").exists())
        input_file.unlink()
        shutil.rmtree(source / "resources")
        (source / "resources").symlink_to(PLUGIN_ROOT / "resources", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare(self.root / "new", source)
        self.assertFalse((self.root / "new").exists())

    def test_partial_write_has_no_completion_record_and_cannot_be_overwritten(self):
        destination = self.root / "interrupted"
        with patch("scripts.prepare_practical_pilot.Path.chmod", side_effect=OSError("fixture disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                prepare(destination)
        self.assertTrue(destination.exists())
        self.assertFalse((destination / "preparation.json").exists())
        with self.assertRaisesRegex(ValueError, "must be new"):
            prepare(destination)

    def test_cli_failure_is_json_and_does_not_replace_existing_output(self):
        result = subprocess.run([sys.executable, str(PLUGIN_ROOT / "scripts/prepare_practical_pilot.py"),
                                 "--prepare", str(self.root)], cwd=self.root,
                                env=dict(os.environ, PATH=""), text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_frozen_probe_bytes_have_the_expected_reference_verifier_outcomes(self):
        probes = transfer_probes()
        self.assertEqual(probes, transfer_probes())
        for name, data in probes.items():
            path = self.root / name
            path.write_bytes(data)
            before = path.read_bytes()
            result = subprocess.run([sys.executable, str(PLUGIN_ROOT / "pilots/zip_manifest_audit/verify_package.py"), str(path)],
                                    cwd=self.root, env=dict(os.environ, PATH=""), text=True,
                                    capture_output=True, timeout=5)
            self.assertEqual(path.read_bytes(), before)
            if name == "probe-1.zip":
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["files_checked"], 1)
            else:
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("size mismatch", result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
