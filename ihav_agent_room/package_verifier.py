"""Verify an Agent Room distribution archive against its package manifest.

This is integrity checking, not publisher authentication: it confirms that the
archive matches its own manifest. Entries are hashed in memory and never
extracted or executed.
"""

import hashlib
import json
import lzma
import os
import re
import stat
import zipfile
import zlib

from ihav_agent_room.common import RoomError

ROOT = "ihav-agent-room/"
# Releases up to 0.3.25 shipped as Agent Room; their archives keep the old root.
LEGACY_ROOT = "agent-room/"
MANIFEST_KEY = "PACKAGE-MANIFEST.json"
MANIFEST_NAME = ROOT + MANIFEST_KEY
MANIFEST_FIELDS = {"version", "files_sha256"}
MAX_ENTRIES = 256
MAX_ENTRY_SIZE = 16 * 1024 * 1024
MAX_TOTAL_SIZE = 64 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
MSDOS_DIRECTORY = 0x10

# zipfile and its decompressors report unreadable or corrupt input through
# several exception types; all of them mean the archive cannot be verified.
ARCHIVE_ERRORS = (
    OSError,
    EOFError,
    ValueError,
    RuntimeError,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    lzma.LZMAError,
)


class VerificationError(RoomError):
    """The archive is unreadable, malformed, or does not match its manifest."""

    def __init__(self, message):
        super().__init__(message, "package")


def verify_archive(path):
    """Return {"version", "files_checked"} or raise VerificationError."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise VerificationError(f"not a regular file: {os.fspath(path)!r}")
        with zipfile.ZipFile(path) as archive:
            return _verify(archive)
    except VerificationError:
        raise
    except ARCHIVE_ERRORS as exc:
        raise VerificationError(f"cannot read archive: {exc}") from exc


def _verify(archive):
    entries = archive.infolist()
    _check_bounds(entries)

    root = LEGACY_ROOT if entries and entries[0].orig_filename.startswith(LEGACY_ROOT) else ROOT
    manifest_name = root + MANIFEST_KEY
    manifest_entry = None
    payload = {}
    seen = set()
    for entry in entries:
        name = entry.orig_filename
        if name in seen:
            raise VerificationError(f"duplicate ZIP entry: {name!r}")
        seen.add(name)
        if not name.startswith(root):
            raise VerificationError(f"entry outside {root!r}: {name!r}")
        _check_path(name, "entry")
        _check_regular_file(entry)
        if name == manifest_name:
            manifest_entry = entry
        else:
            payload[name[len(root):]] = entry
    if manifest_entry is None:
        raise VerificationError(f"missing {manifest_name}")

    version, expected = _read_manifest(archive, manifest_entry)
    missing = sorted(expected.keys() - payload.keys())
    if missing:
        raise VerificationError(f"manifest files missing from archive: {missing!r}")
    extra = sorted(payload.keys() - expected.keys())
    if extra:
        raise VerificationError(f"archive files not in manifest: {extra!r}")
    for rel, entry in payload.items():
        if _sha256(archive, entry) != expected[rel]:
            raise VerificationError(f"SHA-256 mismatch: {rel!r}")
    return {"version": version, "files_checked": len(payload)}


def _check_bounds(entries):
    if len(entries) > MAX_ENTRIES:
        raise VerificationError(f"too many ZIP entries: {len(entries)} > {MAX_ENTRIES}")
    total = 0
    for entry in entries:
        if entry.file_size > MAX_ENTRY_SIZE:
            raise VerificationError(
                f"entry too large: {entry.orig_filename!r} declares {entry.file_size} bytes"
            )
        total += entry.file_size
    if total > MAX_TOTAL_SIZE:
        raise VerificationError(f"archive too large: {total} declared uncompressed bytes")


def _check_path(path, kind):
    if "\\" in path or "\x00" in path or any(
        segment in ("", ".", "..") for segment in path.split("/")
    ):
        raise VerificationError(f"non-canonical {kind} path: {path!r}")


def _check_regular_file(entry):
    mode = entry.external_attr >> 16
    if (
        entry.is_dir()
        or entry.external_attr & MSDOS_DIRECTORY
        or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))
    ):
        raise VerificationError(f"not a regular file entry: {entry.orig_filename!r}")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError(f"duplicate manifest key: {key!r}")
        result[key] = value
    return result


def _read_manifest(archive, entry):
    raw = archive.read(entry)
    _check_size(entry, len(raw))
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise VerificationError(f"manifest is not valid UTF-8: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        raise VerificationError(f"manifest is not valid JSON: {exc}") from exc

    if not isinstance(manifest, dict) or manifest.keys() != MANIFEST_FIELDS:
        raise VerificationError("manifest must be an object with exactly 'version' and 'files_sha256'")
    version = manifest["version"]
    files = manifest["files_sha256"]
    if not isinstance(version, str) or not version:
        raise VerificationError("manifest 'version' must be a nonempty string")
    if not isinstance(files, dict):
        raise VerificationError("manifest 'files_sha256' must be an object")
    for rel, digest in files.items():
        _check_path(rel, "manifest")
        if rel == MANIFEST_KEY:
            raise VerificationError("manifest lists itself")
        if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
            raise VerificationError(f"invalid SHA-256 for {rel!r}")
    return version, files


def _check_size(entry, size):
    # zipfile caps output at the declared size but accepts shorter decoded data.
    if size != entry.file_size:
        raise VerificationError(
            f"size mismatch: {entry.orig_filename!r} declares {entry.file_size} bytes, has {size}"
        )


def _sha256(archive, entry):
    digest = hashlib.sha256()
    size = 0
    with archive.open(entry) as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    _check_size(entry, size)
    return digest.hexdigest()

