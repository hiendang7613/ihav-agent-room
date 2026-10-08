"""A recovered native observation must not erase identity or unknown outcomes."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.store import Store


EXIT_ERROR = "Native background session exited. Stop/start to resume it."
RECONCILIATION_ERROR = "Native Claude liveness is unavailable; native registry requires reconciliation before recovery."


class NativeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-refresh-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.store.initialize("pair")
        self.generation = "refresh-test"
        self.native_id = "saved-claude-session"
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="running", generation=self.generation)
            self.store.put_room(db, room)
        self.supervisor = Supervisor(self.store, self.generation)
        self.supervisor.claude["CLAUDE_01"] = self.native_id
        self.reset_member()

    def reset_member(self, **changes):
        member = dict(native_id=self.native_id, launch_generation=self.generation,
                      status="stopped", pid=101, stamp="old-stamp", error=EXIT_ERROR,
                      unexpected_native_id=None)
        member.update(changes)
        self.store.member("CLAUDE_01", member)

    def native_row(self, **changes):
        row = dict(sessionId=self.native_id, cwd=str(self.store.project),
                   pid=202, id="saved-job", status="idle")
        row.update(changes)
        return row

    def refresh(self, rows, stamp="native-stamp", stamps=None):
        inspection = {"return_value": stamp} if stamps is None else {"side_effect": stamps.get}
        with patch("ihav_agent_room.runtime.claude_agents", return_value=rows), \
                patch("ihav_agent_room.runtime.process_stamp", **inspection) as inspect:
            asyncio.run(self.supervisor.refresh_claude(force=True))
        return inspect

    def test_transient_loss_then_exact_live_observation_keeps_unknown_attempts(self):
        message = self.store.send("CODEX_01", "CLAUDE_01", "Keep an uncertain native outcome")
        attempt = self.store.begin_attempt(message, self.generation)
        self.assertIsNotNone(attempt)
        room_before = self.store.room()
        self.refresh([])
        interrupted = self.store.attempts()
        self.assertEqual(interrupted["items"][0]["state"], "unknown")
        self.assertEqual(self.store.member("CLAUDE_01")["error"], RECONCILIATION_ERROR)

        self.refresh([self.native_row()])

        member = self.store.member("CLAUDE_01")
        self.assertIsNone(member["error"])
        self.assertEqual(member["status"], "idle")
        self.assertEqual(member["pid"], 202)
        self.assertEqual(member["stamp"], "native-stamp")
        self.assertEqual(member["native_id"], self.native_id)
        self.assertEqual(member["launch_generation"], self.generation)
        self.assertEqual(self.store.attempts(), interrupted)
        self.assertEqual(self.store.room(), room_before)

    def test_other_errors_and_identity_or_generation_disagreement_are_preserved(self):
        for changes in ({"error": "Native identity mismatch"},
                        {"unexpected_native_id": "another-session"},
                        {"native_id": "another-session"},
                        {"launch_generation": "older-generation"}):
            with self.subTest(changes=changes):
                self.reset_member(**changes)
                before = self.store.member("CLAUDE_01")
                self.refresh([self.native_row()])
                after = self.store.member("CLAUDE_01")
                for key in ("error", "native_id", "unexpected_native_id", "launch_generation"):
                    self.assertEqual(after.get(key), before.get(key))

    def test_unknown_or_failed_native_status_does_not_clear_exit_error(self):
        for status in (None, "unknown", "future-status", "failed"):
            with self.subTest(status=status):
                self.reset_member()
                row = self.native_row()
                if status is None:
                    row.pop("status")
                else:
                    row["status"] = status
                self.refresh([row])
                self.assertEqual(self.store.member("CLAUDE_01")["error"], EXIT_ERROR)

    def test_waiting_native_input_stays_waiting_after_transient_exit_error(self):
        self.refresh([self.native_row(waitingFor="permission")])
        member = self.store.member("CLAUDE_01")
        self.assertIsNone(member["error"])
        self.assertEqual(member["status"], "waiting_native_input")
        self.assertIn("waits for permission", self.store.inbox(self.store.gateway)["items"][0]["body"])

    def test_dead_process_still_records_exit(self):
        self.reset_member(error=None)
        self.refresh([self.native_row(kind="background", state="done", status=None)], stamp=None)
        member = self.store.member("CLAUDE_01")
        self.assertEqual(member["status"], "stopped")
        self.assertEqual(member["error"], EXIT_ERROR)

    def test_wrong_session_or_project_is_not_a_recovered_observation(self):
        for changes in ({"sessionId": "another-session"},
                        {"cwd": str(self.store.project.parent)}):
            with self.subTest(changes=changes):
                self.reset_member()
                row = self.native_row()
                row.update(changes)
                self.refresh([row])
                member = self.store.member("CLAUDE_01")
                self.assertEqual(member["status"], "stopped")
                self.assertEqual(member["error"], RECONCILIATION_ERROR)

    def test_blocked_no_pid_reports_hold_and_preserves_historical_exit(self):
        room_before = self.store.room()
        member_before = self.store.member("CLAUDE_01")
        message = self.store.send("CODEX_01", "CLAUDE_01", "Preserve an unobserved result")
        attempt = self.store.begin_attempt(message, self.generation)
        self.assertIsNotNone(attempt)

        self.refresh([self.native_row(pid=None, kind="background", state="blocked", status=None)], stamp=None)

        member = self.store.member("CLAUDE_01")
        self.assertEqual(member["error"], RECONCILIATION_ERROR)
        self.assertNotIn("Stop/start", member["error"])
        self.assertEqual(member["previous_native_exit_error"], EXIT_ERROR)
        self.assertEqual(member["native_observation"], {
            "kind": "background", "state": "blocked", "status": None,
            "reason": "native_registry_requires_reconciliation"})
        for key in ("native_id", "launch_generation", "unexpected_native_id", "permission_mode", "token_hash"):
            self.assertEqual(member.get(key), member_before.get(key))
        self.assertEqual(self.store.room(), room_before)
        interrupted = self.store.attempts()
        self.assertEqual(interrupted["items"][0]["state"], "unknown")
        self.assertIn("liveness unavailable", interrupted["items"][0]["detail"])

        self.refresh([self.native_row(kind="background", state="blocked")])
        self.assertIsNone(self.store.member("CLAUDE_01")["error"])
        self.assertEqual(self.store.member("CLAUDE_01")["previous_native_exit_error"], EXIT_ERROR)
        self.assertEqual(self.store.attempts(), interrupted)
        self.assertEqual(self.store.room(), room_before)

    def test_no_matching_registry_row_reports_unverified_liveness(self):
        self.reset_member(error=None)
        self.refresh([], stamp=None)
        member = self.store.member("CLAUDE_01")
        self.assertEqual(member["error"], RECONCILIATION_ERROR)
        self.assertEqual(member["native_observation"], {
            "kind": None, "state": None, "status": None,
            "reason": "native_registry_requires_reconciliation"})

    def test_nonterminal_dead_pid_and_invalid_terminal_pid_remain_held(self):
        rows = [self.native_row(kind="background", state="working"),
                self.native_row(pid=None, kind="background", state="done", status="idle")]
        rows.extend(self.native_row(pid=pid, kind="background", state="done", status=None)
                    for pid in (0, 1, -1, False, "202"))
        for row in rows:
            with self.subTest(row=row):
                self.reset_member(error=None)
                self.refresh([row], stamp=None)
                self.assertEqual(self.store.member("CLAUDE_01")["error"], RECONCILIATION_ERROR)

    def test_terminal_background_row_without_pid_retains_exit_diagnosis(self):
        for state in ("stopped", "failed", "done"):
            with self.subTest(state=state):
                self.reset_member(error=None)
                self.refresh([self.native_row(pid=None, kind="background", state=state, status=None)], stamp=None)
                member = self.store.member("CLAUDE_01")
                self.assertEqual(member["error"], EXIT_ERROR)
                self.assertEqual(member["native_observation"]["reason"], "native_terminal_observed")

    def test_unavailable_liveness_does_not_overwrite_unrelated_errors(self):
        for error in ("Native identity mismatch", "Native permission approval required", "Unexpected native failure"):
            with self.subTest(error=error):
                self.reset_member(error=error)
                self.refresh([self.native_row(pid=None, kind="background", state="blocked", status=None)], stamp=None)
                self.assertEqual(self.store.member("CLAUDE_01")["error"], error)

    def test_reconciliation_error_only_clears_with_current_identity_and_live_status(self):
        for changes in ({"native_id": "another-session"}, {"unexpected_native_id": "another-session"},
                        {"launch_generation": None}, {"launch_generation": "older-generation"}):
            with self.subTest(changes=changes):
                self.reset_member(error=RECONCILIATION_ERROR, **changes)
                self.refresh([self.native_row()])
                self.assertEqual(self.store.member("CLAUDE_01")["error"], RECONCILIATION_ERROR)
        for status in ("unknown", "future-status", "failed"):
            with self.subTest(status=status):
                self.reset_member(error=RECONCILIATION_ERROR)
                self.refresh([self.native_row(status=status)])
                self.assertEqual(self.store.member("CLAUDE_01")["error"], RECONCILIATION_ERROR)

    def test_recovered_observation_preserves_the_stamp_that_established_liveness(self):
        with patch("ihav_agent_room.runtime.claude_agents", return_value=[self.native_row()]), \
                patch("ihav_agent_room.runtime.process_stamp", side_effect=["native-stamp", None]):
            asyncio.run(self.supervisor.refresh_claude(force=True))
        self.assertEqual(self.store.member("CLAUDE_01")["stamp"], "native-stamp")

    def test_retained_terminal_row_cannot_hide_the_only_live_exact_session(self):
        terminal = self.native_row(pid=None, kind="background", state="done", status=None)
        live = self.native_row(kind="interactive", state="working")
        for rows in ([terminal, live], [live, terminal]):
            with self.subTest(rows=rows):
                self.reset_member()
                message = self.store.send("CODEX_01", "CLAUDE_01", "Keep a live turn in flight")
                self.store.begin_attempt(message, self.generation)
                attempts_before = self.store.attempts()
                room_before = self.store.room()

                inspect = self.refresh(rows, stamps={202: "live-stamp"})

                member = self.store.member("CLAUDE_01")
                self.assertEqual(member["status"], "idle")
                self.assertEqual(member["pid"], 202)
                self.assertEqual(member["stamp"], "live-stamp")
                self.assertIsNone(member["error"])
                self.assertEqual(member["native_observation"]["reason"], "native_live_observed")
                self.assertEqual(self.store.attempts(), attempts_before)
                self.assertEqual(self.store.room(), room_before)
                inspect.assert_called_once_with(202)

    def test_multiple_live_rows_hold_and_each_pid_is_inspected_once(self):
        for second_pid in (202, 303):
            with self.subTest(second_pid=second_pid):
                self.reset_member()
                rows = [self.native_row(kind="background"),
                        self.native_row(pid=second_pid, kind="interactive", status="working")]

                inspect = self.refresh(rows, stamps={202: "stamp-202", 303: "stamp-303"})

                member = self.store.member("CLAUDE_01")
                self.assertEqual(member["status"], "stopped")
                self.assertEqual(member["error"], RECONCILIATION_ERROR)
                self.assertEqual(member["pid"], 101)
                self.assertEqual(member["stamp"], "old-stamp")
                self.assertEqual(member["native_observation"]["reason"], "native_registry_requires_reconciliation")
                self.assertEqual(member["native_observation"]["matching_rows"], 2)
                self.assertEqual(member["native_observation"]["live_rows"], 2)
                self.assertEqual(inspect.call_count, len({202, second_pid}))

    def test_terminal_and_nonterminal_no_pid_rows_remain_held_in_either_order(self):
        terminal = self.native_row(pid=None, kind="background", state="done", status=None)
        blocked = self.native_row(pid=None, kind="background", state="blocked", status=None)
        for rows in ([terminal, blocked], [blocked, terminal]):
            with self.subTest(rows=rows):
                self.reset_member()

                inspect = self.refresh(rows, stamps={})

                member = self.store.member("CLAUDE_01")
                self.assertEqual(member["error"], RECONCILIATION_ERROR)
                self.assertEqual(member["previous_native_exit_error"], EXIT_ERROR)
                self.assertEqual(member["native_observation"]["state"], "blocked")
                self.assertEqual(member["native_observation"]["matching_rows"], 2)
                self.assertEqual(member["native_observation"]["live_rows"], 0)
                inspect.assert_not_called()

    def test_every_exact_row_must_be_terminal_and_dead_before_reporting_exit(self):
        rows = [self.native_row(pid=None, kind="background", state="done", status=None),
                self.native_row(pid=404, kind="background", state="stopped", status=None)]
        self.reset_member(error=None)

        inspect = self.refresh(rows, stamps={404: None})

        member = self.store.member("CLAUDE_01")
        self.assertEqual(member["error"], EXIT_ERROR)
        self.assertEqual(member["native_observation"]["reason"], "native_terminal_observed")
        self.assertEqual(member["native_observation"]["matching_rows"], 2)
        self.assertEqual(member["native_observation"]["live_rows"], 0)
        inspect.assert_called_once_with(404)

    def test_invalid_pid_cannot_become_live_from_a_permissive_inspection_fixture(self):
        for pid in (None, 0, 1, -1, False, "202"):
            with self.subTest(pid=pid):
                self.reset_member()

                inspect = self.refresh([self.native_row(pid=pid, kind="background", state="blocked")])

                self.assertEqual(self.store.member("CLAUDE_01")["error"], RECONCILIATION_ERROR)
                inspect.assert_not_called()

    def test_process_inspection_failure_does_not_record_a_false_exit_or_interrupt(self):
        message = self.store.send("CODEX_01", "CLAUDE_01", "Keep outcome pending during inspection failure")
        self.store.begin_attempt(message, self.generation)
        member_before = self.store.member("CLAUDE_01")
        attempts_before = self.store.attempts()
        room_before = self.store.room()
        rows = [self.native_row(pid=None, kind="background", state="done", status=None),
                self.native_row()]
        def unavailable(pid):
            if pid is None:
                return None
            raise RoomError("inspection failed", "process_inspection")
        with patch("ihav_agent_room.runtime.claude_agents", return_value=rows), \
                patch("ihav_agent_room.runtime.process_stamp", side_effect=unavailable):
            with self.assertRaisesRegex(RoomError, "inspection failed"):
                asyncio.run(self.supervisor.refresh_claude(force=True))
        self.assertEqual(self.store.member("CLAUDE_01"), member_before)
        self.assertEqual(self.store.attempts(), attempts_before)
        self.assertEqual(self.store.room(), room_before)

    def test_one_live_row_does_not_hide_a_blocked_exact_row_in_either_order(self):
        live = self.native_row(kind="interactive", state="working")
        blocked = self.native_row(pid=None, kind="background", state="blocked", status=None)
        for rows in ([live, blocked], [blocked, live]):
            with self.subTest(rows=rows):
                self.reset_member()
                member_before = self.store.member("CLAUDE_01")
                room_before = self.store.room()
                message = self.store.send("CODEX_01", "CLAUDE_01", "Retain conflicting native ownership")
                self.store.begin_attempt(message, self.generation)

                inspect = self.refresh(rows, stamps={202: "live-stamp"})

                member = self.store.member("CLAUDE_01")
                self.assertEqual(member["status"], "stopped")
                self.assertEqual(member["error"], RECONCILIATION_ERROR)
                self.assertEqual(member["previous_native_exit_error"], EXIT_ERROR)
                self.assertEqual(member["native_observation"], {
                    "kind": "background", "state": "blocked", "status": None,
                    "matching_rows": 2, "live_rows": 1,
                    "reason": "native_registry_requires_reconciliation"})
                for key in ("native_id", "launch_generation", "unexpected_native_id", "permission_mode", "token_hash", "pid", "stamp"):
                    self.assertEqual(member.get(key), member_before.get(key))
                attempts = self.store.attempts()["items"]
                self.assertTrue(all(item["state"] == "unknown" for item in attempts))
                self.assertTrue(all("liveness unavailable" in item["detail"] for item in attempts))
                self.assertEqual(self.store.room(), room_before)
                inspect.assert_called_once_with(202)

    def test_one_live_row_does_not_hide_an_invalid_terminal_pid_in_either_order(self):
        live = self.native_row(kind="interactive", state="working")
        for pid in (0, 1, -1, False, "202"):
            invalid = self.native_row(pid=pid, kind="background", state="done", status=None)
            for rows in ([live, invalid], [invalid, live]):
                with self.subTest(pid=pid, rows=rows):
                    self.reset_member(error=RECONCILIATION_ERROR)
                    member_before = self.store.member("CLAUDE_01")
                    room_before = self.store.room()
                    message = self.store.send("CODEX_01", "CLAUDE_01", "Keep invalid native correlation held")
                    self.store.begin_attempt(message, self.generation)

                    inspect = self.refresh(rows, stamps={202: "live-stamp"})

                    member = self.store.member("CLAUDE_01")
                    self.assertEqual(member["status"], "stopped")
                    self.assertEqual(member["error"], RECONCILIATION_ERROR)
                    self.assertEqual(member["native_observation"]["reason"], "native_registry_requires_reconciliation")
                    self.assertEqual(member["native_observation"]["state"], "done")
                    self.assertEqual(member["native_observation"]["matching_rows"], 2)
                    self.assertEqual(member["native_observation"]["live_rows"], 1)
                    for key in ("native_id", "launch_generation", "unexpected_native_id", "permission_mode", "token_hash", "pid", "stamp"):
                        self.assertEqual(member.get(key), member_before.get(key))
                    self.assertTrue(all(item["state"] == "unknown" for item in self.store.attempts()["items"]))
                    self.assertEqual(self.store.room(), room_before)
                    inspect.assert_called_once_with(202)


if __name__ == "__main__":
    unittest.main()
