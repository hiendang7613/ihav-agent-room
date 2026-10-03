"""Native launch failures retain a local, inspectable diagnostic path."""

import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.native import start_claude
from scripts.native_smoke import _smoke_known_main_state


class FailedProcess:
    returncode = 23

    async def wait(self):
        return self.returncode


class LaunchedProcess:
    """A successful `claude --bg` that prints the session ID it backgrounded."""
    returncode = 0

    def __init__(self, stream, session):
        stream.write(f"backgrounded · 1a2b3c4d session {session}\n")
        stream.flush()

    async def wait(self):
        return 0


SESSION = "11111111-2222-3333-4444-555555555555"


class SessionDiscoveryTests(unittest.TestCase):
    """Field reports 2026-10-03 from ihav-competitor-search: --cwd filter and a 4 s liveness budget."""

    def launch(self, project, registry, **env):
        async def launched(*args, **kwargs):
            return LaunchedProcess(kwargs["stdout"], SESSION)
        with patch("ihav_agent_room.native.claude_agents", side_effect=registry), \
                patch("ihav_agent_room.native.asyncio.create_subprocess_exec", new=launched), \
                patch("ihav_agent_room.native.process_stamp", return_value="stamp"), \
                patch.dict("os.environ", env):
            return asyncio.run(start_claude(project, SESSION, True, {"PATH": "/usr/bin"}, project / "CLAUDE_EXPERT.log",
                                            member="CLAUDE_EXPERT"))

    def test_a_session_hidden_by_the_cwd_filter_is_found_unscoped_with_its_own_cwd_check(self):
        with tempfile.TemporaryDirectory(prefix="discovery ") as directory:
            project = Path(directory)
            live = {"sessionId": SESSION, "cwd": str(project), "pid": 1, "id": "1a2b3c4d"}
            elsewhere = dict(live, cwd="/somewhere/else")
            calls = []

            def registry(_project, _env=None, scoped=True):
                calls.append(scoped)
                return [] if scoped else [live]
            self.assertEqual(self.launch(project, registry)["sessionId"], SESSION)
            self.assertIn(False, calls)

            def other_project_only(_project, _env=None, scoped=True):
                return [] if scoped else [elsewhere]
            with self.assertRaises(RoomError) as caught:
                self.launch(project, other_project_only, IHAV_AGENT_ROOM_LIVENESS_SECONDS="1")
            self.assertEqual(caught.exception.code, "identity")  # Another project's session is never adopted.

    def test_liveness_uses_a_wall_clock_budget_and_reports_it(self):
        with tempfile.TemporaryDirectory(prefix="liveness ") as directory:
            with self.assertRaises(RoomError) as caught:
                self.launch(Path(directory), lambda *args, **kwargs: [], IHAV_AGENT_ROOM_LIVENESS_SECONDS="1")
            self.assertRegex(str(caught.exception), r"did not become live within 1\.\d s")


class NativeLaunchTests(unittest.TestCase):
    def test_claude_model_and_effort_are_set_only_on_fresh_worker_launches(self):
        with tempfile.TemporaryDirectory(prefix="native model config ") as directory:
            project = Path(directory)
            captured = []

            async def failed_launch(*args, **kwargs):
                captured.append(args)
                return FailedProcess()

            for resume, native_id in ((False, None), (True, "existing-session")):
                with self.subTest(resume=resume), \
                        patch("ihav_agent_room.native.claude_agents", return_value=[]), \
                        patch("ihav_agent_room.native.asyncio.create_subprocess_exec", new=failed_launch):
                    with self.assertRaises(RoomError):
                        asyncio.run(start_claude(project, native_id, resume, {"PATH": "/usr/bin"},
                            project / "runtime" / "CLAUDE_EXPERT.log", member="CLAUDE_EXPERT",
                            model="opus", effort="xhigh"))

            fresh_args, resume_args = captured
            self.assertEqual(fresh_args[fresh_args.index("--model") + 1], "opus")
            self.assertEqual(fresh_args[fresh_args.index("--effort") + 1], "xhigh")
            self.assertNotIn("--model", resume_args)
            self.assertNotIn("--effort", resume_args)

    def test_smoke_cleanup_rejects_duplicate_registry_identity_for_known_session(self):
        with tempfile.TemporaryDirectory(prefix="native cleanup duplicate ") as directory:
            project = Path(directory)
            entry = {"sessionId": "known-session", "cwd": str(project),
                     "kind": "background", "pid": 21}
            with patch("scripts.native_smoke.claude_agents", return_value=[entry, dict(entry)]), \
                    patch("scripts.native_smoke.process_alive", return_value=True):
                state = _smoke_known_main_state(project, "known-session", (21, "stamp-21"), {})
            self.assertEqual(state, "unverified")

    def test_smoke_cleanup_rejects_wrong_cwd_for_known_session(self):
        with tempfile.TemporaryDirectory(prefix="native cleanup project ") as directory, \
                tempfile.TemporaryDirectory(prefix="native cleanup foreign ") as foreign:
            project = Path(directory)
            entry = {"sessionId": "known-session", "cwd": foreign,
                     "kind": "background", "pid": 21}
            with patch("scripts.native_smoke.claude_agents", return_value=[entry]), \
                    patch("scripts.native_smoke.process_alive", return_value=True):
                state = _smoke_known_main_state(project, "known-session", (21, "stamp-21"), {})
            self.assertEqual(state, "unverified")

    def test_smoke_cleanup_rejects_foreground_kind_for_known_session(self):
        with tempfile.TemporaryDirectory(prefix="native cleanup foreground ") as directory:
            project = Path(directory)
            entry = {"sessionId": "known-session", "cwd": str(project),
                     "kind": "foreground", "pid": 21}
            with patch("scripts.native_smoke.claude_agents", return_value=[entry]), \
                    patch("scripts.native_smoke.process_alive", return_value=True):
                state = _smoke_known_main_state(project, "known-session", (21, "stamp-21"), {})
            self.assertEqual(state, "unverified")

    def test_smoke_cleanup_uses_captured_process_when_registry_pid_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="native cleanup missing registry pid ") as directory:
            project = Path(directory)
            entry = {"sessionId": "known-session", "cwd": str(project), "kind": "background"}
            with patch("scripts.native_smoke.claude_agents", return_value=[entry]), \
                    patch("scripts.native_smoke.process_alive", return_value=True):
                state = _smoke_known_main_state(project, "known-session", (21, "stamp-21"), {})
            self.assertEqual(state, "alive")

            entry["status"] = "running"
            with patch("scripts.native_smoke.claude_agents", return_value=[entry]):
                state = _smoke_known_main_state(project, "known-session", None, {})
            self.assertEqual(state, "unverified")

    def test_smoke_cleanup_rejects_registry_pid_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="native cleanup identity ") as directory:
            project = Path(directory)
            registry = [{"sessionId": "known-session", "cwd": str(project),
                         "kind": "background", "pid": 22}]
            with patch("scripts.native_smoke.claude_agents", return_value=registry), \
                    patch("scripts.native_smoke.process_alive", return_value=False):
                state = _smoke_known_main_state(project, "known-session", (21, "stamp-21"), {})
            self.assertEqual(state, "unverified")

    def test_smoke_cleanup_rejects_missing_initial_process_stamp(self):
        with tempfile.TemporaryDirectory(prefix="native cleanup stamp ") as directory:
            project = Path(directory)
            with patch("scripts.native_smoke.claude_agents") as agents:
                state = _smoke_known_main_state(project, "known-session", (21, None), {})
            self.assertEqual(state, "unverified")
            agents.assert_not_called()

    def test_failed_background_launch_reports_retained_member_log(self):
        with tempfile.TemporaryDirectory(prefix="native launch ") as directory:
            project = Path(directory)
            log = project / ".ihav-agent-room" / "runtime" / "CLAUDE_EXPERT.log"

            async def failed_launch(*args, **kwargs):
                kwargs["stderr"].write("fixture native launch diagnostic\n")
                kwargs["stderr"].flush()
                return FailedProcess()

            with patch("ihav_agent_room.native.claude_agents", return_value=[]), \
                    patch("ihav_agent_room.native.asyncio.create_subprocess_exec", new=failed_launch):
                with self.assertRaises(RoomError) as caught:
                    asyncio.run(start_claude(project, None, False, {"PATH": "/usr/bin"}, log,
                                             member="CLAUDE_EXPERT"))

            self.assertEqual(caught.exception.code, "native")
            self.assertEqual(caught.exception.details["returncode"], 23)
            self.assertEqual(caught.exception.details["log_path"], str(log.resolve()))
            self.assertIn(str(log.resolve()), str(caught.exception))
            self.assertIn("fixture native launch diagnostic", log.read_text())


if __name__ == "__main__":
    unittest.main()
