"""Explicit, offline room schema upgrades with a consistent pre-upgrade backup."""

import json
import os
import sqlite3

from ihav_agent_room.common import GATEWAY, RoomError, dumps, file_lock, now, process_alive, uid


VERSION = 3
HOST_SCHEMA = 4  # Same ledger tables, new gateway ownership semantics. Older runtimes must refuse these rooms.
EXTENSIONS = """
CREATE TABLE submissions (id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), data TEXT NOT NULL);
CREATE TABLE reviews (id TEXT PRIMARY KEY, submission TEXT NOT NULL REFERENCES submissions(id), data TEXT NOT NULL);
CREATE TABLE checkpoints (id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), sequence INTEGER NOT NULL,
    data TEXT NOT NULL, UNIQUE(task, sequence));
CREATE TABLE attempts (id TEXT PRIMARY KEY, message TEXT NOT NULL REFERENCES messages(id),
    task TEXT REFERENCES tasks(id), member TEXT NOT NULL, generation TEXT, data TEXT NOT NULL);
CREATE INDEX attempts_task ON attempts(task);
CREATE INDEX attempts_member ON attempts(member, generation);
CREATE INDEX reviews_submission ON reviews(submission);
"""
KNOWLEDGE_SCHEMA = """
CREATE TABLE knowledge (id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL);
"""


def gateway_backup(store):
    """Committed pre-handoff state. A second reader avoids backing up our writer transaction."""
    folder = store.runtime / "backups"
    if folder.is_symlink():
        raise RoomError("Backup directory must not be a symlink", "conflict")
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / ("gateway-" + uid() + ".sqlite3")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    source, target = store.connect(), sqlite3.connect(path)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RoomError("Gateway backup failed integrity validation", "backup")
    finally:
        source.close()
        target.close()
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return str(path)


def migrate(store):
    if not store.exists():
        raise RoomError("Room is not initialized; there is no schema to migrate", "not_initialized")
    with file_lock(store.runtime / "control.lock", blocking=False), file_lock(store.runtime / "supervisor.lock", blocking=False):
        db = store.connect()
        backup = None
        try:
            db.execute("BEGIN IMMEDIATE")
            room = json.loads(db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])
            if room["project"] != str(store.project):
                raise RoomError("Room belongs to another project", "conflict")
            if room["schema"] in {VERSION, HOST_SCHEMA}:
                return {"migrated": False, "schema": room["schema"]}
            previous_schema = room["schema"]
            if previous_schema not in {1, 2}:
                raise RoomError("Only schema 1 or 2 can be migrated to schema 3", "incompatible")
            if room["status"] not in {"stopped", "failed"}:
                raise RoomError("Stop the room using its compatible plugin before migration", "conflict")
            processes = [room.get("supervisor") or {}]
            processes += [json.loads(row[0]) for row in db.execute("SELECT data FROM members WHERE name != ?", (GATEWAY,))]
            for process in processes:
                if process.get("pid") and (not process.get("stamp") or process_alive(process["pid"], process["stamp"])):
                    raise RoomError("Confirm all owned worker/supervisor processes have stopped before migration", "conflict")
            folder = store.runtime / "backups"
            if folder.is_symlink():
                raise RoomError("Backup directory must not be a symlink", "conflict")
            folder.mkdir(mode=0o700, exist_ok=True)
            backup = folder / (f"schema-{previous_schema}-" + uid() + ".sqlite3")
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            # A second reader sees the committed pre-migration DB while this writer
            # excludes all other writers. Backing up our write transaction can hang.
            source = store.connect()
            target = sqlite3.connect(backup)
            try:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RoomError("Pre-migration backup failed integrity validation", "backup")
            finally:
                source.close()
                target.close()
            with open(backup, "rb") as stream:
                os.fsync(stream.fileno())
            for directory in (folder, store.runtime):
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            additions = (EXTENSIONS if previous_schema == 1 else "") + KNOWLEDGE_SCHEMA
            for statement in additions.split(";"):
                if statement.strip():
                    db.execute(statement)
            for row in (db.execute("SELECT id,data FROM tasks").fetchall() if previous_schema == 1 else []):
                task = json.loads(row["data"])
                task.update(review_policy="none", reviewer=None, submission=None, checkpoint_id=None,
                            contract_revision=1, last_progress=task.get("updated", task.get("created")))
                db.execute("UPDATE tasks SET data=? WHERE id=?", (dumps(task), row["id"]))
            room.update(schema=VERSION, migrated=now(), migration_backup=str(backup))
            store.put_room(db, room)
            store.event(db, "schema.migrated", {"from": previous_schema, "to": VERSION, "backup": str(backup)})
            db.execute("COMMIT")
            return {"migrated": True, "schema": VERSION, "backup": str(backup)}
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.close()
