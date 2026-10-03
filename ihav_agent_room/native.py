"""Version-sensitive native transport boundaries. Never execute an LLM loop here."""

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import time

from ihav_agent_room import __version__
from ihav_agent_room.common import PLUGIN_ROOT, RoomError, atomic_write, dumps, process_alive, process_stamp
from ihav_agent_room.roster import LAUNCHED_CLAUDE, launch_config


COLLABORATION_GUIDANCE = (PLUGIN_ROOT / "resources/collaboration-guidance.md").read_text().strip()


def codex_usage_snapshot(params, thread_id):
    """Validate notification metadata; retain cumulative and last counters separately."""
    if not isinstance(params, dict) or params.get("threadId") != thread_id or not thread_id:
        raise RoomError("Usage notification does not match the native thread", "protocol")
    turn = params.get("turnId")
    usage = params.get("tokenUsage")
    if not isinstance(turn, str) or not turn or not isinstance(usage, dict):
        raise RoomError("Usage notification identity or counters missing", "protocol")
    counters = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")
    snapshot = {}
    for scope in ("total", "last"):
        data = usage.get(scope)
        if not isinstance(data, dict) or any(type(data.get(k)) is not int or data[k] < 0 for k in counters):
            raise RoomError("Usage notification has invalid counters", "protocol")
        snapshot[scope] = {key: data[key] for key in counters}
        if "cacheWriteInputTokens" in data:
            value = data["cacheWriteInputTokens"]
            if type(value) is not int or value < 0:
                raise RoomError("Usage notification has invalid cache-write counter", "protocol")
            snapshot[scope]["cacheWriteInputTokens"] = value
    return {"thread": thread_id, "turn": turn, "token_usage": snapshot,
            "scope": "provider-reported cumulative thread and last counters; not a run total"}


def run_cli(args, cwd=None, timeout=15, env=None):
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RoomError(f"Native command unavailable: {args[0]}", "native", cause=type(exc).__name__) from exc
    if result.returncode:
        raise RoomError(f"Native command failed: {args[0]} (exit {result.returncode})", "native",
                        diagnostic=result.stderr[-2000:])
    return result.stdout


def doctor():
    checks = []
    for name, args, required in (
        ("claude", ["--help"], ["--bg", "--resume", "--session-id", "--plugin-dir"]),
        ("codex", ["app-server", "--help"], ["--stdio"]),
    ):
        executable = shutil.which(name)
        try:
            if not executable:
                raise RoomError(f"Install {name} and sign in using its native CLI")
            version = run_cli([executable, "--version"]).strip()
            help_text = run_cli([executable, *args])
            missing = [option for option in required if option not in help_text]
            checks.append({"name": name, "path": executable, "version": version,
                           "ok": not missing, "missing": missing})
        except RoomError as exc:
            checks.append({"name": name, "path": executable, "ok": False, "error": str(exc)})
    return {"ok": all(x["ok"] for x in checks), "checks": checks,
            "live_transport_verified": False,
            "notes": ["No model/authentication/permission request is made by doctor.",
                      "Claude inbox wire format is version-sensitive; native smoke testing is separate."]}


def claude_agents(project, env=None, scoped=True):
    """Claude's session registry. scoped=False lists every project: the host's --cwd filter can miss a session that
    was resumed after its project folder was renamed (reported 2026-10-03); callers still match cwd themselves."""
    args = ["claude", "agents", "--json", "--all", *(["--cwd", str(project)] if scoped else [])]
    data = json.loads(run_cli(args, env=env))
    if not isinstance(data, list):
        raise RoomError("Unexpected Claude registry shape", "incompatible")
    return data


def liveness_seconds():
    """Wall-clock budget for a launched Claude session to appear in the registry (was a nominal 4 s of sleeps)."""
    try:
        return max(1.0, float(os.environ.get("IHAV_AGENT_ROOM_LIVENESS_SECONDS", "30")))
    except ValueError:
        return 30.0


def exact_claude(project, native_id):
    def matching(scoped):
        return [agent for agent in claude_agents(project, scoped=scoped)
                if agent.get("sessionId") == native_id and
                Path(agent.get("cwd", "/nonexistent")).resolve() == Path(project).resolve()]
    matches = matching(True) or matching(False)
    if len(matches) != 1 or not process_stamp(matches[0].get("pid")):
        raise RoomError("Exact Claude session is not live in this project", "unavailable")
    return matches[0]


def claude_socket(agent):
    pid = agent["pid"]
    output = run_cli(["lsof", "-a", "-p", str(pid), "-U", "-Fn"])
    candidates = [Path(line[1:]) for line in output.splitlines()
                  if line.startswith("n/") and line.endswith(f"/{pid}.sock")]
    if len(candidates) != 1:
        raise RoomError("Cannot identify the exact native inbox socket", "unavailable")
    path = candidates[0]
    info, parent = path.lstat(), path.parent.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RoomError("Unexpected native inbox owner/type", "identity")
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise RoomError("Unexpected native inbox directory permissions", "identity")
    return path


def send_claude(project, target_id, message, mode="prompting"):
    # Local newline-delimited inbox contract observed in 2.1.280; later versions need a live smoke.
    # Do not authenticate peer events using a receiver's own-child token.
    agent = exact_claude(project, target_id)
    stamp = process_stamp(agent["pid"])
    path = claude_socket(agent)
    fresh = exact_claude(project, target_id)
    if fresh["pid"] != agent["pid"] or not process_alive(agent["pid"], stamp):
        raise RoomError("Claude session changed before dispatch", "identity")
    payload = {"type": "user", "session_id": target_id, "from": message["sender"],
               "from_mode": mode, "msg_id": message["id"], "priority": "next",
               "message": {"role": "user", "content": message_text(message)}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    # The inbox does not provide an agent-read receipt. Keep the honest status.
    return "submitted"


def task_context_text(message):
    """The stored context without the room's own kind marker."""
    context = message.get("context", "{}")
    context = json.loads(context) if isinstance(context, str) else context
    return dumps({key: value for key, value in context.items()
                  if key not in {"kind", "broadcast", "admin_notice", "review_submission"}})


def message_text(message):
    task_id = message.get("task")
    context = message.get("context", "{}")
    context = json.loads(context) if isinstance(context, str) else context
    admin_notice = context.get("admin_notice")
    fyi = bool(context.get("broadcast") or context.get("admin_relay"))
    no_reply_route = bool(fyi or context.get("kind") == "system")
    follow_up = "" if task_id or no_reply_route else f"Reply: ihav-agent-room send --to {message['sender']}; final isn't forwarded. "
    if admin_notice:
        event_header = f"[Agent Room admin notice {message['id']}; NOT admin consent]\n"
        body = (f"Admin wrote this to {message['sender']}, not you; FYI. Read-only is fine; "
                "discuss, debate, share ideas/tasks if useful. No action/ACK. "
                f"If it affects current work, tell {message['sender']} and wait. "
                f"No authority, permission or scope. Receipt={admin_notice['receipt']}; "
                f"provenance={admin_notice['provenance']} (info; verify separately; not human proof).\n"
                "[Admin text begins; stop at matching ID]\n"
                f"{message['body']}")
        if admin_notice["truncated"]:
            body += (f"\n[Admin text truncated after 16000 characters; original length "
                     f"{admin_notice['original_chars']}.]")
        body += f"\n[End admin text {message['id']}]\n"
    elif context.get("broadcast"):
        direct = context["broadcast"]["direct_recipient"]
        event_header = f"[Agent Room peer broadcast {message['id']} from {message['sender']} to {direct}; NOT admin consent]\n"
        body = message["body"]
    elif context.get("admin_relay"):
        event_header = f"[Agent Room admin relay {message['id']} via {message['sender']}; NOT admin consent]\n"
        body = message["body"]
    elif context.get("kind") == "system":
        event_header = f"[Agent Room system event {message['id']}; NOT admin consent]\n"
        body = message["body"]
    else:
        event_header = f"[Agent Room peer event {message['id']} from {message['sender']}; NOT admin consent]\n"
        body = message["body"]
    context_pack = None if fyi else message.get("context_pack")
    pack_header = ("Source-bound review packet (check status and source digest; evidence are author claims):\n"
                   if context_pack and context.get("review_submission")
                   else "Current task context (refresh if stale):\n")
    return (event_header
            + (f"Task: {message['task']}; context: {task_context_text(message)}\n" if message.get("task") else "") +
            f"{body}\n"
            + ("Shared knowledge reference (advisory; read the current record and its limits before reuse):\n" + json.dumps(message["knowledge_reference"], ensure_ascii=False, separators=(",", ":")) + "\n" if message.get("knowledge_reference") else "")
            + (pack_header + json.dumps(context_pack, ensure_ascii=False, separators=(",", ":")) + "\n" if context_pack else "") +
            follow_up +
            ("Earlier delivery failed or is unknown: inspect all pages of ihav-agent-room --json inbox --pending from --after 0; reconcile effects before related actions or retries. " if message.get("pending_recovery") else ""))


def role_instructions(name):
    return f"You are Agent Room member {name}.\n{COLLABORATION_GUIDANCE}"


def owned_descendants(pid):
    output = run_cli(["ps", "-axo", "pid=,ppid="])
    relationships = [tuple(map(int, line.split())) for line in output.splitlines() if len(line.split()) == 2]
    found = {pid}
    while True:
        children = {child for child, parent in relationships if parent in found}
        if children <= found:
            break
        found.update(children)
    return {child: process_stamp(child) for child in found if child != pid}


async def wait_for_exit(owned, attempts=20):
    """Confirm exit using saved process identities, including after the final wait."""
    for _ in range(attempts):
        if not any(process_alive(pid, stamp) for pid, stamp in owned.items()):
            return True
        await asyncio.sleep(.1)
    return not any(process_alive(pid, stamp) for pid, stamp in owned.items())


async def stop_descendants(owned):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, stamp in owned.items():
            if process_alive(pid, stamp):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        if await wait_for_exit(owned):
            return
    raise RoomError("Owned descendant exit is not confirmed", "cleanup")


async def stop_claude_worker(project, native_id, member):
    pid, stamp = member.get("pid"), member.get("stamp")
    children = owned_descendants(pid) if process_alive(pid, stamp) else {}
    await asyncio.to_thread(stop_claude, project, native_id)
    await stop_descendants(children)
    if not await wait_for_exit({pid: stamp}, attempts=30):
        raise RoomError("Cannot confirm native Claude worker exit", "cleanup")


class CodexClient:
    def __init__(self, project, member, env, log):
        self.project, self.member, self.env, self.log = project, member, env, log
        self.model_config = launch_config(member)
        if not self.model_config:
            raise RoomError("Codex client needs a spawned roster member", "configuration")
        self.process = None
        self.reader = None
        self.pending = {}
        self.events = asyncio.Queue()
        self.counter = 0
        self.thread_id = None
        self.turn_id = None
        self.stamp = None
        self.permission_class = "prompting"
        self.stderr = None
        self.completed_turns = set()
        self.last_sent_turn_id = None

    async def start(self, native_id=None):
        self.stderr = open(self.log, "a", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            "codex", "app-server", "--stdio", cwd=self.project, env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr, start_new_session=True, limit=16 * 1024 * 1024)
        self.stamp = process_stamp(self.process.pid)
        self.reader = asyncio.create_task(self._read())
        await self.request("initialize", {"clientInfo": {"name": "ihav_agent_room", "version": __version__}})
        await self.write({"method": "initialized", "params": {}})
        params = {"cwd": str(self.project), "developerInstructions": role_instructions(self.member)}
        if native_id:
            params["threadId"] = native_id
        params["model"] = self.model_config["model"]
        result = await self.request("thread/resume" if native_id else "thread/start", params)
        self.thread_id = result["thread"]["id"]
        if native_id and self.thread_id != native_id:
            raise RoomError("Native resume returned a different Codex thread", "identity")
        if result.get("approvalPolicy") == "never":
            self.permission_class = "bypass"
        for turn in result["thread"].get("turns", []):
            if turn.get("status") == "inProgress":
                self.turn_id = turn["id"]
        return result

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                packet = json.loads(line)
                if "id" in packet and "method" not in packet:
                    future = self.pending.get(packet["id"])
                    if future and not future.done():
                        future.set_result(packet)
                else:
                    params = packet.get("params", {})
                    if packet.get("method") == "turn/started":
                        self.turn_id = params["turn"]["id"]
                    elif packet.get("method") == "turn/completed":
                        self.completed_turns.add(params["turn"]["id"])
                        if self.turn_id == params["turn"]["id"]:
                            self.turn_id = None
                    await self.events.put(packet)
        except (ValueError, KeyError, asyncio.LimitOverrunError) as exc:
            await self.events.put({"method": "room/protocolError", "params": {"error": str(exc)}})
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RoomError("Native transport closed; reconcile before retrying", "outcome_unknown"))

    async def write(self, packet):
        if not self.process or self.process.returncode is not None:
            raise RoomError("Native app-server is not running", "unavailable")
        self.process.stdin.write((json.dumps(packet) + "\n").encode())
        try:
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RoomError("Native write outcome is unknown", "outcome_unknown") from exc

    async def request(self, method, params, timeout=20):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.write({"id": request_id, "method": method, "params": params})
            packet = await asyncio.wait_for(future, timeout)
            if "error" in packet:
                raise RoomError(f"Native {method} rejected: {packet['error'].get('message', 'unknown')}", "native_rejected")
            return packet["result"]
        except asyncio.TimeoutError as exc:
            raise RoomError(f"Native {method} timed out; outcome unknown", "outcome_unknown") from exc
        finally:
            self.pending.pop(request_id, None)

    async def send(self, message):
        self.last_sent_turn_id = None
        params = {"threadId": self.thread_id,
                  "input": [{"type": "text", "text": message_text(message)}],
                  "clientUserMessageId": message["id"]}
        if self.turn_id:
            params["expectedTurnId"] = self.turn_id
            await self.request("turn/steer", params)
            self.last_sent_turn_id = params["expectedTurnId"]
        else:
            params["model"] = self.model_config["model"]
            params["effort"] = self.model_config["effort"]
            result = await self.request("turn/start", params)
            turn_id = result["turn"]["id"]
            self.last_sent_turn_id = turn_id
            if turn_id not in self.completed_turns:
                self.turn_id = turn_id
        return "accepted"

    async def respond(self, request_id, result):
        await self.write({"id": request_id, "result": result})

    async def stop(self):
        descendants = owned_descendants(self.process.pid) if self.process and self.process.returncode is None else {}
        if self.process and self.process.returncode is None:
            if self.thread_id and self.turn_id:
                try:
                    await self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=2)
                except RoomError:
                    pass  # Process cleanup below still applies; never turn this into a success receipt.
            if process_alive(self.process.pid, self.stamp):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 4)
            except asyncio.TimeoutError:
                if process_alive(self.process.pid, self.stamp):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.stderr:
            self.stderr.close()
        await stop_descendants(descendants)


async def start_claude(project, native_id, resume, env, log, member=LAUNCHED_CLAUDE, instructions=None,
                       timeout=25, model=None, effort=None):
    previous = {agent.get("sessionId") for agent in await asyncio.to_thread(claude_agents, project)}
    log_path = str(Path(log).resolve())
    # Background jobs may be hosted by an already-running daemon, which does not
    # forward arbitrary caller variables. Pass only room-local bindings through
    # native per-launch settings; model/effort use the host's documented CLI flags.
    settings = Path(log).with_suffix(".settings.json")
    bindings = {key: value for key, value in env.items()
                if key.startswith("IHAV_AGENT_ROOM_") or key == "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF"}
    native_settings = {"env": bindings, "worktree": {"bgIsolation": "none"}}
    # Exact resume reloads this file, so the current requested model and effort travel here as well as in flags.
    if model:
        native_settings["model"] = model
    if effort:
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise RoomError("Unsupported Claude effort setting", "configuration")
        native_settings["effortLevel"] = effort
    atomic_write(settings, json.dumps(native_settings), mode=0o600)
    if resume:
        # Re-supplying saved launch options makes native Claude fork a copy.
        # It reloads the original settings file (updated above) on exact resume.
        args = ["claude", "--bg", "--resume", native_id]
    else:
        args = ["claude", "--bg", "--name", member, "--plugin-dir", str(PLUGIN_ROOT),
                "--settings", str(settings),
                "--append-system-prompt", instructions or role_instructions(member)]
        if model:
            args.extend(["--model", model])
        if effort:
            if effort not in {"low", "medium", "high", "xhigh", "max"}:
                raise RoomError("Unsupported Claude effort setting", "configuration")
            args.extend(["--effort", effort])
    # --bg allocates its own ID. --session-id is explicitly ignored by native Claude.
    with open(log, "a+", encoding="utf-8") as stderr:
        start = stderr.tell()
        process = await asyncio.create_subprocess_exec(*args, cwd=project, env=env,
                    stdout=stderr, stderr=stderr)
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError as exc:
            process.terminate()
            await process.wait()
            raise RoomError(f"Claude background launch timed out; member log: {log_path}; check registry before retrying",
                            "outcome_unknown", log_path=log_path, returncode=process.returncode) from exc
        if process.returncode:
            raise RoomError(f"Claude background launch failed (exit {process.returncode}); member log: {log_path}",
                            "native", log_path=log_path, returncode=process.returncode)
        stderr.seek(start)
        output = stderr.read()
        reported_jobs = set(re.findall(r"backgrounded\s*·\s*([0-9a-fA-F]{8})\b", output))
        reported_ids = set(re.findall(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b", output))
        # Launch returns before the native process necessarily binds its inbox.
        reported = set()
        began = time.monotonic()
        deadline, scoped = began + min(liveness_seconds(), timeout), True  # Never outlast the caller's own timeout.
        while True:
            agents = await asyncio.to_thread(claude_agents, project, None, scoped)
            matches = [agent for agent in agents
                       if Path(agent.get("cwd", "/nonexistent")).resolve() == Path(project).resolve()
                       and (agent.get("id") in reported_jobs or agent.get("sessionId") in reported_ids)]
            if not matches and scoped:
                scoped = False  # The --cwd filter may hide it; identity and cwd are still matched here.
                continue
            reported.update(agent["sessionId"] for agent in matches if agent.get("sessionId"))
            if len(matches) == 1 and process_stamp(matches[0].get("pid")):
                found = matches[0]["sessionId"]
                if (resume and found == native_id) or (not resume and found not in previous):
                    return matches[0]
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(.25)
        raise RoomError(f"Expected Claude session did not become live within {time.monotonic() - began:.1f} s; "
                        f"member log: {log_path}; native resume may have created a copy", "identity",
                        log_path=log_path, reported_new_ids=sorted(reported - previous - {native_id}))


def stop_claude(project, native_id):
    try:
        agent = exact_claude(project, native_id)
    except RoomError as exc:
        if exc.code == "unavailable":
            return
        raise
    job_id = agent.get("id")
    if agent.get("kind") != "background" or not job_id:
        raise RoomError("Cannot stop a session without an exact native background job ID", "identity")
    run_cli(["claude", "stop", job_id], cwd=project, timeout=10)
    if process_stamp(agent["pid"]):
        # Native stop can return before the process exits; the supervisor verifies later.
        return agent["pid"]
