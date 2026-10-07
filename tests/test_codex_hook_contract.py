"""Check the actual native wire contract, not just that stdout parses as JSON."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.hooks import context, handle


SCHEMAS = json.loads((Path(__file__).parent / "fixtures/codex-hook-output-0.160.0.json").read_text())["schemas"]


class CodexHookContractTests(unittest.TestCase):
    def assert_wire(self, event, output):
        schema = SCHEMAS[event]
        self.assertLessEqual(set(output), set(schema["properties"]))
        if "hookSpecificOutput" in output:
            specific = output["hookSpecificOutput"]
            definition = schema["definitions"][event + "HookSpecificOutputWire"]
            self.assertLessEqual(set(specific), set(definition["properties"]))
            self.assertEqual(specific["hookEventName"], event)
            self.assertIsInstance(specific["additionalContext"], str)
        if output.get("decision") == "block":
            self.assertIsInstance(output["reason"], str)
            self.assertTrue(output["reason"])

    def test_codex_session_start_never_emits_reload_skills_or_installs_a_claude_alias(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CODEX_THREAD_ID": "host-thread"}, clear=True), \
                patch("ihav_agent_room.scaffold.install_alias") as install:
            result = handle({"hook_event_name": "SessionStart", "session_id": "host-thread", "cwd": directory})
        self.assert_wire("SessionStart", result)
        self.assertNotIn("reloadSkills", result["hookSpecificOutput"])
        install.assert_not_called()

    def test_codex_context_and_stop_use_event_specific_native_fields(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "host-thread"}, clear=True):
            for event in SCHEMAS:
                result = context(event, "Account for receipt P-example.", reloadSkills=True)
                self.assert_wire(event, result)
            self.assertEqual(context("Stop", "Account for receipt P-example."),
                             {"decision": "block", "reason": "Account for receipt P-example."})

    def test_claude_reload_skills_behavior_is_preserved(self):
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": "claude"}, clear=True):
            self.assertTrue(context("SessionStart", "Context", reloadSkills=True)["hookSpecificOutput"]["reloadSkills"])
