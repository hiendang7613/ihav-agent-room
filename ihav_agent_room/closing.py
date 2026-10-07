"""Private, source-linked project closing state. Historical data never grants authority."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess

from ihav_agent_room.common import MODES, RoomError, acting_member, dumps, now, process_alive
from ihav_agent_room.continuity import (BOOTSTRAP_WINDOW, TranscriptMismatch, gateway_sources,
                                      latest_reply, transcript_path)


KEY = "project_closing_v1"
STATE_BYTES = 64000
CURRENT_BYTES = 8 * 1024 * 1024
PRIOR_LIMIT = 4
HISTORY_KEY = KEY + ":history"
PRIOR_PREFIX = KEY + ":prior:"
LOOKUP_PREFIX = KEY + ":prior_lookup:"
FIELD_BYTES = 1200
SECTIONS = ("Goals", "Done", "Doing", "Todos", "Pending", "Quests", "Risks", "Ideas")
FIELDS = ("Conclusion", *SECTIONS, "Backlog")
UNRESOLVED = {"Goals", "Todos", "Pending", "Quests", "Risks", "Ideas", "Backlog"}
ALIASES = {"InProgress": "Doing", "Processing": "Doing", "Questions": "Quests",
           "Backlog": "Backlog", "AIIdeas": "Ideas"}
LABEL = re.compile(r"^\s*\d+\.\s*(?:\*\*)?([A-Za-z]+):?(?:\*\*)?\s*:?[ \t]*$")
ZONE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*Admin-Zone:?\*\*|Admin-Zone:?)[ \t]*$")
# Inner payloads must not consume another citation block or intervening project text.
HOST_CITATION = re.compile(r"[ \t]*<oai-mem-citation>\s*<citation_entries>"
                           r"(?:(?!</?oai-mem-citation\b).)*?</citation_entries>\s*"
                           r"<rollout_ids>(?:(?!</?oai-mem-citation\b).)*?</rollout_ids>\s*"
                           r"</oai-mem-citation>\s*", re.DOTALL)


def host_citation_spans(text):
    """Locate complete unfenced host blocks without consuming same-line project text."""
    offset, opaque_end, fenced = 0, 0, False
    for line in text.splitlines(keepends=True):
        if offset >= opaque_end and not fenced and line.strip() == "<oai-mem-citation>":
            citation = HOST_CITATION.match(text, offset)
            if citation:
                opaque_end = offset + len(citation.group().rstrip())
                yield offset, opaque_end
                offset += len(line)
                continue
        prefix = max(0, min(opaque_end - offset, len(line)))
        structure = " " * prefix + line[prefix:]
        if structure.lstrip().startswith("```"):
            fenced = not fenced
        offset += len(line)


def structural_lines(text):
    """Pair literal lines with an aligned recognition mask and the project fence state."""
    lines = text.splitlines()
    display = "\n".join(lines)
    masked = list(display)
    for start, end in host_citation_spans(display):
        # Spaces keep offsets aligned and real closing-line suffixes recognizable.
        masked[start:end] = ["\n" if char == "\n" else " " for char in display[start:end]]
    fenced = False
    for line, structure in zip(lines, "".join(masked).splitlines()):
        if structure.lstrip().startswith("```"):
            fenced = not fenced
        yield line, structure, not fenced


def without_host_citation(text):
    """Return a display copy without the known complete terminal host trailer."""
    display = "\n".join(text.splitlines())
    for start, end in host_citation_spans(display):
        if not display[end:].strip():
            return display[:start].rstrip(), True
    return text, False


def sections(text):
    """Extract only the project's Admin-Zone; preserve wording, conditions and IDs."""
    text, _ = without_host_citation(text)
    lines, zone_index = list(structural_lines(text)), None
    for index, (_, structure, recognizable) in enumerate(lines):
        if recognizable and ZONE.fullmatch(structure):
            zone_index = index
    if zone_index is None:
        return {}
    lines = lines[zone_index + 1:]
    result, active = {}, None
    for line, structure, recognizable in lines:
        match = LABEL.fullmatch(structure) if recognizable else None
        if match:
            prefix = line[:len(structure) - len(structure.lstrip())].rstrip()
            if active and prefix:
                result[active].append(prefix)  # Keep the citation's literal closing tag.
            name = ALIASES.get(match[1], match[1])
            active = name if name in (*SECTIONS, "Backlog") else None
            if active:
                result[active] = []
        elif active:
            result[active].append(line)
    fields = {key: "\n".join(lines).strip() for key, lines in result.items()}
    if fields:
        preamble = []
        for line, structure, recognizable in lines:
            if recognizable and LABEL.fullmatch(structure):
                prefix = line[:len(structure) - len(structure.lstrip())].rstrip()
                if prefix:
                    preamble.append((prefix, structure[:len(prefix)], recognizable))
                break
            preamble.append((line, structure, recognizable))
        for index, (line, structure, recognizable) in enumerate(preamble):
            conclusion = re.search(r"(?:\*\*)?Conclusion:(?:\*\*)?\s*", structure) if recognizable else None
            if conclusion:
                value = "\n".join([line[conclusion.end():],
                                   *(literal for literal, _, _ in preamble[index + 1:])]).strip()
                visible = "\n".join([structure[conclusion.end():],
                                     *(masked for _, masked, _ in preamble[index + 1:])])
                if visible.strip():
                    fields["Conclusion"] = value
                break
    return fields


def checksum(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def observed_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def valid_prior(item):
    return (isinstance(item, dict) and isinstance(item.get("field"), str) and item["field"] in UNRESOLVED
            and isinstance(item.get("text"), str) and type(item.get("cursor")) is int
            and item.get("host") in {"claude", "codex"}
            and all(isinstance(item.get(key), str) for key in ("session", "source", "reply_digest")))


def load(db, room):
    row = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    if not row:
        return None
    try:
        if not isinstance(row[0], str) or len(row[0].encode()) > CURRENT_BYTES:
            raise ValueError("Oversized closing snapshot")
        record = json.loads(row[0])
        seal = record.pop("digest")
        valid = (record["schema"] == 1 and record["room"] == room["id"]
                 and record["project"] == room["project"] and checksum(record) == seal
                 and type(record.get("revision")) is int and record["revision"] > 0
                 and isinstance(record.get("ledger"), dict) and set(record["ledger"]) == {"tasks", "notes", "approvals"}
                 and all(isinstance(items, dict) for items in record["ledger"].values())
                 and isinstance(record.get("checkout"), dict) and "status" in record["checkout"]
                 and isinstance(record.get("retained"), list)
                 and all(isinstance(key, str) and key in UNRESOLVED for key in record["retained"])
                 and isinstance(record.get("prior_sections_for_reconciliation", []), list)
                 and all(valid_prior(item) for item in record.get("prior_sections_for_reconciliation", []))
                 and ("prior_history_count" not in record or (
                     type(record["prior_history_count"]) is int and record["prior_history_count"] >= 0
                     and isinstance(record.get("prior_history_fields"), list)
                     and all(key in UNRESOLVED for key in record["prior_history_fields"])))
                 and isinstance(record.get("source_gaps_at_capture", []), list)
                 and isinstance(record["fields"], dict)
                 and all(key in FIELDS and isinstance(item, dict)
                         and isinstance(item.get("text"), str)
                         and isinstance(item.get("cursor"), int) and item.get("host") in {"claude", "codex"}
                         and all(isinstance(item.get(name), str) for name in ("session", "source", "reply_digest"))
                         for key, item in record["fields"].items()))
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        valid = False
    if not valid:
        raise RoomError("Project closing state is corrupt or incompatible; preserve it for recovery", "incompatible")
    history = db.execute("SELECT value FROM meta WHERE key=?", (HISTORY_KEY,)).fetchone()
    if not history and "prior_history_count" in record:
        raise RoomError("Project closing history index is missing; preserve it for recovery", "incompatible")
    if history:
        try:
            index = json.loads(history[0])
            digest = index.pop("digest")
            if (checksum(index) != digest or index["schema"] != 1 or index["room"] != room["id"]
                    or index["project"] != room["project"] or type(index["count"]) is not int or index["count"] < 0
                    or not isinstance(index["fields"], list) or any(key not in UNRESOLVED for key in index["fields"])
                    or ("prior_history_count" in record and (record["prior_history_count"] != index["count"]
                        or record["prior_history_fields"] != index["fields"]))):
                raise ValueError("Invalid closing history index")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RoomError("Project closing history index is corrupt; preserve it for recovery", "incompatible")
        # A rollback writer knows only schema 1. Its new snapshot must not erase
        # archives it does not understand; the sealed high-water mark survives.
        record.update(prior_history_count=index["count"], prior_history_fields=index["fields"])
        rows = db.execute("SELECT COUNT(*) FROM meta WHERE key GLOB ?", (PRIOR_PREFIX + "*",)).fetchone()[0]
        if rows != index["count"]:
            raise RoomError("Archived project section count does not match its index; preserve it for recovery", "incompatible")
    record["retained"] = sorted(set(record["retained"]) | set(record.get("prior_history_fields", [])))
    record["retained"] = sorted(set(record["retained"]) | {
        item["field"] for item in record.get("prior_sections_for_reconciliation", [])})
    seal = checksum(record)
    return dict(record, digest=seal)


def archived_item(db, room, number):
    row = db.execute("SELECT value FROM meta WHERE key=?", (PRIOR_PREFIX + str(number),)).fetchone()
    try:
        record = json.loads(row[0])
        seal = record.pop("digest")
        if (checksum(record) != seal or record["schema"] != 1 or record["room"] != room["id"]
                or record["project"] != room["project"] or record["number"] != number
                or not valid_prior(record["item"])):
            raise ValueError("Invalid archived section")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RoomError("Archived project section is corrupt or missing; preserve it for recovery", "incompatible")
    return deepcopy(record["item"])


def prior_page(db, room, record, after=0, limit=PRIOR_LIMIT):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise RoomError("Prior cursor must be nonnegative and limit must be between 1 and 20", "arguments")
    archived = record.get("prior_history_count", 0)
    legacy, seen = [], set()
    for item in record.get("prior_sections_for_reconciliation", []):
        identifier = checksum([item["field"], item["text"]])
        row = db.execute("SELECT value FROM meta WHERE key=?", (LOOKUP_PREFIX + identifier,)).fetchone()
        if row:
            try:
                number = int(row[0])
                existing = archived_item(db, room, number) if 1 <= number <= archived else None
                if not existing or checksum([existing["field"], existing["text"]]) != identifier:
                    raise ValueError("Invalid archived lookup")
            except (ValueError, TypeError):
                raise RoomError("Archived project lookup is corrupt", "incompatible")
        elif identifier not in seen:
            legacy.append(item)
            seen.add(identifier)
    count = archived + len(legacy)
    items = [dict(archived_item(db, room, number) if number <= archived else legacy[number - archived - 1],
                  history_id=number)
             for number in range(after + 1, min(after + limit, count) + 1)]
    cursor = after + len(items)
    following = cursor if cursor < count else None
    return {"items": items, "total": count, "next_after": following,
            "read_command": (f"ihav-agent-room --json context --prior-after {following} --prior-limit 20"
                             if following is not None else None), "authority": "historical_data_only",
            "history_validation": {"validated_ids": [item["history_id"] for item in items],
                                   "unverified_outside_page": count - len(items),
                                   "complete": count == len(items)}}


def prior_history(store, after=0, limit=PRIOR_LIMIT, full=False):
    """Read a bounded page without importing or depending on a native transcript."""
    store.main_only(store.actor())
    with store.read() as db:
        room = store.get_room(db)
        record = load(db, room)
        if not record:
            return {"items": [], "total": 0, "next_after": None, "authority": "none"}
        result = prior_page(db, room, record, after, limit)
    if not full:
        for item in result["items"]:
            raw = item["text"].encode()
            item["text_truncated"] = len(raw) > FIELD_BYTES
            if item["text_truncated"]:
                item["text"] = raw[:FIELD_BYTES].decode("utf-8", "ignore")
        if any(item["text_truncated"] for item in result["items"]):
            result["full_read_command"] = f"ihav-agent-room --json context --prior-after {after} --prior-limit {limit} --full"
    return result


def write_state(db, room, record, archives):
    """Archive retired text and publish a schema-1 snapshot in one local transaction."""
    raw = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(raw) > CURRENT_BYTES:
        return False  # No archive, page or index changes before the capacity check.
    try:
        for number, identifier, item in archives:
            archived = {"schema": 1, "room": room["id"], "project": room["project"],
                        "number": number, "item": item}
            archived["digest"] = checksum(archived)
            db.execute("INSERT INTO meta(key,value) VALUES (?,?)", (PRIOR_PREFIX + str(number), dumps(archived)))
            db.execute("INSERT INTO meta(key,value) VALUES (?,?)", (LOOKUP_PREFIX + identifier, str(number)))
    except sqlite3.IntegrityError as exc:
        raise RoomError("Archived project storage conflicts with its index; preserve it for recovery", "incompatible") from exc
    index = {"schema": 1, "room": room["id"], "project": room["project"],
             "count": record["prior_history_count"], "fields": record["prior_history_fields"]}
    index["digest"] = checksum(index)
    db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (HISTORY_KEY, dumps(index)))
    db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (KEY, raw.decode("ascii")))
    return True


def ledger_state(db):
    result = {}
    for table in ("tasks", "notes", "approvals"):
        result[table] = {row["id"]: {key: item.get(key) for key in ("version", "state", "owner")}
                         for row in db.execute(f"SELECT id,data FROM {table}")
                         for item in [json.loads(row["data"]) ]}
    return result


def checkout(project):
    """Local Git evidence only. No remote lookup, network, or write operation."""
    try:
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=project,
                              capture_output=True, timeout=1, check=False)
        status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
                                cwd=project, capture_output=True, timeout=1, check=False)
        if head.returncode or status.returncode:
            return {"status": "unavailable"}
        # This describes paths/status, not file-content equality or provider evidence.
        return {"status": "observed", "head": head.stdout.decode().strip(),
                "path_status_digest": hashlib.sha256(status.stdout).hexdigest(),
                "basis": "HEAD and Git path/status digest; dirty file contents are not compared"}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable"}


def collect(store):
    found, gaps = [], []
    for host, session in gateway_sources(store):
        source = {"host": host, "session": session}
        source_reason = None
        try:
            path = transcript_path(store.project, host, session)
            replies = latest_reply(path, store.project, host, session,
                                   accept=lambda text: bool(sections(text)), all_matches=True) if path else []
            has_project = lambda reply: reply["complete"] and re.search(
                r"\bL\d+\.", sections(reply["text"]).get("Goals", ""))
            if path and not any(has_project(reply) for reply in replies or []):
                replies = latest_reply(path, store.project, host, session,
                                       accept=lambda text: bool(sections(text)), all_matches=True,
                                       window_bytes=BOOTSTRAP_WINDOW)
        except TranscriptMismatch as exc:
            replies, source_reason = [], str(exc)
        except (OSError, ValueError, TypeError):
            replies = []
        if not replies:
            gaps.append(source | {"reason": source_reason or "No verified structured closing in the bounded bootstrap window",
                                  "window_bytes": BOOTSTRAP_WINDOW})
        elif not any(has_project(reply) for reply in replies):
            gaps.append(source | {"reason": "No complete project Goals with an L aim in the bounded bootstrap window",
                                  "window_bytes": BOOTSTRAP_WINDOW})
        cut = [reply for reply in replies or [] if not reply["complete"]]
        if cut:
            newest = max(cut, key=lambda reply: reply["cursor"])
            gaps.append(source | {"reason": "Structured closing reply exceeds the reply byte budget",
                                  "count": len(cut), "omitted_records": len(cut) - 1,
                                  "observed_at": newest["observed_at"], "cursor": newest["cursor"]})
        for reply in replies or []:
            if not reply["complete"]:
                continue  # A cut reply cannot replace a known durable closing state.
            fields = sections(reply["text"])
            # A connection-only Goals G1 must never replace the project's L aims.
            if fields.get("Goals") and not re.search(r"\bL\d+\.", fields["Goals"]):
                fields.pop("Goals")
            found.append({"host": host, "session": session, "source": str(path),
                          "observed_at": reply["observed_at"], "cursor": reply["cursor"],
                          "reply_digest": checksum(reply["text"]), "fields": fields})
    return sorted(found, key=lambda item: observed_time(item["observed_at"]) or 0), gaps


def capture(store, *, hook_owner=None):
    """Save historical state through the bound CLI or exact-owner hook metadata.

    Hook identity is a routing assertion, never authority for tasks, receipts,
    permissions or native actions. Only recorded native source text is read.
    """
    expected = store.room()
    if hook_owner is None:
        store.main_only(store.actor())
    else:
        # A hook's host payload may be valid while its shell exports are missing
        # or stale. Recheck that exact owner here without changing the env or
        # the ordinary CLI/receipt authorization path. Workers cannot use it.
        owner = expected.get("owner") or {}
        if (not isinstance(hook_owner, dict) or not hook_owner.get("session")
                or hook_owner.get("session") != owner.get("session")
                or hook_owner.get("host") != owner.get("host", "claude")
                or acting_member() != store.gateway or os.environ.get("IHAV_AGENT_ROOM_BINDING")):
            raise RoomError("Native hook does not match the unbound gateway owner", "identity")
    collected, gaps = collect(store)
    if not collected:
        return {"saved": False, "reason": "No complete structured Admin-Zone in verified native sources",
                "source_gaps": gaps}
    git = checkout(store.project)
    with store.tx() as db:
        room = store.get_room(db)
        if (room.get("owner"), room.get("generation")) != (expected.get("owner"), expected.get("generation")):
            return {"saved": False, "reason": "Gateway changed during capture"}
        old = load(db, room)
        fields = deepcopy(old["fields"]) if old else {}
        retained = set(old.get("retained", []) if old else [])
        changed = bool(old and old.get("source_gaps_at_capture", []) != gaps)
        project_fields_changed = False
        prior_count = old.get("prior_history_count", 0) if old else 0
        prior_fields = set(old.get("prior_history_fields", [])) if old else set()
        archives, staged = [], set()

        def archive(item):
            nonlocal prior_count
            identifier = checksum([item["field"], item["text"]])
            prior_fields.add(item["field"])
            row = db.execute("SELECT value FROM meta WHERE key=?", (LOOKUP_PREFIX + identifier,)).fetchone()
            if row:
                try:
                    number = int(row[0])
                except (ValueError, TypeError):
                    raise RoomError("Archived project lookup is corrupt", "incompatible")
                existing = archived_item(db, room, number) if 1 <= number <= prior_count else None
                if not existing or checksum([existing["field"], existing["text"]]) != identifier:
                    raise RoomError("Archived project lookup is corrupt", "incompatible")
            elif identifier not in staged:
                prior_count += 1
                archives.append((prior_count, identifier, deepcopy(item)))
                staged.add(identifier)

        if old:
            for item in old.get("prior_sections_for_reconciliation", []):
                archive(item)
            changed |= bool(archives)
        for source in collected:
            provenance = {key: value for key, value in source.items() if key != "fields"}
            for key, text in source["fields"].items():
                previous = fields.get(key)
                if previous:
                    same = (previous["host"], previous["session"], previous["source"]) == (
                        source["host"], source["session"], source["source"])
                    if same and source["cursor"] <= previous["cursor"]:
                        continue
                    if not same:
                        before, after = observed_time(previous.get("observed_at")), observed_time(source.get("observed_at"))
                        if after is None or (before is not None and after < before):
                            continue  # Cross-session ordering is unknown without native timestamps.
                if not text:
                    if previous and previous.get("reply_digest") != source["reply_digest"] and key in UNRESOLVED:
                        if key not in retained:
                            retained.add(key)  # Empty display is not proof of resolution.
                            changed = True
                    continue
                if previous and previous["text"] != text and key in UNRESOLVED:
                    archive(dict(previous, field=key))
                    retained.add(key)  # Nonempty replacement also cannot prove old choices resolved.
                replacement = dict(provenance, text=text)
                if previous != replacement:
                    fields[key], changed = replacement, True
                    project_fields_changed |= key in UNRESOLVED
                    if key not in prior_fields:
                        retained.discard(key)
        if not changed:
            return {"saved": False, "reason": "No newer project sections", "revision": old["revision"] if old else None,
                    "source_gaps": gaps}
        record = {"schema": 1, "room": room["id"], "project": room["project"],
                  "revision": (old["revision"] if old else 0) + 1, "saved_at": now(),
                  "fields": fields, "retained": sorted(retained),
                  "prior_sections_for_reconciliation": [], "prior_history_count": prior_count,
                  "prior_history_fields": sorted(prior_fields),
                  "source_gaps_at_capture": gaps,
                  # A gap or retained empty section is not a new closing snapshot.
                  # Keep the old comparison baseline so start can still expose drift.
                  "ledger": ledger_state(db) if project_fields_changed or not old else old["ledger"],
                  "checkout": git if project_fields_changed or not old else old["checkout"],
                  "authority": "historical_data_only"}
        record["digest"] = checksum(record)
        if not write_state(db, room, record, archives):
            return {"saved": False, "reason": "Closing-state byte budget exceeded; previous state preserved",
                    "source_gaps": gaps}
        store.event(db, "context.saved", {"revision": record["revision"], "digest": record["digest"],
                                         "owner": room.get("owner"), "authority": "historical_data_only"})
        return {"saved": True, "revision": record["revision"], "retained": record["retained"],
                "source_gaps": gaps}


def recover(store, full=False):
    store.main_only(store.actor())
    with store.read() as db:
        room = store.get_room(db)
        try:
            record = load(db, room)
            history = prior_page(db, room, record) if record else None
        except RoomError as exc:
            return {"status": "invalid", "reason": str(exc), "authority": "none"}
        current = ledger_state(db)
    if not record:
        return {"status": "missing", "reason": "No durable project closing state yet", "authority": "none"}
    drift = [{"table": table, "id": key, "before": record["ledger"][table].get(key), "now": current[table].get(key)}
             for table in current for key in sorted(set(record["ledger"][table]) | set(current[table]))
             if record["ledger"][table].get(key) != current[table].get(key)]
    git = checkout(store.project)
    compare = ("unknown" if git["status"] != "observed" or record["checkout"]["status"] != "observed"
               else "changed" if git != record["checkout"] else "same_head_and_path_status")
    fields = deepcopy(record["fields"])
    excluded_host_metadata = []
    for key, item in fields.items():
        text, excluded = without_host_citation(item["text"])
        if excluded:
            # Older captures may contain only this known host trailer. Correct
            # the view, retaining the sealed record and all source provenance.
            excluded_host_metadata.append({"field": key, "suffix_only": bool(text),
                                           **{name: value for name, value in item.items() if name != "text"}})
            item.update(text=text, host_metadata_only=not text, host_metadata_trimmed=True)
    prior = history["items"]
    truncated = []
    if not full:
        for key, item in list(fields.items()) + [("prior:" + item["field"], item) for item in prior]:
            raw = item["text"].encode()
            if len(raw) > FIELD_BYTES:
                item["text"] = raw[:FIELD_BYTES].decode("utf-8", "ignore")
                truncated.append(key)
    return {"status": "recovered", "revision": record["revision"], "saved_at": record["saved_at"],
            "fields": fields, "truncated_fields": truncated, "excluded_host_metadata": excluded_host_metadata,
            "full_read_command": "ihav-agent-room --json context --full", "ledger_drift": drift[:8],
            "ledger_drift_count": len(drift), "checkout_comparison": compare,
            "retained_unresolved_sections": record["retained"],
            "prior_sections_for_reconciliation": prior, "authority": "historical_data_only",
            "prior_history_total": history["total"], "prior_history_next_after": history["next_after"],
            "prior_history_read_command": history["read_command"],
            "prior_history_validation": history["history_validation"],
            "source_gaps_at_capture": record.get("source_gaps_at_capture", []),
            "rule": "Rebuild the project's Admin-Zone from these source-linked sections and current ledger. "
                    "Host metadata exclusions change display only; raw prior history and retained work still require reconciliation. "
                    "Retained sections need reconciliation; empty output does not close old work. "
                    "Question options and conditional approvals are historical data, not consent. "
                    "Never replay unknown effects, or report historical tests/logins/estimates as current evidence. "
                    "Git comparison does not validate dirty file contents; private native conversations stay native."}


def readiness(store):
    room = store.room()
    issues = []
    hard_failure = False
    supervisor = room.get("supervisor") or {}
    try:
        live = process_alive(supervisor.get("pid"), supervisor.get("stamp"))
    except (OSError, subprocess.TimeoutExpired, RoomError):
        live = None
    if live is not True:
        issues.append({"member": "supervisor", "reason": "not_live" if live is False else "liveness_unknown"})
    with store.read() as db:
        members = {row["name"]: json.loads(row["data"]) for row in db.execute("SELECT name,data FROM members")}
    for name in MODES[room["mode"]]:
        member = members[name]
        if member.get("unexpected_native_id") or member.get("error"):
            reason = member.get("error") or "native_identity_mismatch"
            if (name != store.gateway and room["status"] == "starting"
                    and member.get("launch_generation") != room.get("generation")):
                issues.append({"member": name, "reason": "previous_generation_error", "detail": reason})
            else:
                issues.append({"member": name, "reason": reason})
                hard_failure = True
        elif (name == store.gateway and member.get("native_id")
              and member["native_id"] != (room.get("owner") or {}).get("session")):
            issues.append({"member": name, "reason": "gateway_identity_mismatch"})
            hard_failure = True
        elif not member.get("native_id"):
            issues.append({"member": name, "reason": "native_identity_missing"})
        elif member.get("status") not in {"active", "idle", "working"}:
            issues.append({"member": name, "reason": member.get("status", "unknown")})
        elif name != store.gateway:
            try:
                member_live = process_alive(member.get("pid"), member.get("stamp"))
            except (OSError, subprocess.TimeoutExpired, RoomError):
                member_live = None
            if member_live is not True:
                issues.append({"member": name, "reason": "worker_liveness_unconfirmed"})
    return {"status": "blocked" if hard_failure or (issues and room["status"] != "starting") else "starting" if issues else "observed_running",
            "issues": issues, "native_readiness": "unverified",
            "basis": "Ledger identity/status plus process inspection; not a fresh native identity, model response or delivery receipt"}
