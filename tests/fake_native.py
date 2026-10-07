#!/usr/bin/env python3
"""Local CLI boundary fixture: records effects, never calls any model or network."""

import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import uuid


ROOT = Path(os.environ["FAKE_NATIVE_ROOT"])
ROOT.mkdir(parents=True, exist_ok=True)


def emit(value):
    print(json.dumps(value), flush=True)


def record(kind, data):
    with open(ROOT / "effects.jsonl", "a") as stream:
        stream.write(json.dumps({"kind": kind, "data": data, "member": os.environ.get("IHAV_AGENT_ROOM_MEMBER")}) + "\n")


def write_registry(path, data, replace=os.replace):
    """Publish complete registry JSON so concurrent fake readers never parse a partial write."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def commit_external_effect(effect_id):
    """Persist a fake downstream commit separately from the native event log."""
    with open(ROOT / "external_effects.jsonl", "a") as stream:
        stream.write(json.dumps({"schema_version": 1, "observation_id": str(uuid.uuid4()),
                                 "route": "Agent Room", "effect_id": effect_id,
                                 "outcome": "committed"}) + "\n")


def spawn_daemon(session, project, name, env=None, kind="background"):
    child_env = env or {key: value for key, value in os.environ.items() if not key.startswith("IHAV_AGENT_ROOM_")}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--daemon", session, project, name, kind],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, env=child_env)
    registry = ROOT / (session + ".agent.json")
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and not registry.exists():
        time.sleep(.02)
    if not registry.exists():
        process.terminate()
        raise RuntimeError(f"fake daemon registry missing for {session}")
    return process


def hide_registry_name(name):
    return bool(name and name.startswith("IHAV_AGENT_ROOM_SMOKE_MAIN_")
                and (ROOT / "hide_registry_name").exists())


def daemon(session, project, name=None, kind="background", hide_pid=False, hide_name=False):
    sockets = ROOT / "sockets"
    sockets.mkdir(mode=0o700, exist_ok=True)
    path = sockets / f"{os.getpid()}.sock"
    registry = ROOT / (session + ".agent.json")
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()
    server.settimeout(.2)
    metadata = {"sessionId": session, "id": session[:8], "kind": kind,
                "cwd": project, "status": "done"}
    if name is not None and not hide_name:
        metadata["name"] = name
    if hide_pid:
        (ROOT / (session + ".actual-pid")).write_text(str(os.getpid()), encoding="ascii")
    else:
        metadata["pid"] = os.getpid()
    write_registry(registry, metadata)
    active = True
    def stop(_signum, _frame):
        nonlocal active
        active = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while active:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                record("claude_inbox", json.loads(connection.makefile().readline()))
    finally:
        server.close()
        path.unlink(missing_ok=True)
        stopped = {**metadata, "status": "stopped"}
        stopped.pop("pid", None)
        write_registry(registry, stopped)


def app_server():
    thread = None
    active_turn = None
    pending = None
    for line in sys.stdin:
        packet = json.loads(line)
        method = packet.get("method")
        params = packet.get("params", {})
        request_id = packet.get("id")
        record("codex_packet", packet)
        if not method:
            if pending is not None and request_id == pending:
                if packet["result"].get("decision") == "accept":
                    record("approved_effect", {"request": pending})
                emit({"method": "serverRequest/resolved", "params": {"requestId": pending, "threadId": thread}})
                emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": active_turn, "status": "completed"}}})
                pending = None
            continue
        if method == "initialized":
            continue
        if method == "initialize":
            emit({"id": request_id, "result": {"userAgent": "fake-native-contract-fixture"}})
        elif method == "thread/read":
            host = json.loads((ROOT / "codex-host.json").read_text())
            if params["threadId"] != host["id"]:
                emit({"id": request_id, "error": {"code": -1, "message": "Unknown host thread"}})
            else:
                emit({"id": request_id, "result": {"thread": host}})
        elif method == "thread/queue/list":
            if (ROOT / "queue_unsupported").exists():
                emit({"id": request_id, "error": {"code": -32601, "message": "Queue unavailable"}})
            else:
                emit({"id": request_id, "result": {"data": [], "nextCursor": None}})
        elif method == "thread/queue/add":
            record("codex_gateway_queue", params)
            if (ROOT / "queue_crash_after_input").exists():
                os._exit(7)
            emit({"id": request_id, "result": {"queuedSubmission": {
                "id": str(uuid.uuid4()), "clientUserMessageId": params["clientUserMessageId"], "input": params["input"]}}})
        elif method in {"thread/start", "thread/resume"}:
            thread = params.get("threadId") or str(uuid.uuid4())
            if method == "thread/resume" and not (ROOT / (thread + ".thread")).exists():
                emit({"id": request_id, "error": {"code": -1, "message": "Unknown native thread"}})
                continue
            (ROOT / (thread + ".thread")).write_text("persisted")
            emit({"id": request_id, "result": {"thread": {"id": thread, "turns": []}, "approvalPolicy": "on-request"}})
        elif method in {"turn/start", "turn/steer"}:
            text = params["input"][0]["text"]
            broadcast = re.match(r"\[Agent Room peer broadcast [^ ]+ from [^ ]+ to ([^;]+);", text)
            direct_addressee = broadcast.group(1) if broadcast else None
            is_direct_request = direct_addressee is None or direct_addressee == os.environ.get("IHAV_AGENT_ROOM_MEMBER")
            if "crash after input" in text and is_direct_request:
                effect_id = params.get("clientUserMessageId")
                if effect_id:
                    commit_external_effect(effect_id)
                record("unknown_effect", {"message": effect_id})
                os._exit(7)
            if "spawn owned child" in text and is_direct_request:
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                record("owned_child", {"pid": child.pid})
            if method == "turn/steer" and params.get("expectedTurnId") != active_turn:
                emit({"id": request_id, "error": {"code": -1, "message": "Wrong active turn"}})
                continue
            active_turn = active_turn or str(uuid.uuid4())
            if "send peer result" in text and is_direct_request:
                result = subprocess.run(["ihav-agent-room", "send", "--to", "CLAUDE_01"],
                    input="Fixture peer finding; source remains unchanged", text=True, capture_output=True)
                record("peer_cli", {"returncode": result.returncode, "result": result.stdout})
            emit({"id": request_id, "result": {"turn": {"id": active_turn, "status": "inProgress"}}})
            emit({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": active_turn, "status": "inProgress"}}})
            if "needs approval" in text and is_direct_request:
                pending = "approval-42"
                emit({"id": pending, "method": "item/commandExecution/requestApproval", "params": {
                    "threadId": thread, "turnId": active_turn, "itemId": "item-42", "startedAtMs": 1,
                    "command": "echo approved", "cwd": os.getcwd()}})
            elif "keep busy" not in text:
                emit({"method": "item/completed", "params": {"threadId": thread, "item": {"id": "reply", "type": "agentMessage", "text": "FAKE response; no provider called"}}})
                emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": active_turn, "status": "completed"}}})
                active_turn = None
        elif method == "turn/interrupt":
            emit({"id": request_id, "result": {}})
            if active_turn:
                emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": active_turn, "status": "interrupted"}}})
            active_turn = None
        else:
            emit({"id": request_id, "error": {"code": -32601, "message": "Unsupported fixture method"}})


def main():
    args = sys.argv[1:]
    name = Path(sys.argv[0]).name
    if args and args[0] == "--daemon":
        daemon(args[1], args[2], args[3] if len(args) > 3 else None,
               args[4] if len(args) > 4 else "background",
               hide_pid=(ROOT / "hide_registry_pid").exists(),
               hide_name=hide_registry_name(args[3] if len(args) > 3 else None))
        return
    if "--version" in args:
        print("2.1.283 (Claude Code)" if name == "claude" else "codex-cli 0.157.1")
    elif "--help" in args:
        print("fixture --bg --resume --session-id --plugin-dir --stdio")
    elif name == "codex" and args[:1] == ["app-server"]:
        app_server()
    elif name == "claude" and args[:1] == ["agents"]:
        before_agents = ROOT / "before_agents_sessions.json"
        if before_agents.exists():
            entries = json.loads(before_agents.read_text(encoding="utf-8"))
            for item in entries:
                item_name = os.environ.get("IHAV_AGENT_ROOM_SMOKE_RUN_NAME", "") if item.get("name") == "$MAIN_NAME" else item.get("name", "")
                spawn_daemon(item["sessionId"], item["cwd"], item_name,
                             kind=item.get("kind", "background"))
            before_agents.unlink()
        emit([json.loads(path.read_text()) for path in ROOT.glob("*.agent.json")])
    elif name == "claude" and args[:1] == ["stop"]:
        candidates = list(ROOT.glob(args[1] + "-*.agent.json"))
        if len(candidates) != 1:
            raise SystemExit("Native stop requires an unambiguous job ID, not session UUID")
        path = candidates[0]
        record("claude_stop_attempt", args[1])
        failed_stop = ROOT / ("fail_stop_" + args[1])
        fail_next_stop = ROOT / "fail_next_stop"
        if failed_stop.exists() or fail_next_stop.exists():
            failed_stop.unlink(missing_ok=True)
            fail_next_stop.unlink(missing_ok=True)
            raise SystemExit("fixture stop failure")
        record("claude_stop", path.name.removesuffix(".agent.json"))
        if path.exists():
            pid = json.loads(path.read_text()).get("pid")
            if pid:
                os.kill(pid, signal.SIGTERM)
    elif name == "claude" and "--bg" in args:
        flag = "--resume" if "--resume" in args else None
        configured_ids = ROOT / "launch_session_ids.json"
        configured = json.loads(configured_ids.read_text(encoding="utf-8")) if configured_ids.exists() else []
        session = args[args.index(flag) + 1] if flag else (configured[0] if configured else str(uuid.uuid4()))
        agent_name = args[args.index("--name") + 1] if "--name" in args else None
        options_path = ROOT / (session + ".options.json")
        options = json.loads(options_path.read_text()) if flag and options_path.exists() else {}
        if agent_name is not None:
            options["name"] = agent_name
        else:
            agent_name = options.get("name")
        if "--settings" in args:
            options["settings"] = args[args.index("--settings") + 1]
        if flag == "--resume" and ((ROOT / "copy_claude").exists() or any(x in args for x in ("--settings", "--name", "--plugin-dir"))):
            session = str(uuid.uuid4())
        (ROOT / (session + ".options.json")).write_text(json.dumps(options))
        record("claude_start", {"id": session, "resume": flag == "--resume",
                                 "model": args[args.index("--model") + 1] if "--model" in args else None,
                                 "effort": args[args.index("--effort") + 1] if "--effort" in args else None})
        child_env = {key: value for key, value in os.environ.items() if not key.startswith("IHAV_AGENT_ROOM_")}
        if options.get("settings"):
            child_env.update(json.loads(Path(options["settings"]).read_text()).get("env", {}))
        spawn_daemon(session, os.getcwd(), agent_name or "", child_env)
        injected = ROOT / "concurrent_sessions.json"
        if injected.exists():
            for item in json.loads(injected.read_text(encoding="utf-8")):
                item_name = agent_name if item.get("name") == "$MAIN_NAME" else item.get("name", "")
                spawn_daemon(item["sessionId"], item["cwd"], item_name, child_env,
                             kind=item.get("kind", "background"))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            path = ROOT / (session + ".agent.json")
            if path.exists() and json.loads(path.read_text()).get("pid"):
                break
            time.sleep(.02)
        if (ROOT / "fail_launch_after_daemon").exists():
            raise SystemExit(23)  # Exercise a failed launcher after its detached worker has started.
        if (ROOT / "hold_claude_launch").exists():
            time.sleep(10)  # Simulate an interrupted launch after its background child exists.
        print("backgrounded · " + session[:8])
    else:
        raise SystemExit("Unsupported fake native invocation")


if __name__ == "__main__":
    main()
