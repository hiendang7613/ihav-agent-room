"""Roster aliases, requested model settings and the host-managed gateway contract."""

import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from ihav_agent_room.cli import parser, run
from ihav_agent_room.common import GATEWAY, MEMBERS, MODES, RoomError, acting_member, canonical_member
from ihav_agent_room.roster import ALIASES, DEFAULT_MEMBERS, LAUNCHED_CLAUDE, ROSTER, launch_config
from test_evidence import EvidenceFixture

os.environ.pop("CLAUDE_EFFORT", None)  # Hermetic: the host session effort must not leak into room state.


class RosterTests(EvidenceFixture, unittest.TestCase):
    def test_default_and_full_modes_both_include_all_four_members(self):
        self.assertEqual(MODES["default"], ("CLAUDE_01", "CODEX_01", "CLAUDE_EXPERT", "CODEX_EXPERT"))
        self.assertEqual(MODES["full"], ("CLAUDE_01", "CODEX_01", "CLAUDE_EXPERT", "CODEX_EXPERT"))
        self.assertEqual((MEMBERS, DEFAULT_MEMBERS, GATEWAY, LAUNCHED_CLAUDE), (MODES["full"], MODES["default"], "CLAUDE_01", "CLAUDE_EXPERT"))

    def test_the_roster_is_consistent_data(self):
        names = [member["name"] for member in ROSTER]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(sum(member["gateway"] for member in ROSTER), 1)
        self.assertEqual({member["host"] for member in ROSTER}, {"claude", "codex"})
        self.assertEqual(sorted(member["role"] for member in ROSTER), ["expert", "expert", "worker", "worker"])
        self.assertEqual(ALIASES, {"CLAUDE_WORKER": "CLAUDE_01", "CODEX_WORKER": "CODEX_01"})
        self.assertFalse(set(ALIASES) & set(names))  # An alias never shadows an id.

    def test_model_effort_defaults_are_roster_data_and_gateway_remains_host_managed(self):
        self.assertEqual({member["name"]: (member["label"], member["model"], member["effort"])
                          for member in ROSTER},
                         {"CLAUDE_01": ("Sonnet 5.5", "sonnet", "xhigh"),
                          "CODEX_01": ("Luna 6", "gpt-6-luna", "xhigh"),
                          "CLAUDE_EXPERT": ("Opus 5.5", "opus", "xhigh"),
                          "CODEX_EXPERT": ("Sol 6.1", "gpt-6.1-sol", "xhigh")})
        self.assertIsNone(launch_config("CLAUDE_01"))
        self.assertEqual(launch_config("CODEX_WORKER"), {"model": "gpt-6-luna", "effort": "xhigh"})
        self.assertEqual(launch_config("CLAUDE_EXPERT"), {"model": "opus", "effort": "xhigh"})
        members = {member["name"]: member for member in self.store.status()["members"]}
        self.assertEqual(members["CODEX_EXPERT"]["requested_model"], "gpt-6.1-sol")
        self.assertEqual(members["CODEX_EXPERT"]["requested_effort"], "xhigh")
        self.assertIsNone(members["CODEX_EXPERT"]["observed_model"])
        self.assertEqual(members["CODEX_EXPERT"]["settings_application"], "configured; not started")

    def test_legacy_member_rows_get_requested_defaults_without_claiming_application(self):
        self.store.member("CODEX_01", {"native_id": "legacy-thread"})
        with self.store.tx() as db:
            row = db.execute("SELECT data FROM members WHERE name=?", ("CODEX_01",)).fetchone()
            member = json.loads(row[0])
            for key in ("requested_model", "requested_effort", "model_label", "settings_application",
                        "observed_model", "observed_effort", "model_observed_at"):
                member.pop(key, None)
            db.execute("UPDATE members SET data=? WHERE name=?", (json.dumps(member), "CODEX_01"))
        status = {member["name"]: member for member in self.store.status()["members"]}["CODEX_01"]
        self.assertEqual(status["requested_model"], "gpt-6-luna")
        self.assertEqual(status["requested_effort"], "xhigh")
        self.assertIsNone(status["observed_model"])
        self.assertEqual(status["settings_application"], "existing session; settings application unknown")

    def test_aliases_work_at_the_cli_and_in_the_environment(self):
        args = parser().parse_args(["--project", str(self.project), "send", "--to", "CODEX_WORKER", "--body", "hello"])
        self.assertEqual(args.to, "CODEX_01")
        args = parser().parse_args(["--project", str(self.project), "task", "list", "--owner", "CLAUDE_WORKER"])
        self.assertEqual(args.owner, "CLAUDE_01")
        self.assertEqual(canonical_member("CLAUDE_01"), "CLAUDE_01")
        self.assertEqual(canonical_member("nobody"), "nobody")
        for given, expected in (("CLAUDE_WORKER", "CLAUDE_01"), ("CODEX_WORKER", "CODEX_01"), ("CODEX_EXPERT", "CODEX_EXPERT")):
            with self.subTest(env=given), patch.dict(os.environ, IHAV_AGENT_ROOM_MEMBER=given):
                self.assertEqual(acting_member(), expected)  # A non-gateway member cannot be mistaken for the default.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IHAV_AGENT_ROOM_MEMBER", None)
            self.assertEqual(acting_member(), GATEWAY)

    def test_an_alias_acts_as_the_gateway_and_unknown_names_are_still_refused(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "main"}
            self.store.put_room(db, room)
        with patch.dict(os.environ, IHAV_AGENT_ROOM_MEMBER="CLAUDE_WORKER", IHAV_AGENT_ROOM_SESSION_ID="main"):
            self.assertEqual(self.store.actor(), "CLAUDE_01")
            self.assertEqual(run(parser().parse_args(["--project", str(self.project), "task", "list"])), [])
        with patch.dict(os.environ, IHAV_AGENT_ROOM_MEMBER="NOBODY", IHAV_AGENT_ROOM_SESSION_ID="main"):
            with self.assertRaises(RoomError) as caught:
                self.store.actor()
            self.assertEqual(caught.exception.code, "identity")
        with self.assertRaises(SystemExit):
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                parser().parse_args(["--project", str(self.project), "send", "--to", "NOBODY", "--body", "x"])

    def test_only_the_gateway_records_admin_intent(self):
        for member in MEMBERS:
            with self.subTest(member=member):
                if member == GATEWAY:
                    self.store.main_only(member)
                else:
                    with self.assertRaises(RoomError) as caught:
                        self.store.main_only(member)
                    self.assertEqual(caught.exception.code, "authority")
                    self.assertIn(GATEWAY, str(caught.exception))

    def test_no_module_but_the_roster_spells_a_member_id_as_a_literal(self):
        """Ratchet against hardcoding: code uses GATEWAY, MEMBERS and the roster, never a quoted member id."""
        pattern = re.compile(r"""["'](CLAUDE_01|CODEX_01|CLAUDE_EXPERT|CODEX_EXPERT)["']""")
        offenders = []
        for path in sorted((Path(__file__).resolve().parents[1] / "ihav_agent_room").glob("*.py")):
            if path.name == "roster.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
