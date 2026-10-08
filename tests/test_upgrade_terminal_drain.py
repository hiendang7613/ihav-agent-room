"""Upgrade cleanup admits a confirmed dead worker and preserves uncertain work."""

import asyncio
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.store import Store


class UpgradeTerminalDrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="upgrade-drain-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.store.initialize("pair")
        self.generation = "owned-upgrade-generation"
        self.native_id = "saved-claude-session"
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", gateway="CODEX_01", schema=4,
                        owner={"host": "codex", "session": "saved-codex-session", "permission_mode": "default"},
                        generation=self.generation, restart_requested=True,
                        mode_transition={"reason": "upgrade", "state": "draining",
                                         "from": "pair", "to": "pair"})
            self.store.put_room(db, room)
        self.supervisor = Supervisor(self.store, self.generation)
        self.supervisor.claude["CLAUDE_01"] = self.native_id
        self.reset_member()

    def reset_member(self, **changes):
        values = dict(native_id=self.native_id, launch_generation=self.generation,
                      status="stopped", pid=101, stamp="old-stamp", error=None,
                      turn_id=None, unexpected_native_id=None, native_observation=None)
        values.update(changes)
        self.store.member("CLAUDE_01", values)

    def native_row(self, **changes):
        row = dict(sessionId=self.native_id, cwd=str(self.store.project),
                   kind="background", state="stopped", status=None, pid=None)
        row.update(changes)
        return row

    def refresh(self, rows):
        with patch("ihav_agent_room.runtime.claude_agents", return_value=rows), \
                patch("ihav_agent_room.runtime.process_stamp", return_value=None):
            asyncio.run(self.supervisor.refresh_claude(force=True))

    def drain(self, alive=False, reused_stamp=None):
        with patch("ihav_agent_room.runtime.process_alive", return_value=alive), \
                patch("ihav_agent_room.runtime.process_stamp", return_value=reused_stamp):
            return self.supervisor.finish_mode_restart()

    def test_confirmed_terminal_background_job_drains_without_changing_identity(self):
        for state in ("stopped", "failed", "done"):
            with self.subTest(state=state):
                self.setUp_transition()
                self.reset_member()
                self.refresh([self.native_row(state=state)])
                before = self.store.member("CLAUDE_01")
                owner = self.store.room()["owner"]
                self.assertTrue(self.drain())
                self.assertEqual(self.store.room()["status"], "stopping")
                self.assertEqual(self.store.room()["mode_transition"]["state"], "restarting")
                self.assertEqual(self.store.room()["owner"], owner)
                self.assertEqual(self.store.member("CLAUDE_01"), before)

    def setUp_transition(self, reason="upgrade", **changes):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", restart_requested=True, manual_stop=False,
                        mode_transition={"reason": reason, "state": "draining",
                                         "from": "pair", "to": "pair"})
            room.update(changes)
            self.store.put_room(db, room)

    def test_missing_blocked_or_conflicting_native_rows_remain_held(self):
        cases = [[], [self.native_row(state="blocked")],
                 [self.native_row(status="working")],
                 [self.native_row(), self.native_row(state="blocked")],
                 [self.native_row(pid=True)],
                 [self.native_row(cwd=str(self.store.project.parent))]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.setUp_transition()
                self.reset_member()
                self.refresh(rows)
                self.assertFalse(self.drain())
                self.assertEqual(self.store.room()["status"], "running")
                self.assertEqual(self.store.room()["mode_transition"]["waiting"], ["CLAUDE_01"])

    def test_exit_observation_cannot_override_identity_generation_or_active_turn(self):
        for changes in ({"native_id": "different-session"},
                        {"unexpected_native_id": "different-session"},
                        {"launch_generation": "older-generation"},
                        {"turn_id": "unreconciled-turn"}):
            with self.subTest(changes=changes):
                self.setUp_transition()
                self.reset_member(**changes)
                self.refresh([self.native_row()])
                self.assertFalse(self.drain())

    def test_current_live_reused_or_uninspectable_process_remains_held(self):
        self.refresh([self.native_row()])
        for alive, stamp in ((True, None), (None, None), (False, "reused-pid")):
            with self.subTest(alive=alive, stamp=stamp):
                self.assertFalse(self.drain(alive, stamp))

    def test_manual_stop_and_other_transition_reasons_do_not_auto_recover(self):
        self.refresh([self.native_row()])
        for reason, manual in (("upgrade", True), ("handoff", False), ("settings", False)):
            with self.subTest(reason=reason, manual=manual):
                self.setUp_transition(reason=reason, manual_stop=manual)
                self.assertFalse(self.drain())

    def test_pending_native_approval_is_never_answered_or_expired_by_drain(self):
        self.refresh([self.native_row()])
        with self.store.tx() as db:
            db.execute("INSERT INTO approvals VALUES (?, ?)",
                       ("A-fixture", json.dumps({"state": "pending", "member": "CLAUDE_01",
                                                 "generation": self.generation})))
        self.assertFalse(self.drain())
        with self.store.read() as db:
            data = json.loads(db.execute("SELECT data FROM approvals WHERE id=?", ("A-fixture",)).fetchone()[0])
        self.assertEqual(data["state"], "pending")

    def test_unknown_attempt_is_retained_during_confirmed_terminal_drain(self):
        message = self.store.send("CODEX_01", "CLAUDE_01", "Keep ambiguous side effect")
        self.store.begin_attempt(message, self.generation)
        self.refresh([self.native_row()])
        attempts = self.store.attempts()
        self.assertEqual(attempts["items"][0]["state"], "unknown")
        self.assertTrue(self.drain())
        self.assertEqual(self.store.attempts(), attempts)

    def test_idle_worker_control_still_drains(self):
        self.reset_member(status="idle")
        self.assertTrue(self.drain())

    def test_supervisor_loop_uses_owned_restart_without_replaying_unknown_work(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"]["permission_mode"] = "default"
            self.store.put_room(db, room)
        owner = self.store.room()["owner"]
        message = self.store.send("CODEX_01", "CLAUDE_01", "Unconfirmed effect; do not replay")
        self.store.begin_attempt(message, self.generation)
        with ExitStack() as stack:
            for name in ("launch", "refresh_gateway", "native_events", "approvals", "watch_catalogs"):
                stack.enter_context(patch.object(self.supervisor, name, new_callable=AsyncMock))
            for name in ("request_upgrade", "check_hook_silence"):
                stack.enter_context(patch.object(self.supervisor, name))
            stack.enter_context(patch.object(self.supervisor, "owner_alive", return_value=True))
            stack.enter_context(patch("ihav_agent_room.runtime.claude_agents", return_value=[self.native_row()]))
            stack.enter_context(patch("ihav_agent_room.runtime.process_stamp", return_value=None))
            stack.enter_context(patch("ihav_agent_room.runtime.process_alive", return_value=False))
            stop = stack.enter_context(patch("ihav_agent_room.runtime.stop_claude_worker", new_callable=AsyncMock))
            restart = stack.enter_context(patch("ihav_agent_room.runtime.start_room"))
            dispatch = stack.enter_context(patch.object(self.supervisor, "dispatch", new_callable=AsyncMock))
            import_queue = stack.enter_context(patch.object(self.supervisor, "import_global_queue", new_callable=AsyncMock))
            asyncio.run(self.supervisor.run())
        restart.assert_called_once_with(self.store, owner["session"], permission_mode="default", automatic=True)
        stop.assert_not_awaited()
        dispatch.assert_not_awaited()
        import_queue.assert_not_awaited()
        self.assertEqual(self.store.room()["owner"], owner)
        self.assertEqual(self.store.member("CLAUDE_01")["native_id"], self.native_id)
        self.assertEqual(self.store.attempts()["items"][0]["state"], "unknown")

    def test_terminal_admission_cannot_stop_a_later_live_or_unverifiable_session(self):
        cases = ([self.native_row(state="running", status="working", pid=202)],
                 [self.native_row(state="blocked")], [],
                 RoomError("Registry unavailable", "native"))
        for later in cases:
            with self.subTest(later=later):
                self.setUp_transition()
                self.reset_member()
                with ExitStack() as stack:
                    for name in ("launch", "refresh_gateway", "native_events", "approvals", "watch_catalogs"):
                        stack.enter_context(patch.object(self.supervisor, name, new_callable=AsyncMock))
                    for name in ("request_upgrade", "check_hook_silence"):
                        stack.enter_context(patch.object(self.supervisor, name))
                    stack.enter_context(patch.object(self.supervisor, "owner_alive", return_value=True))
                    stack.enter_context(patch("ihav_agent_room.runtime.claude_agents",
                                              side_effect=[[self.native_row()], later]))
                    stack.enter_context(patch("ihav_agent_room.runtime.process_stamp",
                                              side_effect=lambda pid: "new-stamp" if pid == 202 else None))
                    stack.enter_context(patch("ihav_agent_room.runtime.process_alive", return_value=False))
                    stop = stack.enter_context(patch("ihav_agent_room.runtime.stop_claude_worker", new_callable=AsyncMock))
                    kill = stack.enter_context(patch("ihav_agent_room.runtime.os.kill"))
                    killpg = stack.enter_context(patch("ihav_agent_room.runtime.os.killpg"))
                    restart = stack.enter_context(patch("ihav_agent_room.runtime.start_room"))
                    asyncio.run(self.supervisor.run())
                stop.assert_not_awaited()
                kill.assert_not_called()
                killpg.assert_not_called()
                restart.assert_not_called()
                self.assertEqual(self.store.room()["status"], "failed")
                self.assertEqual(self.store.member("CLAUDE_01")["native_id"], self.native_id)
                self.assertIn("Terminal upgrade cleanup held", self.store.room()["error"])

    def test_terminal_cleanup_preserves_a_late_native_approval(self):
        self.refresh([self.native_row()])
        self.assertTrue(self.drain())
        approval = {"state": "pending", "member": "CLAUDE_01", "generation": self.generation}
        with self.store.tx() as db:
            db.execute("INSERT INTO approvals VALUES (?, ?)", ("A-late", json.dumps(approval)))
        with patch("ihav_agent_room.runtime.claude_agents", return_value=[self.native_row()]), \
                patch("ihav_agent_room.runtime.process_stamp", return_value=None), \
                patch("ihav_agent_room.runtime.process_alive", return_value=False), \
                patch("ihav_agent_room.runtime.stop_claude_worker", new_callable=AsyncMock) as stop:
            asyncio.run(self.supervisor.shutdown())
        stop.assert_not_awaited()
        self.assertEqual(self.store.room()["status"], "failed")
        with self.store.read() as db:
            current = json.loads(db.execute("SELECT data FROM approvals WHERE id=?", ("A-late",)).fetchone()[0])
        self.assertEqual(current, approval)

    def test_terminal_cleanup_holds_a_reused_recorded_pid(self):
        self.refresh([self.native_row()])
        self.assertTrue(self.drain())
        with patch("ihav_agent_room.runtime.claude_agents", return_value=[self.native_row()]), \
                patch("ihav_agent_room.runtime.process_stamp", return_value="reused-stamp"), \
                patch("ihav_agent_room.runtime.process_alive", return_value=False), \
                patch("ihav_agent_room.runtime.stop_claude_worker", new_callable=AsyncMock) as stop:
            asyncio.run(self.supervisor.shutdown())
        stop.assert_not_awaited()
        self.assertEqual(self.store.room()["status"], "failed")

    def test_process_inspection_timeout_records_failure_instead_of_escaping_shutdown(self):
        for row, timeout_pid in ((self.native_row(pid=303), 303), (self.native_row(), 101)):
            with self.subTest(timeout_pid=timeout_pid):
                self.setUp_transition()
                self.reset_member()
                self.refresh([self.native_row()])
                self.assertTrue(self.drain())
                def inspect(pid):
                    if pid == timeout_pid:
                        raise subprocess.TimeoutExpired(["ps", "-p", str(pid)], 3)
                    return None
                with patch("ihav_agent_room.runtime.claude_agents", return_value=[row]), \
                        patch("ihav_agent_room.runtime.process_stamp", side_effect=inspect), \
                        patch("ihav_agent_room.runtime.process_alive", return_value=False), \
                        patch("ihav_agent_room.runtime.stop_claude_worker", new_callable=AsyncMock) as stop, \
                        patch("ihav_agent_room.runtime.os.kill") as kill, \
                        patch("ihav_agent_room.runtime.os.killpg") as killpg:
                    asyncio.run(self.supervisor.shutdown())
                stop.assert_not_awaited()
                kill.assert_not_called()
                killpg.assert_not_called()
                self.assertEqual(self.store.room()["status"], "failed")
                self.assertIsNone(self.store.room()["supervisor"])
                self.assertIn("Terminal upgrade cleanup held", self.store.room()["error"])

    def test_approval_arriving_before_shutdown_commit_is_preserved(self):
        self.refresh([self.native_row()])
        self.assertTrue(self.drain())
        approval = {"state": "pending", "member": "CLAUDE_01", "generation": self.generation}
        original_tx = self.store.tx
        injected = False
        @contextmanager
        def inject_late_approval(*args, **kwargs):
            nonlocal injected
            with original_tx(*args, **kwargs) as db:
                member = json.loads(db.execute("SELECT data FROM members WHERE name='CLAUDE_01'").fetchone()[0])
                if member["pid"] is None and not injected:
                    # Another process queues a prompt after the worker exit was committed.
                    db.execute("INSERT INTO approvals VALUES (?, ?)", ("A-final", json.dumps(approval)))
                    injected = True
                yield db
        with patch.object(self.store, "tx", side_effect=inject_late_approval), \
                patch("ihav_agent_room.runtime.claude_agents", return_value=[self.native_row()]), \
                patch("ihav_agent_room.runtime.process_stamp", return_value=None), \
                patch("ihav_agent_room.runtime.process_alive", return_value=False), \
                patch("ihav_agent_room.runtime.stop_claude_worker", new_callable=AsyncMock) as stop:
            asyncio.run(self.supervisor.shutdown())
        stop.assert_not_awaited()
        self.assertTrue(injected)
        self.assertEqual(self.store.room()["status"], "failed")
        with self.store.read() as db:
            current = json.loads(db.execute("SELECT data FROM approvals WHERE id=?", ("A-final",)).fetchone()[0])
        self.assertEqual(current, approval)


if __name__ == "__main__":
    unittest.main()
