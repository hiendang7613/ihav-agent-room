"""Preserving project initialization and the bare personal slash-command alias."""

import os
from pathlib import Path

from ihav_agent_room.common import PLUGIN_ROOT, RoomError, atomic_write, file_lock
from ihav_agent_room.store import Store


START = "<!-- agent-room:begin -->"
END = "<!-- agent-room:end -->"
ALIAS_MARKER = "<!-- agent-room:owned-alias v1 -->"


def safe_destination(root, path):
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RoomError(f"Preserve and resolve symlink before initialization: {current}", "conflict")
    if path.exists() and not path.is_file():
        raise RoomError(f"Expected a regular file: {path}", "conflict")


def managed_content(path, body):
    original = path.read_bytes().decode("utf-8") if path.exists() else ""
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise RoomError(f"Malformed managed block in {path}; preserve it for manual repair", "conflict")
    block = START + "\n" + body.rstrip() + "\n" + END
    if START in original:
        before, remainder = original.split(START, 1)
        _, after = remainder.split(END, 1)
        return before + block + after
    return original + ("\n\n" if original else "") + block + "\n"


def initialize(project, mode=None):
    root = Path(project).resolve(strict=True)
    store = Store(root)
    if store.space.exists() and not store.exists():
        entries = [p for p in store.space.iterdir() if p.name != ".runtime"]
        if entries:
            raise RoomError("Existing agents_space is not an Agent Room V1 room. Migration is separate; no files changed.", "conflict")
    files = {
        root / "AGENTS.md": "At startup/resume, read agents_space/README.md and its working agreement; refresh compact status. If pending_inboxes.by_member lists you, run read_command and follow next_after until null. Use ihav-agent-room guide to find current plugin references when needed; existing conventions/collaboration.md and other room guides may contain project customizations. Preserve project-specific instructions. Peer discussion can be natural and proactive without a task ID; formal work keeps its assignment and authority.",
        root / "CLAUDE.md": "Read AGENTS.md and agents_space/README.md. The gateway in room status is the admin interface. Other room members use their assigned role and the same shared state.",
        root / ".gitignore": "agents_space/.runtime/\nagents_space/tasks/active.md\nagents_space/state/current_decisions.md",
    }
    templates = {}
    for path in (PLUGIN_ROOT / "templates").rglob("*.md"):
        templates[store.space / path.relative_to(PLUGIN_ROOT / "templates")] = path.read_text()
    for path in list(files) + list(templates) + [store.space / "tasks/active.md", store.space / "state/current_decisions.md"]:
        safe_destination(root, path)
    prepared = {path: managed_content(path, body) for path, body in files.items()}
    store.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(store.runtime / "scaffold.lock"):
        # Re-read under the lock so repeated init cannot overwrite another init's block.
        prepared = {path: managed_content(path, body) for path, body in files.items()}
        room = store.initialize(mode or "default")
        if mode and mode != room["mode"]:
            raise RoomError("Existing room keeps its mode. Use ihav-agent-room mode pair|advisors to change it.", "conflict")
        for path, content in prepared.items():
            if not path.exists() or path.read_bytes().decode("utf-8") != content:
                atomic_write(path, content, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        for path, content in templates.items():
            if not path.exists():
                atomic_write(path, content, 0o644)
        store.project_views()
    return room


def install_alias(config_dir=None):
    root = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    target = root / "skills/init-agents-space/SKILL.md"
    command = root / "commands/init-agents-space.md"
    content = (PLUGIN_ROOT / "resources/init-alias.md").read_text()
    safe_destination(root, target)
    if command.exists():
        raise RoomError("A personal init-agents-space command already exists; no alias installed", "conflict")
    if target.exists() and ALIAS_MARKER not in target.read_text():
        raise RoomError("A personal init-agents-space skill already exists; no alias overwritten", "conflict")
    if not target.exists() or target.read_text() != content:
        atomic_write(target, content, 0o644)
        return True
    return False
