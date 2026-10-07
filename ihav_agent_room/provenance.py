"""Host transcript labels as deny-only evidence about who sent a prompt.

The UserPromptSubmit payload has no origin field, but the host transcript labels each row. A row is
found by its text near the file offset recorded when the prompt arrived, so an older identical row
cannot stand in for it. A label can refuse a receipt; a confirmed human row is the only thing that lets a
receipt act on admin authority (Store.source applies DEC-009: unverified or absent provenance is refused
for the protected uses). One transcript row backs at most one receipt.
"""

import json
import os

from ihav_agent_room.common import strip_peer_wrapper

# Host transcript labels (origin.kind) for messages that a human did not type.
NON_HUMAN_ORIGINS = frozenset({"peer", "task-notification", "coordinator", "channel", "observer", "slack-ping",
                               "observer-activity", "plugin", "unclassified"})
HUMAN_ORIGINS = frozenset({"human"})
WINDOW_MARGIN = 8 * 1024
WINDOW_LIMIT = 2 * 1024 * 1024


def transcript_size(path):
    """Current size of a transcript file; None when there is no usable transcript."""
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def row_text(row):
    """(text, origin) of a transcript row that carries prompt text, else (None, None)."""
    attachment = row.get("attachment") or {}
    if row.get("type") == "response_item" and isinstance(row.get("payload"), dict):
        payload = row["payload"]
        if payload.get("type") != "message" or payload.get("role") != "user":
            return None, None
        text = payload.get("content")
        origin = payload.get("origin") or row.get("origin")
        if payload.get("clientId") or payload.get("client_user_message_id"):
            origin = {"kind": "plugin"}  # An injected input is never a human approval.
        if isinstance(text, list):
            text = "\n".join(block.get("text", "") for block in text if isinstance(block, dict)
                             and block.get("type") in {"input_text", "text"})
    elif row.get("type") == "user":
        text, origin = (row.get("message") or {}).get("content"), row.get("origin")
        if isinstance(text, list):
            text = "\n".join(block.get("text", "") for block in text if isinstance(block, dict) and block.get("type") == "text")
    elif row.get("type") == "attachment" and attachment.get("type") == "queued_command":
        text, origin = attachment.get("prompt"), attachment.get("origin")
    else:
        return None, None
    return (text if isinstance(text, str) else None), origin


HOST_LABELS = ("entrypoint", "promptSource", "turnOrigin")


def verdict(state, reason=None, kind=None, row=None, labels=None):
    """state is human, non_human or unverified; `row` is the byte offset of the transcript row that decided it.

    `labels` are the host's own markers on that row (entrypoint, promptSource, turnOrigin). They are recorded so the
    delivery route can be observed; they never change the decision.
    """
    return {key: value for key, value in (("state", state), ("reason", reason), ("kind", kind), ("row", row),
                                           ("labels", labels or None)) if value is not None}


def host_labels(row):
    """The host's own markers on a transcript row, as strings."""
    return {key: row[key] for key in HOST_LABELS if isinstance(row.get(key), str)}


def window_rows(path, offset, wanted):
    """Transcript rows whose text is `wanted` near `offset`, as dicts with start, end and origin kind; (None, reason) if unusable."""
    if transcript_size(path) is None or not isinstance(offset, int) or offset < 0:
        return None, "no usable transcript path or offset"
    start = max(0, offset - (2 * len(wanted.encode()) + WINDOW_MARGIN))
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            data = stream.read(WINDOW_LIMIT)
    except OSError:
        return None, "transcript unreadable"
    rows, position = [], start
    for index, line in enumerate(data.split(b"\n")):
        here, position = position, position + len(line) + 1
        if not line.strip() or (start and index == 0):  # A window cut starts mid-row.
            continue
        try:
            row = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        text, origin = row_text(row) if isinstance(row, dict) else (None, None)
        if text is not None and wanted in (text.strip(), strip_peer_wrapper(text)):
            rows.append({"start": here, "end": position, "kind": origin.get("kind") if isinstance(origin, dict) else None,
                         "labels": host_labels(row)})
    return rows, None


def gap(row, offset):
    """Distance from the hook offset: a row written before it ends at or before it, a later row starts at or after it.

    A tie goes to the row written before the hook (the usual order), so a row that starts where another ends is not
    mistaken for it.
    """
    return (offset - row["end"], 0) if row["end"] <= offset else (max(0, row["start"] - offset), 1)


def assess_chain(path, offsets, body):
    """Verdict for the receipt with hook offset offsets[-1]; earlier receipts with the same text claim their rows first.

    One transcript row backs at most one receipt, so a later receipt with the same text cannot reuse a row that an
    earlier receipt already has.
    """
    wanted, claimed, result = body.strip(), set(), verdict("unverified", "empty prompt")
    if not wanted:
        return result
    for offset in offsets:
        rows, reason = window_rows(path, offset, wanted)
        if rows is None:
            result = verdict("unverified", reason)
            continue
        free = [row for row in rows if row["start"] not in claimed]
        if not free:
            result = verdict("unverified", "the transcript row already backs an earlier receipt" if rows else "no matching transcript row near the prompt")
            continue
        best = min(free, key=lambda row: gap(row, offset))
        claimed.add(best["start"])
        if best["kind"] in HUMAN_ORIGINS:
            result = verdict("human", row=best["start"], labels=best["labels"])
        elif best["kind"] in NON_HUMAN_ORIGINS:
            result = verdict("non_human", kind=best["kind"], row=best["start"], labels=best["labels"])
        else:
            reason = (f"unrecognized transcript origin: {best['kind']}" if best["kind"]
                      else "transcript row has no origin label")
            result = verdict("unverified", reason, kind=best["kind"], row=best["start"], labels=best["labels"])
    return result


def assess(path, offset, body):
    """Verdict for one prompt from the transcript row nearest to `offset`: human, non_human or unverified.

    `offset` is the transcript size when the hook ran. The row is either just before it (already written) or just
    after it (written later), so only rows near it count.
    """
    return assess_chain(path, [offset], body)
