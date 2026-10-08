"""Test-owned Unix WebSocket host; no model calls or external network."""

import asyncio
import json
from pathlib import Path
import signal
import sys
import uuid

from websockets.asyncio.server import unix_serve

from fake_native import ROOT, record


async def handle(connection):
    async for text in connection:
        packet = json.loads(text)
        record("codex_packet", packet)
        method, params = packet.get("method"), packet.get("params", {})
        identity = packet.get("id")
        if method == "initialized":
            continue
        if method == "initialize":
            result = {"userAgent": "fake-unix-websocket-host"}
        elif method == "thread/read":
            threads = [json.loads(path.read_text()) for path in
                       (ROOT / "codex-host.json", ROOT / "codex-previous.json") if path.exists()]
            thread = next((thread for thread in threads if thread["id"] == params.get("threadId")), None)
            if thread is None:
                await connection.send(json.dumps({"id": identity, "error": {"code": -32602, "message": "Thread unavailable"}}))
                continue
            result = {"thread": thread}
        elif method == "thread/queue/list":
            if (ROOT / "queue_unsupported").exists():
                await connection.send(json.dumps({"id": identity, "error": {"code": -32601, "message": "Queue unavailable"}}))
                continue
            result = {"data": [], "nextCursor": None}
        elif method == "thread/queue/add":
            record("codex_gateway_queue", params)
            if (ROOT / "queue_crash_after_input").exists():
                connection.transport.abort()
                return
            result = {"queuedSubmission": {"id": str(uuid.uuid4()),
                "clientUserMessageId": params["clientUserMessageId"], "input": params["input"]}}
        else:
            await connection.send(json.dumps({"id": identity, "error": {"code": -32601, "message": "Unsupported host method"}}))
            continue
        await connection.send(json.dumps({"id": identity, "result": result}))


async def main(path):
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    async with unix_serve(handle, path, compression=None):
        Path(path + ".ready").touch()
        await stopped.wait()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
