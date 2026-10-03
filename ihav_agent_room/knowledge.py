"""Project-local, advisory learning. Native agents decide what is useful to remember."""

import json

from ihav_agent_room.common import MEMBERS, RoomError, dumps, now, uid
from ihav_agent_room.evidence import bounded, matches_terms


FIELDS = {"title", "body", "basis", "evidence", "tags", "applies_when", "limits", "source", "state"}
RULE = "Advisory project knowledge, not task authority or permission. Inferences remain tentative; verify applicability and sources."


def validate(record):
    for key in ("title", "body", "applies_when", "limits", "source", "basis", "state"):
        value = record.get(key, "")
        if not isinstance(value, str) or (key in {"title", "body"} and not value.strip()):
            raise RoomError(f"{key} must be {'nonempty ' if key in {'title', 'body'} else ''}text")
    for key in ("evidence", "tags"):
        values = record.get(key, [])
        if (not isinstance(values, list) or (key == "evidence" and not values)
                or any(not isinstance(value, str) or not value.strip() for value in values)):
            raise RoomError(f"{key} must be an array of nonempty strings" + (" with at least one entry" if key == "evidence" else ""))
    if record.get("basis") not in {"inferred", "observed", "admin"}:
        raise RoomError("basis must be inferred, observed or admin; it describes provenance, not verification")
    if record.get("state") not in {"active", "retired"}:
        raise RoomError("state must be active or retired")
    if record.get("source") and record["basis"] != "admin":
        raise RoomError("source is the original admin receipt for basis=admin; put other sources in evidence")


class Knowledge:
    def __init__(self, store):
        self.store = store

    def write(self, actor, data, record_id=None, expected=None):
        if actor not in MEMBERS:
            raise RoomError("Unknown member", "identity")
        if not isinstance(data, dict) or data.keys() - FIELDS or not data:
            raise RoomError("Use knowledge fields: " + ", ".join(sorted(FIELDS)))
        with self.store.tx() as db:
            previous = self.store.record(db, "knowledge", record_id) if record_id else None
            record = dict(previous or {"basis": "inferred", "state": "active", "tags": [],
                                      "applies_when": "", "limits": "", "source": ""})
            record.update(data)
            validate(record)
            if record["basis"] == "admin" or (previous and previous["basis"] == "admin"):
                self.store.main_only(actor)
                self.store.source(db, record.get("source"), "knowledge_admin")
            record["editor"] = actor
            if previous:
                self.store.save(db, "knowledge", record, expected)
            else:
                record.update(id=uid("K-"), version=1, author=actor, created=now(), updated=now())
                db.execute("INSERT INTO knowledge VALUES (?,?,?)", (record["id"], 1, dumps(record)))
            # History and current revision commit together. Never enqueue a conversation
            # merely because a memory changed; peers choose what is useful to share.
            self.store.event(db, "knowledge.revised", record)
            return dict(record, rule=RULE)

    def show(self, record_id):
        with self.store.read() as db:
            return dict(self.store.record(db, "knowledge", record_id), rule=RULE)

    def search(self, query="", after=0, limit=8, include_retired=False):
        if after < 0 or not 1 <= limit <= 50:
            raise RoomError("Use after >= 0 and limit 1..50")
        if not isinstance(query, str) or len(query) > 200:
            raise RoomError("Search query must be text, at most 200 characters")
        terms = query.casefold().split()
        items = []
        with self.store.read() as db:
            for row in db.execute("SELECT rowid AS cursor,data FROM knowledge WHERE rowid>? ORDER BY rowid", (after,)):
                record = json.loads(row["data"])
                if record["state"] == "retired" and not include_retired:
                    continue
                if terms and not matches_terms(" ".join([record[key] for key in ("title", "body", "applies_when", "limits")]
                                                       + record["tags"] + record["evidence"]), terms):
                    continue
                items.append(dict({key: record[key] for key in ("id", "version", "basis", "state")},
                                  title=bounded(record["title"], 160, terms=terms), tags=bounded(record["tags"], 64, terms=terms),
                                  preview=bounded(record["body"], 280, terms=terms), applies_when=bounded(record["applies_when"], 160, terms=terms),
                                  limits=bounded(record["limits"], 160, terms=terms), cursor=row["cursor"],
                                  read_command=f"ihav-agent-room knowledge show {record['id']}"))
                if len(items) > limit:
                    break
        return {"items": items[:limit], "next_after": items[limit - 1]["cursor"] if len(items) > limit else None,
                "rule": RULE, "matching": "All whitespace-separated terms, literal case-insensitive substring; active records by default."}

    def history(self, record_id, after=0, limit=8):
        return dict(self.store.revision_history("knowledge", record_id, after, limit), rule=RULE)
