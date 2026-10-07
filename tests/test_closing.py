import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from test_continuity import ContinuityTests
from ihav_agent_room.cli import parser, run
from ihav_agent_room.closing import (KEY, FIELD_BYTES, capture, checksum, recover,
                                    readiness, sections, without_host_citation)
from ihav_agent_room.common import RoomError
from ihav_agent_room.continuity import BOOTSTRAP_WINDOW, REPLY_BYTES, TRANSCRIPT_WINDOW, latest_reply
from ihav_agent_room.hooks import handle
from ihav_agent_room.runtime import Supervisor, await_detached_shutdown
from ihav_agent_room.store import Store


CLOSING = """**Admin-Zone**

**Conclusion:** The send remains sent_unknown; awaiting a read-only survey decision.
0. **Goals:**
   - **L1.** Support 11 chatbots; historical estimate 50 percent.
   - **G1.** Save response.md after the first send.
   - **B1.** Gemini; login for nine remaining sites.
1. **Done:**
   - M0-17 committed f893a1d; 577 tests were reported on the old snapshot.
2. **Doing:**
3. **Todos:**
   - Survey the exact saved conversation after its own approval.
4. **Pending:**
   - Eleven local commits await push approval.
5. **Quests:**
   - Q1. Approve: one read-only survey of the named conversation?
   - Q2. Conditional approval requires a preview before any live send.
6. **Risks:**
   - R1. sent_unknown must not trigger another send.
7. **Ideas:**
   - I1. Share findings when the owning room confirms them.
"""

HOST_CITATION = """<oai-mem-citation>
<citation_entries>
MEMORY.md:30-30|note=[Historical recovery requirement]
</citation_entries>
<rollout_ids>
01a10a41-cbe7-7ae1-b20c-4be83c992edb
</rollout_ids>
</oai-mem-citation>
"""


class ClosingTests(unittest.TestCase):
    setUp = ContinuityTests.setUp
    claude_transcript = ContinuityTests.claude_transcript
    codex_transcript = ContinuityTests.codex_transcript

    def test_terminal_host_citation_is_not_a_project_section(self):
        for closing in (CLOSING, CLOSING.replace("   - I1. Share findings when the owning room confirms them.\n", "")):
            with self.subTest(empty_ideas="I1." not in closing):
                self.assertEqual(sections(closing + "\n" + HOST_CITATION), sections(closing))

    def test_capture_preserves_project_text_between_separate_host_citations(self):
        cases = (("Ideas", "   - I2. Keep this real project idea.\n"),
                 ("Quests", "5. **Quests:**\n   - Q3. Do not send without a preview and approval.\n"))
        for field, content in cases:
            with self.subTest(field=field):
                text = CLOSING + "\n" + HOST_CITATION + content + HOST_CITATION
                path = self.codex_transcript("old-codex", text)
                before = path.read_bytes()
                self.assertTrue(capture(self.store)["saved"])
                state = recover(self.store, full=True)
                expected = ("I2. Keep this real project idea." if field == "Ideas"
                            else "Q3. Do not send without a preview and approval.")
                self.assertIn(expected, state["fields"][field]["text"])
                self.assertEqual(state["fields"][field]["reply_digest"], checksum(text))
                self.assertEqual(path.read_bytes(), before)

    def test_citation_before_own_zone_cannot_hide_the_current_closing(self):
        text = HOST_CITATION + "\n" + CLOSING + HOST_CITATION
        path = self.codex_transcript("old-codex", text)
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        state = recover(self.store, full=True)
        self.assertIn("L1.** Support 11 chatbots", state["fields"]["Goals"]["text"])
        self.assertIn("Q2. Conditional approval requires a preview before any live send.",
                      state["fields"]["Quests"]["text"])
        self.assertEqual(state["fields"]["Goals"]["reply_digest"], checksum(text))
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_view_preserves_real_text_between_host_citations(self):
        body = HOST_CITATION + "I2. Keep this real project idea.\n" + HOST_CITATION
        text = CLOSING + "\n" + body
        path = self.codex_transcript("old-codex", text)
        original_parser = sections
        with patch("ihav_agent_room.closing.sections",
                   side_effect=lambda reply: original_parser(reply) | {"Ideas": body}):
            self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            before = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        self.assertEqual(state["fields"]["Ideas"]["text"],
                         HOST_CITATION + "I2. Keep this real project idea.")
        self.assertEqual(state["fields"]["Ideas"]["reply_digest"], checksum(text))
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], before)
        self.assertEqual(latest_reply(path, self.project, "codex", "old-codex")["text"], text)

    def test_host_citation_removal_preserves_native_text_and_reply_provenance(self):
        text = CLOSING + "\n" + HOST_CITATION
        path = self.codex_transcript("old-codex", text)
        before = path.read_bytes()
        reply = latest_reply(path, self.project, "codex", "old-codex")
        self.assertEqual(reply["text"], text)
        self.assertTrue(capture(self.store)["saved"])
        state = recover(self.store)
        self.assertEqual(state["fields"]["Ideas"]["text"], sections(CLOSING)["Ideas"])
        self.assertEqual(state["fields"]["Ideas"]["reply_digest"], checksum(text))
        self.assertEqual(path.read_bytes(), before)

    def test_citation_metadata_cannot_select_another_admin_zone(self):
        trailer = HOST_CITATION.replace("MEMORY.md:30-30|note=[Historical recovery requirement]",
                                        "**Admin-Zone**\n0. **Goals:**\n - L1. Metadata, not a project aim.")
        self.assertEqual(sections(CLOSING + "\n" + trailer), sections(CLOSING))

    def structural_citation(self, payload):
        return HOST_CITATION.replace("MEMORY.md:30-30|note=[Historical recovery requirement]", payload)

    def test_structural_citation_zone_does_not_select_metadata(self):
        citation = self.structural_citation("**Admin-Zone**\n0. **Goals:**\n   - L1. Metadata, not the project aim.")
        fields = sections(CLOSING + "\n" + citation + "I2. Keep this real project idea.\n" + HOST_CITATION)
        for field in ("Goals", "Quests", "Conclusion"):
            self.assertEqual(fields[field], sections(CLOSING)[field])
        self.assertIn(citation.strip(), fields["Ideas"])
        self.assertIn("I2. Keep this real project idea.", fields["Ideas"])

    def test_structural_citation_label_does_not_replace_project_quests(self):
        citation = self.structural_citation("5. **Quests:**\n   - Q99. Metadata, not a project decision.")
        fields = sections(CLOSING + "\n" + citation + "I2. Keep this real project idea.\n" + HOST_CITATION)
        self.assertEqual(fields["Quests"], sections(CLOSING)["Quests"])
        self.assertIn(citation.strip(), fields["Ideas"])
        self.assertIn("I2. Keep this real project idea.", fields["Ideas"])

    def assert_structural_citation_capture(self, payload, host):
        citation = self.structural_citation(payload)
        text = CLOSING + "\n" + citation + "I2. Keep this real project idea.\n" + HOST_CITATION
        path = self.codex_transcript("old-codex", text) if host == "codex" else self.claude_transcript(text)
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            sealed = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        for field in ("Goals", "Quests", "Conclusion"):
            self.assertEqual(state["fields"][field]["text"], sections(CLOSING)[field])
        self.assertIn(citation.strip(), state["fields"]["Ideas"]["text"])
        self.assertIn("I2. Keep this real project idea.", state["fields"]["Ideas"]["text"])
        for field in ("Goals", "Quests", "Ideas"):
            self.assertEqual(state["fields"][field]["reply_digest"], checksum(text))
            self.assertEqual(state["fields"][field]["host"], host)
            self.assertEqual(state["fields"][field]["session"], "old-" + host)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], sealed)
        self.assertEqual(path.read_bytes(), before)

    def test_structural_citation_zone_capture_recovers_real_codex_fields(self):
        self.assert_structural_citation_capture(
            "**Admin-Zone**\n0. **Goals:**\n   - L1. Metadata, not the project aim.", "codex")

    def test_structural_citation_label_capture_recovers_real_claude_fields(self):
        self.assert_structural_citation_capture(
            "5. **Quests:**\n   - Q99. Metadata, not a project decision.", "claude")

    def test_structural_citation_cannot_replace_the_real_conclusion(self):
        citation = self.structural_citation("**Conclusion:** Metadata, not the project conclusion.")
        text = CLOSING.replace("**Conclusion:**", citation + "\n**Conclusion:**", 1)
        self.assertEqual(sections(text)["Conclusion"], sections(CLOSING)["Conclusion"])

    def test_structural_citation_payload_fence_does_not_hide_later_sections(self):
        citation = self.structural_citation("```xml\nMemory citation literal, not a project code fence.")
        text = CLOSING + citation + "5. **Quests:**\n   - Q3. A real project question.\n" + HOST_CITATION
        self.assertEqual(sections(text)["Quests"], "- Q3. A real project question.")
        self.assertIn(citation.strip(), sections(text)["Ideas"])

    def test_same_line_conclusion_after_known_citation_is_captured(self):
        citation = self.structural_citation("Phục hồi lịch sử; **Conclusion:** metadata only.")
        text = CLOSING.replace("**Conclusion:**", citation.rstrip() + "**Conclusion:**", 1)
        path = self.codex_transcript("old-codex", text)
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            sealed = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        expected = sections(CLOSING)["Conclusion"]
        self.assertEqual(state["fields"].get("Conclusion", {}).get("text"), expected)
        self.assertEqual(sections(text)["Conclusion"], expected)
        self.assertEqual(state["fields"]["Conclusion"]["reply_digest"], checksum(text))
        self.assertEqual(state["fields"]["Conclusion"]["cursor"], latest_reply(
            path, self.project, "codex", "old-codex")["cursor"])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], sealed)
        self.assertEqual(path.read_bytes(), before)

    def test_empty_conclusion_with_only_citation_preserves_previous_provenance(self):
        path = self.codex_transcript("old-codex", CLOSING)
        self.assertTrue(capture(self.store)["saved"])
        previous = recover(self.store, full=True)["fields"]["Conclusion"]
        text = ("**Admin-Zone**\n\n**Conclusion:**\n" + HOST_CITATION
                + "0. **Goals:**\n   - L1. Keep the newer project aim.\n" + HOST_CITATION)
        with path.open("a") as output:
            output.write(json.dumps({"type": "response_item", "timestamp": "2026-10-05T10:00:00Z",
                "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": text}]}}) + "\n")
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            sealed = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        self.assertEqual(state["fields"]["Conclusion"], previous)
        self.assertNotIn("Conclusion", sections(text))
        self.assertEqual(state["fields"]["Goals"]["text"], "- L1. Keep the newer project aim.")
        self.assertEqual(state["fields"]["Goals"]["reply_digest"], checksum(text))
        self.assertEqual(json.loads(sealed)["fields"]["Conclusion"]["reply_digest"], checksum(CLOSING))
        self.assertEqual(json.loads(sealed)["fields"]["Conclusion"]["cursor"], previous["cursor"])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], sealed)
        self.assertEqual(path.read_bytes(), before)

    def test_same_line_idea_after_known_citation_remains_literal_in_claude_capture(self):
        for space in ("", " "):
            with self.subTest(space=space):
                body = CLOSING + HOST_CITATION.rstrip() + space + "I2. Keep this real idea.\n"
                self.assertEqual(without_host_citation(body), (body, False))
                self.assertIn("I2. Keep this real idea.", sections(body)["Ideas"])
        text = CLOSING + HOST_CITATION.rstrip() + "I2. Keep this real idea.\n" + HOST_CITATION
        path = self.claude_transcript(text)
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        state = recover(self.store, full=True)
        self.assertIn(HOST_CITATION.rstrip() + "I2. Keep this real idea.", state["fields"]["Ideas"]["text"])
        self.assertEqual(state["fields"]["Ideas"]["reply_digest"], checksum(text))
        self.assertEqual(state["fields"]["Ideas"]["session"], "old-claude")
        self.assertEqual(path.read_bytes(), before)

    def test_same_line_quest_label_replaces_previous_field_and_keeps_literal_citation(self):
        path = self.codex_transcript("old-codex", CLOSING)
        self.assertTrue(capture(self.store)["saved"])
        previous = recover(self.store, full=True)["fields"]["Quests"]
        text = (CLOSING + HOST_CITATION.rstrip() + "5. **Quests:**\n"
                + "   - Q3. Keep this current project question.\n" + HOST_CITATION)
        with path.open("a") as output:
            output.write(json.dumps({"type": "response_item", "timestamp": "2026-10-05T10:00:00Z",
                "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": text}]}}) + "\n")
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            sealed = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        self.assertEqual(state["fields"]["Quests"]["text"], "- Q3. Keep this current project question.")
        self.assertEqual(state["fields"]["Quests"]["reply_digest"], checksum(text))
        self.assertGreater(state["fields"]["Quests"]["cursor"], previous["cursor"])
        self.assertIn(HOST_CITATION.strip(), json.loads(sealed)["fields"]["Ideas"]["text"])
        self.assertEqual(sections(text)["Ideas"], json.loads(sealed)["fields"]["Ideas"]["text"])
        self.assertNotIn("Q3.", state["fields"]["Ideas"]["text"])
        self.assertEqual(sections(text)["Quests"], state["fields"]["Quests"]["text"])
        self.assertTrue(any(item["field"] == "Quests" and item["text"] == previous["text"]
                            and item["reply_digest"] == checksum(CLOSING)
                            for item in state["prior_sections_for_reconciliation"]))
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], sealed)
        self.assertEqual(path.read_bytes(), before)

    def test_same_line_citation_fence_keeps_example_labels_opaque_in_claude_capture(self):
        text = (CLOSING + HOST_CITATION.rstrip() + "```text\n0. **Goals:**\n"
                + "   - L99. A fenced example, not the current aim.\n```\n"
                + "5. **Quests:**\n   - Q3. The real question after the example.\n" + HOST_CITATION)
        path = self.claude_transcript(text)
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            sealed = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        self.assertEqual(state["fields"]["Goals"]["text"], sections(CLOSING)["Goals"])
        self.assertEqual(state["fields"]["Quests"]["text"], "- Q3. The real question after the example.")
        self.assertIn(HOST_CITATION.rstrip() + "```text", state["fields"]["Ideas"]["text"])
        self.assertIn("L99. A fenced example", state["fields"]["Ideas"]["text"])
        self.assertEqual(sections(text)["Quests"], state["fields"]["Quests"]["text"])
        self.assertEqual(state["fields"]["Quests"]["reply_digest"], checksum(text))
        self.assertEqual(state["fields"]["Quests"]["session"], "old-claude")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], sealed)
        self.assertEqual(path.read_bytes(), before)

    def test_same_line_citation_fence_preserves_literal_terminal_citation(self):
        text = CLOSING + HOST_CITATION.rstrip() + "```xml\n" + HOST_CITATION
        self.assertEqual(without_host_citation(text), (text, False))
        self.assertIn("```xml\n" + HOST_CITATION.strip(), sections(text)["Ideas"])

    def test_same_line_goal_label_closes_a_literal_citation_in_the_conclusion(self):
        text = CLOSING.replace("0. **Goals:**", HOST_CITATION.rstrip() + "0. **Goals:**", 1)
        path = self.claude_transcript(text)
        before = path.read_bytes()
        self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            sealed = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        expected = sections(CLOSING)["Conclusion"]
        self.assertEqual(state["fields"]["Conclusion"]["text"], expected)
        self.assertEqual(state["fields"]["Goals"]["text"], sections(CLOSING)["Goals"])
        self.assertEqual(json.loads(sealed)["fields"]["Conclusion"]["text"],
                         expected + "\n" + HOST_CITATION.strip())
        self.assertEqual(state["fields"]["Conclusion"]["reply_digest"], checksum(text))
        self.assertTrue(any(item["field"] == "Conclusion" and item["suffix_only"]
                            for item in state["excluded_host_metadata"]))
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], sealed)
        self.assertEqual(path.read_bytes(), before)

    def test_fenced_incomplete_and_nonterminal_citation_text_is_retained(self):
        for suffix in ("```xml\n" + HOST_CITATION,
                       "```xml\n" + HOST_CITATION + "```\n",
                       HOST_CITATION.replace("</oai-mem-citation>\n", ""),
                       HOST_CITATION.replace("<rollout_ids>", "<other_ids>").replace("</rollout_ids>", "</other_ids>"),
                       HOST_CITATION + "A real project idea follows.\n"):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix.strip(), sections(CLOSING + "\n" + suffix)["Ideas"])

    def test_citation_overflow_keeps_the_raw_byte_limit_and_an_explicit_context_gap(self):
        path = self.codex_transcript("old-codex", CLOSING)
        self.assertTrue(capture(self.store)["saved"])
        closing = CLOSING + "x" * (REPLY_BYTES - 50 - len(CLOSING.encode()))
        raw = closing + HOST_CITATION
        with path.open("a") as output:
            output.write(json.dumps({"type": "response_item", "timestamp": "2026-10-05T10:00:00Z",
                "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": raw}]}}) + "\n")
        reply = latest_reply(path, self.project, "codex", "old-codex")
        self.assertFalse(reply["complete"])
        self.assertLessEqual(len(reply["text"].encode()), REPLY_BYTES)
        capture(self.store)
        state = recover(self.store)
        self.assertTrue(any("exceeds the reply byte budget" in gap["reason"]
                            for gap in state["source_gaps_at_capture"]))
        self.assertEqual(state["fields"]["Ideas"]["reply_digest"], checksum(CLOSING))

    def test_legacy_metadata_only_field_has_a_clean_view_without_a_ledger_migration(self):
        text = CLOSING.replace("   - I1. Share findings when the owning room confirms them.\n", "") + HOST_CITATION
        path = self.codex_transcript("old-codex", text)
        original_parser = sections
        with patch("ihav_agent_room.closing.sections", side_effect=lambda reply: original_parser(reply) | {"Ideas": HOST_CITATION.strip()}):
            self.assertTrue(capture(self.store)["saved"])
        with self.store.read() as db:
            before = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0]
        state = recover(self.store, full=True)
        self.assertEqual(state["fields"]["Ideas"]["text"], "")
        self.assertTrue(state["fields"]["Ideas"]["host_metadata_only"])
        self.assertEqual(state["excluded_host_metadata"][0]["field"], "Ideas")
        self.assertEqual(state["excluded_host_metadata"][0]["reply_digest"], checksum(text))
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()[0], before)
        self.assertIn(HOST_CITATION.strip(), json.loads(before)["fields"]["Ideas"]["text"])
        self.assertIn(HOST_CITATION, latest_reply(path, self.project, "codex", "old-codex")["text"])

    def test_legacy_real_idea_survives_suffix_cleanup_and_fenced_text_stays_exact(self):
        cases = (("I1. A real idea cites this example.\n" + HOST_CITATION, "I1. A real idea cites this example."),
                 ("```xml\n" + HOST_CITATION + "```", "```xml\n" + HOST_CITATION + "```"))
        for index, (body, expected) in enumerate(cases):
            with self.subTest(body=body):
                self.codex_transcript("old-codex", CLOSING + " " * (index + 1))
                original_parser = sections
                with patch("ihav_agent_room.closing.sections", side_effect=lambda reply: original_parser(reply) | {"Ideas": body}):
                    capture(self.store)
                state = recover(self.store, full=True)
                self.assertEqual(state["fields"]["Ideas"]["text"], expected)
                if index == 0:
                    self.assertFalse(state["fields"]["Ideas"]["host_metadata_only"])
                    self.assertTrue(state["excluded_host_metadata"][0]["suffix_only"])
                else:
                    self.assertFalse(state["excluded_host_metadata"])
                    self.assertNotIn("host_metadata_only", state["fields"]["Ideas"])

    def test_all_done_and_deleted_native_source_still_restore_after_process_reload(self):
        with self.store.tx() as db:
            db.execute("INSERT INTO tasks VALUES (?,?,?)", ("T-completed", 1,
                       json.dumps({"version": 1, "state": "done", "owner": "CODEX_01"})))
        path = self.claude_transcript(CLOSING)
        self.assertTrue(capture(self.store)["saved"])
        path.unlink()
        result = recover(Store(self.project))
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])
        self.assertIn("Conditional approval", result["fields"]["Quests"]["text"])
        self.assertIn("sent_unknown", result["fields"]["Conclusion"]["text"])
        self.assertEqual(result["authority"], "historical_data_only")
        with self.store.read() as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM tasks").fetchone()[0])["state"], "done")
        self.assertEqual(result["fields"]["Goals"]["session"], "old-claude")

    def test_short_later_final_and_commentary_cannot_erase_structured_closing(self):
        path = self.codex_transcript("old-codex", CLOSING)
        with path.open("a") as output:
            for phase in ("final_answer", "commentary"):
                output.write(json.dumps({"type": "response_item", "timestamp": "2026-10-05T10:00:00Z",
                    "payload": {"type": "message", "role": "assistant", "phase": phase,
                    "content": [{"type": "output_text", "text": "Connected, no active tasks."}]}}) + "\n")
        self.assertTrue(capture(self.store)["saved"])
        self.assertIn("sent_unknown", recover(self.store)["fields"]["Risks"]["text"])

    def append_tool_output(self, path, size):
        with path.open("a") as output:
            output.write(json.dumps({"type": "response_item", "payload": {
                "type": "function_call_output", "output": "x" * size}}) + "\n")
            output.write(json.dumps({"type": "response_item", "timestamp": "2026-10-05T10:00:00Z",
                "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Connected."}]}}) + "\n")

    def test_bootstrap_finds_closing_pushed_beyond_ordinary_tail_by_tool_output(self):
        path = self.codex_transcript("old-codex", CLOSING)
        self.append_tool_output(path, TRANSCRIPT_WINDOW + 512)
        self.assertTrue(capture(self.store)["saved"])
        result = recover(self.store)
        self.assertEqual(result["fields"]["Goals"]["session"], "old-codex")
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])
        self.assertFalse(any(gap["session"] == "old-codex" for gap in result["source_gaps_at_capture"]))

    def test_old_source_recovery_does_not_hide_another_closing_outside_bootstrap_cap(self):
        self.claude_transcript(CLOSING)
        path = self.codex_transcript("old-codex", CLOSING)
        self.append_tool_output(path, BOOTSTRAP_WINDOW + 512)
        self.assertTrue(capture(self.store)["saved"])
        result = recover(self.store)
        self.assertEqual(result["status"], "recovered")
        self.assertTrue(any(gap["session"] == "old-codex" and gap["window_bytes"] == BOOTSTRAP_WINDOW
                            for gap in result["source_gaps_at_capture"]))

    def test_cut_closing_is_a_gap_and_cannot_replace_saved_project_fields(self):
        path = self.claude_transcript(CLOSING)
        capture(self.store)
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["message"]["content"][0]["text"] = CLOSING.replace("11 chatbots", "x" * REPLY_BYTES)
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        capture(self.store)
        result = recover(self.store)
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])
        self.assertTrue(any("exceeds" in gap["reason"] for gap in result["source_gaps_at_capture"]))

    def test_transcript_window_cannot_exceed_bootstrap_cap(self):
        path = self.codex_transcript("old-codex", CLOSING)
        with self.assertRaises(ValueError):
            latest_reply(path, self.project, "codex", "old-codex", window_bytes=BOOTSTRAP_WINDOW + 1)

    def test_many_cut_closings_are_aggregated_without_blocking_valid_recovery(self):
        path = self.codex_transcript("old-codex", CLOSING)
        row = next(json.loads(line) for line in path.read_text().splitlines()
                   if json.loads(line).get("payload", {}).get("channel") == "final")
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["payload"]["content"][0]["text"] = CLOSING.replace("11 chatbots", "x" * REPLY_BYTES)
        with path.open("a") as output:
            output.write((json.dumps(row) + "\n") * 600)
        self.assertTrue(capture(self.store)["saved"])
        result = recover(self.store)
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])
        cut = [gap for gap in result["source_gaps_at_capture"] if "exceeds" in gap["reason"]]
        self.assertEqual(len(cut), 1)
        self.assertEqual(cut[0]["count"], 600)
        self.assertEqual(cut[0]["omitted_records"], 599)

    def test_complete_closing_at_exact_tail_boundary_is_not_discarded(self):
        for window in (TRANSCRIPT_WINDOW, BOOTSTRAP_WINDOW):
            with self.subTest(window=window):
                path = self.codex_transcript("old-codex", CLOSING)
                lines = path.read_bytes().splitlines(keepends=True)
                header = lines[0]
                row = next(line for line in lines if json.loads(line).get("payload", {}).get("channel") == "final")
                path.write_bytes(header + row + b" " * (window - len(row) - 1) + b"\n")
                reply = latest_reply(path, self.project, "codex", "old-codex", window_bytes=window)
                self.assertIsNotNone(reply)
                self.assertIn("11 chatbots", reply["text"])

    def test_connection_goal_and_empty_display_do_not_close_old_work(self):
        path = self.claude_transcript(CLOSING)
        capture(self.store)
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["message"]["content"][0]["text"] = (
            "**Admin-Zone**\n0. **Goals:**\n - G1. Connect room.\n1. **Done:**\n - Connected.\n"
            "4. **Pending:**\n5. **Quests:**\n6. **Risks:**\n")
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        capture(self.store)
        result = recover(self.store)
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])
        self.assertIn("Quests", result["retained_unresolved_sections"])
        self.assertIn("Eleven local commits", result["fields"]["Pending"]["text"])

    def test_first_capture_recovers_rich_reply_before_connection_only_admin_zone(self):
        path = self.claude_transcript(CLOSING)
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["message"]["content"][0]["text"] = (
            "**Admin-Zone**\n0. **Goals:**\n - G1. Connect room.\n1. **Done:**\n - Connected.\n"
            "4. **Pending:**\n5. **Quests:**\n6. **Risks:**\n")
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        capture(self.store)
        result = recover(self.store)
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])
        self.assertIn("Conditional approval", result["fields"]["Quests"]["text"])
        self.assertFalse(capture(self.store)["saved"])

    def test_replacement_question_preserves_previous_choices_for_reconciliation(self):
        path = self.claude_transcript(CLOSING)
        capture(self.store)
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["message"]["content"][0]["text"] = CLOSING.replace(
            "Q1. Approve: one read-only survey of the named conversation?",
            "Q1. Approve: prepare the Gemini controller?")
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        capture(self.store)
        result = recover(self.store)
        self.assertIn("Gemini controller", result["fields"]["Quests"]["text"])
        prior = [item for item in result["prior_sections_for_reconciliation"] if item["field"] == "Quests"]
        self.assertEqual(len(prior), 1)
        self.assertIn("read-only survey", prior[0]["text"])
        self.assertIn("Quests", result["retained_unresolved_sections"])
        self.assertFalse(capture(self.store)["saved"])

    def test_capture_is_idempotent_and_context_read_never_writes(self):
        self.claude_transcript(CLOSING)
        self.assertTrue(capture(self.store)["saved"])
        self.assertFalse(capture(self.store)["saved"])
        before = self.store.path.read_bytes()
        result = run(parser().parse_args(["--project", str(self.project), "context"]))
        self.assertEqual(result["project_state"]["status"], "recovered")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_new_claude_gateway_recovers_saved_codex_brief(self):
        self.codex_transcript("new-codex", CLOSING)
        capture(self.store)
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(gateway="CLAUDE_01", owner={"host": "claude", "session": "new-claude"})
            self.store.put_room(db, room)
        with patch.dict(os.environ, {"IHAV_AGENT_ROOM_HOST": "claude", "CLAUDE_CODE_SESSION_ID": "new-claude"}, clear=True):
            result = recover(Store(self.project))
        self.assertEqual(result["fields"]["Goals"]["host"], "codex")
        self.assertIn("Gemini", result["fields"]["Goals"]["text"])

    def test_drift_is_reported_without_mutating_or_resolving_old_choices(self):
        self.claude_transcript(CLOSING)
        capture(self.store)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes VALUES (?,?,?)", ("Q-test", 1, json.dumps({"version": 1, "state": "answered"})))
        before = self.store.path.read_bytes()
        result = recover(self.store)
        self.assertEqual(result["ledger_drift_count"], 1)
        self.assertEqual(result["ledger_drift"][0]["id"], "Q-test")
        self.assertIn("Q1. Approve", result["fields"]["Quests"]["text"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_start_preserves_drift_when_a_new_gateway_has_no_closing(self):
        self.claude_transcript(CLOSING)
        old_checkout = {"status": "observed", "head": "old-head", "path_status_digest": "old-paths"}
        new_checkout = {"status": "observed", "head": "new-head", "path_status_digest": "new-paths"}
        with patch("ihav_agent_room.closing.checkout", return_value=old_checkout):
            capture(self.store)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes VALUES (?,?,?)", ("Q-changed", 1,
                       json.dumps({"version": 1, "state": "answered"})))
            room = self.store.get_room(db)
            room["owner"]["session"] = "next-codex"
            self.store.put_room(db, room)
        with patch.dict(os.environ, CODEX_THREAD_ID="next-codex"), \
                patch("ihav_agent_room.closing.checkout", return_value=new_checkout), \
                patch("ihav_agent_room.cli.doctor", return_value={"ok": True}), \
                patch("ihav_agent_room.cli.connection_plan", return_value={"resume_required": False, "blockers": []}), \
                patch("ihav_agent_room.cli.probe_codex", new_callable=AsyncMock), \
                patch("ihav_agent_room.cli.start_room", return_value={"started": False, "reason": "supervisor already running"}):
            result = run(parser().parse_args(["--project", str(self.project), "start"]))["working_context"]["project_state"]
        self.assertEqual(result["ledger_drift_count"], 1)
        self.assertEqual(result["ledger_drift"][0]["id"], "Q-changed")
        self.assertEqual(result["checkout_comparison"], "changed")
        self.assertIn("next-codex", {gap["session"] for gap in result["source_gaps_at_capture"]})
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])

    def test_empty_later_quest_does_not_refresh_the_old_closing_baseline(self):
        path = self.claude_transcript(CLOSING)
        capture(self.store)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes VALUES (?,?,?)", ("Q-changed", 1,
                       json.dumps({"version": 1, "state": "answered"})))
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["message"]["content"][0]["text"] = "**Admin-Zone**\n5. **Quests:**\n"
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        self.assertTrue(capture(self.store)["saved"])
        result = recover(self.store)
        self.assertEqual(result["ledger_drift_count"], 1)
        self.assertIn("Quests", result["retained_unresolved_sections"])
        self.assertIn("Conditional approval", result["fields"]["Quests"]["text"])

    def test_a_new_structured_closing_refreshes_its_comparison_baseline(self):
        path = self.claude_transcript(CLOSING)
        capture(self.store)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes VALUES (?,?,?)", ("Q-new", 1,
                       json.dumps({"version": 1, "state": "answered"})))
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["message"]["content"][0]["text"] = CLOSING.replace("Eleven local commits", "Twelve local commits")
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        self.assertTrue(capture(self.store)["saved"])
        self.assertEqual(recover(self.store)["ledger_drift_count"], 0)
        self.assertIn("Twelve local commits", recover(self.store)["fields"]["Pending"]["text"])

    def test_connection_only_done_cannot_hide_changes_to_the_project(self):
        path = self.claude_transcript(CLOSING)
        capture(self.store)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes VALUES (?,?,?)", ("Q-changed", 1,
                       json.dumps({"version": 1, "state": "answered"})))
        row = json.loads(path.read_text())
        row["timestamp"] = "2026-10-05T10:00:00Z"
        row["message"]["content"][0]["text"] = (
            "**Admin-Zone**\n**Conclusion:** Connected.\n0. **Goals:**\n - G1. Connect room.\n"
            "1. **Done:**\n - Connected.\n5. **Quests:**\n")
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        self.assertTrue(capture(self.store)["saved"])
        result = recover(self.store)
        self.assertEqual(result["ledger_drift_count"], 1)
        self.assertIn("11 chatbots", result["fields"]["Goals"]["text"])

    def test_zone_name_in_prose_or_fenced_example_cannot_replace_the_actual_zone(self):
        prefix = ("The previous Admin-Zone needs reconciliation.\n"
                  "**Conclusion:** This is commentary before the closing.\n"
                  "```text\n**Admin-Zone**\n0. **Goals:**\n - L1. Wrong example.\n```\n")
        result = sections(prefix + CLOSING)
        self.assertEqual(result["Conclusion"], "The send remains sent_unknown; awaiting a read-only survey decision.")
        self.assertIn("11 chatbots", result["Goals"])
        self.assertNotIn("Wrong example", result["Goals"])

    def test_corrupt_state_is_explicit_and_preserved(self):
        self.claude_transcript(CLOSING)
        capture(self.store)
        with self.store.tx() as db:
            db.execute("UPDATE meta SET value='[]' WHERE key=?", (KEY,))
        before = self.store.path.read_bytes()
        self.assertEqual(recover(self.store)["status"], "invalid")
        with self.assertRaises(RoomError):
            capture(self.store)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_stale_capture_never_writes_over_a_changed_gateway(self):
        self.claude_transcript(CLOSING)
        def checkout_then_handoff(project):
            with self.store.tx() as db:
                room = self.store.get_room(db)
                room["owner"]["session"] = "new-owner"
                self.store.put_room(db, room)
            return {"status": "unavailable"}
        with patch("ihav_agent_room.closing.checkout", side_effect=checkout_then_handoff):
            result = capture(self.store)
        self.assertFalse(result["saved"])
        self.assertIn("changed", result["reason"])

    def test_byte_truncation_has_supported_full_reader(self):
        self.claude_transcript(CLOSING.replace("Gemini; login", "g" * (FIELD_BYTES + 10) + "; login"))
        capture(self.store)
        compact = recover(self.store)
        full = run(parser().parse_args(["--project", str(self.project), "context", "--full"]))["project_state"]
        self.assertIn("Goals", compact["truncated_fields"])
        self.assertEqual(full["truncated_fields"], [])
        self.assertGreater(len(full["fields"]["Goals"]["text"]), len(compact["fields"]["Goals"]["text"]))

    def test_compact_context_avoids_duplicate_native_text_and_exposes_full_read(self):
        self.claude_transcript(CLOSING)
        capture(self.store)
        compact = run(parser().parse_args(["--project", str(self.project), "context"]))
        full = run(parser().parse_args(["--project", str(self.project), "context", "--full"]))
        self.assertTrue(compact["historical_text_deferred"])
        self.assertNotIn("text", compact["historical_replies"][0])
        self.assertIn("text", full["historical_replies"][0])
        self.assertLess(len(json.dumps(compact).encode()), 22000)

    def test_bound_codex_stop_saves_context_without_starting_a_turn(self):
        self.codex_transcript("new-codex", CLOSING)
        output = handle({"cwd": str(self.project), "session_id": "new-codex", "hook_event_name": "Stop"})
        self.assertEqual(output, {})
        self.assertEqual(recover(self.store)["status"], "recovered")

    def test_stopped_or_mismatched_worker_never_reports_pair_ready(self):
        self.store.member("CLAUDE_01", {"status": "stopped"})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            result = readiness(self.store)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(item["member"] == "CLAUDE_01" for item in result["issues"]))
        self.store.member("CLAUDE_01", {"status": "idle", "unexpected_native_id": "different-session"})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            self.assertEqual(readiness(self.store)["status"], "blocked")

    def test_identity_errors_stay_blocked_during_initial_launch(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(mode="pair", status="starting")
            self.store.put_room(db, room)
        self.store.member("CODEX_01", {"native_id": "new-codex", "status": "active"})
        self.store.member("CLAUDE_01", {"native_id": "old-claude", "status": "idle",
                                       "unexpected_native_id": "another-session",
                                       "launch_generation": self.store.room()["generation"]})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            result = readiness(self.store)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("native_identity_mismatch", {item["reason"] for item in result["issues"]})
        self.store.member("CLAUDE_01", {"unexpected_native_id": None, "error": "native registry refused"})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            self.assertEqual(readiness(self.store)["status"], "blocked")

    def test_initial_missing_worker_identity_is_starting_without_hiding_gateway_mismatch(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(mode="pair", status="starting")
            self.store.put_room(db, room)
        self.store.member("CODEX_01", {"native_id": "new-codex", "status": "active"})
        self.store.member("CLAUDE_01", {"native_id": None, "status": "stopped", "error": None})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            self.assertEqual(readiness(self.store)["status"], "starting")
        self.store.member("CODEX_01", {"native_id": "different-gateway"})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            result = readiness(self.store)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("gateway_identity_mismatch", {item["reason"] for item in result["issues"]})

    def test_worker_error_from_the_old_launch_does_not_block_the_requested_recovery(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(mode="pair", status="starting", generation="new-launch")
            self.store.put_room(db, room)
        self.store.member("CODEX_01", {"native_id": "new-codex", "status": "active"})
        reason = "Native background session exited. Stop/start to resume it."
        self.store.member("CLAUDE_01", {"native_id": "old-claude", "status": "stopped", "error": reason,
                                       "launch_generation": "old-launch"})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            result = readiness(self.store)
        self.assertEqual(result["status"], "starting")
        self.assertIn({"member": "CLAUDE_01", "reason": "previous_generation_error", "detail": reason}, result["issues"])
        self.store.member("CLAUDE_01", {"launch_generation": "new-launch"})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            self.assertEqual(readiness(self.store)["status"], "blocked")

    def test_launch_resets_error_generation_before_the_native_worker_attempt(self):
        import asyncio
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(mode="pair", status="starting", generation="new-launch")
            self.store.put_room(db, room)
        self.store.member("CLAUDE_01", {"error": "old failure", "launch_generation": "old-launch"})
        observed = []
        async def launch_failure(*args, **kwargs):
            observed.append(self.store.member("CLAUDE_01"))
            raise RoomError("controlled native launch failure")
        supervisor = Supervisor(self.store, "new-launch")
        with patch("ihav_agent_room.runtime.CodexGateway.start", new_callable=AsyncMock), \
                patch("ihav_agent_room.runtime.GlobalSpace.register"), \
                patch.object(supervisor, "recover_owned", new_callable=AsyncMock), \
                patch.object(supervisor, "owner_alive", return_value=True), \
                patch("ihav_agent_room.runtime.start_claude", side_effect=launch_failure):
            with self.assertRaises(RoomError):
                asyncio.run(supervisor.launch())
        self.assertEqual(observed[0]["launch_generation"], "new-launch")
        self.assertIsNone(observed[0]["error"])

    def test_live_process_and_stored_identity_do_not_claim_native_readiness(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room["mode"] = "pair"
            self.store.put_room(db, room)
        self.store.member("CODEX_01", {"native_id": "new-codex", "status": "active"})
        self.store.member("CLAUDE_01", {"native_id": "old-claude", "status": "idle", "pid": 42})
        with patch("ihav_agent_room.closing.process_alive", return_value=True):
            result = readiness(self.store)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["status"], "observed_running")
        self.assertEqual(result["native_readiness"], "unverified")

    def test_section_parser_does_not_interpret_code_as_display_sections(self):
        result = sections(CLOSING + "\n```text\n0. Goals:\nL1. Wrong goal\n```\n")
        self.assertIn("11 chatbots", result["Goals"])
        self.assertNotIn("Wrong goal", result["Goals"])

    def test_wrong_gateway_cannot_save_or_read_closing_context(self):
        with patch.dict(os.environ, CODEX_THREAD_ID="other-thread"):
            with self.assertRaises(RoomError):
                capture(self.store)
            with self.assertRaises(RoomError):
                recover(self.store)

    def set_cleanup(self):
        with self.store.tx() as db:
            room = self.store.get_room(db)
            room.update(status="stopping", host_sessions={"codex": "new-codex"},
                        supervisor={"pid": 42, "stamp": "owned"})
            self.store.put_room(db, room)

    def test_new_session_waits_for_closed_controller_cleanup_without_forcing_it(self):
        self.set_cleanup()
        def finish_cleanup(_):
            with self.store.tx() as db:
                room = self.store.get_room(db)
                room.update(status="stopped", supervisor={})
                self.store.put_room(db, room)
        with patch("ihav_agent_room.runtime.probe_detached_codex", new_callable=AsyncMock) as probe, \
                patch("ihav_agent_room.runtime.process_alive", side_effect=lambda pid, stamp: bool(pid)), \
                patch("ihav_agent_room.runtime.time.sleep", side_effect=finish_cleanup):
            await_detached_shutdown(self.store, "brand-new-session")
        probe.assert_awaited_once_with(self.project, "brand-new-session", "new-codex")
        self.assertEqual(self.store.room()["owner"]["session"], "new-codex")

    def test_live_or_unknown_old_controller_never_starts_cleanup(self):
        self.set_cleanup()
        before = self.store.path.read_bytes()
        for code in ("conflict", "outcome_unknown"):
            with patch("ihav_agent_room.runtime.probe_detached_codex", new_callable=AsyncMock,
                       side_effect=RoomError("native hold", code)), patch("ihav_agent_room.runtime.time.sleep") as wait:
                with self.assertRaises(RoomError):
                    await_detached_shutdown(self.store, "brand-new-session")
                wait.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_recovery_deadline_keeps_old_owner_and_reports_pending(self):
        self.set_cleanup()
        before = self.store.path.read_bytes()
        with patch("ihav_agent_room.runtime.probe_detached_codex", new_callable=AsyncMock):
            with self.assertRaises(RoomError) as caught:
                await_detached_shutdown(self.store, "brand-new-session", budget=0)
        self.assertEqual(caught.exception.code, "recovery_pending")
        self.assertEqual(self.store.path.read_bytes(), before)
