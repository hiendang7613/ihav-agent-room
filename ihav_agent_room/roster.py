"""Roster identity and requested native model settings live in one data table.

The gateway is the existing host session, so its model/effort are observed from the host when available, not changed by
the room. Spawned members receive their configured model/effort at the native boundary. Stable ledger ids do not
change; aliases are another name for the same member. The room records which worker is the host-managed gateway.
"""

ROSTER = (
    {"name": "CLAUDE_01", "alias": "CLAUDE_WORKER", "host": "claude", "role": "worker", "gateway": True, "default_mode": True,
     "model": "sonnet", "effort": "xhigh", "label": "Sonnet 5.5", "control": "host"},
    {"name": "CODEX_01", "alias": "CODEX_WORKER", "host": "codex", "role": "worker", "gateway": False, "default_mode": True,
     "model": "gpt-6-luna", "effort": "xhigh", "label": "Luna 6", "control": "room"},
    {"name": "CLAUDE_EXPERT", "alias": None, "host": "claude", "role": "expert", "gateway": False, "default_mode": True,
     "model": "opus", "effort": "xhigh", "label": "Opus 5.5", "control": "room"},
    {"name": "CODEX_EXPERT", "alias": None, "host": "codex", "role": "expert", "gateway": False, "default_mode": True,
     "model": "gpt-6.1-sol", "effort": "xhigh", "label": "Sol 6.1", "control": "room"},
)

MEMBERS = tuple(member["name"] for member in ROSTER)
ROSTER_BY_NAME = {member["name"]: member for member in ROSTER}
DEFAULT_MEMBERS = tuple(member["name"] for member in ROSTER if member["default_mode"])
GATEWAY = next(member["name"] for member in ROSTER if member["gateway"])
ALIASES = {member["alias"]: member["name"] for member in ROSTER if member["alias"]}
LAUNCHED_CLAUDE = next(member["name"] for member in ROSTER if member["host"] == "claude" and not member["gateway"])
HOST_GATEWAYS = {host: next(member["name"] for member in ROSTER if member["host"] == host and member["role"] == "worker")
                 for host in ("claude", "codex")}


def room_gateway(room):
    """Legacy rooms keep their Claude gateway; malformed owner metadata fails closed."""
    owner = room.get("owner") or {}
    host = owner.get("host", "claude")
    if host not in HOST_GATEWAYS:
        raise ValueError("Unknown room owner host")
    gateway = room.get("gateway", HOST_GATEWAYS[host])
    if gateway != HOST_GATEWAYS[host]:
        raise ValueError("Room gateway does not match its owner host")
    return gateway


def canonical_member(name):
    """The stable id for a member id or alias; other text (including an empty name) is returned unchanged."""
    return ALIASES.get(name, name)


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Room modes (admin decision 2026-10-03): `pair` is the default for new rooms (it supersedes DEC-021's four-member
# default for new rooms only); `advisors` is the four-member room. The stored legacy modes `default` and `full` keep
# all four members and the advisors settings, so existing rooms do not change silently.
MODE_MEMBERS = {
    "pair": ("CLAUDE_01", "CODEX_01"),
    "advisors": MEMBERS,
    "default": DEFAULT_MEMBERS,
    "full": MEMBERS,
}
NEW_ROOM_MODE = "pair"
SELECTABLE_MODES = ("pair", "advisors")
MODE_SETTINGS = {
    "pair": {"CLAUDE_01": {"model": "opus", "effort": "medium", "label": "Opus 5.5"},
             "CODEX_01": {"model": "gpt-6.1-sol", "effort": "medium", "label": "Sol 6.1"}},
    "advisors": {member["name"]: {"model": member["model"], "effort": member["effort"], "label": member["label"]}
                 for member in ROSTER},
}
LEGACY_MODE_SETTINGS = {"default": "advisors", "full": "advisors"}


def mode_settings(mode, name):
    """Requested model/effort for one member in one mode. The gateway's values are advice; the host applies them."""
    settings = MODE_SETTINGS[LEGACY_MODE_SETTINGS.get(mode, mode)]
    name = canonical_member(name)
    return dict(settings.get(name) or MODE_SETTINGS["advisors"][name])


def launch_config(name):
    """Native model settings for spawned members; the existing gateway remains host-managed."""
    member = ROSTER_BY_NAME.get(canonical_member(name))
    if not member or member["control"] != "room":
        return None
    return {"model": member["model"], "effort": member["effort"]}
