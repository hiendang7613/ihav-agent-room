"""Native Claude Code / Codex room coordination. No model execution loop."""

from pathlib import Path
import sys

# Ship a pure Python SDK wheel with the plugin. Native hosts may use an
# interpreter without site packages; starting a room must not run pip.
_websocket_wheel = Path(__file__).resolve().parent.parent / "third_party" / "websockets-14.2-py3-none-any.whl"
if _websocket_wheel.is_file():
    sys.path.insert(0, str(_websocket_wheel))

__version__ = "0.8.9"
# bin/ihav-agent-room runs only releases that declare the same launcher protocol.
LAUNCHER_PROTOCOL = 1
