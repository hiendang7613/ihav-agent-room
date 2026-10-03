import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from ihav_agent_room.package_verifier import VerificationError, verify_archive

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "bin" / "ihav-agent-room"
FIXTURE = PROJECT_ROOT / "dist" / "agent-room-0.3.1.zip"
FIXTURE_SHA256 = "22da094310a5a88f95bd3d8e23ebc12ecf0cf78e395b6e5eb04f6256bb5df28e"
MANIFEST = "ihav-agent-room/PACKAGE-MANIFEST.json"
MIB = 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def manifest_bytes(files, version="1.2.3"):
    return json.dumps(
        {"version": version, "files_sha256": {rel: sha256(data) for rel, data in files.items()}}
    ).encode()


class VerifierTestCase(unittest.TestCase):
    def setUp(self):
        workdir = tempfile.TemporaryDirectory(prefix="package verifier ")
        self.addCleanup(workdir.cleanup)
        self.workdir = Path(workdir.name)
        self.counter = 0

    def write_zip(self, entries, compression=zipfile.ZIP_STORED):
        """entries: (name or ZipInfo, bytes) pairs, written in order."""
        self.counter += 1
        path = self.workdir / f"archive-{self.counter}.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns on intentional duplicate names
            with zipfile.ZipFile(path, "w", compression) as archive:
                for name, data in entries:
                    archive.writestr(name, data)
        return path

    def package(self, files, manifest=None, extra=(), compression=zipfile.ZIP_STORED):
        """Build an archive from payload files plus a manifest (valid by default)."""
        body = manifest_bytes(files) if manifest is None else manifest
        entries = [(MANIFEST, body)]
        entries += [("ihav-agent-room/" + rel, data) for rel, data in files.items()]
        entries += list(extra)
        return self.write_zip(entries, compression)

    def assertRejected(self, path, fragment=None):
        with self.assertRaises(VerificationError) as ctx:
            verify_archive(path)
        if fragment is not None:
            self.assertIn(fragment, str(ctx.exception))
        return ctx.exception

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--json", "verify-package", *map(str, args)],
            capture_output=True,
            text=True,
            cwd=self.workdir,
            env=dict(os.environ, PATH="", IHAV_AGENT_ROOM_SESSION_ID="unbound", IHAV_AGENT_ROOM_MEMBER="CODEX_EXPERT"),
            timeout=5,
            check=False,
        )

    def assertCliError(self, result, code="package", exit_code=1):
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        body = json.loads(result.stdout)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], code)
        self.assertNotIn("Traceback", result.stdout)
        self.assertFalse((self.workdir / "agents_space").exists())


class ReferenceFixtureTests(VerifierTestCase):
    def test_reference_archive_verifies_without_mutation(self):
        before = sha256(FIXTURE.read_bytes())
        self.assertEqual(before, FIXTURE_SHA256)
        self.assertEqual(verify_archive(FIXTURE), {"version": "0.3.1", "files_checked": 32})
        self.assertEqual(sha256(FIXTURE.read_bytes()), FIXTURE_SHA256)

    def test_cli_success_prints_one_json_object(self):
        result = self.run_cli(FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout), {"ok": True, "data": {"version": "0.3.1", "files_checked": 32}}
        )
        self.assertEqual(sha256(FIXTURE.read_bytes()), FIXTURE_SHA256)


class AcceptanceTests(VerifierTestCase):
    def test_empty_payload_set(self):
        path = self.package({})
        self.assertEqual(verify_archive(path), {"version": "1.2.3", "files_checked": 0})

    def test_nested_deflated_payload(self):
        files = {"a.txt": b"alpha", "dir/sub/b.bin": bytes(range(256)), ".hidden/c": b""}
        path = self.package(files, compression=zipfile.ZIP_DEFLATED)
        self.assertEqual(verify_archive(path), {"version": "1.2.3", "files_checked": 3})

    def test_entry_at_size_limit(self):
        path = self.package({"big.bin": bytes(16 * MIB)}, compression=zipfile.ZIP_DEFLATED)
        self.assertEqual(verify_archive(path)["files_checked"], 1)

    def test_entry_count_at_limit(self):
        files = {f"f{i}": str(i).encode() for i in range(255)}
        self.assertEqual(verify_archive(self.package(files))["files_checked"], 255)

    def test_unspecified_unix_mode_is_regular(self):
        info = zipfile.ZipInfo("ihav-agent-room/plain")
        info.external_attr = 0
        files = {"plain": b"data"}
        path = self.write_zip([(MANIFEST, manifest_bytes(files)), (info, b"data")])
        self.assertEqual(verify_archive(path)["files_checked"], 1)


class BoundsTests(VerifierTestCase):
    def assertRejectedWithoutReading(self, path, fragment):
        with mock.patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("read")):
            self.assertRejected(path, fragment)

    def test_too_many_entries(self):
        files = {f"f{i}": b"" for i in range(256)}
        self.assertRejectedWithoutReading(self.package(files), "too many ZIP entries")

    def test_entry_over_size_limit(self):
        path = self.package({"big.bin": bytes(16 * MIB + 1)}, compression=zipfile.ZIP_DEFLATED)
        self.assertRejectedWithoutReading(path, "entry too large")

    def test_total_over_size_limit(self):
        files = {f"part{i}": bytes(13 * MIB) for i in range(5)}
        path = self.package(files, compression=zipfile.ZIP_DEFLATED)
        self.assertRejectedWithoutReading(path, "archive too large")


class ArchiveStructureTests(VerifierTestCase):
    def test_missing_input(self):
        self.assertRejected(self.workdir / "absent.zip")

    def test_directory_input(self):
        self.assertRejected(self.workdir, "not a regular file")

    def test_not_a_zip(self):
        path = self.workdir / "junk.zip"
        path.write_bytes(b"not a zip archive")
        self.assertRejected(path, "cannot read archive")

    def test_truncated_zip(self):
        data = self.package({"a": b"alpha"}).read_bytes()
        path = self.workdir / "truncated.zip"
        path.write_bytes(data[: len(data) // 2])
        self.assertRejected(path)

    def test_corrupt_payload_bytes(self):
        path = self.package({"a": b"A" * 64})
        data = bytearray(path.read_bytes())
        offset = data.index(b"A" * 64)
        data[offset] ^= 0xFF
        path.write_bytes(bytes(data))
        self.assertRejected(path)

    def test_encrypted_entry(self):
        path = self.package({"a": b"alpha"})
        data = bytearray(path.read_bytes())
        central = data.rindex(b"PK\x01\x02")  # last central header: ihav-agent-room/a
        data[central + 8] |= 0x01  # general-purpose flag: encrypted
        path.write_bytes(bytes(data))
        self.assertRejected(path, "encrypted")

    def overstate_size(self, path, name, actual):
        """Raise the local and central declared uncompressed size of one entry by one byte."""
        data = bytearray(path.read_bytes())
        encoded = name.encode()
        for signature, size_offset, name_offset in ((b"PK\x03\x04", 22, 30), (b"PK\x01\x02", 24, 46)):
            start = 0
            while (header := data.find(signature, start)) >= 0:
                start = header + 4
                if data[header + name_offset : header + name_offset + len(encoded)] == encoded:
                    self.assertEqual(int.from_bytes(data[header + size_offset : header + size_offset + 4], "little"), actual)
                    data[header + size_offset : header + size_offset + 4] = (actual + 1).to_bytes(4, "little")
        path.write_bytes(bytes(data))
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.getinfo(name).file_size, actual + 1)

    def test_payload_shorter_than_declared(self):
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                path = self.package({"a": b"alpha"}, compression=compression)
                self.overstate_size(path, "ihav-agent-room/a", 5)
                self.assertRejected(path, "size mismatch")
                self.assertCliError(self.run_cli(path))

    def test_manifest_shorter_than_declared(self):
        body = manifest_bytes({})
        path = self.package({}, manifest=body, compression=zipfile.ZIP_DEFLATED)
        self.overstate_size(path, MANIFEST, len(body))
        self.assertRejected(path, "size mismatch")

    def test_missing_manifest(self):
        self.assertRejected(self.write_zip([("ihav-agent-room/a", b"alpha")]), "missing")

    def test_duplicate_manifest(self):
        body = manifest_bytes({})
        path = self.write_zip([(MANIFEST, body), (MANIFEST, body)])
        self.assertRejected(path, "duplicate ZIP entry")

    def test_duplicate_payload_name(self):
        files = {"a": b"alpha"}
        path = self.package(files, extra=[("ihav-agent-room/a", b"alpha")])
        self.assertRejected(path, "duplicate ZIP entry")

    def test_directory_entry(self):
        self.assertRejected(self.package({}, extra=[("ihav-agent-room/dir/", b"")]))

    def test_unix_directory_mode(self):
        info = zipfile.ZipInfo("ihav-agent-room/dir")
        info.external_attr = (stat.S_IFDIR | 0o755) << 16
        self.assertRejected(self.package({}, extra=[(info, b"")]), "not a regular file")

    def test_msdos_directory_attribute(self):
        info = zipfile.ZipInfo("ihav-agent-room/dir")
        info.external_attr = 0x10
        self.assertRejected(self.package({}, extra=[(info, b"")]), "not a regular file")

    def test_symlink_entry(self):
        info = zipfile.ZipInfo("ihav-agent-room/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        files = {"link": b"../../etc/passwd"}
        path = self.write_zip([(MANIFEST, manifest_bytes(files)), (info, files["link"])])
        self.assertRejected(path, "not a regular file")

    def test_fifo_entry(self):
        info = zipfile.ZipInfo("ihav-agent-room/pipe")
        info.external_attr = (stat.S_IFIFO | 0o644) << 16
        files = {"pipe": b""}
        path = self.write_zip([(MANIFEST, manifest_bytes(files)), (info, b"")])
        self.assertRejected(path, "not a regular file")

    def test_non_canonical_entry_names(self):
        names = [
            "/ihav-agent-room/a",
            "other/a",
            "ihav-agent-room",
            "ihav-agent-room/",
            "ihav-agent-room//a",
            "ihav-agent-room/./a",
            "ihav-agent-room/../a",
            "ihav-agent-room/a/..",
            "ihav-agent-room/a\\b",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertRejected(self.package({}, extra=[(name, b"x")]))

    def test_nul_in_original_entry_name(self):
        # zipfile truncates ZipInfo.filename at NUL; the original spelling must be rejected.
        path = self.package({}, extra=[("ihav-agent-room/aXb", b"x")])
        data = path.read_bytes().replace(b"ihav-agent-room/aXb", b"ihav-agent-room/a\x00b")
        path.write_bytes(data)
        with zipfile.ZipFile(path) as archive:
            self.assertIn("ihav-agent-room/a", archive.namelist())
        self.assertRejected(path, "non-canonical entry path")


class ManifestTests(VerifierTestCase):
    def assertManifestRejected(self, body, fragment=None, files=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return self.assertRejected(self.package(files or {}, manifest=body), fragment)

    def test_invalid_utf8(self):
        self.assertManifestRejected(b'{"version": "\xff", "files_sha256": {}}', "UTF-8")

    def test_invalid_json(self):
        self.assertManifestRejected(b"{not json", "JSON")

    def test_deeply_nested_json(self):
        self.assertManifestRejected(b"[" * 200000 + b"]" * 200000, "JSON")

    def test_oversized_integer(self):
        self.assertManifestRejected(b'{"version": ' + b"9" * 5000 + b"}", "JSON")

    def test_duplicate_top_level_key(self):
        body = b'{"version": "1", "version": "2", "files_sha256": {}}'
        self.assertManifestRejected(body, "duplicate manifest key")

    def test_duplicate_file_key(self):
        digest = sha256(b"a").encode()
        body = (
            b'{"version": "1", "files_sha256": {"a": "' + digest + b'", "a": "' + digest + b'"}}'
        )
        self.assertManifestRejected(body, "duplicate manifest key", files={"a": b"a"})

    def test_wrong_top_level_shape(self):
        bodies = [
            [],
            {"version": "1"},
            {"files_sha256": {}},
            {"version": "1", "files_sha256": {}, "extra": 1},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertManifestRejected(body, "exactly")

    def test_bad_version(self):
        for version in ["", 1, None, ["1"]]:
            with self.subTest(version=version):
                self.assertManifestRejected({"version": version, "files_sha256": {}}, "version")

    def test_files_not_object(self):
        self.assertManifestRejected({"version": "1", "files_sha256": []}, "files_sha256")

    def test_bad_digests(self):
        good = sha256(b"a")
        for digest in [good.upper(), good[:-1], good + "0", "g" * 64, None, 1]:
            with self.subTest(digest=digest):
                body = {"version": "1", "files_sha256": {"a": digest}}
                self.assertManifestRejected(body, "invalid SHA-256", files={"a": b"a"})

    def test_manifest_lists_itself(self):
        body = {"version": "1", "files_sha256": {"PACKAGE-MANIFEST.json": sha256(b"")}}
        self.assertManifestRejected(body, "lists itself")

    def test_non_canonical_manifest_keys(self):
        for key in ["/a", "a//b", "./a", "../a", "a/..", "a\\b", "", "a/"]:
            with self.subTest(key=key):
                body = {"version": "1", "files_sha256": {key: sha256(b"a")}}
                self.assertManifestRejected(body, "non-canonical manifest path")

    def test_missing_payload(self):
        body = {"version": "1", "files_sha256": {"a": sha256(b"a")}}
        self.assertManifestRejected(body, "missing from archive")

    def test_extra_payload(self):
        path = self.package({}, extra=[("ihav-agent-room/extra", b"x")])
        self.assertRejected(path, "not in manifest")

    def test_hash_mismatch(self):
        body = {"version": "1", "files_sha256": {"a": sha256(b"other")}}
        self.assertManifestRejected(body, "SHA-256 mismatch", files={"a": b"a"})


class CliTests(VerifierTestCase):
    def test_missing_file(self):
        self.assertCliError(self.run_cli(self.workdir / "absent.zip"))

    def test_newline_in_path(self):
        self.assertCliError(self.run_cli(self.workdir / "bad\nname.zip"))

    def test_invalid_archive(self):
        path = self.workdir / "junk.zip"
        path.write_bytes(b"junk")
        self.assertCliError(self.run_cli(path))

    def test_hash_mismatch(self):
        body = json.dumps({"version": "1", "files_sha256": {"a": sha256(b"b")}}).encode()
        self.assertCliError(self.run_cli(self.package({"a": b"a"}, manifest=body)))

    def test_wrong_argument_count(self):
        self.assertCliError(self.run_cli(), code="arguments", exit_code=2)
        self.assertCliError(self.run_cli("a.zip", "b.zip"), code="arguments", exit_code=2)


if __name__ == "__main__":
    unittest.main()
