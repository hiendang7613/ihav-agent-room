#!/usr/bin/env python3
"""Prepare one coding/knowledge-transfer pilot locally; never execute native agents."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ihav_agent_room import __version__
from ihav_agent_room.common import PLUGIN_ROOT
from ihav_agent_room.package import PATTERNS


EDIT_SCOPE = ["ihav_agent_room/package_verifier.py", "ihav_agent_room/cli.py",
              "tests/test_package_verifier.py", "resources/distribution-readme.md"]
# Historical workload input; preparation does not require an unreleased current-version ZIP.
REFERENCE_RELEASE = "0.3.1"
SCOPE = {"native_executed": False, "provider_authorization": "required before launch",
         "planned_runs": 1, "initial_mode": "default", "initial_members": ["CLAUDE_01", "CODEX_EXPERT"],
         "max_members": 4, "max_room_messages": 24, "max_execution_seconds": 1200,
         "max_mode_changes": 1, "retries": 0,
         "transfer_condition": "Completed coding task and a relevant, evidenced ordinary lesson saved without an additional learning request",
         "limits": "Message/time bounds are not token or monetary caps; cleanup is additional",
         "source_edit_scope": EDIT_SCOPE}
WORK_ORDER = """# Integrate the package verifier

Add `ihav-agent-room --json verify-package ARCHIVE` to this scratch plugin. Reuse and adapt
`reference/verify_package.py` and its tests; preserve their validation contract. Use the
existing CLI envelope: success contains version/files_checked; archive/I/O failure exits
1 with error.code=package. Argument errors retain exit 2. It must work without a room,
member identity, native programs or a provider call. Read archives without extraction or
execution. Document that manifest integrity does not authenticate the publisher.

CLAUDE_01 owns implementation and integration. CODEX_EXPERT reviews the exact submitted
source and can offer questions, counterexamples and suggestions directly. Use the room's
existing task, claim, evidence and review mechanisms. Choose useful conversations and
routine implementation details yourselves; no fixed discussion rounds or output template.

Edit only ihav_agent_room/package_verifier.py, ihav_agent_room/cli.py,
tests/test_package_verifier.py, and resources/distribution-readme.md. Keep imports at
module scope, standard library only. Keep versions/schema and other behavior unchanged.
Run focused tests, including the reference error cases and roomless public CLI behavior.
The release ZIP in fixtures/ is read-only. Reference files are read-only evidence.

Use `ihav-agent-room` on PATH for coordination through the original plugin. Run the candidate
explicitly with `python3 ./bin/ihav-agent-room ...` when testing it. Do not switch coordinator
code, install globally, call other services, spawn additional agents, publish or read the
observer/protocol outside this project. Tests may create and clean up their own temporary
fixtures. Native room startup, mode changes and cleanup belong to the operator.

One task, within the operator's shared 24-message/1,200-second experiment budget, including
automatic task/review messages. Do not continue new work once this task is complete.
These limits are for this experiment; ordinary Agent Room conversation has no fixed quota.
"""
TRANSFER_REQUEST = """Inspect transfer/probe-1.zip and transfer/probe-2.zip using this
project's verifier. Explain what the results establish about each archive and whether
they establish publisher authenticity. This is read-only analysis; do not change source,
create tasks, spawn agents, change settings or use external services. You can consult the
project's ordinary knowledge and source. Send one substantive conclusion to CLAUDE_01
with the evidence you used, ACK this request, and finish. Existing members will let you
investigate independently. Use the remaining shared experiment budget; no automatic retry.
"""


def digest(content):
    return hashlib.sha256(content).hexdigest()


def selected_files(source):
    """Resolve the complete, explicit input set before creating the destination."""
    paths = {path.relative_to(source).as_posix(): path
             for pattern in PATTERNS for path in source.glob(pattern)}
    extras = ("tests/test_cli.py", "scripts/native_smoke.py", "scripts/learning_smoke.py",
              "pilots/zip_manifest_audit/verify_package.py",
              "pilots/zip_manifest_audit/tests/test_verify_package.py",
              "pilots/zip_manifest_audit/README.md",
              "pilots/zip_manifest_audit/fixtures/agent-room-0.2.1.zip",
              f"dist/agent-room-{REFERENCE_RELEASE}.zip", "docs/practical-learning-pilot.md",
              "scripts/prepare_practical_pilot.py")
    paths.update({name: source / name for name in extras})
    required = {"bin/ihav-agent-room", "ihav_agent_room/cli.py", "ihav_agent_room/knowledge.py",
                "resources/distribution-readme.md", ".claude-plugin/plugin.json"}
    if required - paths.keys():
        raise ValueError("Missing required plugin inputs")
    result = {}
    for name, path in sorted(paths.items()):
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source)
                or any(parent.is_symlink() for parent in path.parents if parent != source and parent.is_relative_to(source))):
            raise ValueError(f"Input must be a regular file without symlink components: {name}")
        result[name] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return result


def transfer_probes():
    """Frozen analysis inputs: self-consistent content and an overstated ZIP size."""
    payload = b"This locally authored archive carries no publisher signature.\n"
    manifest = json.dumps({"version": __version__, "files_sha256": {"README.md": digest(payload)}}).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (("PACKAGE-MANIFEST.json", manifest), ("README.md", payload)):
            info = zipfile.ZipInfo("agent-room/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    good = stream.getvalue()
    bad = bytearray(good)
    # Modify both headers' declared uncompressed length, retaining the actual bytes/CRC.
    with zipfile.ZipFile(io.BytesIO(good)) as archive:
        entry = archive.getinfo("agent-room/README.md")
        struct.pack_into("<I", bad, entry.header_offset + 22, entry.file_size + 7)
        cursor = archive.start_dir
        for info in archive.infolist():
            name_size, extra_size, comment_size = struct.unpack_from("<HHH", bad, cursor + 28)
            if info.filename == entry.filename:
                struct.pack_into("<I", bad, cursor + 24, entry.file_size + 7)
            cursor += 46 + name_size + extra_size + comment_size
    return {"probe-1.zip": good, "probe-2.zip": bytes(bad)}


def prepare(destination, source=PLUGIN_ROOT):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination must be new; existing pilots are preserved")
    # Require an existing trusted parent; do not create an unrelated folder hierarchy.
    destination = destination.parent.resolve(strict=True) / destination.name
    inputs = selected_files(source)
    output = {}
    references = {"pilots/zip_manifest_audit/verify_package.py": "reference/verify_package.py",
                  "pilots/zip_manifest_audit/tests/test_verify_package.py": "reference/tests/test_verify_package.py",
                  "pilots/zip_manifest_audit/README.md": "reference/README.md",
                  "pilots/zip_manifest_audit/fixtures/agent-room-0.2.1.zip": "reference/fixtures/agent-room-0.2.1.zip"}
    for name, value in inputs.items():
        if name in {"docs/practical-learning-pilot.md", "scripts/prepare_practical_pilot.py"}:
            continue
        relative = references.get(name, "fixtures/" + Path(name).name if name.startswith("dist/") else name)
        output["project/" + relative] = value
    output["project/README.md"] = inputs["resources/distribution-readme.md"]
    output["project/WORK_ORDER.md"] = (WORK_ORDER.encode(), 0o644)
    output["protocol.md"] = inputs["docs/practical-learning-pilot.md"]
    for name, content in transfer_probes().items():
        output["observer/" + name] = (content, 0o644)
    output["observer/request.txt"] = (TRANSFER_REQUEST.encode(), 0o644)
    expected = {"probe-1.zip": {"integrity": "pass", "publisher_authenticated": False, "files_checked": 1},
                "probe-2.zip": {"integrity": "reject", "reason": "decoded size differs from declared size"}}
    output["observer/expected.json"] = ((json.dumps(expected, indent=2) + "\n").encode(), 0o644)
    record = {"prepared_at": datetime.now(timezone.utc).isoformat(), "source": str(source),
              "project": str(destination / "project"), "plugin_version": __version__, "scope": SCOPE,
              "source_sha256": {name: digest(value[0]) for name, value in inputs.items()},
              "output_sha256": {name: digest(value[0]) for name, value in sorted(output.items())},
              "native_status": "not executed", "task_id": None, "learning_result": None,
              "transfer_result": None, "cleanup_result": "no processes launched"}
    output["preparation.json"] = ((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode(), 0o644)
    destination.mkdir()  # exclusive; a competing prepare cannot reuse this destination
    # Publish the record last; interrupted copies retain evidence without a completion record.
    order = sorted(name for name in output if name != "preparation.json") + ["preparation.json"]
    for name in order:
        content, mode = output[name]
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
        path.chmod(mode)
    return {"prepared": True, "destination": str(destination), "project": record["project"],
            "record": str(destination / "preparation.json"), "files": len(output), "scope": SCOPE}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path, metavar="NEW_DIRECTORY", help="Write the local preparation; no native execution")
    args = parser.parse_args(argv)
    try:
        result = prepare(args.prepare) if args.prepare else {"prepared": False, "proposed_scope": SCOPE}
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
