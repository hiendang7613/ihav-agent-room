"""Long-running closing state survives source loss, migration and failed writes."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import test_closing
import test_continuity
from ihav_agent_room.cli import parser, run
from ihav_agent_room.closing import (CURRENT_BYTES, FIELD_BYTES, HISTORY_KEY, KEY, LOOKUP_PREFIX,
                                     PRIOR_PREFIX, STATE_BYTES, capture, checksum, load,
                                     prior_history, recover, sections, write_state)
from ihav_agent_room.common import RoomError, dumps
from ihav_agent_room.store import Store


class ClosingStorageTests(unittest.TestCase):
    setUp = test_continuity.ContinuityTests.setUp

    def source(self, number, choice=None):
        text = test_closing.CLOSING.replace(
            "Q1. Approve: one read-only survey of the named conversation?",
            choice or (f"Q1. Unapproved choice {number}: " + "condition " * 80))
        path = self.root / "verified-closing.jsonl"
        path.write_text(text)
        return {"host": "codex", "session": "new-codex", "source": str(path),
                "observed_at": (datetime(2026, 10, 5, tzinfo=timezone.utc)
                                + timedelta(seconds=number)).isoformat(),
                "cursor": number + 1, "reply_digest": checksum(text), "fields": sections(text)}

    def save(self, source, gaps=None):
        with patch("ihav_agent_room.closing.collect", return_value=([source], gaps or [])), \
                patch("ihav_agent_room.closing.checkout", return_value={"status": "unavailable"}):
            return capture(self.store)

    def stored(self):
        with self.store.read() as db:
            return list(map(tuple, db.execute("SELECT key,value FROM meta WHERE key LIKE ? ORDER BY key",
                                             (KEY + "%",))))

    def record(self):
        with self.store.read() as db:
            return load(db, self.store.get_room(db))

    def test_more_than_two_hundred_updates_keep_exact_paged_history_after_source_loss(self):
        first = self.source(0)
        self.assertTrue(self.save(first)["saved"])
        for number in range(1, 205):
            self.assertTrue(self.save(self.source(number))["saved"])
        (self.root / "verified-closing.jsonl").unlink()
        fresh = Store(self.project)
        result = recover(fresh)
        self.assertEqual(result["revision"], 205)
        self.assertEqual(result["prior_history_total"], 204)
        self.assertEqual(len(result["prior_sections_for_reconciliation"]), 4)
        self.assertEqual(result["prior_history_next_after"], 4)
        self.assertLess(len(dumps(result).encode()), 16000)
        cursor, all_items = 0, []
        while cursor is not None:
            page = prior_history(fresh, cursor, limit=20, full=True)
            all_items.extend(page["items"])
            cursor = page["next_after"]
        self.assertEqual(len(all_items), 204)
        self.assertEqual([item["history_id"] for item in all_items], list(range(1, 205)))
        self.assertEqual(all_items[0]["text"], first["fields"]["Quests"])
        for key in ("source", "session", "observed_at", "cursor", "reply_digest"):
            self.assertEqual(all_items[0][key], first[key])
        self.assertIn("choice 204", result["fields"]["Quests"]["text"])
        self.assertEqual(result["authority"], "historical_data_only")

    def test_identical_and_alternating_sections_deduplicate_without_losing_first_source(self):
        for number in range(20):
            self.assertTrue(self.save(self.source(number, "Q1. Same choice."))["saved"])
        self.assertEqual(prior_history(self.store)["total"], 0)
        first = self.source(20, "Q1. First alternative.")
        self.save(first)
        for number in range(21, 40):
            self.save(self.source(number, "Q1. Second alternative." if number % 2 else "Q1. First alternative."))
        items = prior_history(self.store, limit=20, full=True)["items"]
        self.assertEqual(len(items), 3)
        matching = next(item for item in items if "First alternative" in item["text"])
        self.assertEqual(matching["reply_digest"], first["reply_digest"])
        self.assertEqual(matching["cursor"], first["cursor"])

    def test_current_ledger_above_the_old_budget_saves_without_growing_inline_history(self):
        with self.store.tx() as db:
            db.executemany("INSERT INTO tasks VALUES (?,?,?)", [
                (f"T-{number:04}-" + "x" * 60, 1, dumps({"version": 1, "state": "done", "owner": "CODEX_01"}))
                for number in range(1000)])
        self.assertTrue(self.save(self.source(0))["saved"])
        record = self.record()
        self.assertGreater(len(dumps(record["ledger"]).encode()), STATE_BYTES)
        self.assertEqual(record["prior_sections_for_reconciliation"], [])
        self.assertEqual(recover(self.store)["ledger_drift_count"], 0)
        with self.store.tx() as db:
            row = db.execute("SELECT id,data FROM tasks LIMIT 1").fetchone()
            changed = json.loads(row["data"]) | {"version": 2, "state": "ready"}
            db.execute("UPDATE tasks SET version=2,data=? WHERE id=?", (dumps(changed), row["id"]))
        self.assertEqual(recover(self.store)["ledger_drift_count"], 1)

    def test_event_failure_rolls_back_snapshot_archive_and_lookup_together(self):
        self.save(self.source(0))
        before = self.stored()
        with patch.object(self.store, "event", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.save(self.source(1))
        self.assertEqual(self.stored(), before)
        self.assertEqual(recover(self.store)["revision"], 1)
        self.assertEqual(prior_history(self.store)["total"], 0)

    def test_current_snapshot_capacity_refusal_leaves_no_orphaned_archives(self):
        self.save(self.source(0))
        before = self.stored()
        record = self.record()
        item = dict(record["fields"]["Quests"], field="Quests")
        with patch("ihav_agent_room.closing.CURRENT_BYTES", 10):
            # Refuse an oversized first snapshot without changing existing storage.
            with self.store.tx() as db:
                room = self.store.get_room(db)
                self.assertFalse(write_state(db, room, record, [(1, checksum(["Quests", item["text"]]), item)]))
        self.assertEqual(self.stored(), before)
        self.assertLess(len(dumps(self.record()).encode()), CURRENT_BYTES)

    def test_legacy_inline_history_migrates_with_wording_and_provenance_intact(self):
        first, second = self.source(0), self.source(1)
        self.save(first)
        record = self.record()
        record.pop("prior_history_count")
        record.pop("prior_history_fields")
        item = dict(record["fields"]["Quests"], field="Quests")
        record["prior_sections_for_reconciliation"] = [item]
        record.pop("digest")
        record["digest"] = checksum(record)
        with self.store.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?", (HISTORY_KEY,))
            db.execute("UPDATE meta SET value=? WHERE key=?", (dumps(record), KEY))
        self.assertEqual(prior_history(self.store, full=True)["items"][0]["text"], item["text"])
        self.assertTrue(self.save(second)["saved"])
        page = prior_history(self.store, full=True)
        self.assertEqual(page["total"], 1)
        self.assertEqual({key: value for key, value in page["items"][0].items() if key != "history_id"}, item)
        self.assertEqual(self.record()["prior_sections_for_reconciliation"], [])

    def test_rollback_style_schema_one_writer_cannot_erase_new_archive_high_water_mark(self):
        self.save(self.source(0))
        self.save(self.source(1))
        record = self.record()
        self.assertEqual(record["schema"], 1)
        record.pop("prior_history_count")
        record.pop("prior_history_fields")
        record.pop("digest")
        record["digest"] = checksum(record)
        with self.store.tx() as db:
            db.execute("UPDATE meta SET value=? WHERE key=?", (dumps(record), KEY))
        self.assertTrue(self.save(self.source(2))["saved"])
        self.assertEqual(prior_history(self.store, full=True)["total"], 2)
        self.assertEqual(recover(self.store)["revision"], 3)

    def test_rollback_inline_prior_is_readable_before_capture_with_transcript_deleted(self):
        self.save(self.source(0))
        self.save(self.source(1))
        record = self.record()
        old = dict(record["fields"]["Quests"], field="Quests")
        source = self.source(2)
        record["fields"]["Quests"] = {key: value for key, value in source.items() if key != "fields"}
        record["fields"]["Quests"]["text"] = source["fields"]["Quests"]
        record.pop("prior_history_count")
        record.pop("prior_history_fields")
        record["prior_sections_for_reconciliation"] = [old]
        record.pop("digest")
        record["digest"] = checksum(record)
        with self.store.tx() as db:
            db.execute("UPDATE meta SET value=? WHERE key=?", (dumps(record), KEY))
        (self.root / "verified-closing.jsonl").unlink()
        before = self.store.path.read_bytes()
        self.assertFalse(capture(self.store)["saved"])
        result = recover(self.store)
        self.assertEqual(result["prior_history_total"], 2)
        self.assertEqual(result["prior_sections_for_reconciliation"][1]["text"], old["text"])
        self.assertEqual(prior_history(self.store, after=1, full=True)["items"][0]["reply_digest"], old["reply_digest"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_rollback_writer_cannot_clear_retained_flags_backed_by_archived_history(self):
        self.save(self.source(0))
        self.save(self.source(1))
        record = self.record()
        record.pop("prior_history_count")
        record.pop("prior_history_fields")
        record["retained"] = []
        record.pop("digest")
        record["digest"] = checksum(record)
        with self.store.tx() as db:
            db.execute("UPDATE meta SET value=? WHERE key=?", (dumps(record), KEY))
        self.assertIn("Quests", recover(self.store)["retained_unresolved_sections"])

    def test_later_history_corruption_is_explicitly_unverified_then_refused_on_its_page(self):
        for number in range(21):
            self.save(self.source(number))
        with self.store.tx() as db:
            db.execute("UPDATE meta SET value='{}' WHERE key=?", (PRIOR_PREFIX + "10",))
        result = recover(self.store)
        self.assertEqual(result["status"], "recovered")
        self.assertFalse(result["prior_history_validation"]["complete"])
        self.assertEqual(result["prior_history_validation"]["unverified_outside_page"], 16)
        self.assertIn("--prior-limit 20", result["prior_history_read_command"])
        self.assertNotIn("--full", result["prior_history_read_command"])
        with self.assertRaises(RoomError):
            prior_history(self.store, after=8)
        with self.store.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?", (PRIOR_PREFIX + "10",))
        self.assertEqual(recover(self.store)["status"], "invalid")

    def test_existing_archive_collision_is_a_controlled_error_and_preserves_storage(self):
        self.save(self.source(0))
        self.save(self.source(1))
        record = self.record()
        item = dict(record["fields"]["Quests"], field="Quests")
        before = self.stored()
        with self.assertRaisesRegex(RoomError, "storage conflicts"):
            with self.store.tx() as db:
                write_state(db, self.store.get_room(db), record, [(1, checksum(["Quests", item["text"]]), item)])
        self.assertEqual(self.stored(), before)

    def test_closing_capture_reports_the_same_cwd_gap_as_the_native_reader(self):
        test_continuity.ContinuityTests.claude_transcript(self, test_closing.CLOSING, cwd=self.root)
        result = capture(self.store)
        self.assertFalse(result["saved"])
        self.assertTrue(any("cwd differs" in gap["reason"] for gap in result["source_gaps"]))

    def test_corrupt_archive_missing_archive_and_corrupt_index_fail_closed(self):
        self.save(self.source(0))
        self.save(self.source(1))
        original = self.stored()
        for key, value in ((PRIOR_PREFIX + "1", "[]"), (HISTORY_KEY, "{}"), (KEY, "[]")):
            with self.subTest(key=key):
                with self.store.tx() as db:
                    db.executemany("UPDATE meta SET value=? WHERE key=?", [(value, key)])
                self.assertEqual(recover(self.store)["status"], "invalid")
                with self.assertRaises(RoomError):
                    prior_history(self.store)
                with self.store.tx() as db:
                    db.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", original)
        with self.store.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?", (PRIOR_PREFIX + "1",))
        self.assertEqual(recover(self.store)["status"], "invalid")

    def test_corrupt_deduplication_lookup_refuses_capture_without_partial_changes(self):
        self.save(self.source(0, "Q1. First."))
        self.save(self.source(1, "Q1. Second."))
        with self.store.tx() as db:
            db.execute("UPDATE meta SET value='not-an-integer' WHERE key LIKE ?", (LOOKUP_PREFIX + "%",))
        before = self.stored()
        self.save(self.source(2, "Q1. First."))
        with self.assertRaisesRegex(RoomError, "lookup is corrupt"):
            self.save(self.source(3, "Q1. Second."))
        # The successful intermediate capture remains; the refused capture adds nothing.
        refused = self.stored()
        self.assertNotEqual(refused, before)
        with self.assertRaises(RoomError):
            self.save(self.source(4, "Q1. Third."))
        self.assertEqual(self.stored(), refused)

    def test_paged_cli_context_is_read_only_and_full_page_restores_truncated_text(self):
        self.save(self.source(0, "Q1. Exact condition " + "điều kiện " * FIELD_BYTES))
        self.save(self.source(1))
        before = self.store.path.read_bytes()
        args = ["--project", str(self.project), "context", "--prior-after", "0", "--prior-limit", "1"]
        compact = run(parser().parse_args(args))["prior_history"]
        self.assertTrue(compact["items"][0]["text_truncated"])
        full = run(parser().parse_args(args + ["--full"]))["prior_history"]
        self.assertGreater(len(full["items"][0]["text"].encode()), FIELD_BYTES)
        self.assertEqual(self.store.path.read_bytes(), before)
        for after, limit in ((-1, 4), (0, 0), (0, 21)):
            with self.subTest(after=after, limit=limit), self.assertRaises(RoomError):
                prior_history(self.store, after, limit)

    def test_last_unfenced_admin_zone_and_markdown_heading_select_the_projects_own_closing(self):
        peer = "Admin-Zone\nConclusion: peer draft\n0. Goals:\n L1. Peer only.\n"
        reply = peer + "\n## Admin-Zone\nConclusion: own closing\n0. Goals:\n L1. Own project.\n"
        self.assertEqual(sections(reply)["Conclusion"], "own closing")
        self.assertEqual(sections(reply)["Goals"], "L1. Own project.")


if __name__ == "__main__":
    unittest.main()
