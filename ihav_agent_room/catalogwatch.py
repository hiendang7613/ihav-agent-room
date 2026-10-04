"""Announce plugin catalog changes in the machine agents space (plan N-ac8dc5ab).

Every supervisor calls `check_catalogs` from its loop; a lease in the ledger lets only one of them look per interval.
For each catalog URL in policy.json `catalogs`, it reads the head of `main` with `git ls-remote`, and on a new commit
reads `.claude-plugin/marketplace.json` at that commit and posts one `release` entry that lists added, removed and
re-pinned plugins. The first look at a catalog only records a baseline. Entries are data; nothing is installed or
activated. Network or git failures are skipped until the next interval.
"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from ihav_agent_room.common import RoomError
from ihav_agent_room.globalspace import GlobalSpace

INTERVAL = 600
GIT_TIMEOUT = 30


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


def read_catalog(url):
    """(head sha, {plugin: (ref, sha)}) of the catalog's main branch."""
    head = git("ls-remote", url, "refs/heads/main").split()
    if not head:
        raise RoomError(f"{url} has no main branch", "native")
    with tempfile.TemporaryDirectory(prefix="ihav-catalog-watch-") as directory:
        git("clone", "--quiet", "--depth", "1", "--branch", "main", url, directory)
        if git("rev-parse", "HEAD", cwd=directory).strip() != head[0]:
            raise RoomError("Catalog moved during the read; retry next interval", "conflict")
        data = json.loads((Path(directory) / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugins = {}
    for entry in data.get("plugins", []):
        source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
        plugins[entry["name"]] = [source.get("ref"), source.get("sha")]
    return head[0], plugins


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


def check_catalogs(space=None, now=None, read=read_catalog):
    """One look per interval for the whole machine; returns the IDs of entries posted."""
    space = space or GlobalSpace(timeout=0.5)
    urls = configured_catalogs(space)
    if not urls or not take_lease(space, now):
        return []
    posted = []
    for url in urls:
        try:
            head, plugins = read(url)
        except (RoomError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            continue
        key = f"catalog_seen:{url}"
        db = space.connect()
        try:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        finally:
            db.close()
        seen = json.loads(row[0]) if row else None
        if seen and seen.get("head") == head:
            continue
        changes = describe_change(seen["plugins"], plugins) if seen else []
        if changes:
            summary = "; ".join(changes)
            entry = space.post("release", f"catalog: {summary}"[:200],
                               f"Catalog {url} moved to {head[:12]}.\n" + "\n".join(changes) +
                               "\nNothing was installed or activated; hosts update plugins from the catalog.")
            posted.append(entry["id"])
        db = space.connect()
        try:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps({"head": head, "plugins": plugins})))
        finally:
            db.close()
    return posted
