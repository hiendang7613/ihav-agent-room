"""Attach to an existing Codex host's Unix WebSocket, without owning its thread.

Queue APIs are experimental. Missing capabilities block startup; no resume/start/steer,
approval response or database write substitutes for the host's own dispatch.
"""

import asyncio
import json
import os
from pathlib import Path
import stat

try:
    from websockets.asyncio.client import unix_connect
    from websockets.exceptions import WebSocketException
except ImportError:
    unix_connect = None

    class WebSocketException(Exception):
        """Unavailable SDK placeholder; start fails before transport use."""

from ihav_agent_room import __version__
from ihav_agent_room.common import RoomError


class CodexGateway:
    def __init__(self, project, session, timeout=5):
        self.project, self.session, self.timeout = Path(project).resolve(), session, timeout
        self.connection = None
        self.reader = None
        self.pending = {}
        self.sequence = 0
        self.failure = None

    async def start(self):
        if unix_connect is None:
            raise RoomError("Codex gateway requires the bundled websockets runtime; reinstall the complete plugin package", "dependency")
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        control = home / "app-server-control" / "app-server-control.sock"
        if not control.is_socket():
            raise RoomError("Codex host control socket is unavailable; open this project in a local Codex host", "unavailable")
        # The native locator is normally a symlink into a private daemon directory.
        locator, locator_parent = control.lstat(), control.parent.lstat()
        resolved = control.resolve()
        info, parent = resolved.lstat(), resolved.parent.lstat()
        if (locator.st_uid != os.getuid() or locator_parent.st_uid != os.getuid() or locator_parent.st_mode & 0o022
                or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or parent.st_uid != os.getuid() or parent.st_mode & 0o022):
            raise RoomError("Unexpected Codex control socket owner/type/permissions", "identity")
        try:
            # The control socket speaks WebSocket frames after HTTP Upgrade,
            # not newline JSON. Connect once; unknown writes are never replayed.
            self.connection = await unix_connect(str(resolved), open_timeout=self.timeout,
                close_timeout=2, max_size=16 * 1024 * 1024, max_queue=16, compression=None)
            self.reader = asyncio.create_task(self._read())
            await self.request("initialize", {"clientInfo": {"name": "ihav_room_gateway", "version": __version__},
                                               "capabilities": {"experimentalApi": True}})
            await self.write({"method": "initialized"})
            await self.thread()
            # Probe capability without adding a prompt or starting a model turn.
            result = await self.request("thread/queue/list", {"threadId": self.session, "limit": 1})
            if not isinstance(result.get("data"), list):
                raise RoomError("Codex queue schema is incompatible", "incompatible")
            return self
        except (OSError, TimeoutError, WebSocketException) as exc:
            await self.close()
            raise RoomError(f"Codex control socket connection failed: {type(exc).__name__}", "unavailable") from exc
        except BaseException:
            await self.close()
            raise

    async def _read(self):
        try:
            async for line in self.connection:
                packet = json.loads(line)
                if not isinstance(packet, dict):
                    raise ValueError("Expected an RPC object")
                if "method" in packet:
                    # Host approvals stay with the user's native client. This connection
                    # never resumes/subscribes to the host thread and never answers requests.
                    continue
                future = self.pending.get(packet.get("id"))
                if future and not future.done():
                    future.set_result(packet)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError, WebSocketException) as exc:
            self.failure = RoomError(f"Codex gateway protocol failed: {type(exc).__name__}", "outcome_unknown")
        finally:
            self.failure = self.failure or RoomError("Codex gateway transport closed", "outcome_unknown")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)

    async def write(self, packet):
        if self.failure:
            raise self.failure
        try:
            await asyncio.wait_for(self.connection.send(json.dumps(packet, ensure_ascii=False)), self.timeout)
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise RoomError("Codex gateway write outcome is unknown", "outcome_unknown") from exc

    async def request(self, method, params):
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.write({"id": identity, "method": method, "params": params})
            packet = await asyncio.wait_for(future, self.timeout)
            if "error" in packet:
                code = "incompatible" if packet["error"].get("code") == -32601 else "native_rejected"
                raise RoomError(f"Codex gateway {method} rejected: {packet['error'].get('message', 'unknown')}", code)
            result = packet.get("result")
            if not isinstance(result, dict):
                raise RoomError("Codex gateway returned an invalid RPC result", "incompatible")
            return result
        except asyncio.TimeoutError as exc:
            raise RoomError(f"Codex gateway {method} timed out; outcome unknown", "outcome_unknown") from exc
        finally:
            self.pending.pop(identity, None)

    async def thread(self):
        result = await self.request("thread/read", {"threadId": self.session})
        thread = result.get("thread") or {}
        if (thread.get("id") != self.session or thread.get("ephemeral") is not False
                or not isinstance(thread.get("cwd"), str)
                or Path(thread["cwd"]).resolve() != self.project):
            raise RoomError("Codex gateway must be the exact durable thread in this project", "identity")
        if thread.get("canAcceptDirectInput") is not True:
            raise RoomError("Codex gateway is not attached to a host that accepts input", "unavailable")
        return thread

    async def send(self, message):
        # Revalidate before every mutation. No unknown write is automatically retried.
        await self.thread()
        from_text = message["native_text"]
        result = await self.request("thread/queue/add", {"threadId": self.session,
            "clientUserMessageId": message["id"],
            "input": [{"type": "text", "text": from_text, "text_elements": []}]})
        queued = result.get("queuedSubmission") or {}
        if queued.get("clientUserMessageId") != message["id"] or not queued.get("id"):
            raise RoomError("Codex queue response identity mismatch; reconcile before retry", "outcome_unknown")
        return "accepted"

    async def close(self):
        # Own only this connection. Never interrupt the user's host/thread.
        if self.connection:
            await self.connection.close()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


async def probe_codex(project, session):
    client = CodexGateway(project, session)
    try:
        await client.start()
        return await client.thread()
    finally:
        await client.close()


async def probe_detached_codex(project, current, previous):
    """Read both exact identities through the current host; never resume either."""
    client = CodexGateway(project, current)
    try:
        await client.start()
        result = await client.request("thread/read", {"threadId": previous})
        thread = result.get("thread") or {}
        if (not isinstance(thread, dict) or thread.get("id") != previous or thread.get("ephemeral") is not False
                or not isinstance(thread.get("cwd"), str)
                or Path(thread["cwd"]).resolve() != Path(project).resolve()):
            raise RoomError("The previous Codex conversation could not be verified in this project", "identity")
        status = thread.get("status")
        if (not isinstance(status, dict) or status.get("type") != "notLoaded"
                or thread.get("canAcceptDirectInput") not in (None, False)):
            raise RoomError("Another Codex conversation still owns this room; it will not be replaced", "conflict")
        await client.thread()  # Both observations must use the same still-attached native client.
        return thread
    finally:
        await client.close()
