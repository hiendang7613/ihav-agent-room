"""Source identities and bounded context, independent of native model execution."""

import hashlib
from pathlib import Path

from ihav_agent_room.common import RoomError, dumps, scoped_path


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def capture(project, paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 256 or any(not isinstance(p, str) or not p for p in paths):
        raise RoomError("paths must contain 1..256 individual project files")
    snapshot = {}
    for value in paths:
        relative = scoped_path(project, value)
        current = Path(project)
        for part in Path(value).parts:
            current /= part
            if current.is_symlink():
                raise RoomError("Review/checkpoint paths must not contain symlinks", "conflict")
        path = Path(project) / relative
        if not path.exists():
            snapshot[relative] = None  # An explicit deletion/absence is part of the scope.
        elif not path.is_file():
            raise RoomError("Review/checkpoint paths must be individual regular files")
        else:
            hashed = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hashed.update(chunk)
            snapshot[relative] = hashed.hexdigest()
    return snapshot


def source_matches(project, snapshot):
    try:
        return capture(project, list(snapshot)) == snapshot
    except (OSError, RoomError):
        return False


def nonempty_strings(value, label, maximum=100):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum or any(not isinstance(x, str) or not x.strip() or len(x) > 4000 for x in value):
        raise RoomError(f"{label} requires 1..{maximum} nonempty strings of at most 4000 characters")


# Match already-folded query terms literally; fold the source once per record.
def matches_terms(content, terms):
    folded = content.casefold()
    return all(term in folded for term in terms)


def bounded(value, limit=1200, *, terms=()):
    """Bound a preview, optionally near a literal folded search term; never alter the record."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        start = 0
        if terms:
            folded = value.casefold()
            offset = min((pos for term in terms if (pos := folded.find(term)) >= 0), default=0)
            # Folded offsets differ from source offsets, e.g. sharp-s becomes "ss".
            for index, char in enumerate(value):
                offset -= len(char.casefold())
                if offset < 0:
                    start = max(0, min(index - limit // 4, len(value) - limit))
                    break
        return ("[excerpt] " if start else "") + value[start:start + limit] + " [truncated; read full record]"
    if isinstance(value, list):
        result = [bounded(item, limit, terms=terms) for item in value[:12]]
        if len(value) > 12:
            result.append(f"[{len(value) - 12} more; read full record]")
        return result
    if isinstance(value, dict):
        return {key: bounded(item, limit, terms=terms) for key, item in value.items()}
    return value
