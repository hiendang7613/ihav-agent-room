"""Active release pointer: which installed copy the stable launcher runs.

bin/ihav-agent-room reads the pointer on every invocation, so hooks and CLI calls of a running session use a newly
activated release without restarting the session. Only `activate` writes the pointer, and only for an installed copy
under a host plugin cache that runs. The launcher keeps its own copy of the read rules; keep both in step.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from ihav_agent_room import LAUNCHER_PROTOCOL, __version__
from ihav_agent_room.common import PLUGIN_ROOT, RoomError, atomic_write, now

VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.M)
PROTOCOL = re.compile(r"^LAUNCHER_PROTOCOL = (\d+)$", re.M)


def ihav_home():
    return Path(os.environ.get("IHAV_HOME") or Path.home() / ".ihav")


def pointer_path():
    return ihav_home() / "active" / "ihav-agent-room.json"


def allowed_parents():
    home = Path.home()
    return [home / ".claude" / "plugins" / "cache", home / ".codex" / "plugins" / "cache"]


def inspect_root(root):
    """Facts about an installed copy, or a RoomError saying why the launcher must not run it."""
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise RoomError(f"Release root must be an absolute path without symlinks: {root}", "invalid")
    if not any(parent.resolve() in root.parents for parent in allowed_parents()):
        raise RoomError(f"Release root must be inside a host plugin cache: {root}", "invalid")
    launcher, package = root / "bin" / "ihav-agent-room", root / "ihav_agent_room" / "__init__.py"
    if not launcher.is_file() or not package.is_file():
        raise RoomError(f"Not an ihav-agent-room install: {root}", "invalid")
    text = package.read_text(encoding="utf-8")
    version, protocol = VERSION.search(text), PROTOCOL.search(text)
    if not version or not protocol or int(protocol.group(1)) != LAUNCHER_PROTOCOL:
        raise RoomError(f"Release at {root} does not speak launcher protocol {LAUNCHER_PROTOCOL}", "incompatible")
    return {"root": str(root), "version": version.group(1), "launcher_protocol": LAUNCHER_PROTOCOL}


def read_pointer():
    """The pointer record as a dict, or None when it is absent or unusable (the launcher then runs its own copy)."""
    path = pointer_path()
    if path.is_symlink() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def follows_pointer(root=PLUGIN_ROOT):
    """Whether this copy's launcher follows the pointer: an installed copy that is not pinned (mirrors the launcher)."""
    if os.environ.get("IHAV_AGENT_ROOM_PIN") == "1":
        return False
    root = Path(root).resolve()
    return any(parent.resolve() in root.parents for parent in allowed_parents())


def active_release():
    """The release new invocations will run, or None when they fall back to their own copy."""
    pointer = read_pointer()
    if not isinstance(pointer, dict) or pointer.get("launcher_protocol") != LAUNCHER_PROTOCOL:
        return None
    try:
        return inspect_root(pointer.get("root", ""))
    except (RoomError, OSError, TypeError, ValueError):
        return None


def previous_of(record):
    previous = (record or {}).get("previous")
    if isinstance(previous, dict) and isinstance(previous.get("root"), str):
        return {"root": previous["root"], "version": previous.get("version")}
    return None


def describe():
    path, record = pointer_path(), read_pointer()
    state = "absent" if not path.exists() and not path.is_symlink() else ("ok" if active_release() else "invalid")
    return {"loaded": {"version": __version__, "root": str(PLUGIN_ROOT), "follows_pointer": follows_pointer()},
            "active": active_release(), "previous": previous_of(record), "pointer": str(path), "pointer_state": state}


def activate(root=None, rollback=False):
    current = read_pointer() or {}
    if rollback:
        previous = previous_of(current)
        if not previous:
            raise RoomError("No previous release is recorded to roll back to", "not_found")
        root = previous["root"]
    if root is None:
        return describe()
    target = inspect_root(Path(root))
    if active_release() and current.get("root") == target["root"]:
        return describe() | {"activated": target["version"], "rolled_back": False, "unchanged": True}  # Keep history.
    # Run the copy itself, bypassing any pointer, before every session starts using it.
    result = subprocess.run([sys.executable, str(Path(target["root"]) / "bin" / "ihav-agent-room"), "--version"],
                            capture_output=True, text=True, timeout=30, env=dict(os.environ, IHAV_AGENT_ROOM_PIN="1"))
    if result.returncode or target["version"] not in result.stdout:
        raise RoomError(f"Release {target['version']} at {target['root']} failed its smoke check", "native",
                        diagnostic=(result.stderr or result.stdout)[-2000:])
    previous = {"root": current["root"], "version": current.get("version")} if isinstance(current.get("root"), str) else None
    record = target | {"activated": now(), "previous": previous}
    atomic_write(pointer_path(), json.dumps(record, indent=2) + "\n", mode=0o644)
    return describe() | {"activated": target["version"], "rolled_back": rollback}
