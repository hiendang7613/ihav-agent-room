"""Offline lifecycle invariants, with native CLI/OS boundaries replaced only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import RoomError, dumps, file_lock
from ihav_agent_room.runtime import Supervisor, bind_main, start_room
from ihav_agent_room.scaffold import initialize
from ihav_agent_room.session_replacement import (begin_replacement_launch, check_replacement_start,
                                                complete_replacement_launch, fail_replacement_launch,
                                                prepare_claude_replacement)
from ihav_agent_room.store import Store
from receipts import human_receipt
import test_codex_gateway as gateway_tests


class SessionReplacementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="replacement tests "))
        self.project = Path(temporary).resolve()
        self.session = "11111111-1111-4111-8111-111111111111"
        self.retired = "22222222-2222-4222-8222-222222222222"
        self.fresh = "33333333-3333-4333-8333-333333333333"
        environment = dict(os.environ)
        for key in ("IHAV_AGENT_ROOM_BINDING", "IHAV_AGENT_ROOM_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID"):
            environment.pop(key, None)
        environment.update(IHAV_AGENT_ROOM_HOST="codex", IHAV_AGENT_ROOM_MEMBER="CODEX_01", CODEX_THREAD_ID=self.session)
        self.stack.enter_context(patch.dict(os.environ, environment, clear=True))
        initialize(self.project, "pair")
        self.store = Store(self.project)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="stopped", manual_stop=True, schema=4, gateway="CODEX_01", supervisor=None,
                        owner={"host": "codex", "session": self.session, "permission_mode": "default"},
                        host_sessions={"codex": self.session}, background_sessions={"CLAUDE_01": self.retired})
            self.store.put_room(db, room)
        self.store.member("CODEX_01", {"native_id": self.session, "status": "active"})
        self.store.member("CLAUDE_01", {"native_id": self.retired, "job_id": self.retired[:8],
                                      "status": "stopped", "pid": None, "stamp": None, "turn_id": None})
        self.request_id = "replacement-fixture"
        self.reason = "Explicit admin request for a new Claude in this preserved room."
        self.rows = [{"sessionId": self.retired, "id": self.retired[:8], "cwd": str(self.project),
                      "kind": "background", "state": "stopped", "status": None}]
        self.registry = self.stack.enter_context(patch("ihav_agent_room.session_replacement.claude_agents", side_effect=lambda *a, **k: self.rows))
        self.stack.enter_context(patch("ihav_agent_room.session_replacement.process_alive", return_value=False))
        self.stamp = self.stack.enter_context(patch("ihav_agent_room.session_replacement.process_stamp", return_value=None))
        source = human_receipt(self.store, "Analyze preserved tasks", session=self.session)
        self.task = self.store.create_task("CODEX_01", {"title": "Unfinished analysis", "request": "Read source",
             "acceptance": "Traceable evidence", "next": "Continue reading", "owner": "CODEX_01", "source": source,
             "authority": "analysis", "scope": ["reviews"], "dependencies": []})
        self.message = self.store.send("CODEX_01", "CLAUDE_01", "Unknown delivery must not be replayed", self.task["id"])
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["status"] = "running"
            self.store.put_room(db, room)
        self.attempt = self.store.begin_attempt(self.message, self.store.room()["generation"])
        self.assertIsNotNone(self.attempt)
        self.store.finish_dispatch(self.attempt["id"], "unknown", "Fixture native response lost; preserve unknown state")
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["status"] = "stopped"
            self.store.put_room(db, room)

    def prepare(self, **changes):
        args = dict(member="CLAUDE_01", expected_session=self.retired, request_id=self.request_id, reason=self.reason)
        args.update(changes)
        return prepare_claude_replacement(self.store, **args)

    def table_rows(self, path=None):
        with sqlite3.connect(path or self.store.path) as db:
            return {name: db.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                    for name in ("prompts", "tasks", "claims", "notes", "messages", "approvals", "submissions", "reviews", "checkpoints", "attempts", "knowledge")}

    def launch_ready(self):
        result = self.prepare()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="starting", manual_stop=False, generation="fresh-generation")
            self.store.put_room(db, room)
        self.store.member("CLAUDE_01", {"status": "starting", "token_hash": "fresh-binding", "launch_generation": "fresh-generation"})
        return result

    def test_prepare_keeps_room_and_all_work_and_saves_consistent_backup(self):
        room, member, tables = self.store.room(), self.store.member("CLAUDE_01"), self.table_rows()
        result = self.prepare()
        self.assertTrue(result["prepared"])
        current = self.store.room()
        self.assertEqual(current["schema"], 6)  # A version that ignores launch intent/identity must refuse this ledger.
        for key in ("id", "mode", "generation", "owner", "host_sessions", "manual_stop", "status"):
            self.assertEqual(current[key], room[key], key)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.session)
        self.assertIsNone(self.store.member("CLAUDE_01")["native_id"])
        self.assertNotIn("CLAUDE_01", current["background_sessions"])
        self.assertEqual(self.table_rows(), tables)
        backup = result["replacement"]["backup"]
        self.assertEqual(self.table_rows(backup), tables)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(json.loads(db.execute("SELECT data FROM members WHERE name='CLAUDE_01'").fetchone()[0]), member)
            self.assertEqual(json.loads(db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0]), room)
        self.registry.assert_called_once_with(self.project, scoped=False)

    def test_gateway_rebind_keeps_the_replacement_schema_and_record(self):
        result = self.prepare()
        with patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock):
            bind_main(self.store, self.session)
        room = Store(self.project).room()
        self.assertEqual(room["schema"], 6)
        self.assertEqual(room["worker_session_history"][0], result["replacement"])

    def test_legacy_journal_upgrade_keeps_work_and_backs_up_schema_five(self):
        self.prepare()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["schema"] = 5
            self.store.put_room(db, room)
        before = self.table_rows()
        with patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock):
            bind_main(self.store, self.session)
        room = self.store.room()
        self.assertEqual(room["schema"], 6)
        self.assertEqual(self.table_rows(), before)
        with sqlite3.connect(room["worker_identity_schema_backup"]) as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])["schema"], 5)

    def test_retired_native_cannot_reuse_a_new_settings_token_after_binding(self):
        self.launch_ready()
        token = "fixture-binding"
        self.store.member("CLAUDE_01", {"token_hash": hashlib.sha256(token.encode()).hexdigest()})
        begin_replacement_launch(self.store, "CLAUDE_01", "fresh-generation")
        complete_replacement_launch(self.store, self.request_id, "fresh-generation", {"native_id": self.fresh})
        for session, allowed in ((self.fresh, True), (self.retired, False), ("another-native", False), ("", False)):
            with self.subTest(session=session), patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": "claude",
                 "IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01", "IHAV_AGENT_ROOM_BINDING": token,
                 "IHAV_AGENT_ROOM_SESSION_ID": session}):
                if allowed:
                    self.assertEqual(Store(self.project).actor(), "CLAUDE_01")
                else:
                    with self.assertRaises(RoomError):
                        Store(self.project).actor()

    def test_initial_native_binding_is_allowed_only_for_fresh_current_generation(self):
        self.launch_ready()
        token = "fixture-binding"
        self.store.member("CLAUDE_01", {"token_hash": hashlib.sha256(token.encode()).hexdigest()})
        begin_replacement_launch(self.store, "CLAUDE_01", "fresh-generation")
        for session, allowed in ((self.fresh, True), (self.retired, False)):
            with self.subTest(session=session), patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": "claude",
                 "IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01", "IHAV_AGENT_ROOM_BINDING": token,
                 "IHAV_AGENT_ROOM_SESSION_ID": session}):
                if allowed:
                    self.assertEqual(Store(self.project).actor(), "CLAUDE_01")
                else:
                    with self.assertRaises(RoomError):
                        Store(self.project).actor()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["worker_session_history"][0]["launch_generation"] = "stale-generation"
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": "claude", "IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01",
             "IHAV_AGENT_ROOM_BINDING": token, "IHAV_AGENT_ROOM_SESSION_ID": self.fresh}):
            with self.assertRaises(RoomError):
                Store(self.project).actor()

    def test_same_request_is_idempotent_after_reload_and_after_completion(self):
        first = self.launch_ready()
        begin_replacement_launch(self.store, "CLAUDE_01", "fresh-generation")
        complete_replacement_launch(self.store, self.request_id, "fresh-generation", {"native_id": self.fresh, "job_id": self.fresh[:8]})
        self.store = Store(self.project)
        before = self.store.room()
        result = self.prepare()
        self.assertFalse(result["prepared"])
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.store.room(), before)
        self.assertEqual(result["replacement"]["backup"], first["replacement"]["backup"])
        self.assertEqual(result["replacement"]["native_id"], self.fresh)
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], self.fresh)
        self.assertEqual(len(list((self.store.runtime / "backups").glob("*.sqlite3"))), 1)
        self.assertEqual(self.registry.call_count, 1)

    def test_idempotent_prepared_request_does_not_create_a_second_backup(self):
        first = self.prepare()
        with file_lock(self.store.runtime / "supervisor.lock", blocking=False):
            second = self.prepare()  # An already launched controller cannot make the same request allocate again.
        self.assertEqual(second["replacement"], first["replacement"])
        self.assertEqual(len(self.store.room()["worker_session_history"]), 1)
        self.assertEqual(self.registry.call_count, 1)

    def test_start_rechecks_launch_state_after_dependency_inspection(self):
        self.launch_ready()
        def concurrent_launch():
            begin_replacement_launch(self.store, "CLAUDE_01", "fresh-generation")
            return {"ok": True}
        with patch("ihav_agent_room.runtime.doctor", side_effect=concurrent_launch), \
                patch("ihav_agent_room.runtime.subprocess.Popen") as spawn:
            with self.assertRaises(RoomError) as refused:
                start_room(self.store, self.session)
            self.assertEqual(refused.exception.code, "outcome_unknown")
            spawn.assert_not_called()

    def test_reusing_a_request_id_with_changed_parameters_refuses(self):
        self.prepare()
        with self.assertRaises(RoomError):
            self.prepare(reason="Another instruction")
        with self.assertRaises(RoomError):
            self.prepare(expected_session=self.fresh)
        self.assertEqual(len(self.store.room()["worker_session_history"]), 1)

    def test_stale_saved_identity_cannot_be_cleared(self):
        before = self.store.room()
        with self.assertRaises(RoomError):
            self.prepare(expected_session=self.fresh)
        self.assertEqual(self.store.room(), before)
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], self.retired)
        self.registry.assert_not_called()

    def test_worker_and_wrong_gateway_session_cannot_request_replacement(self):
        before = self.store.room()
        for environment in ({"IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01", "IHAV_AGENT_ROOM_BINDING": "worker"},
                            {"CODEX_THREAD_ID": "another-host"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                with self.assertRaises(RoomError):
                    self.prepare()
                self.assertEqual(self.store.room(), before)
        self.registry.assert_not_called()

    def test_gateway_and_disabled_member_and_codex_replacement_refuse(self):
        for name in ("CODEX_01", "CODEX_EXPERT", "CLAUDE_EXPERT"):
            with self.subTest(member=name), self.assertRaises(RoomError):
                self.prepare(member=name)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(gateway="CLAUDE_01", owner={"host": "claude", "session": self.retired})
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": "claude", "IHAV_AGENT_ROOM_MEMBER": "CLAUDE_01",
                                     "IHAV_AGENT_ROOM_SESSION_ID": self.retired}):
            with self.assertRaises(RoomError):
                self.prepare()
        self.registry.assert_not_called()

    def test_running_stopping_and_automatic_stop_states_refuse(self):
        for changes in ({"status": "running"}, {"status": "stopping"}, {"manual_stop": False},
                        {"restart_requested": True}, {"mode_transition": {"state": "draining"}}):
            with self.subTest(changes=changes):
                original = self.store.room()
                with self.store.tx() as db:
                    self.store.put_room(db, original | changes)
                with self.assertRaises(RoomError):
                    self.prepare()
                self.assertEqual(self.store.member("CLAUDE_01")["native_id"], self.retired)
                with self.store.tx() as db:
                    self.store.put_room(db, original)
        self.registry.assert_not_called()

    def test_working_waiting_or_mismatched_worker_is_held(self):
        for changes in ({"status": "working"}, {"status": "waiting_permission"}, {"turn_id": "turn"},
                        {"unexpected_native_id": self.fresh}):
            with self.subTest(changes=changes):
                original = self.store.member("CLAUDE_01")
                self.store.member("CLAUDE_01", changes)
                with self.assertRaises(RoomError):
                    self.prepare()
                self.store.member("CLAUDE_01", original)
        self.registry.assert_not_called()

    def test_host_identity_cannot_be_retired_as_a_worker(self):
        self.store.member("CLAUDE_01", {"native_id": self.session})
        with self.assertRaises(RoomError):
            self.prepare(expected_session=self.session)
        self.registry.assert_not_called()

    def test_live_or_unknown_or_reused_pid_is_held(self):
        for observed in (True, None):
            with self.subTest(observed=observed), patch("ihav_agent_room.session_replacement.process_alive", return_value=observed):
                with self.assertRaises(RoomError):
                    self.prepare()
        self.store.member("CLAUDE_01", {"pid": 7001, "stamp": "old-stamp"})
        self.stamp.return_value = "reused-live-process"
        with self.assertRaises(RoomError):
            self.prepare()
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], self.retired)

    def test_native_registry_unknown_active_foreign_or_invalid_rows_refuse(self):
        base = dict(self.rows[0])
        for changes in ({"state": "waiting", "status": None}, {"state": None, "status": None},
                        {"state": "stopped", "status": "running"}, {"cwd": str(self.project / "another-project")},
                        {"pid": True}, {"kind": "interactive"}):
            with self.subTest(changes=changes):
                self.rows = [base | changes]
                with self.assertRaises(RoomError):
                    self.prepare()
                self.assertNotIn("worker_session_history", self.store.room())
        self.registry.side_effect = RoomError("Registry unavailable", "unavailable")
        with self.assertRaises(RoomError):
            self.prepare()

    def test_confirmed_registry_absence_and_legacy_terminal_rows_are_supported(self):
        for rows in ([], [dict(self.rows[0], state=None, status="stopped")]):
            with self.subTest(rows=rows):
                self.rows = rows
                result = self.prepare()
                self.assertTrue(result["prepared"])
                with self.store.tx() as db:
                    room = self.store.get_room(db)
                    room.pop("worker_session_history")
                    self.store.put_room(db, room)
                self.store.member("CLAUDE_01", {"native_id": self.retired, "session_replacement": None})

    def test_pending_native_approvals_remain_held_and_unchanged(self):
        for state in ("pending", "respond", "submitted"):
            with self.subTest(state=state):
                with self.store.tx() as db:
                    db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?)", ("A-held", dumps({"state": state})))
                before = self.table_rows()
                with self.assertRaises(RoomError):
                    self.prepare()
                self.assertEqual(self.table_rows(), before)
                self.assertNotIn("worker_session_history", self.store.room())

    def test_backup_failure_rolls_back_identity_and_history(self):
        room, member = self.store.room(), self.store.member("CLAUDE_01")
        with patch("ihav_agent_room.session_replacement.gateway_backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(self.store.room(), room)
        self.assertEqual(self.store.member("CLAUDE_01"), member)

    def test_active_supervisor_lock_refuses_even_if_metadata_is_stale(self):
        with file_lock(self.store.runtime / "supervisor.lock", blocking=False):
            with self.assertRaises(RoomError):
                self.prepare()
        self.registry.assert_not_called()

    def test_concurrent_requests_do_not_retire_twice(self):
        entered, release = Event(), Event()
        def paused_registry(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.rows
        self.registry.side_effect = paused_registry
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.prepare)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(RoomError):
                    self.prepare(request_id="another-request")
            finally:
                release.set()
            self.assertTrue(future.result(5)["prepared"])
        self.assertEqual(len(self.store.room()["worker_session_history"]), 1)

    def test_lost_launch_response_is_durable_and_restart_cannot_allocate_again(self):
        self.launch_ready()
        before = self.table_rows()
        begin_replacement_launch(self.store, "CLAUDE_01", "fresh-generation")
        self.store = Store(self.project)
        with patch("ihav_agent_room.runtime.subprocess.Popen") as spawn:
            with self.assertRaises(RoomError) as refused:
                start_room(self.store, self.session)
            self.assertEqual(refused.exception.code, "outcome_unknown")
            spawn.assert_not_called()
        self.assertEqual(self.table_rows(), before)
        self.assertEqual(self.store.room()["worker_session_history"][0]["state"], "launching")
        with self.assertRaises(RoomError):
            self.prepare(request_id="replacement-after-crash")

    def test_failed_launch_is_held_after_reload_without_replay(self):
        self.launch_ready()
        begin_replacement_launch(self.store, "CLAUDE_01", "fresh-generation")
        fail_replacement_launch(self.store, self.request_id, RoomError("Native timeout", "outcome_unknown"))
        self.store = Store(self.project)
        with self.assertRaises(RoomError):
            check_replacement_start(self.store)
        record = self.store.room()["worker_session_history"][0]
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["error_code"], "outcome_unknown")
        self.assertIsNone(self.store.member("CLAUDE_01")["native_id"])

    def test_completion_cannot_adopt_retired_gateway_or_mismatched_identity(self):
        self.launch_ready()
        begin_replacement_launch(self.store, "CLAUDE_01", "fresh-generation")
        for session in (self.retired, self.session, None):
            with self.subTest(session=session), self.assertRaises(RoomError):
                complete_replacement_launch(self.store, self.request_id, "fresh-generation", {"native_id": session})
        with self.assertRaises(RoomError):
            complete_replacement_launch(self.store, self.request_id, "stale-generation", {"native_id": self.fresh})
        self.store.member("CLAUDE_01", {"native_id": "unexpected-session"})
        with self.assertRaises(RoomError):
            complete_replacement_launch(self.store, self.request_id, "fresh-generation", {"native_id": self.fresh})
        self.assertEqual(self.store.room()["worker_session_history"][0]["state"], "launching")

    def test_native_success_records_new_identity_atomically_and_ordinary_restart_resumes_it(self):
        self.launch_ready()
        observed = []
        async def native_launch(project, native_id, resume, env, log, **kwargs):
            record = Store(project).room()["worker_session_history"][0]
            observed.append((native_id, resume, record["state"]))
            self.assertEqual((native_id, resume, record["state"]), (None, False, "launching"))
            # Native SessionStart may bind the authenticated new UUID before launch returns.
            self.store.member("CLAUDE_01", {"native_id": self.fresh})
            return {"sessionId": self.fresh, "id": self.fresh[:8], "pid": 7002}
        gateway = Mock()
        gateway.start = AsyncMock()
        supervisor = Supervisor(self.store, "fresh-generation")
        with patch("ihav_agent_room.runtime.CodexGateway", return_value=gateway), \
                patch("ihav_agent_room.runtime.start_claude", side_effect=native_launch) as launch, \
                patch("ihav_agent_room.runtime.process_stamp", return_value="fresh-stamp"):
            asyncio.run(supervisor.launch())
            launch.assert_called_once()
        self.assertEqual(observed, [(None, False, "launching")])
        worker = self.store.member("CLAUDE_01")
        record = self.store.room()["worker_session_history"][0]
        self.assertEqual((worker["native_id"], worker["job_id"], record["state"], record["native_id"]),
                         (self.fresh, self.fresh[:8], "completed", self.fresh))
        check_replacement_start(Store(self.project))
        self.assertIsNone(begin_replacement_launch(self.store, "CLAUDE_01", "next-generation"))
        with self.store.read() as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM attempts WHERE id=?", (self.attempt["id"],)).fetchone()[0])["state"], "unknown")

    def test_supervisor_native_timeout_persists_unknown_before_cleanup(self):
        self.launch_ready()
        gateway = Mock()
        gateway.start = AsyncMock()
        supervisor = Supervisor(self.store, "fresh-generation")
        with patch("ihav_agent_room.runtime.CodexGateway", return_value=gateway), \
                patch("ihav_agent_room.runtime.start_claude", side_effect=RoomError("Launch response lost", "outcome_unknown")):
            with self.assertRaises(RoomError):
                asyncio.run(supervisor.launch())
        self.assertEqual(self.store.room()["worker_session_history"][0]["state"], "unknown")
        with self.assertRaises(RoomError):
            check_replacement_start(Store(self.project))

    def test_cli_requires_explicit_identity_and_request_and_preserves_the_guard(self):
        result = run(parser().parse_args(["--project", str(self.project), "replace-session", "--member", "CLAUDE_WORKER",
                "--expected-session", self.retired, "--request-id", self.request_id, "--reason", self.reason]))
        self.assertTrue(result["prepared"])
        for changes in ({"expected_session": ""}, {"request_id": "invalid request"}, {"reason": ""}):
            with self.subTest(changes=changes), self.assertRaises(RoomError):
                self.prepare(**changes)
        self.assertIsNone(self.store.member("CLAUDE_01")["native_id"])


class SessionReplacementRuntimeTests(unittest.TestCase):
    """A subprocess round trip through the real CLI/supervisor and offline CLIs."""

    setUp = gateway_tests.CodexGatewayRuntimeTests.setUp
    wait = gateway_tests.CodexGatewayRuntimeTests.wait
    call = gateway_tests.CodexGatewayRuntimeTests.call
    start = gateway_tests.CodexGatewayRuntimeTests.start
    effects = gateway_tests.CodexGatewayRuntimeTests.effects
    cleanup_runtime = gateway_tests.CodexGatewayRuntimeTests.cleanup_runtime
    cleanup_host = gateway_tests.CodexGatewayRuntimeTests.cleanup_host

    def test_fresh_worker_in_same_room_and_subsequent_start_resumes_the_fresh_identity(self):
        self.start("pair")
        room_id = self.store.room()["id"]
        retired = self.store.member("CLAUDE_01")["native_id"]
        self.call("stop")
        source = human_receipt(self.store, "Continue unfinished analysis after worker replacement", session=self.codex_session)
        task = self.store.create_task("CODEX_01", {"title": "Preserved analysis", "request": "Read the same source",
                 "acceptance": "Keep the prior evidence", "next": "Continue", "owner": "CODEX_01", "source": source,
                 "authority": "analysis", "scope": ["reviews"]})
        message = self.store.send("CODEX_01", "CLAUDE_01", "Preserve this queued history", task["id"])
        args = ("replace-session", "--member", "CLAUDE_01", "--expected-session", retired,
                "--request-id", "subprocess-replacement", "--reason", "Admin explicitly requested a fresh Claude worker")
        result = self.call(*args)
        self.assertTrue(result["prepared"])
        self.assertEqual(self.store.room()["id"], room_id)
        with self.store.read() as db:
            self.assertEqual(self.store.record(db, "tasks", task["id"]), task)
        with sqlite3.connect(result["replacement"]["backup"]) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(json.loads(db.execute("SELECT data FROM members WHERE name='CLAUDE_01'").fetchone()[0])["native_id"], retired)
            self.assertTrue(db.execute("SELECT 1 FROM messages WHERE id=?", (message["id"],)).fetchone())
        self.start()
        fresh = self.store.member("CLAUDE_01")["native_id"]
        self.assertTrue(fresh)
        self.assertNotEqual(fresh, retired)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        self.assertEqual(self.store.room()["id"], room_id)
        self.assertEqual(self.store.room()["worker_session_history"][0]["state"], "completed")
        starts = len(self.effects("claude_start"))
        repeated = self.call(*args)
        self.assertTrue(repeated["unchanged"])
        self.assertEqual(repeated["replacement"]["native_id"], fresh)
        self.assertEqual(len(self.effects("claude_start")), starts)
        self.call("stop")
        self.start()
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], fresh)
        self.assertEqual(self.store.member("CODEX_01")["native_id"], self.codex_session)
        with self.store.read() as db:
            self.assertEqual(self.store.record(db, "tasks", task["id"]), task)
        self.assertEqual(self.main_process.poll(), None)  # Unrelated fixture host was never interrupted.
