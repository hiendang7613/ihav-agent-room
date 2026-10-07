"""Keep tests away from the real machine agents space and active-release pointer (~/.ihav)."""

import os
import tempfile

import pytest

os.environ["IHAV_HOME"] = tempfile.mkdtemp(prefix="ihav home tests ")


@pytest.fixture(autouse=True)
def isolate_native_identity(monkeypatch):
    """Fixtures choose their own host; never inherit a real room or Codex thread."""
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                 "IHAV_AGENT_ROOM_HOST", "IHAV_AGENT_ROOM_MEMBER", "IHAV_AGENT_ROOM_BINDING",
                 "IHAV_AGENT_ROOM_SESSION_ID", "IHAV_AGENT_ROOM_PERMISSION_MODE"):
        monkeypatch.delenv(name, raising=False)
