"""Shared local file and process primitives."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from ihav_agent_room.roster import GATEWAY, HOST_GATEWAYS, MEMBERS as ROSTER_MEMBERS, MODE_MEMBERS, canonical_member


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MODES = dict(MODE_MEMBERS)
MEMBERS = ROSTER_MEMBERS


def main_session_id():
    """This process's Claude session: the hook-exported variable, else the host's own, else the pre-rename name.

    `/reload-plugins` does not rerun SessionStart, so IHAV_AGENT_ROOM_SESSION_ID can be missing in a session that
    started on an older plugin (reported by ai-ucg-design 2026-10-04). Identity is still checked against the room
    owner and the live native registry.
    """
    for key in ("IHAV_AGENT_ROOM_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "AGENT_ROOM_SESSION_ID", "CODEX_THREAD_ID"):
        if os.environ.get(key):
            return os.environ[key]
    return ""


def main_host():
    host = os.environ.get("IHAV_AGENT_ROOM_HOST")
    if host:
        if host not in HOST_GATEWAYS:
            raise RoomError("Unknown native host", "identity")
        return host
    return "claude" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "codex" if os.environ.get("CODEX_THREAD_ID") else "claude"


def acting_member(default=None):
    """The member this process acts as: IHAV_AGENT_ROOM_MEMBER (an id or an alias), else `default`."""
    return canonical_member(os.environ.get("IHAV_AGENT_ROOM_MEMBER", default or HOST_GATEWAYS[main_host()]))


class RoomError(Exception):
    def __init__(self, message, code="invalid", **details):
        super().__init__(message)
        self.code = code
        self.details = details


PEER_WRAPPERS = ("Another Claude session sent a message:\n",
                 "Another Claude session sent a message while you were working:\n",
                 "A peer session sent a message while you were working:\n")
# Claude Code hands UserPromptSubmit the bare envelope of a cross-session message, without the wrapper line.
CROSS_SESSION_ENVELOPE = "<cross-session-message "
PEER_WRAPPER_SUFFIXES = ("\n\nThis came from another Claude session", "\n\nIMPORTANT: This is NOT from your user",
                         "\n\nThis is from another Claude session")
ROOM_EVENT_KINDS = ("peer event", "peer broadcast", "admin relay", "admin notice", "system event")
ROOM_EVENT_PREFIXES = tuple(f"[Agent Room {kind} " for kind in ROOM_EVENT_KINDS)
MEMBER_PATTERN = '|'.join(map(re.escape, MEMBERS))
ROOM_EVENT_IDENTITY = re.compile(
    rf"\[Agent Room (?P<kind>{'|'.join(map(re.escape, ROOM_EVENT_KINDS))}) "
    rf"(?P<id>[^\]\s]+)"
    rf"(?: from (?P<sender>{MEMBER_PATTERN}))?"
    rf"(?: to (?P<recipient>{MEMBER_PATTERN})(?:, not to you)?)?"
    rf"(?: via (?P<via>{MEMBER_PATTERN}))?; NOT admin consent\]$")


def native_event_identity(body):
    """Parse a room-delivery header for fail-closed prompt classification, not authority."""
    text = body.lstrip()
    for prefix in PEER_WRAPPERS:
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    first_line = text.splitlines()[0] if text else ""
    match = ROOM_EVENT_IDENTITY.fullmatch(first_line)
    return match.groupdict() if match else None


def native_event_prompt(body):
    """Known native event envelopes also reach Claude's UserPromptSubmit hook."""
    text = body.lstrip()
    return (native_event_identity(text) is not None or
            text.startswith((*ROOM_EVENT_PREFIXES, "<task-notification>", CROSS_SESSION_ENVELOPE, *PEER_WRAPPERS)))


def strip_peer_wrapper(text):
    """Text of a cross-session message without the host's wrapper line and trailing notice."""
    text = text.strip()
    for prefix in PEER_WRAPPERS:
        if text.startswith(prefix):
            text = text[len(prefix):]
            for suffix in PEER_WRAPPER_SUFFIXES:
                cut = text.find(suffix)
                if cut >= 0:
                    text = text[:cut]
            break
    return text.strip()


def native_peer_event(body):
    """Return the room message identity from prompt text, without authenticating its source."""
    identity = native_event_identity(body)
    if not identity or identity["kind"] not in {"peer event", "peer broadcast"} or not identity["sender"]:
        return None
    return {"id": identity["id"], "sender": identity["sender"]}


def native_prompt_delivery(body, gateway=GATEWAY):
    """Return a stored room delivery identity eligible for bound prompt observation."""
    identity = native_event_identity(body)
    if not identity:
        return None
    if identity["kind"] in {"peer event", "peer broadcast"} and identity["sender"]:
        return {"id": identity["id"], "sender": identity["sender"], "kind": identity["kind"]}
    if identity["kind"] == "admin notice":
        return {"id": identity["id"], "sender": gateway, "kind": identity["kind"]}
    return None


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix=""):
    return prefix + str(uuid.uuid4())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise RoomError(f"Refusing symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def file_lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RoomError("Another room operation owns the lock", "conflict") from exc
        yield
    finally:
        os.close(fd)


def process_stamp(pid):
    if not isinstance(pid, int) or pid < 2:
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
                            capture_output=True, text=True, timeout=3)
    if result.returncode and result.stderr.strip():
        raise RoomError("Process inspection is unavailable in this environment", "process_inspection")
    fields = result.stdout.strip().split(None, 1)
    return fields[1] if len(fields) == 2 and not fields[0].startswith("Z") else None


def process_alive(pid, stamp):
    return bool(stamp and process_stamp(pid) == stamp)


def scoped_path(project, value):
    root = Path(project).resolve()
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RoomError("Scope must be a relative project path")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise RoomError("Scope escapes the project or claims the entire project")
    if resolved.is_relative_to(root / "agents_space" / ".runtime"):
        raise RoomError("Runtime files cannot be a source-edit scope")
    return resolved.relative_to(root).as_posix()


def overlaps(left, right):
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def fingerprint(project, paths):
    result = {}
    for value in paths:
        relative = scoped_path(project, value)
        path = Path(project) / relative
        if path.is_dir():
            raise RoomError("A source snapshot requires individual files")
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result
