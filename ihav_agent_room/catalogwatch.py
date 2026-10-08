"""Announce plugin catalog changes in the machine agents space (plan N-ac8dc5ab).

Every supervisor calls `check_catalogs` from its loop; a lease limits scans per interval,
and a nonblocking machine lock prevents cooperating scans from overlapping after lease expiry.
For each catalog URL in policy.json `catalogs`, it reads the head of `main` with `git ls-remote`, and on a new commit
reads `.claude-plugin/marketplace.json` at that commit and posts one `release` entry that lists added, removed and
re-pinned plugins. The first look at a catalog only records a baseline. Entries are data; nothing is installed or
activated. Network or git failures are skipped until the next interval.
"""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from ihav_agent_room.common import RoomError, file_lock
from ihav_agent_room.globalspace import GlobalSpace, MAX_BODY_BYTES

INTERVAL = 600
GIT_TIMEOUT = 30
CATALOG_BYTES = 1024 * 1024


def configured_catalogs(space):
    try:
        urls = json.loads((space.root / "policy.json").read_text(encoding="utf-8")).get("catalogs", [])
        return [url for url in urls if isinstance(url, str) and url.startswith(("https://", "git@", "file://", "/"))]
    except (OSError, ValueError, AttributeError):
        return []


def take_lease(space, now=None, interval=INTERVAL):
    """True for exactly one caller per interval across every supervisor on the machine."""
    now = now if now is not None else time.time()
    db = space.connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM meta WHERE key='catalog_lease'").fetchone()
        if row and float(row[0]) > now:
            db.execute("ROLLBACK")
            return False
        db.execute("INSERT OR REPLACE INTO meta VALUES ('catalog_lease', ?)", (str(now + interval),))
        db.execute("COMMIT")
        return True
    finally:
        db.close()


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if result.returncode:
        raise RoomError(f"git {args[0]} failed: {result.stderr.strip()[:200]}", "native")
    return result.stdout


def read_manifest(directory, relative):
    path = (Path(directory) / relative).resolve()
    if Path(directory).resolve() not in path.parents or path.stat().st_size > CATALOG_BYTES:
        raise RoomError("Catalog manifest escapes its checkout or exceeds the byte limit", "invalid")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise RoomError("Catalog manifest must contain a plugins list", "invalid")
    return data


def read_catalog_snapshot(url):
    """Read both catalogs at one commit; retained metadata is data, never qualification."""
    head = git("ls-remote", url, "refs/heads/main").split()
    if not head:
        raise RoomError(f"{url} has no main branch", "native")
    with tempfile.TemporaryDirectory(prefix="ihav-catalog-watch-") as directory:
        git("clone", "--quiet", "--depth", "1", "--branch", "main", url, directory)
        if git("rev-parse", "HEAD", cwd=directory).strip() != head[0]:
            raise RoomError("Catalog moved during the read; retry next interval", "conflict")
        data = read_manifest(directory, ".claude-plugin/marketplace.json")
        try:
            codex = read_manifest(directory, ".agents/plugins/marketplace.json")
        except (RoomError, OSError, ValueError):
            codex = None  # Unavailable Codex metadata holds updates but does not hide Claude catalog news.
    plugins = {}
    for entry in data.get("plugins", []):
        source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
        plugins[entry["name"]] = [source.get("ref"), source.get("sha")]
    return {"url": str(url), "head": head[0], "plugins": plugins, "catalogs": {"claude": data, "codex": codex}}


def read_catalog(url):
    """Preserve the existing (head sha, {plugin: (ref, sha)}) read interface."""
    snapshot = read_catalog_snapshot(url)
    return snapshot["head"], snapshot["plugins"]


def describe_change(before, after):
    lines = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old is None:
            lines.append(f"added {name} {new[0]} ({(new[1] or 'unpinned')[:12]})")
        elif new is None:
            lines.append(f"removed {name}")
        else:
            lines.append(f"{name} {old[0]} -> {new[0]} ({(new[1] or 'unpinned')[:12]})")
    return lines


def check_catalogs(space=None, now=None, read=None):
    """Serialize cooperating scans through fetch, notice and metadata persistence."""
    space = space or GlobalSpace(timeout=0.5)
    urls = configured_catalogs(space)
    if not urls:
        return []
    space.connect().close()  # Check the ledger root before creating its scan lock.
    with ExitStack() as locks:
        try:
            locks.enter_context(file_lock(space.root / "catalog-watch.lock", blocking=False))
        except RoomError as exc:
            if exc.code == "conflict":
                return []
            raise
        if not take_lease(space, now):
            return []
        return scan_catalogs(space, urls, read)


def catalog_notice(url, head, changes, marker):
    """Bound the data-only notice without dropping the complete saved snapshot."""
    prefix = f"Catalog moved to {head}; {len(changes)} changes.\nSource: {url}\n" + "\n".join(changes)
    footer = f"\n{marker}\nNothing was installed or activated; hosts update plugins from the catalog."
    body = prefix + footer
    if len(body.encode("utf-8")) <= MAX_BODY_BYTES:
        return body
    footer = "\nNotice shortened; inspect this catalog commit for available metadata." + footer
    budget = MAX_BODY_BYTES - len(footer.encode("utf-8"))
    return prefix.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + footer


def scan_catalogs(space, urls, read):
    """Called only while the scan lock is held; older watcher code does not use it."""
    posted = []
    for url in urls:
        try:
            value = (read or read_catalog_snapshot)(url)
            if isinstance(value, dict):
                head, plugins = value["head"], value["plugins"]
                snapshot = {"url": url, "head": head, "plugins": plugins, "catalogs": value["catalogs"]}
            else:
                head, plugins = value  # Existing injected readers only know the announcement pins.
                snapshot = {"url": url, "head": head, "plugins": plugins}
        except (RoomError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            continue
        key = f"catalog_seen:{url}"
        db = space.connect()
        try:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        finally:
            db.close()
        seen = json.loads(row[0]) if row else None
        unchanged = seen and seen.get("head") == head
        changes = describe_change(seen["plugins"], plugins) if seen else []
        if changes and not unchanged:
            transition = hashlib.sha256(json.dumps([url, seen["head"], head],
                                                   separators=(",", ":")).encode("utf-8")).hexdigest()
            marker = "Catalog transition: " + transition
            db = space.connect()
            try:
                committed = db.execute("SELECT id FROM entries WHERE kind='release' AND origin_room IS NULL "
                                       "AND instr(body, ?) > 0 LIMIT 1", (marker,)).fetchone()
            finally:
                db.close()
            # The notice may already be committed even when a prior scan never saved its snapshot.
            # Recover that fact before posting again; normal queue import preserves its existing delivery.
            if committed is None:
                summary = "; ".join(changes)
                entry = space.post("release", f"catalog: {summary}"[:200],
                                   catalog_notice(url, head, changes, marker))
                posted.append(entry["id"])
        db = space.connect()
        try:
            # An unchanged announcement head can still acquire both-host source metadata after an upgrade.
            # Keep richer metadata when a legacy reader only returns its pin projection.
            if unchanged and "catalogs" not in snapshot and "catalogs" in seen:
                snapshot["catalogs"] = seen["catalogs"]
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(snapshot)))
        finally:
            db.close()
    return posted
