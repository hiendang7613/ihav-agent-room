#!/usr/bin/env python3
"""Create a local plugin ZIP. Does not install, publish or invoke a provider."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ihav_agent_room import __version__
from ihav_agent_room.common import PLUGIN_ROOT, RoomError
from ihav_agent_room.package import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PLUGIN_ROOT / "dist" / f"ihav-agent-room-{__version__}.zip")
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output), indent=2))
    except (RoomError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
