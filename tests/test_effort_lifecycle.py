"""Gateway observations through hooks, with no native lookup, launch or provider."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import GATEWAY
from ihav_agent_room.hooks import handle
from ihav_agent_room.store import Store


class EffortHookLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='room-effort-hooks-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = Store(self.project)
        self.store.initialize('advisors')
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room['owner'] = {'session': 'fake-main'}
            self.store.put_room(db, room)
        self.store.set_effort('low', member='CODEX_WORKER')
        for patcher in (
            patch.dict(os.environ, {'IHAV_AGENT_ROOM_MEMBER': GATEWAY, 'IHAV_AGENT_ROOM_SKIP_ALIAS': '1',
                                    'CLAUDE_ENV_FILE': '', 'CLAUDE_EFFORT': ''}),
            patch('ihav_agent_room.hooks.bind_main'),
            patch('ihav_agent_room.hooks.start_room', return_value={'reason': 'fake, no launch'}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def hook(self, event, effort=None):
        payload = {'hook_event_name': event, 'cwd': str(self.project), 'session_id': 'fake-main',
                   'prompt': 'Analyze this approach'}
        if effort is not None:
            payload['effort'] = {'level': effort}
        return handle(payload)

    def test_changed_effort_after_session_start_clears_every_follower_override(self):
        self.hook('SessionStart', 'high')
        self.hook('UserPromptSubmit', 'medium')
        for member in ('CODEX_01', 'CODEX_EXPERT', 'CLAUDE_EXPERT'):
            with self.subTest(member=member):
                self.assertEqual(self.store.member(member)['requested_effort'], 'medium')
                self.assertEqual(self.store.member(member)['effort_source'], 'gateway')
        self.assertEqual(self.store.room()['synced_effort'], 'medium')
        self.assertEqual(self.store.member(GATEWAY)['observed_effort'], 'medium')

    def test_unchanged_effort_after_session_start_keeps_override(self):
        self.hook('SessionStart', 'high')
        self.hook('UserPromptSubmit', 'high')
        self.assertEqual(self.store.member('CODEX_01')['requested_effort'], 'low')
        self.assertEqual(self.store.member('CODEX_01')['effort_source'], 'override')
        self.assertEqual(self.store.member('CODEX_EXPERT')['requested_effort'], 'xhigh')
        self.assertEqual(self.store.room()['synced_effort'], 'high')

    def test_existing_synced_baseline_outranks_resume_observation(self):
        self.store.sync_gateway_effort("high")
        self.hook("SessionStart", "medium")
        self.hook("UserPromptSubmit", "high")
        self.assertEqual(self.store.member("CODEX_01")["requested_effort"], "low")
        self.assertEqual(self.store.member("CODEX_01")["effort_source"], "override")
        self.assertEqual(self.store.room()["synced_effort"], "high")

    def assert_first_prompt_is_baseline(self, effort):
        self.hook("SessionStart", effort)
        self.hook("UserPromptSubmit", "high")
        self.assertEqual(self.store.member("CODEX_01")["requested_effort"], "low")
        self.assertEqual(self.store.member("CODEX_01")["effort_source"], "override")
        self.assertEqual(self.store.room()["synced_effort"], "high")

    def test_missing_start_effort_keeps_first_prompt_as_baseline(self):
        self.assert_first_prompt_is_baseline(None)

    def test_invalid_start_effort_keeps_first_prompt_as_baseline(self):
        self.assert_first_prompt_is_baseline("unknown")


if __name__ == '__main__':
    unittest.main()
