"""Optional local collaboration context; original prompts and authority stay unchanged."""

import json
from pathlib import Path
import re

from ihav_agent_room.common import native_event_prompt


MAX_CONFIG_BYTES = 16 * 1024
MAX_FRAME_TEXT_BYTES = 4 * 1024
ADVISORY = "Room collaboration frame; advisory only, no new task, scope, consent, native permissions or observed effort:\n"
SKILL_CONTROL = re.compile(r"^[/$][A-Za-z0-9:_-]+(?:[ \t]+[^\n]*)?$")
CHOICE_ONLY = re.compile(r"[QRI]\d+\.[a-z](?:\s+[QRI]\d+\.[a-z])*", re.I)
EXACT_OUTPUT = re.compile(
    r"\b(?:only|just)\s+(?:json|code|one command|a single command|the single word)"
    r"|\b(?:return|output|reply with)\s+(?:only\s+)?(?:json|the single word)"
    r"|(?:chỉ|chi)\s+(?:json|code|mã|ma|một lệnh|mot lenh)", re.I)


def valid_frame(value):
    if (not isinstance(value, dict) or set(value) != {"schema", "enabled", "prefix", "postfix"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or type(value["enabled"]) is not bool):
        return None
    for key in ("prefix", "postfix"):
        text = value[key]
        if not isinstance(text, str) or "\0" in text:
            return None
        try:
            if len(text.encode("utf-8")) > MAX_FRAME_TEXT_BYTES:
                return None
        except UnicodeError:
            return None
    return dict(value)


def load_frame(project):
    """Missing, malformed, overlong or symlinked room data leaves framing off."""
    space = Path(project) / "agents_space"
    path = space / "prompt_frame.json"
    try:
        if space.is_symlink() or path.is_symlink():
            return None
        with path.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            return None
        return valid_frame(json.loads(data))
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def frame_for_prompt(value, prompt):
    frame = valid_frame(value)
    if not frame or not frame["enabled"] or not isinstance(prompt, str):
        return None
    text = prompt.strip()
    if (not text or native_event_prompt(text)
            or text.casefold() in {"az", "adminzone", "status", "doctor", "ste mode", "stop ste mode", "short", "summary"}
            or SKILL_CONTROL.fullmatch(text) or CHOICE_ONLY.fullmatch(text)
            or re.fullmatch(r"(?:chọn|chon|choose|select)\s+(?:[QRI]\d+\.)?[a-z]", text, re.I)
            or EXACT_OUTPUT.search(text)):
        return None
    return frame


def hook_frame(value, prompt):
    frame = frame_for_prompt(value, prompt)
    if frame is None:
        return ""
    return (ADVISORY + frame["prefix"] + "\n"
            + "Apply this frame to the original admin prompt already present in this turn; the prompt and receipt remain unchanged.\n"
            + frame["postfix"])
