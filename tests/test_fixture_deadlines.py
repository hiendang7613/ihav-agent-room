"""Offline regression coverage for the runtime fixture's deadlines and evidence."""
from contextlib import contextmanager
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_runtime as runtime
from ihav_agent_room import native


class FixtureDeadlineTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        case = runtime.RuntimeTests("test_stop_recovers_after_supervisor_crash")
        try:
            case.setUp()
            yield case
        finally:
            self.assertTrue(case.doCleanups(), "fixture cleanup failed")
            if hasattr(case, "main_process"):
                self.assertIsNotNone(case.main_process.poll(), "fixture gateway leaked")
            if hasattr(case, "root"):
                self.assertFalse(case.root.exists(), "fixture directory leaked")

    def delay_first_initialize(self, case, seconds):
        fake = case.root / "bin/codex"
        fake.unlink()
        fake.write_text("#!" + sys.executable + "\n"
            "import os, pathlib, sys, time\n"
            "marker = pathlib.Path(os.environ['FAKE_NATIVE_ROOT']) / 'delay-once'\n"
            "if sys.argv[1:2] == ['app-server'] and '--help' not in sys.argv and not marker.exists():\n"
            "    marker.touch(); time.sleep(" + repr(seconds) + ")\n"
            "fixture = pathlib.Path(" + repr(str(runtime.FIXTURE.resolve())) + ")\n"
            "exec(compile(fixture.read_text(), str(fixture), 'exec'), {'__name__':'__main__','__file__':str(fixture)})\n")
        fake.chmod(0o755)

    def test_supported_initialize_delay_reaches_original_crash_recovery_assertions(self):
        with self.fixture() as case:
            self.delay_first_initialize(case, 16)
            case.test_stop_recovers_after_supervisor_crash()
            self.assertEqual(case.store.room()["status"], "stopped")

    def test_initialize_over_native_budget_still_fails_without_retry(self):
        with self.fixture() as case:
            self.delay_first_initialize(case, 21)
            with self.assertRaisesRegex(AssertionError, "Startup failed:.*initialize.*timed out"):
                case.start()
            self.assertEqual(case.store.room()["status"], "failed")
            self.assertTrue((case.root / "native/delay-once").is_file())

    def test_wait_timeout_retains_member_state_and_log_tail(self):
        with self.fixture() as case:
            (case.store.runtime / "supervisor.log").write_text("fixture diagnostic marker")
            with self.assertRaises(AssertionError) as error:
                case.wait(lambda: False, timeout=.01)
            self.assertIn("fixture diagnostic marker", str(error.exception))
            self.assertIn('"members"', str(error.exception))
            self.assertIn("CLAUDE_01", str(error.exception))

    def test_cli_timeout_retains_partial_output_and_room_state(self):
        with self.fixture() as case:
            timeout = subprocess.TimeoutExpired(["fixture"], 35,
                output=b"partial stdout marker", stderr=b"partial stderr marker")
            real_run = runtime.subprocess.run
            def run(command, **kwargs):
                if command[:2] == [sys.executable, str(runtime.CLI)]:
                    raise timeout
                return real_run(command, **kwargs)
            with patch.object(runtime.subprocess, "run", side_effect=run):
                with self.assertRaises(AssertionError) as error:
                    case.call("status")
            self.assertIn("partial stdout marker", str(error.exception))
            self.assertIn("partial stderr marker", str(error.exception))
            self.assertIn('"members"', str(error.exception))

    def test_smoke_total_deadline_retains_output_and_runs_cleanup(self):
        with self.fixture() as case:
            source = case.root / "stalling_smoke.py"
            source.write_text("import time\nprint('partial smoke output marker', flush=True)\ntime.sleep(30)\n")
            with self.assertRaises(AssertionError) as error:
                case.review_smoke_fixture(acknowledge=True, source_path=source, total_timeout=.25)
            self.assertIn("partial smoke output marker", str(error.exception))
            self.assertIn("Smoke fixture did not finish", str(error.exception))
            self.assertIn("stdout=", str(error.exception))
            self.assertIn("stderr=", str(error.exception))
            self.assertIn("diagnostics=", str(error.exception))

    def test_startup_budget_includes_claude_process_registry_and_registry_commands(self):
        with self.fixture() as case:
            with patch.object(case, "call"), patch.object(case, "wait") as wait:
                case.start()
            # Four-member Claude-hosted fixture: two Codex members each need
            # initialize + thread/start; Claude needs independent process and
            # liveness budgets plus registry calls and process inspection.
            self.assertGreaterEqual(wait.call_args.kwargs.get("timeout", 15), 183)

    def test_real_claude_boundary_allows_separate_process_and_registry_phases(self):
        with self.fixture() as case:
            fake = case.root / "bin/claude"
            fake.unlink()
            fake.write_text("#!" + sys.executable + "\n"
                "import os, pathlib, sys, time\n"
                "marker = pathlib.Path(os.environ['FAKE_NATIVE_ROOT']) / 'hide-until'\n"
                "if '--bg' in sys.argv:\n"
                "    time.sleep(3); marker.write_text(str(time.time()+3))\n"
                "if sys.argv[1:2] == ['agents'] and marker.exists() and time.time() < float(marker.read_text()):\n"
                "    print('[]'); raise SystemExit(0)\n"
                "fixture = pathlib.Path(" + repr(str(runtime.FIXTURE.resolve())) + ")\n"
                "exec(compile(fixture.read_text(), str(fixture), 'exec'), {'__name__':'__main__','__file__':str(fixture)})\n")
            fake.chmod(0o755)
            def cleanup_extra_session():
                sessions = [p.name.removesuffix('.agent.json') for p in (case.root / 'native').glob('*.agent.json')
                            if p.name.removesuffix('.agent.json') != case.session]
                for session in sessions:
                    case.cleanup_fake_registry_sessions([session])
            case.addCleanup(cleanup_extra_session)
            launched = None
            with patch.dict(os.environ, case.env):
                began = time.monotonic()
                try:
                    launched = asyncio.run(native.start_claude(case.project, None, False, case.env,
                        case.store.runtime / 'separate-phase.log', member='CLAUDE_EXPERT', timeout=5))
                    self.assertGreater(time.monotonic() - began, 5)
                finally:
                    if launched:
                        native.stop_claude(case.project, launched['sessionId'])
                        case.wait(lambda: not runtime.process_alive(launched['pid'], runtime.process_stamp(launched['pid'])))

    def test_cli_timeout_survives_state_and_log_collection_errors(self):
        with self.fixture() as case:
            log = case.store.runtime / 'supervisor.log'
            log.write_text('diagnostic fixture')
            real_read = Path.read_text
            real_run = runtime.subprocess.run
            def read(path, *args, **kwargs):
                if path == log:
                    raise OSError('log disappeared')
                return real_read(path, *args, **kwargs)
            def run(command, **kwargs):
                if command[:2] == [sys.executable, str(runtime.CLI)]:
                    raise subprocess.TimeoutExpired(command, 35, output=b'primary output', stderr=b'primary error')
                return real_run(command, **kwargs)
            with patch.object(case.store, 'status', side_effect=RuntimeError('state unavailable')), \
                 patch.object(Path, 'read_text', new=read), patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(AssertionError) as error:
                    case.call('status')
            message = str(error.exception)
            for expected in ('Fixture CLI timed out', 'primary output', 'primary error', 'state unavailable', 'log disappeared'):
                self.assertIn(expected, message)

    def register_smoke_cleanup(self, case, project):
        def cleanup():
            store = runtime.Store(project)
            errors = []
            try:
                if not store.exists() or runtime.fixture_processes_stopped(store):
                    return
            except Exception as exc:
                errors.append(f'initial stopped-state observation: {type(exc).__name__}: {exc}')
            session = None
            try:
                session = (store.room().get('owner') or {}).get('session')
            except Exception as exc:
                errors.append(f'owner lookup: {type(exc).__name__}: {exc}')
            if not session:
                self.fail('test-owned smoke has no cleanup session; ' + '; '.join(errors))
            env = dict(case.env, IHAV_AGENT_ROOM_SESSION_ID=session)
            with patch.dict(os.environ, env):
                try:
                    result = subprocess.run([sys.executable, str(runtime.CLI), '--project', str(project), 'stop'],
                        env=env, capture_output=True, timeout=35)
                    if result.returncode:
                        errors.append(f'room stop exit={result.returncode}; {result.stdout!r}; {result.stderr!r}')
                except Exception as exc:
                    errors.append(f'room stop: {type(exc).__name__}: {exc}')
                try:
                    native.stop_claude(project, session)
                except Exception as exc:
                    errors.append(f'native stop: {type(exc).__name__}: {exc}')
                try:
                    case.wait(lambda: runtime.fixture_processes_stopped(store))
                except Exception as exc:
                    errors.append(f'owned process observation: {type(exc).__name__}: {exc}')
            if errors:
                self.fail('Registered smoke cleanup: ' + '; '.join(errors))
        case.addCleanup(cleanup)
        return cleanup

    def test_smoke_timeout_survives_state_and_stop_errors_and_stops_owned_workers(self):
        with self.fixture() as case:
            project = (case.root / 'smoke-with-ack').resolve()
            self.register_smoke_cleanup(case, project)
            source = case.root / 'initialized_stalling_smoke.py'
            ready = case.root / 'initialized-timeout-ready'
            script = (runtime.PLUGIN_ROOT / 'scripts/native_smoke.py').read_text()
            anchor = '        wait(lambda: store.room()["status"] == "running", "default native startup")'
            self.assertEqual(script.count(anchor), 1)
            source.write_text(script.replace(anchor,
                "        print('initialized smoke output marker', flush=True)\n"
                + "        Path(" + repr(str(ready)) + ").touch()\n        time.sleep(30)\n" + anchor))
            real_status = runtime.Store.status
            real_run = runtime.subprocess.run
            cleanup_commands = []
            def status(store, **kwargs):
                if store.project == project:
                    raise RuntimeError('smoke state unavailable')
                return real_status(store, **kwargs)
            def run(command, **kwargs):
                if '--project' in command and Path(command[command.index('--project') + 1]).resolve() == project and command[-1] == 'stop':
                    cleanup_commands.append('room stop timed out')
                    raise subprocess.TimeoutExpired(command, 15)
                if command[:2] == ['claude', 'stop']:
                    cleanup_commands.append('native stop attempted')
                return real_run(command, **kwargs)
            with patch.object(runtime.Store, 'status', new=status), patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(AssertionError) as error:
                    case.review_smoke_fixture(acknowledge=True, source_path=source, total_timeout=5,
                                             timeout_ready_marker=ready)
            message = str(error.exception)
            self.assertIn('Smoke fixture did not finish', message)
            self.assertIn('initialized smoke output marker', message)
            self.assertIn('fault_timeout_armed=True', message)
            self.assertIn('smoke state unavailable', message)
            self.assertIn('Smoke backup cleanup failed', '\n'.join(error.exception.__notes__))
            self.assertEqual(cleanup_commands, ['room stop timed out', 'native stop attempted'])
            store = runtime.Store(project)
            case.wait(lambda: runtime.fixture_processes_stopped(store))

    def test_smoke_timeout_retains_nonzero_cleanup_results_and_attempts_both_commands(self):
        with self.fixture() as case:
            project = (case.root / 'smoke-with-ack').resolve()
            cleanup = self.register_smoke_cleanup(case, project)
            source = case.root / 'nonzero_cleanup_smoke.py'
            ready = case.root / 'nonzero-timeout-ready'
            script = (runtime.PLUGIN_ROOT / 'scripts/native_smoke.py').read_text()
            anchor = '        wait(lambda: store.room()["status"] == "running", "default native startup")'
            self.assertEqual(script.count(anchor), 1)
            source.write_text(script.replace(anchor,
                "        print('nonzero cleanup output marker', flush=True)\n"
                + "        Path(" + repr(str(ready)) + ").touch()\n        time.sleep(30)\n" + anchor))
            real_run = runtime.subprocess.run
            attempts = []
            def run(command, **kwargs):
                if '--project' in command and Path(command[command.index('--project')+1]).resolve() == project and command[-1] == 'stop':
                    attempts.append('room stop')
                    return subprocess.CompletedProcess(command, 17, b'room stop stdout', b'room stop refusal')
                if command[:2] == ['claude', 'stop']:
                    attempts.append('native stop')
                    return subprocess.CompletedProcess(command, 23, b'native stop stdout', b'native stop failure')
                return real_run(command, **kwargs)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(AssertionError) as error:
                    case.review_smoke_fixture(acknowledge=True, source_path=source, total_timeout=5,
                                             timeout_ready_marker=ready)
            self.assertIn('Smoke fixture did not finish', str(error.exception))
            self.assertIn('nonzero cleanup output marker', str(error.exception))
            self.assertIn('fault_timeout_armed=True', str(error.exception))
            self.assertEqual(attempts, ['room stop', 'native stop'])
            notes = '\n'.join(error.exception.__notes__)
            for expected in ('exit=17', 'room stop refusal', 'exit=23', 'native stop failure'):
                self.assertIn(expected, notes)

            # Exercise the registered finalizer's own refusal path, with the
            # real native stop still allowed to terminate the fixture gateway.
            actual_stop = native.stop_claude
            def refuse_room_stop(command, **kwargs):
                if '--project' in command and Path(command[command.index('--project')+1]).resolve() == project and command[-1] == 'stop':
                    return subprocess.CompletedProcess(command, 29, b'finalizer stdout', b'finalizer refusal')
                return real_run(command, **kwargs)
            with patch.object(runtime.subprocess, 'run', side_effect=refuse_room_stop), \
                 patch.object(native, 'stop_claude', wraps=actual_stop) as stop:
                with self.assertRaisesRegex(AssertionError, 'Registered smoke cleanup:.*exit=29'):
                    cleanup()
                stop.assert_called_once()
            self.assertTrue(runtime.fixture_processes_stopped(runtime.Store(project)))

    def test_registered_finalizer_attempts_native_stop_after_state_observation_failure(self):
        with self.fixture() as case:
            case.start()
            project = case.project.resolve()
            cleanup = self.register_smoke_cleanup(case, project)
            real_status = runtime.Store.status
            real_stop = native.stop_claude
            def status(store, **kwargs):
                if store.project == project:
                    raise RuntimeError('initial state unavailable')
                return real_status(store, **kwargs)
            with patch.object(runtime.Store, 'status', new=status), \
                 patch.object(case, 'wait', side_effect=RuntimeError('final state unavailable')), \
                 patch.object(native, 'stop_claude', wraps=real_stop) as stop:
                with self.assertRaises(AssertionError) as error:
                    cleanup()
                stop.assert_called_once()
            self.assertIn('initial state unavailable', str(error.exception))
            self.assertIn('final state unavailable', str(error.exception))
            case.wait(lambda: runtime.fixture_processes_stopped(runtime.Store(project)))
