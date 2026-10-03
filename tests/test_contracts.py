"""Cross-room contracts (admin request 2026-10-03): rooms ask each other for work without the admin relaying."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.common import RoomError
from ihav_agent_room.contracts import Contracts
from ihav_agent_room.globalspace import GlobalSpace

ATTEST = ["read_named_files_only", "no_paid_cost", "one_turn"]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="contracts ")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.space = GlobalSpace(base / "agents_space")
        for name in ("competitor-search", "counter", "leaderboards"):
            (base / name).mkdir()
            self.space.register(f"room-{name}", base / name, "0.6.0")
        self.private = base / "private" / "client"
        self.private.mkdir(parents=True)
        self.policy({"descriptions_only_prefixes": [str(base / "private")],
                     "self_accept": {"decision": "D-a6d8d8f6", "types": ["analysis", "review", "artifact_handoff"], "max_files": 5}})
        self.space.register("room-private", self.private, "0.6.0")
        self.contracts = Contracts(self.space)

    def policy(self, data):
        (self.space.root / "policy.json").write_text(json.dumps(data))

    def survey(self, **changes):
        args = dict(requester="room-competitor-search", provider="room-counter", kind="artifact_handoff",
                    title="Evaluate the traffic-source survey", request="Does it change the source chain?",
                    acceptance="adopt, defer or reject with reasons", files=["agents_space/reviews/traffic-sources-survey.md"])
        args.update(changes)
        return self.contracts.propose(**args)

    def test_the_survey_handoff_runs_without_the_admin_under_the_standing_policy(self):
        contract = self.survey()
        self.assertEqual(self.contracts.resolve_room("counter"), "room-counter")
        self.assertIn("1 waiting", self.contracts.waiting_summary("room-counter"))
        self.assertIsNone(self.contracts.waiting_summary("room-competitor-search"))
        accepted = self.contracts.act("room-counter", contract["id"], "accept", attest=ATTEST)
        self.assertEqual((accepted["state"], accepted["authority"]["basis"], accepted["authority"]["decision"]),
                         ("accepted", "standing_policy", "D-a6d8d8f6"))
        self.contracts.act("room-counter", contract["id"], "deliver", note="defer: no change to the chain yet")
        self.assertIn("1 waiting", self.contracts.waiting_summary("room-competitor-search"))
        done = self.contracts.act("room-competitor-search", contract["id"], "confirm")
        self.assertEqual((done["state"], done["closed"]), ("confirmed", True))
        self.assertEqual([e["action"] for e in done["history"]], ["propose", "accept", "deliver", "confirm"])
        self.assertEqual(self.contracts.listing("room-counter"), [])

    def test_code_changes_and_larger_reads_need_the_provider_admin(self):
        code = self.survey(provider="room-counter", kind="compatibility_request", requester="room-leaderboards",
                           title="Keep JSON stable", files=[])
        with self.assertRaises(RoomError) as caught:
            self.contracts.act("room-counter", code["id"], "accept", attest=ATTEST)
        self.assertEqual(caught.exception.code, "authority")
        many = self.survey(files=[f"docs/{n}.md" for n in range(6)])
        with self.assertRaises(RoomError):
            self.contracts.act("room-counter", many["id"], "accept", attest=ATTEST)
        with self.assertRaises(RoomError):  # Every attestation is required.
            self.contracts.act("room-counter", self.survey()["id"], "accept", attest=ATTEST[:2])
        accepted = self.contracts.act("room-counter", code["id"], "accept", source="P-provider-admin")
        self.assertEqual(accepted["authority"], {"basis": "provider_admin_receipt", "receipt": "P-provider-admin"})

    def test_without_a_policy_nothing_is_self_accepted(self):
        self.policy({"descriptions_only_prefixes": []})
        with self.assertRaises(RoomError):
            self.contracts.act("room-counter", self.survey()["id"], "accept", attest=ATTEST)
        (self.space.root / "policy.json").write_text("[]")
        self.assertIsNone(self.contracts.self_accept_rule())

    def test_each_side_acts_only_for_itself_and_states_move_in_order(self):
        contract = self.survey()
        for room, action in (("room-competitor-search", "accept"), ("room-leaderboards", "accept"),
                             ("room-counter", "confirm"), ("room-counter", "withdraw")):
            with self.subTest(room=room, action=action), self.assertRaises(RoomError) as caught:
                self.contracts.act(room, contract["id"], action, attest=ATTEST)
            self.assertEqual(caught.exception.code, "authority")
        with self.assertRaises(RoomError):
            self.contracts.act("room-counter", contract["id"], "deliver", note="early")  # Not accepted yet.
        self.contracts.act("room-counter", contract["id"], "accept", attest=ATTEST)
        self.contracts.act("room-counter", contract["id"], "deliver", note="first try")
        with self.assertRaises(RoomError):
            self.contracts.act("room-competitor-search", contract["id"], "reject")  # A reason is required.
        self.contracts.act("room-competitor-search", contract["id"], "reject", note="no reasons given")
        self.contracts.act("room-counter", contract["id"], "deliver", note="defer, because the chain is unchanged")
        self.assertEqual(self.contracts.act("room-competitor-search", contract["id"], "confirm")["state"], "confirmed")
        with self.assertRaises(RoomError):
            self.contracts.act("room-competitor-search", contract["id"], "withdraw")

    def test_inputs_and_privacy(self):
        for files in (["/etc/passwd"], ["../x"], ["a\\b"]):
            with self.subTest(files=files), self.assertRaises(RoomError):
                self.survey(files=files)
        with self.assertRaises(RoomError):
            self.survey(provider="room-competitor-search")  # Not with itself.
        with self.assertRaises(RoomError) as caught:
            self.survey(requester="room-private", files=["notes.md"])
        self.assertEqual(caught.exception.code, "policy")
        plain = self.survey(requester="room-private", files=[], title="Retest please", request="Our output changed",
                            acceptance="tell us if it still parses")
        self.assertEqual(plain["files"], [])
        accepted = self.contracts.act("room-counter", plain["id"], "accept", attest=ATTEST)
        self.assertEqual(accepted["state"], "accepted")
        with self.assertRaises(RoomError):
            self.contracts.resolve_room("nobody")


class ContractCliTests(unittest.TestCase):
    def test_writes_need_the_gateway_and_an_admin_accept_needs_a_human_receipt(self):
        from ihav_agent_room.cli import parser, run
        from ihav_agent_room.scaffold import initialize
        from ihav_agent_room.store import Store
        with tempfile.TemporaryDirectory(prefix="contract cli ") as directory, \
                patch.dict(os.environ, {"IHAV_HOME": str(Path(directory) / "home")}):
            projects = {}
            for name in ("requester", "provider"):
                projects[name] = Path(directory) / name
                projects[name].mkdir()
                initialize(projects[name], "pair")
                GlobalSpace().register(Store(projects[name]).room()["id"], projects[name], "0.6.0")

            def cli(project, *args, member="CLAUDE_01"):
                with patch.dict(os.environ, {"IHAV_AGENT_ROOM_MEMBER": member}):
                    return run(parser().parse_args(["--project", str(projects[project]), *args]))
            contract = cli("requester", "contract", "propose", "--to", "provider", "--type", "review", "--title", "t",
                           "--request", "r", "--acceptance", "a")
            with self.assertRaises(RoomError):
                cli("provider", "contract", "decline", contract["id"], "--reason", "x", member="CODEX_01")
            receipt = Store(projects["provider"]).intake("main", "accept it", provenance={"hook": {"state": "unverified"}})
            with self.assertRaises(RoomError):
                cli("provider", "contract", "accept", contract["id"], "--source", receipt)
            self.assertEqual(cli("provider", "contract", "list", "--waiting")[0]["id"], contract["id"])


if __name__ == "__main__":
    unittest.main()
