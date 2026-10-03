"""PREREG gate O3: model-facing context per event class stays within a fixed byte budget.

Budgets are the sizes measured on 2026-09-30 (goal v14 campaign, plugin 0.3.20 plus fixes) plus 10%,
rounded up to 50 bytes, fixed before this check existed. A larger message fails here. To grow one, raise
its budget in the same change with a dated comment that says what the extra bytes buy; shrinking needs no
edit. Byte size is context cost only: it is not evidence of fewer model tokens, lower cost or latency.
"""

import json
import os
import unittest
from unittest.mock import patch

from ihav_agent_room import hooks
from ihav_agent_room.native import COLLABORATION_GUIDANCE, message_text, role_instructions
from ihav_agent_room.store import MAX_MESSAGE_CHARS, MAX_MESSAGE_ID_BYTES, MAX_REVIEW_PACKET_BYTES
from test_evidence import EvidenceFixture

os.environ.pop("CLAUDE_EFFORT", None)  # Hermetic: the host session effort must not leak into room state.

BUDGETS = {
    # 2026-10-03: +70 bytes buy the admin's three reply zones in the guidance line (also in worker instructions).
    "guidance": 1170,
    "worker_role_instructions": 1210,
    "delivery_taskless_overhead": 450,
    "delivery_taskless_recovery_overhead": 650,
    "delivery_task_overhead_excluding_pack": 550,
    "task_pack_compact": 750,
    "review_delivery_overhead_excluding_pack": 550,
    "review_packet_compact": MAX_REVIEW_PACKET_BYTES,
    # 2026-10-01: the 1,500-byte packet cap keeps three paths/eight bounded claims; FYI copies carry no packet.
    "review_delivery_total": 2050,
    "review_fyi_total": 1300,
    # 2026-10-03: +40 bytes buy the ihav-agent-room command name in status next steps.
    "status_compact_one_task": 2740,
    # 2026-10-02: +20 bytes buy the admin's eight-section reply shape (6. Risks, 7. AIIdeas) in the guidance line.
    # 2026-10-03: +90 bytes buy the admin's three reply zones (Agents-Zone, Result-Zone, Admin-Zone).
    "main_sessionstart_context": 1210,
    "admin_prompt_context": 300,
    # DEC-020 (2026-10-01): cap wrapper bytes using the longest generated notice ID and max receipt ID.
    "admin_notice_delivery_overhead": 550,
    "native_event_context": 150,
}


def size(text):
    return len(text.encode("utf-8"))


class ContextBudgetTests(EvidenceFixture, unittest.TestCase):
    def measurements(self):
        body = "What evidence changes your view about the retry behavior?"
        task = self.task(review_policy="none", reviewer=None)
        # Longest legal addressed IDs too: the delivered header repeats the ID, so the cap must hold for the cross
        # product of the longest ID and the longest digest, not only for generated IDs.
        taskless = self.store.send("CODEX_EXPERT", "CLAUDE_01", body, message_id="M-" + "d" * (MAX_MESSAGE_ID_BYTES - 2))
        tasked = self.store.send("CLAUDE_01", "CODEX_EXPERT", body, task["id"], message_id="M-" + "e" * (MAX_MESSAGE_ID_BYTES - 2))
        self.assertEqual((len(taskless["id"]), len(tasked["id"])), (MAX_MESSAGE_ID_BYTES, MAX_MESSAGE_ID_BYTES))
        pack = self.store.task_context(task["id"], compact=True)
        pack_text = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
        compact_status_size = size(json.dumps(self.store.status(compact=True), ensure_ascii=False, separators=(",", ":")))
        review_paths = ["work.py", "src/worker.py", "tests/test_worker.py"]
        (self.project / "src").mkdir()
        (self.project / "tests").mkdir()
        (self.project / "src/worker.py").write_text("value = 1\n")
        (self.project / "tests/test_worker.py").write_text("assert 1 == 1\n")
        review_task = self.task(scope=review_paths,
                                acceptance="The worker returns the expected value and tests cover boundary cases." * 2)
        review_task = self.current(review_task)
        review_evidence = [f"Evidence {index}: " + ("verified output and boundary behavior; " * 7)
                           for index in range(8)]
        review_submission = self.store.submit_task(review_task["owner"], review_task["id"], review_task["version"], {
            "paths": review_paths, "evidence": review_evidence, "summary": "Ready for independent review"})["submission"]
        with self.store.read() as db:
            review_messages = [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE task=? ORDER BY seq", (review_task["id"],))]
        review_direct = next(message for message in review_messages
                             if message["recipient"] == "CODEX_EXPERT" and
                             not json.loads(message["context"]).get("broadcast"))
        review_fyi = next(message for message in review_messages if json.loads(message["context"]).get("broadcast"))
        review_pack = self.store.review_packet(review_task["id"], review_submission["id"])
        review_pack_text = json.dumps(review_pack, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(review_pack["status"], "current")
        self.assertEqual(review_pack["submission"]["evidence_count"], 8)
        self.assertEqual(len(review_pack["submission"]["paths"]), 3)
        self.assertLessEqual(size(review_pack_text), MAX_REVIEW_PACKET_BYTES)
        # Exercise the longest legal event ID alongside the largest review packet.
        review_direct["id"] = "M-" + "r" * (MAX_MESSAGE_ID_BYTES - 2)
        review_direct_text = message_text(review_direct | {"context_pack": review_pack})
        review_fyi_text = message_text(review_fyi | {"context_pack": review_pack})
        self.assertNotIn(review_pack["digest"], review_fyi_text,
                         "The reviewer packet belongs only in the assigned reviewer's delivery")
        self.assertNotIn("Source-bound review packet", review_fyi_text)
        self.store.broadcast_gateway_prompt("x", "budget-admin-notice", receipt_id="P-" + "r" * 62,
                                            provenance_state="manual_recovery")
        with self.store.read() as db:
            admin_notice = next(dict(row) for row in db.execute("SELECT * FROM messages ORDER BY seq DESC")
                                if json.loads(row["context"]).get("admin_notice"))
        self.assertRegex(admin_notice["id"], r"M-[0-9a-f]{36}",
                         "Admin notice IDs are generated from a fixed 36-hex digest")
        admin_notice_text = message_text(admin_notice)
        self.assertLessEqual(len(admin_notice["body"]), MAX_MESSAGE_CHARS)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["owner"] = {"session": "main"}
            self.store.put_room(db, room)
        found = {
            "guidance": size(COLLABORATION_GUIDANCE),
            "worker_role_instructions": size(role_instructions("CODEX_EXPERT")),
            "delivery_taskless_overhead": size(message_text(taskless)) - size(body),
            "delivery_taskless_recovery_overhead": size(message_text(taskless | {"pending_recovery": True})) - size(body),
            "delivery_task_overhead_excluding_pack": size(message_text(tasked | {"context_pack": pack})) - size(body) - size(pack_text),
            "task_pack_compact": size(pack_text),
            "review_delivery_overhead_excluding_pack": size(review_direct_text) - size(review_direct["body"]) - size(review_pack_text),
            "review_packet_compact": size(review_pack_text),
            "review_delivery_total": size(review_direct_text),
            "review_fyi_total": size(review_fyi_text),
            "status_compact_one_task": compact_status_size,
            "admin_notice_delivery_overhead": size(admin_notice_text) - size(admin_notice["body"]),
        }
        payload = {"cwd": str(self.project), "session_id": "main"}
        with patch.object(hooks, "bind_main"), patch.object(hooks, "start_room", return_value={"reason": "x"}), \
                patch.object(hooks, "install_alias", return_value=False), \
                patch.dict(os.environ, IHAV_AGENT_ROOM_MEMBER="CLAUDE_01", CLAUDE_ENV_FILE=""):
            def context(**fields):
                return hooks.handle(payload | fields)["hookSpecificOutput"]["additionalContext"]
            found["main_sessionstart_context"] = size(context(hook_event_name="SessionStart", source="startup"))
            found["admin_prompt_context"] = size(context(hook_event_name="UserPromptSubmit", prompt="Do the thing"))
            admin_notice = message_text({
                "id": "M-" + "a" * 36, "sender": "CLAUDE_01", "body": "Notice body",
                "context": {"admin_notice": {"receipt": "P-" + "b" * 36, "provenance": "unverified",
                                               "truncated": False, "original_chars": 11}},
            })
            found["native_event_context"] = max(
                size(context(hook_event_name="UserPromptSubmit",
                             prompt="<task-notification>\n<summary>x</summary>\n</task-notification>")),
                size(context(hook_event_name="UserPromptSubmit", prompt=admin_notice)),
            )
        return found

    def test_every_event_class_stays_within_its_budget(self):
        found = self.measurements()
        self.assertEqual(found.keys(), BUDGETS.keys(), "Every measured class needs a budget and the reverse")
        for name, budget in BUDGETS.items():
            with self.subTest(event=name):
                self.assertLessEqual(found[name], budget, f"{name} is {found[name]} bytes; budget {budget}. "
                                     "Shrink it, or raise the budget with a dated comment saying what the bytes buy.")


if __name__ == "__main__":
    unittest.main()
