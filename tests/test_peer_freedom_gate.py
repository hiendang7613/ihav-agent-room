"""PREREG gate O5: task-free peer discussion stays possible, with no mandatory format, rounds or ACK-only reply.

This checks that the ledger and delivery text still allow free discussion; it cannot show that members
actually discuss. Measuring real discussion quality belongs to the blinded B1/B2 gates.
"""

import itertools
import unittest

from ihav_agent_room.common import MODES
from ihav_agent_room.native import COLLABORATION_GUIDANCE, message_text, role_instructions
from test_evidence import EvidenceFixture

FREE_CLAUSES = ("Discussion needs no task or format", "Roles do not limit contributions", "no debate/self-improvement quota")


class PeerFreedomGateTests(EvidenceFixture, unittest.TestCase):
    def test_any_two_members_can_exchange_a_task_free_message_in_free_form(self):
        for sender, recipient in itertools.permutations(MODES["full"], 2):
            with self.subTest(sender=sender, recipient=recipient):
                row = self.store.send(sender, recipient, "What if we tried the opposite? Half-formed idea: no schema yet.")
                self.assertIsNone(row["task"])
                self.assertEqual(row["status"], "queued")
                self.assertEqual(row["context"], "{}")

    def test_task_free_delivery_names_a_free_reply_channel_and_demands_no_format(self):
        row = self.store.send("CODEX_EXPERT", "CLAUDE_01", "A tentative thought, not a finding.")
        text = message_text(row)
        self.assertNotIn("Task:", text, "Task-free discussion uses the shared role guidance")
        self.assertIn("A tentative thought, not a finding.", text)
        self.assertIn("ihav-agent-room send --to CODEX_EXPERT", text)
        self.assertIn("final isn't forwarded", text)  # Why a reply goes through send.
        self.assertIn("ihav-agent-room ack", COLLABORATION_GUIDANCE)  # A processing record, never a required reply message.
        for forbidden in ("required format", "template", "must reply", "round limit", "fixed rounds"):
            self.assertNotIn(forbidden, text.casefold())

    def test_startup_guidance_keeps_the_free_discussion_clauses_for_every_member_kind(self):
        for clause in FREE_CLAUSES:
            with self.subTest(clause=clause):
                self.assertIn(clause, COLLABORATION_GUIDANCE)
                self.assertIn(clause, role_instructions("CODEX_EXPERT"))
                self.assertIn(clause, role_instructions("CLAUDE_EXPERT"))

    def test_admin_reply_shape_is_the_admin_chosen_one_and_preserves_evidence(self):
        """The admin chose the reply shape on 2026-10-02/03 (i-have-asd-ste100 0.11.0: three zones, eight sections)."""
        for clause in ("**Agents-Zone** (timed steps)", "**Result-Zone**", "**Admin-Zone**",
                       "key-first bullets", "bold Conclusion", "all eight",
                       "0. Done 1. InProgress 2. Pending 3. Questions 4. Todos 5. Backlog 6. Risks 7. AIIdeas",
                       "Preserve facts, conditions, uncertainty, authority and evidence"):
            with self.subTest(clause=clause):
                self.assertIn(clause, COLLABORATION_GUIDANCE)
        self.assertNotIn("no forced headings/footer", COLLABORATION_GUIDANCE)


if __name__ == "__main__":
    unittest.main()
