"""An explicit start recovers a confirmed exited pair without replacing sessions."""

from contextlib import ExitStack
import json
import os
import sqlite3
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ihav_agent_room.common import RoomError, dumps, process_alive
from ihav_agent_room.runtime import start_room
import test_continuity


class PairRecoveryTests(unittest.TestCase):
    setUp = test_continuity.ContinuityTests.setUp

    def prepare(self, host="codex"):
        self.host = host
        self.session = "new-codex" if host == "codex" else "main-claude"
        self.gateway = "CODEX_01" if host == "codex" else "CLAUDE_01"
        self.worker = "CLAUDE_01" if host == "codex" else "CODEX_01"
        self.saved_worker = "saved-" + self.worker
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(mode="pair", status="running", gateway=self.gateway,
                        owner={"host": host, "session": self.session, "permission_mode": "default"},
                        host_sessions={host: self.session}, manual_stop=False, restart_requested=False,
                        supervisor={"pid": 5501, "stamp": "old-supervisor", "handoff_protocol": 1})
            room.pop("mode_transition", None)
            room["error"] = None
            self.store.put_room(db, room)
        self.store.member(self.gateway, {"native_id": self.session, "status": "active"})
        self.store.member(self.worker, {"native_id": self.saved_worker, "pid": 5502, "stamp": "dead-worker",
                                      "status": "stopped", "error": "Native background session exited.",
                                      "turn_id": None, "unexpected_native_id": None})
        self.clock = 0
        self.cleanup = "complete"
        self.old_live = True
        self.sleep_count = 0
        self.registry_rows = []
        self.registry_error = None
        self.native_inspection = None

    def registry(self, *args, **kwargs):
        if self.native_inspection:
            self.native_inspection()
        if self.registry_error:
            raise self.registry_error
        return self.registry_rows

    def stamp(self, pid):
        if pid == 5501:
            return "old-supervisor" if self.old_live else None
        return {5503: "new-supervisor", 5504: "native-live", 5505: "native-live",
                9401: "host-live"}.get(pid)

    def sleep(self, duration):
        self.clock += duration
        self.sleep_count += 1
        if self.cleanup == "complete":
            self.old_live = False
            with self.store.tx() as db:
                room = self.store.get_room(db)
                room.update(status="stopped", supervisor=None)
                self.store.put_room(db, room)
        elif self.cleanup == "failed":
            with self.store.tx() as db:
                room = self.store.get_room(db)
                room.update(status="failed", error="Exact native cleanup could not be confirmed", supervisor=None)
                self.store.put_room(db, room)
            self.old_live = False
        elif self.cleanup == "owner_changed":
            with self.store.tx() as db:
                room = self.store.get_room(db)
                room["owner"]["session"] = "another-host"
                self.store.put_room(db, room)

    def call(self, *, automatic=False, liveness=None, mode=None):
        environment = dict(os.environ)
        environment.pop("CODEX_THREAD_ID", None)
        environment.pop("CODEX_SESSION_ID", None)
        environment.update(IHAV_AGENT_ROOM_HOST=self.host)
        environment["CODEX_THREAD_ID" if self.host == "codex" else "CLAUDE_CODE_SESSION_ID"] = self.session
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            stack.enter_context(patch("ihav_agent_room.runtime.doctor", return_value={"ok": True}))
            stack.enter_context(patch("ihav_agent_room.runtime.probe_codex", new_callable=AsyncMock))
            stack.enter_context(patch("ihav_agent_room.runtime.exact_claude", return_value={"pid": 9401}))
            stack.enter_context(patch("ihav_agent_room.runtime.claude_agents", side_effect=self.registry))
            # Exercise the production two-argument API. Only OS inspection is
            # replaced, so a missing stamp or wrong call signature cannot hide.
            stack.enter_context(patch("ihav_agent_room.common.process_stamp", side_effect=self.stamp))
            stack.enter_context(patch("ihav_agent_room.runtime.process_alive", new=liveness or process_alive))
            stack.enter_context(patch("ihav_agent_room.runtime.process_stamp", side_effect=self.stamp))
            stack.enter_context(patch("ihav_agent_room.runtime.time.monotonic", side_effect=lambda: self.clock))
            stack.enter_context(patch("ihav_agent_room.runtime.time.sleep", side_effect=self.sleep))
            self.spawn = stack.enter_context(patch("ihav_agent_room.runtime.subprocess.Popen", return_value=Mock(pid=5503)))
            return start_room(self.store, self.session, automatic=automatic, mode=mode)

    def test_one_start_replaces_only_the_dead_controller_and_preserves_both_saved_ids(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                self.prepare(host)
                before = self.store.room()
                with self.store.tx() as db:
                    db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?)", ("T-preserved", 1,
                               dumps({"id": "T-preserved", "version": 1, "state": "ready", "owner": self.worker})))
                result = self.call()
                self.assertTrue(result["started"], result)
                self.assertTrue(result["pair_recovery"]["requested"])
                self.assertEqual(self.spawn.call_count, 1)
                self.assertGreater(self.sleep_count, 0)
                current = self.store.room()
                self.assertEqual((current["id"], current["mode"]), (before["id"], "pair"))
                self.assertNotEqual(current["generation"], before["generation"])
                self.assertEqual(self.store.member(self.gateway)["native_id"], self.session)
                self.assertEqual(self.store.member(self.worker)["native_id"], self.saved_worker)
                with self.store.read() as db:
                    self.assertEqual(json.loads(db.execute("SELECT data FROM tasks WHERE id='T-preserved'").fetchone()[0])["state"], "ready")
                backup = result["pair_recovery"]["backup"]
                with sqlite3.connect(backup) as db:
                    self.assertEqual(db.execute("pragma integrity_check").fetchone()[0], "ok")
                    self.assertEqual(json.loads(db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0])["generation"], before["generation"])

    def test_a_healthy_or_unverifiable_worker_is_never_stopped(self):
        cases = [
            {"status": "idle", "pid": 5504},
            {"status": "working", "turn_id": "native-turn"},
            {"status": "waiting_permission"},
            {"status": "waiting_native_input"},
            {"status": "starting"},
            {"unexpected_native_id": "different-session"},
            {"native_id": None},
            {"native_id": "new-codex"},
            {"pid": 5504},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.prepare()
                self.store.member(self.worker, changes)
                before = self.store.room()
                result = self.call()
                self.assertFalse(result["started"])
                self.assertEqual(self.store.room()["generation"], before["generation"])
                self.assertEqual(self.store.room()["status"], "running")
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_unknown_process_liveness_is_not_absence(self):
        self.prepare()
        result = self.call(liveness=lambda pid, stamp: None if pid == 5502 else process_alive(pid, stamp))
        self.assertFalse(result["started"])
        self.spawn.assert_not_called()
        self.assertEqual(self.store.room()["status"], "running")

    def test_an_exact_claude_session_live_outside_the_ledger_blocks_recovery(self):
        for pids in ((5504,), (5504, 5505)):
            with self.subTest(pids=pids):
                self.prepare()
                self.registry_rows = [{"sessionId": self.saved_worker, "cwd": str(self.project), "pid": pid} for pid in pids]
                result = self.call()
                self.assertFalse(result["started"])
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_registry_failure_does_not_request_cleanup(self):
        self.prepare()
        self.registry_error = RoomError("Native registry unavailable", "native")
        with self.assertRaises(RoomError):
            self.call()
        self.spawn.assert_not_called()
        self.assertEqual(self.store.room()["status"], "running")

    def test_an_exited_claude_registry_row_is_recoverable_with_the_real_liveness_api(self):
        self.prepare()
        self.registry_rows = [{"sessionId": self.saved_worker, "cwd": str(self.project), "pid": 5502}]
        result = self.call()
        self.assertTrue(result["started"], result)
        self.assertEqual(self.store.member(self.worker)["native_id"], self.saved_worker)

    def test_terminal_background_registry_rows_without_a_pid_are_recoverable(self):
        # Native `claude agents --json --all` retains completed jobs without
        # a PID. This is the actual stopped-row shape from the own-room canary.
        for state in ("stopped", "failed", "done"):
            for include_null_pid in (False, True):
                with self.subTest(state=state, include_null_pid=include_null_pid):
                    self.prepare()
                    row = {"id": "saved-job", "sessionId": self.saved_worker,
                           "cwd": str(self.project), "kind": "background", "state": state}
                    if include_null_pid:
                        row["pid"] = None
                    self.registry_rows = [row]
                    result = self.call()
                    self.assertTrue(result["started"], result)
                    self.assertTrue(result["pair_recovery"]["requested"])
                    self.assertEqual(self.spawn.call_count, 1)
                    self.assertEqual(self.store.member(self.worker)["native_id"], self.saved_worker)

    def test_pidless_registry_rows_require_terminal_inactive_background_metadata(self):
        changes = [
            {"state": None}, {"state": "working"}, {"state": "blocked"},
            {"state": "future-state"}, {"kind": None}, {"kind": "interactive"},
            {"status": "idle"}, {"status": "working"}, {"status": ""},
            {"pid": True}, {"pid": 1}, {"pid": -1}, {"pid": "5502"},
            {"cwd": None}, {"cwd": str(self.project.parent)},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.prepare()
                self.registry_rows = [{"sessionId": self.saved_worker, "cwd": str(self.project),
                                       "kind": "background", "state": "stopped"} | change]
                result = self.call()
                self.assertFalse(result["started"], result)
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_a_pidless_nonterminal_native_job_returns_an_explicit_hold(self):
        for state in ("blocked", "working", "future-state", None):
            with self.subTest(state=state):
                self.prepare()
                before = self.store.room()
                self.registry_rows = [{"sessionId": self.saved_worker, "cwd": str(self.project),
                                       "kind": "background", "state": state}]
                with patch("ihav_agent_room.runtime.gateway_backup") as backup:
                    result = self.call()
                self.assertFalse(result["started"], result)
                held = result["pair_recovery"]
                self.assertTrue(held["held"])
                self.assertFalse(held["requested"])
                self.assertEqual(held["reason"], "native_registry_requires_reconciliation")
                self.assertEqual((held["worker"], held["native_id"]), (self.worker, self.saved_worker))
                self.assertEqual(held["native_observation"]["state"], state)
                self.assertEqual(self.store.room(), before)
                self.assertEqual(self.store.member(self.worker)["native_id"], self.saved_worker)
                backup.assert_not_called()
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_a_pidless_terminal_row_with_active_or_unknown_kind_is_held_explicitly(self):
        for change in ({"status": "idle"}, {"status": "working"}, {"kind": "interactive"}, {"kind": None}):
            with self.subTest(change=change):
                self.prepare()
                self.registry_rows = [{"sessionId": self.saved_worker, "cwd": str(self.project),
                                       "kind": "background", "state": "stopped"} | change]
                result = self.call()
                self.assertFalse(result["started"], result)
                self.assertTrue(result["pair_recovery"]["held"])
                self.assertFalse(result["pair_recovery"]["requested"])
                self.assertEqual(result["pair_recovery"]["native_observation"]["kind"],
                                 self.registry_rows[0]["kind"])
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_a_pidless_terminal_row_does_not_hide_a_live_exact_duplicate(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.prepare()
                rows = [
                    {"sessionId": self.saved_worker, "cwd": str(self.project),
                     "kind": "background", "state": "stopped"},
                    {"sessionId": self.saved_worker, "cwd": str(self.project),
                     "kind": "background", "pid": 5504, "status": "idle"},
                ]
                self.registry_rows = list(reversed(rows)) if reverse else rows
                result = self.call()
                self.assertFalse(result["started"], result)
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_unknown_codex_process_metadata_stays_held(self):
        for changes in ({"pid": None}, {"pid": True}, {"pid": 1}, {"stamp": None}, {"stamp": ""}):
            with self.subTest(changes=changes):
                self.prepare("claude")
                self.store.member(self.worker, changes)
                result = self.call()
                self.assertFalse(result["started"], result)
                self.assertEqual(self.store.room()["status"], "running")
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_a_reused_live_pid_is_not_affirmative_worker_absence(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                self.prepare(host)
                self.store.member(self.worker, {"pid": 5504, "stamp": "former-process"})
                result = self.call()
                self.assertFalse(result["started"], result)
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_an_unverifiable_exact_claude_registry_row_stays_held(self):
        for changes in ({"pid": None}, {"pid": True}, {"pid": 1}, {"cwd": None}, {"cwd": str(self.project.parent)}):
            with self.subTest(changes=changes):
                self.prepare()
                self.registry_rows = [{"sessionId": self.saved_worker, "cwd": str(self.project), "pid": 5502} | changes]
                result = self.call()
                self.assertFalse(result["started"], result)
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_pending_approval_stays_pending_without_cleanup(self):
        self.prepare()
        with self.store.tx() as db:
            db.execute("INSERT INTO approvals VALUES (?,?)", ("A-held", dumps({"id": "A-held", "state": "pending"})))
        result = self.call()
        self.assertFalse(result["started"])
        self.spawn.assert_not_called()
        with self.store.read() as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM approvals WHERE id='A-held'").fetchone()[0])["state"], "pending")

    def test_automatic_start_and_an_existing_transition_do_not_trigger_repair(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                self.prepare()
                if not automatic:
                    with self.store.tx() as db:
                        room = self.store.get_room(db)
                        room["mode_transition"] = {"reason": "handoff", "state": "draining"}
                        self.store.put_room(db, room)
                result = self.call(automatic=automatic)
                self.assertFalse(result["started"])
                self.spawn.assert_not_called()
                self.assertEqual(self.sleep_count, 0)

    def test_manual_stop_remains_stopped_on_automatic_start(self):
        self.prepare()
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["manual_stop"] = True
            self.store.put_room(db, room)
        result = self.call(automatic=True)
        self.assertFalse(result["started"])
        self.assertTrue(self.store.room()["manual_stop"])
        self.spawn.assert_not_called()

    def test_an_explicit_mode_change_is_not_used_to_repair_the_pair(self):
        self.prepare()
        with self.assertRaises(RoomError) as caught:
            self.call(mode="advisors")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.store.room()["status"], "running")
        self.assertEqual(self.sleep_count, 0)
        self.spawn.assert_not_called()

    def test_a_worker_changed_during_the_native_check_is_not_stopped(self):
        self.prepare()
        self.native_inspection = lambda: self.store.member(self.worker, {"status": "working", "turn_id": "new-turn"})
        with self.assertRaises(RoomError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.store.room()["status"], "running")
        self.spawn.assert_not_called()

    def test_cleanup_timeout_never_starts_a_competing_controller(self):
        self.prepare()
        self.cleanup = "pending"
        result = self.call()
        self.assertTrue(result["recovery_pending"], result)
        self.assertFalse(result["started"])
        self.assertEqual(self.store.room()["status"], "stopping")
        self.assertTrue(self.store.room()["restart_requested"])
        self.spawn.assert_not_called()

    def test_failed_cleanup_and_changed_owner_do_not_continue_start(self):
        for cleanup, code in (("failed", "cleanup"), ("owner_changed", "conflict")):
            with self.subTest(cleanup=cleanup):
                self.prepare()
                self.cleanup = cleanup
                with self.assertRaises(RoomError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, code)
                self.spawn.assert_not_called()
