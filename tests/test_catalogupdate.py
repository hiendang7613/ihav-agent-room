"""Offline L5 plans and real shared SQLite reservations; no native child runs."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ihav_agent_room.catalogupdate import (ACTIVE_KEY, LAST_ACTIVATION_KEY, PREFIX, UpdateJournal,
                                         checksum, plan_update, plugin_update_command)
from ihav_agent_room.common import RoomError
from ihav_agent_room.globalspace import GlobalSpace


class UpdateFixture:
    def setUp(self):
        self.name = "ihav-example"
        self.source = {"source": "git-subdir", "url": "https://example.invalid/plugin.git",
                       "path": "plugins/ihav-example", "ref": "v1.1.0", "sha": "a" * 40}
        entry = {"name": self.name, "source": self.source}
        self.snapshot = {"url": "https://example.invalid/ihav.git", "head": "b" * 40,
                         "catalogs": {host: {"name": "ihav", "plugins": [deepcopy(entry)]}
                                      for host in ("claude", "codex")}}
        self.qualified = {"source": deepcopy(self.source), "tag_commit": "a" * 40,
                          "version": "1.1.0", "payload_sha256": "c" * 64,
                          "host_versions": {"claude": "1.1.0", "codex": "1.1.0"},
                          "release_kind": "routine",
                          "canary": {"room": "ihav-agent-room", "passed": True, "sha": "a" * 40,
                                     "version": "1.1.0", "started_at": 100, "verified_at": 3700}}
        self.qualification = {"catalog_url": self.snapshot["url"], "catalog_head": self.snapshot["head"],
                              "catalog_check": {"passed": True, "artifact_sha256": "d" * 64},
                              "plugins": {self.name: self.qualified}}
        old_source = dict(self.source, ref="v1.0.0", sha="e" * 40)
        self.installed = {"name": self.name, "marketplace": "ihav", "scope": "user", "enabled": True,
                          "source": old_source, "version": "1.0.0", "payload_sha256": "f" * 64,
                          "rollback": {"path": "/owned/rollback/1.0.0", "sha256": "1" * 64}}
        self.policy = {"enabled": True, "catalog_url": self.snapshot["url"]}

    def plan(self, host="claude", now=4000):
        return plan_update(self.snapshot, self.name, host, self.qualification, self.installed, self.policy, now=now)

    def assert_held(self, reason, host="claude", now=4000):
        result = self.plan(host, now)
        self.assertEqual((result["state"], result["reason"]), ("held", reason))
        self.assertNotIn("commands", result)


class UpdatePlanTests(UpdateFixture, unittest.TestCase):
    def test_qualified_claude_plan_names_one_marketplace_and_one_existing_user_selector(self):
        with patch("subprocess.run", side_effect=AssertionError("planning must not run a child")):
            plan = self.plan()
        self.assertEqual(plan["state"], "eligible")
        self.assertEqual(plan["commands"], [["claude", "plugin", "marketplace", "update", "ihav", "--json"]])
        self.assertEqual(plan["rollback"], self.installed["rollback"])
        self.assertFalse(plan["loaded"])
        self.assertNotIn("--yes", repr(plan["commands"]))
        self.assertNotIn("--accept-command", repr(plan["commands"]))

    def test_codex_add_is_not_assumed_to_be_a_supported_existing_plugin_update(self):
        self.assert_held("supported_plugin_update_unverified", host="codex")

    def test_refresh_must_preserve_the_qualified_revision_before_plugin_update(self):
        plan = self.plan()
        self.assertEqual(plugin_update_command(plan, self.snapshot),
                         {"state": "eligible", "command": ["claude", "plugin", "update", "ihav-example@ihav", "--scope", "user", "--json"]})
        moved = deepcopy(self.snapshot)
        moved["head"] = "2" * 40
        held = plugin_update_command(plan, moved)
        self.assertEqual(held, {"state": "held", "reason": "refreshed_catalog_requires_qualification"})
        moved = deepcopy(self.snapshot)
        moved["catalogs"]["claude"]["plugins"][0]["source"]["sha"] = "2" * 40
        self.assertEqual(plugin_update_command(plan, moved)["state"], "held")

    def test_empty_default_policy_cannot_form_native_commands(self):
        self.policy = {}
        self.assert_held("automatic_updates_not_enabled")

    def test_unpinned_branch_and_command_sources_are_held(self):
        for changes in ({"sha": None}, {"ref": "main"}, {"source": "command", "command": "echo unsafe"},
                        {"headersHelper": "echo credential"}, {"url": "https://user:secret@example.invalid/plugin.git"},
                        {"path": "../outside"}, {"path": "/outside"}):
            with self.subTest(changes=changes):
                old = deepcopy(self.snapshot)
                for catalog in self.snapshot["catalogs"].values():
                    catalog["plugins"][0]["source"].update(changes)
                self.assert_held("unqualified_catalog_entry")
                self.snapshot = old

    def test_each_host_must_resolve_the_same_source_not_just_the_same_ref(self):
        self.snapshot["catalogs"]["codex"]["plugins"][0]["source"]["url"] = "https://other.invalid/plugin.git"
        self.assert_held("unqualified_catalog_entry")

    def test_normalized_relative_subdirectory_is_the_same_source(self):
        self.snapshot["catalogs"]["codex"]["plugins"][0]["source"]["path"] = "./plugins/ihav-example"
        self.assertEqual(self.plan()["state"], "eligible")

    def test_duplicate_names_and_missing_host_catalog_are_held(self):
        old = deepcopy(self.snapshot)
        self.snapshot["catalogs"]["claude"]["plugins"].append(deepcopy(self.snapshot["catalogs"]["claude"]["plugins"][0]))
        self.assert_held("unqualified_catalog_entry")
        self.snapshot = old
        self.snapshot["catalogs"]["codex"] = None
        self.assert_held("unqualified_catalog_entry")

    def test_check_and_tag_evidence_bind_the_exact_catalog_commit(self):
        self.qualification["catalog_head"] = "2" * 40
        self.assert_held("qualification_revision_mismatch")
        self.qualification["catalog_head"] = self.snapshot["head"]
        self.qualification["catalog_check"]["passed"] = False
        self.assert_held("catalog_check_not_verified")
        self.qualification["catalog_check"]["passed"] = True
        self.qualified["tag_commit"] = "3" * 40
        self.assert_held("tag_or_source_not_verified")

    def test_qualified_version_payload_and_both_manifests_must_match(self):
        self.qualified["version"] = "1.1.1"
        self.assert_held("qualified_payload_mismatch")
        self.qualified["version"] = "1.1.0"
        self.qualified["payload_sha256"] = "missing"
        self.assert_held("qualified_payload_mismatch")
        self.qualified["payload_sha256"] = "c" * 64
        self.qualified["host_versions"]["codex"] = "1.0.0"
        self.assert_held("host_manifest_mismatch")

    def test_local_disabled_and_project_selections_are_preserved(self):
        for changes in ({"marketplace": "ihav-agent-room-local"}, {"enabled": False},
                        {"scope": "project"}, {"scope": "managed"}):
            with self.subTest(changes=changes):
                old = deepcopy(self.installed)
                self.installed.update(changes)
                self.assert_held("preserve_existing_selection")
                self.installed = old

    def test_downgrade_and_same_version_different_payload_are_held(self):
        self.installed["version"] = "1.2.0"
        self.assert_held("downgrade_requires_owner")
        self.installed["version"] = "1.1.0"
        self.assert_held("version_identity_collision")

    def test_exact_existing_payload_is_current_but_not_reported_loaded(self):
        self.installed.update(source=deepcopy(self.source), version="1.1.0", payload_sha256="c" * 64)
        plan = self.plan()
        self.assertEqual((plan["state"], plan["loaded"]), ("current", False))
        self.assertNotIn("commands", plan)

    def test_missing_rollback_inventory_is_held(self):
        self.installed.pop("rollback")
        self.assert_held("rollback_inventory_missing")

    def test_routine_canary_has_a_completed_hour_at_the_exact_pin(self):
        for changes, reason in (({"sha": "4" * 40}, "canary_not_verified"),
                                ({"passed": False}, "canary_not_verified"),
                                ({"verified_at": 3699}, "canary_hour_not_verified"),
                                ({"verified_at": 4001}, "canary_hour_not_verified"),
                                ({"started_at": True}, "canary_hour_not_verified")):
            with self.subTest(changes=changes):
                old = deepcopy(self.qualified["canary"])
                self.qualified["canary"].update(changes)
                self.assert_held(reason)
                self.qualified["canary"] = old

    def test_daily_cadence_allows_the_same_batch_but_holds_a_different_catalog_head(self):
        self.policy["last_activation"] = {"head": "5" * 40, "at": 3900}
        self.assert_held("daily_batch_hold")
        self.policy["last_activation"]["head"] = self.snapshot["head"]
        self.assertEqual(self.plan()["state"], "eligible")
        self.policy["last_activation"] = {"head": "5" * 40, "at": 0}
        self.assertEqual(self.plan(now=90000)["state"], "eligible")

    def test_security_or_blocking_classification_only_skips_cadence_not_other_gates(self):
        for kind in ("security", "blocking"):
            with self.subTest(kind=kind):
                self.qualified["release_kind"] = kind
                self.qualified.pop("canary", None)
                self.policy["last_activation"] = {"head": "5" * 40, "at": 3999}
                self.assertEqual(self.plan()["state"], "eligible")
                self.qualification["catalog_check"]["passed"] = False
                self.assert_held("catalog_check_not_verified")
                self.qualification["catalog_check"]["passed"] = True

    def test_malformed_or_future_activation_history_is_held(self):
        for value in (False, {}, {"head": "5" * 40, "at": float("nan")}, {"head": "5" * 40, "at": 4001}):
            with self.subTest(value=value):
                self.policy["last_activation"] = value
                self.assert_held("activation_history_unverified")


class UpdateJournalTests(UpdateFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="qualified catalog update ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "space"
        self.space = GlobalSpace(self.root)
        self.space.connect().close()
        self.journal = UpdateJournal(self.space)
        self.eligible = self.plan()
        self.observed = {key: self.eligible[key] for key in ("host", "selector", "source", "version", "payload_sha256")}

    def records(self):
        db = self.space.connect()
        try:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM meta WHERE key LIKE ?", (PREFIX + "%",))}
        finally:
            db.close()

    def test_two_supervisors_reserve_one_durable_intent(self):
        def begin(_):
            return UpdateJournal(GlobalSpace(self.root)).begin(self.eligible, now=4000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(begin, range(2)))
        self.assertEqual(sum(result["started"] for result in results), 1)
        self.assertEqual(len([key for key in self.records() if key != ACTIVE_KEY]), 1)

    def test_restart_keeps_the_intent_hold_without_another_effect(self):
        first = self.journal.begin(self.eligible, now=4000)
        stored = self.records()[PREFIX + first["attempt"]]
        self.assertEqual(stored["state"], "intent")
        with patch("subprocess.run", side_effect=AssertionError("journal cannot dispatch")):
            after = UpdateJournal(GlobalSpace(self.root)).begin(self.eligible, now=4010)
        self.assertFalse(after["started"])
        self.assertEqual(after["reason"], "attempt_requires_reconciliation")
        self.assertEqual(self.records()[PREFIX + first["attempt"]], stored)

    def test_a_new_python_process_reads_the_committed_intent_and_does_not_reserve_again(self):
        first = self.journal.begin(self.eligible, now=4000)
        script = ("import json,sys; from ihav_agent_room.catalogupdate import UpdateJournal; "
                  "from ihav_agent_room.globalspace import GlobalSpace; "
                  "print(json.dumps(UpdateJournal(GlobalSpace(sys.argv[1])).begin(json.loads(sys.argv[2]), now=4010)))")
        child = subprocess.run([sys.executable, "-c", script, str(self.root), json.dumps(self.eligible)],
                               cwd=Path(__file__).resolve().parent.parent,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                               capture_output=True, text=True, timeout=10, check=True)
        result = json.loads(child.stdout)
        self.assertEqual((result["started"], result["attempt"], result["reason"]),
                         (False, first["attempt"], "attempt_requires_reconciliation"))

    def test_failure_between_intent_and_active_reservation_rolls_back_both(self):
        db = self.space.connect()
        try:
            db.execute("CREATE TRIGGER reject_active BEFORE INSERT ON meta WHEN NEW.key='" + ACTIVE_KEY + "' "
                       "BEGIN SELECT RAISE(ABORT, 'injected active write failure'); END")
        finally:
            db.close()
        with self.assertRaisesRegex(Exception, "injected active write failure"):
            self.journal.begin(self.eligible, now=4000)
        self.assertEqual(self.records(), {})

    def test_unknown_or_nonzero_native_results_never_clear_the_hold_or_replay(self):
        for exit_code in (None, 1, 9, False):
            with self.subTest(exit_code=exit_code):
                isolated = GlobalSpace(Path(self.temp.name) / ("case-" + str(exit_code)))
                journal = UpdateJournal(isolated)
                attempt = journal.begin(self.eligible, now=4000)
                outcome = journal.finish(attempt["attempt"], attempt["token"], exit_code=exit_code, observed=self.observed, now=4010)
                self.assertEqual((outcome["state"], outcome["loaded"]), ("unknown", False))
                self.assertFalse(UpdateJournal(isolated).begin(self.eligible, now=5000)["started"])
                other = deepcopy(self.eligible)
                other.update(payload_sha256="6" * 64)
                unsigned = dict(other)
                unsigned.pop("digest")
                other["digest"] = checksum(unsigned)
                self.assertEqual(journal.begin(other, now=5000)["reason"], "active_attempt_requires_reconciliation")

    def test_zero_exit_without_matching_postimage_is_unknown(self):
        attempt = self.journal.begin(self.eligible, now=4000)
        observed = dict(self.observed, payload_sha256="7" * 64)
        outcome = self.journal.finish(attempt["attempt"], attempt["token"], exit_code=0, observed=observed, now=4010)
        self.assertEqual(outcome["state"], "unknown")
        self.assertIn(ACTIVE_KEY, self.records())

    def test_verified_postimage_releases_the_reservation_without_claiming_session_loading(self):
        attempt = self.journal.begin(self.eligible, now=4000)
        outcome = self.journal.finish(attempt["attempt"], attempt["token"], exit_code=0, observed=self.observed, now=4010)
        self.assertEqual((outcome["state"], outcome["loaded"]), ("installed", False))
        self.assertNotIn(ACTIVE_KEY, self.records())
        self.assertEqual(self.records()[LAST_ACTIVATION_KEY], {"head": self.snapshot["head"], "at": 4010})
        result = UpdateJournal(GlobalSpace(self.root)).begin(self.eligible, now=5000)
        self.assertEqual((result["started"], result["reason"]), (False, "matching_installed_attempt"))

    def test_same_payload_in_a_new_catalog_head_does_not_get_a_second_attempt(self):
        attempt = self.journal.begin(self.eligible, now=4000)
        self.journal.finish(attempt["attempt"], attempt["token"], exit_code=0, observed=self.observed, now=4010)
        self.snapshot["head"] = "8" * 40
        self.qualification["catalog_head"] = self.snapshot["head"]
        result = self.journal.begin(self.plan(now=90000), now=90000)
        self.assertEqual((result["started"], result["reason"]), (False, "matching_installed_attempt"))

    def test_shared_activation_history_closes_a_race_between_independent_planners(self):
        attempt = self.journal.begin(self.eligible, now=4000)
        self.journal.finish(attempt["attempt"], attempt["token"], exit_code=0, observed=self.observed, now=4010)
        self.snapshot["head"] = "8" * 40
        self.qualification["catalog_head"] = self.snapshot["head"]
        self.qualified["payload_sha256"] = "9" * 64
        next_plan = self.plan(now=4020)
        self.assertEqual(next_plan["state"], "eligible")  # Planner did not have the fresh journal row.
        held = self.journal.begin(next_plan, now=4020)
        self.assertEqual((held["started"], held["reason"]), (False, "daily_batch_hold"))

    def test_tampered_plan_or_wrong_token_cannot_change_the_record(self):
        tampered = deepcopy(self.eligible)
        tampered["commands"][0].append("--yes")
        with self.assertRaises(RoomError):
            self.journal.begin(tampered, now=4000)
        self.assertEqual(self.records(), {})
        attempt = self.journal.begin(self.eligible, now=4000)
        before = self.records()
        with self.assertRaises(RoomError):
            self.journal.finish(attempt["attempt"], "wrong", exit_code=0, observed=self.observed, now=4010)
        self.assertEqual(self.records(), before)

    def test_corrupt_shared_hold_fails_closed_without_new_attempts(self):
        db = self.space.connect()
        try:
            db.execute("INSERT INTO meta VALUES (?, ?)", (ACTIVE_KEY, "not json"))
        finally:
            db.close()
        with self.assertRaises(RoomError):
            self.journal.begin(self.eligible, now=4000)
        db = self.space.connect()
        try:
            rows = list(db.execute("SELECT key,value FROM meta WHERE key LIKE ?", (PREFIX + "%",)))
            self.assertEqual([(row[0], row[1]) for row in rows], [(ACTIVE_KEY, "not json")])
        finally:
            db.close()

    def test_clock_regression_cannot_reserve_or_finish_an_intent(self):
        with self.assertRaises(RoomError):
            self.journal.begin(self.eligible, now=3999)
        self.assertEqual(self.records(), {})
        attempt = self.journal.begin(self.eligible, now=4000)
        before = self.records()
        with self.assertRaises(RoomError):
            self.journal.finish(attempt["attempt"], attempt["token"], exit_code=0, observed=self.observed, now=3999)
        self.assertEqual(self.records(), before)


if __name__ == "__main__":
    unittest.main()
