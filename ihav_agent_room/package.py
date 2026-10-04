"""Build an installable local distribution using an explicit source allowlist."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

from ihav_agent_room import __version__
from ihav_agent_room.common import PLUGIN_ROOT, RoomError, dumps
from ihav_agent_room.guides import GUIDES


PATTERNS = ("ihav_agent_room/*.py", "bin/ihav-agent-room", ".claude-plugin/*.json", ".codex-plugin/*.json", "hooks/*.json",
            "resources/*.md", "skills/*/SKILL.md", "templates/**/*.md", "docs/v1.1.md")


def build(output, source=PLUGIN_ROOT):
    source, output = Path(source).resolve(), Path(output)
    selected = {}
    for pattern in PATTERNS:
        for path in source.glob(pattern):
            if path.is_symlink() or not path.resolve().is_relative_to(source) or not path.is_file():
                raise RoomError(f"Package source must be a regular in-project file: {path}", "conflict")
            selected[path.relative_to(source).as_posix()] = path.read_bytes()
    required = {"bin/ihav-agent-room", "ihav_agent_room/cli.py", "hooks/hooks.json", "resources/init-alias.md",
                "skills/init/SKILL.md", "skills/init-agents-space/SKILL.md", "templates/README.md", ".claude-plugin/plugin.json",
                ".claude-plugin/marketplace.json", "docs/v1.1.md", "resources/distribution-readme.md",
                "ihav_agent_room/guides.py", *GUIDES.values(),
                "resources/collaboration-guidance.md", "ihav_agent_room/knowledge.py", "ihav_agent_room/package_verifier.py"}
    if required - selected.keys():
        raise RoomError("Missing required package files", "package", missing=sorted(required - selected.keys()))
    plugin = json.loads(selected[".claude-plugin/plugin.json"])
    marketplace = json.loads(selected[".claude-plugin/marketplace.json"])
    codex = json.loads(selected.get(".codex-plugin/plugin.json", b"{}"))
    if plugin["version"] != __version__ or marketplace["plugins"][0]["version"] != __version__ or \
            codex.get("version", __version__) != __version__:
        raise RoomError("Package, plugin and marketplace versions disagree", "package")
    selected["README.md"] = selected["resources/distribution-readme.md"]
    manifest = {"version": __version__, "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(selected.items())}}
    output.parent.mkdir(parents=True, exist_ok=True)
    selected["PACKAGE-MANIFEST.json"] = (dumps(manifest) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".ihav-agent-room-", suffix=".zip", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in sorted(selected.items()):
                    info = zipfile.ZipInfo("ihav-agent-room/" + name, date_time=(2026, 1, 1, 0, 0, 0))
                    info.external_attr = (0o100755 if name == "bin/ihav-agent-room" else 0o100644) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomically expose the complete artifact without replacing an existing path.
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"path": str(output.resolve()), "version": __version__, "files": len(selected),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "providers_called": False}
