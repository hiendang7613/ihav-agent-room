"""Termination keeps process identity and truthful outcomes across shared wait paths."""

import signal
import unittest
from unittest.mock import AsyncMock, patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.native import stop_claude_worker, stop_descendants


class NativeCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooperative_exit_and_reused_pid_are_not_killed_again(self):
        for replacement in (None, "new-process"):
            with self.subTest(replacement=replacement):
                processes = {42: "owned", 99: "unrelated"}
                def alive(pid, stamp):
                    return processes.get(pid) == stamp
                def kill(pid, sig):
                    self.assertEqual((pid, sig), (42, signal.SIGTERM))
                    processes[pid] = replacement
                with patch("ihav_agent_room.native.process_alive", side_effect=alive), \
                        patch("ihav_agent_room.native.os.kill", side_effect=kill) as sent, \
                        patch("ihav_agent_room.native.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    await stop_descendants({42: "owned"})
                sent.assert_called_once_with(42, signal.SIGTERM)
                sleep.assert_not_awaited()
                self.assertEqual(processes[99], "unrelated")

    async def test_exit_during_last_wait_is_confirmed_after_escalation(self):
        waits = 0
        async def sleep(delay):
            nonlocal waits
            waits += 1
        with patch("ihav_agent_room.native.process_alive", side_effect=lambda pid, stamp: waits < 40), \
                patch("ihav_agent_room.native.os.kill") as sent, \
                patch("ihav_agent_room.native.asyncio.sleep", side_effect=sleep):
            await stop_descendants({42: "owned"})
        self.assertEqual([call.args for call in sent.call_args_list], [(42, signal.SIGTERM), (42, signal.SIGKILL)])
        self.assertEqual(waits, 40)

    async def test_unconfirmed_exit_and_signal_denial_remain_failures(self):
        with patch("ihav_agent_room.native.process_alive", return_value=True), \
                patch("ihav_agent_room.native.os.kill"), \
                patch("ihav_agent_room.native.asyncio.sleep", new_callable=AsyncMock), \
                self.assertRaises(RoomError) as error:
            await stop_descendants({42: "owned"})
        self.assertEqual(error.exception.code, "cleanup")
        with patch("ihav_agent_room.native.process_alive", return_value=True), \
                patch("ihav_agent_room.native.os.kill", side_effect=PermissionError), \
                self.assertRaises(PermissionError):
            await stop_descendants({42: "owned"})

    async def test_claude_stop_return_is_not_process_exit_evidence(self):
        with patch("ihav_agent_room.native.process_alive", return_value=True), \
                patch("ihav_agent_room.native.owned_descendants", return_value={}), \
                patch("ihav_agent_room.native.stop_claude") as stop, \
                patch("ihav_agent_room.native.asyncio.sleep", new_callable=AsyncMock), \
                self.assertRaises(RoomError) as error:
            await stop_claude_worker("project", "exact-session", {"pid": 42, "stamp": "owned"})
        stop.assert_called_once_with("project", "exact-session")
        self.assertEqual(error.exception.code, "cleanup")
