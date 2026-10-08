"""Qualified L5 update plans and a shared, durable attempt journal.

These adapters do not execute host commands. Catalog snapshots and peer notices
do not supply qualification or authority. An owning installer must verify the
source-bound qualification, rollback inventory, native permissions and actual
host capability before dispatch, then supply its independent postimage here.
The independent fleet tool remains owned by the ihav catalog project.
"""

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import secrets
import time
from urllib.parse import urlsplit

from ihav_agent_room.common import RoomError

CANARY_SECONDS = 3600
DAILY_SECONDS = 86400
PREFIX = "catalog_update_v1:"
ACTIVE_KEY = PREFIX + "active"
LAST_ACTIVATION_KEY = PREFIX + "last_activation"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PLUGIN_NAME = re.compile(r"ihav-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def source_identity(source):
    """Only pinned HTTPS Git sources; command/headers-helper sources stay held."""
    if not isinstance(source, dict) or source.get("source") not in {"url", "git-subdir"}:
        return None
    if source.keys() & {"command", "headersHelper"}:
        return None
    url, ref, sha = source.get("url"), source.get("ref"), source.get("sha")
    if not isinstance(url, str) or not isinstance(ref, str) or not isinstance(sha, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
    except ValueError:
        return None
    if not VERSION.fullmatch(ref.removeprefix("v")) or not ref.startswith("v") or not HEX40.fullmatch(sha):
        return None
    path = source.get("path", ".")
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or ":" in path:
        return None
    if source["source"] == "url" and str(relative) != ".":
        return None
    return {"source": source["source"], "url": url, "path": str(relative), "ref": ref, "sha": sha}


def catalog_entry(snapshot, name):
    catalogs = snapshot.get("catalogs")
    if not isinstance(catalogs, dict):
        return None
    selected = []
    for host in ("claude", "codex"):
        catalog = catalogs.get(host)
        if not isinstance(catalog, dict) or catalog.get("name") != "ihav" or not isinstance(catalog.get("plugins"), list):
            return None
        entries = catalog["plugins"]
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in entries):
            return None
        names = [item["name"] for item in entries]
        if len(names) != len(set(names)):
            return None
        matches = [item for item in entries if item["name"] == name]
        if len(matches) != 1 or matches[0].keys() & {"command", "headersHelper"}:
            return None
        selected.append(source_identity(matches[0].get("source")))
    return selected[0] if selected[0] is not None and selected[0] == selected[1] else None


def plan_update(snapshot, name, host, qualification, installed, policy, *, now=None):
    """Pure plan from already verified owner evidence, never native readiness.

    A caller cannot turn catalog JSON, CLI success or a peer approval into the
    required source-bound qualification. No command is formed for a held plan.
    """
    now = time.time() if now is None else now
    held = {"state": "held", "host": host, "plugin": name, "reason": "invalid_evidence"}

    def refuse(reason):
        return dict(held, reason=reason)

    if not timestamp(now) or not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name):
        return refuse("invalid_target")
    if host not in {"claude", "codex"}:
        return refuse("unsupported_host")
    if not all(isinstance(value, dict) for value in (snapshot, qualification, installed, policy)):
        return refuse("missing_evidence")
    if policy.get("enabled") is not True:
        return refuse("automatic_updates_not_enabled")
    url, head = snapshot.get("url"), snapshot.get("head")
    if not isinstance(url, str) or url != policy.get("catalog_url") or not isinstance(head, str) or not HEX40.fullmatch(head):
        return refuse("catalog_identity_mismatch")
    source = catalog_entry(snapshot, name)
    if source is None:
        return refuse("unqualified_catalog_entry")
    check = qualification.get("catalog_check")
    artifacts = qualification.get("plugins")
    qualified = artifacts.get(name) if isinstance(artifacts, dict) else None
    if qualification.get("catalog_url") != url or qualification.get("catalog_head") != head:
        return refuse("qualification_revision_mismatch")
    if not isinstance(check, dict) or check.get("passed") is not True or not isinstance(check.get("artifact_sha256"), str) or not HEX64.fullmatch(check["artifact_sha256"]):
        return refuse("catalog_check_not_verified")
    if not isinstance(qualified, dict) or source_identity(qualified.get("source")) != source or qualified.get("tag_commit") != source["sha"]:
        return refuse("tag_or_source_not_verified")
    version, payload = qualified.get("version"), qualified.get("payload_sha256")
    if version != source["ref"][1:] or not isinstance(payload, str) or not HEX64.fullmatch(payload):
        return refuse("qualified_payload_mismatch")
    if qualified.get("host_versions") != {"claude": version, "codex": version}:
        return refuse("host_manifest_mismatch")
    if installed.get("name") != name or installed.get("marketplace") != "ihav" or installed.get("enabled") is not True or installed.get("scope") != "user":
        return refuse("preserve_existing_selection")
    current_source = source_identity(installed.get("source"))
    current_version = installed.get("version")
    if current_source is None or not isinstance(current_version, str) or not VERSION.fullmatch(current_version):
        return refuse("installed_identity_unverified")
    if (current_source["source"], current_source["url"], current_source["path"]) != (source["source"], source["url"], source["path"]):
        return refuse("installed_source_mismatch")
    if tuple(map(int, version.split("."))) < tuple(map(int, current_version.split("."))):
        return refuse("downgrade_requires_owner")
    if current_version == version:
        if current_source != source or installed.get("payload_sha256") != payload:
            return refuse("version_identity_collision")
        return {"state": "current", "host": host, "plugin": name, "reason": "matching_installed_identity", "loaded": False}
    kind = qualified.get("release_kind", "routine")
    if kind not in {"routine", "security", "blocking"}:
        return refuse("release_kind_unverified")
    if kind == "routine":
        canary = qualified.get("canary")
        if not isinstance(canary, dict) or canary.get("room") != "ihav-agent-room" or canary.get("passed") is not True or canary.get("sha") != source["sha"] or canary.get("version") != version:
            return refuse("canary_not_verified")
        start, end = canary.get("started_at"), canary.get("verified_at")
        if not timestamp(start) or not timestamp(end) or end > now or end - start < CANARY_SECONDS:
            return refuse("canary_hour_not_verified")
        last = policy.get("last_activation")
        if last is not None:
            if not isinstance(last, dict) or not timestamp(last.get("at")) or last["at"] > now or not isinstance(last.get("head"), str) or not HEX40.fullmatch(last["head"]):
                return refuse("activation_history_unverified")
            if last["head"] != head and now - last["at"] < DAILY_SECONDS:
                return refuse("daily_batch_hold")
    rollback = installed.get("rollback")
    if not isinstance(rollback, dict) or not isinstance(rollback.get("path"), str) or not rollback["path"].startswith("/") or not isinstance(rollback.get("sha256"), str) or not HEX64.fullmatch(rollback["sha256"]):
        return refuse("rollback_inventory_missing")
    if host == "codex":
        # Installed help exposes marketplace upgrade and add, but no documented
        # existing-plugin update. Do not invent remove/add or claim reinstall safety.
        return refuse("supported_plugin_update_unverified")
    selector = name + "@ihav"
    plan = {"state": "eligible", "host": host, "plugin": name, "selector": selector,
            "catalog_url": url, "catalog_head": head, "source": source, "version": version,
            "payload_sha256": payload, "catalog_check_sha256": check["artifact_sha256"],
            "release_kind": kind, "rollback": dict(rollback), "planned_at": now, "loaded": False,
            "commands": [["claude", "plugin", "marketplace", "update", "ihav", "--json"]]}
    plan["digest"] = checksum(plan)
    return plan


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("state") != "eligible" or plan.get("host") != "claude":
        raise RoomError("Only a qualified executable host plan can reserve an attempt", "invalid")
    copied = dict(plan)
    digest = copied.pop("digest", None)
    name = copied.get("plugin")
    if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name) or digest != checksum(copied):
        raise RoomError("Catalog update plan changed after qualification", "conflict")
    selector = name + "@ihav"
    expected = [["claude", "plugin", "marketplace", "update", "ihav", "--json"]]
    if copied.get("selector") != selector or copied.get("commands") != expected:
        raise RoomError("Unsupported host update command", "invalid")
    return copied


def plugin_update_command(plan, refreshed_snapshot):
    """Form the plugin mutation only after the refresh still matches qualification.

    This compares supplied, independently read metadata; it does not execute a
    refresh or attest which catalog a native process will subsequently consume.
    """
    validate_plan(plan)
    if not isinstance(refreshed_snapshot, dict) or refreshed_snapshot.get("url") != plan["catalog_url"] or refreshed_snapshot.get("head") != plan["catalog_head"] or catalog_entry(refreshed_snapshot, plan["plugin"]) != plan["source"]:
        return {"state": "held", "reason": "refreshed_catalog_requires_qualification"}
    return {"state": "eligible", "command": ["claude", "plugin", "update", plan["selector"],
                                               "--scope", "user", "--json"]}


class UpdateJournal:
    """Use the existing machine ledger; a reservation is not write authority.

    Commit intent before an installer effect. After a crash or ambiguous result,
    retain the global hold; only owning reconciliation can settle that attempt.
    No method executes a native command, removes a cache or restores a profile.
    """

    def __init__(self, space):
        self.space = space

    @contextmanager
    def transaction(self):
        db = self.space.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def read(db, key):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise RoomError("Catalog update journal is corrupt; reconcile it before dispatch", "incompatible") from exc
        if not isinstance(value, dict):
            raise RoomError("Catalog update journal is not an object", "incompatible")
        return value

    @staticmethod
    def identity(plan):
        return checksum({key: plan[key] for key in ("host", "selector", "source", "version", "payload_sha256")})

    def begin(self, plan, *, now=None):
        validate_plan(plan)
        now = time.time() if now is None else now
        if not timestamp(now):
            raise RoomError("Invalid attempt timestamp", "invalid")
        if not timestamp(plan.get("planned_at")) or now < plan["planned_at"]:
            raise RoomError("Attempt time predates its qualified plan", "conflict")
        attempt_id = self.identity(plan)
        with self.transaction() as db:
            previous = self.read(db, PREFIX + attempt_id)
            if previous is not None:
                return {"started": False, "attempt": attempt_id, "state": previous.get("state"),
                        "reason": "matching_installed_attempt" if previous.get("state") == "installed" else "attempt_requires_reconciliation"}
            active = self.read(db, ACTIVE_KEY)
            if active is not None:
                return {"started": False, "attempt": active.get("attempt"), "state": "held",
                        "reason": "active_attempt_requires_reconciliation"}
            last = self.read(db, LAST_ACTIVATION_KEY)
            if plan["release_kind"] == "routine" and last is not None:
                if not timestamp(last.get("at")) or last["at"] > now or not isinstance(last.get("head"), str) or not HEX40.fullmatch(last["head"]):
                    raise RoomError("Invalid catalog activation history", "incompatible")
                if last["head"] != plan["catalog_head"] and now - last["at"] < DAILY_SECONDS:
                    return {"started": False, "state": "held", "reason": "daily_batch_hold"}
            record = {"schema": 1, "attempt": attempt_id, "token": secrets.token_hex(16), "state": "intent",
                      "created_at": now, "plan": plan, "loaded": False}
            db.execute("INSERT INTO meta VALUES (?, ?)", (PREFIX + attempt_id, json.dumps(record)))
            db.execute("INSERT INTO meta VALUES (?, ?)", (ACTIVE_KEY, json.dumps({"attempt": attempt_id})))
            return {"started": True, "attempt": attempt_id, "token": record["token"], "state": "intent"}

    def finish(self, attempt_id, token, *, exit_code, observed=None, now=None):
        """A zero command exit needs an independently verified matching postimage.

        Any other result stays unknown, preserving the machine-wide hold. Neither
        an installed result nor a supplied postimage establishes session loading.
        """
        now = time.time() if now is None else now
        if not timestamp(now):
            raise RoomError("Invalid result timestamp", "invalid")
        with self.transaction() as db:
            record = self.read(db, PREFIX + attempt_id)
            active = self.read(db, ACTIVE_KEY)
            if not record or record.get("token") != token or record.get("state") != "intent" or not active or active.get("attempt") != attempt_id:
                raise RoomError("Attempt ownership or state changed; reconcile before recording a result", "conflict")
            plan = record["plan"]
            validate_plan(plan)
            if not timestamp(record.get("created_at")) or now < record["created_at"]:
                raise RoomError("Result time predates its reserved intent", "conflict")
            expected = {key: plan[key] for key in ("host", "selector", "source", "version", "payload_sha256")}
            matching = isinstance(observed, dict) and all(observed.get(key) == value for key, value in expected.items())
            installed = type(exit_code) is int and exit_code == 0 and matching
            record.update(state="installed" if installed else "unknown", finished_at=now,
                          exit_code=exit_code if type(exit_code) is int else None,
                          reason="verified_installed_identity" if installed else "native_effect_requires_reconciliation")
            db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(record), PREFIX + attempt_id))
            if installed:
                db.execute("DELETE FROM meta WHERE key=?", (ACTIVE_KEY,))
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                           (LAST_ACTIVATION_KEY, json.dumps({"head": plan["catalog_head"], "at": now})))
            return {"attempt": attempt_id, "state": record["state"], "reason": record["reason"], "loaded": False,
                    "rollback": plan["rollback"]}
