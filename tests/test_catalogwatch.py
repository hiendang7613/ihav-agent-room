"""Catalog changes reach every room through the agents space (plan N-ac8dc5ab, admin I1.a and R1.a)."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ihav_agent_room.catalogwatch import check_catalogs, describe_change, read_catalog, read_catalog_snapshot, read_manifest, take_lease
from ihav_agent_room.common import RoomError
from ihav_agent_room.globalspace import GlobalSpace, MAX_BODY_BYTES


class CatalogWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="catalog watch ")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.space = GlobalSpace(base / "agents_space")
        self.space.connect().close()
        (self.space.root / "policy.json").write_text(json.dumps({"descriptions_only_prefixes": [], "catalogs": ["https://example.invalid/ihav.git"]}))
        self.heads = [("h1", {"a": ["v1", "s1"]}), ("h2", {"a": ["v2", "s2"], "b": ["v1", "t1"]}), ("h2", {})]
        self.calls = 0

    def read(self, url):
        self.calls += 1
        return self.heads.pop(0)

    def test_baseline_then_one_entry_per_catalog_commit(self):
        self.assertEqual(check_catalogs(self.space, now=0, read=self.read), [])  # First look records a baseline.
        posted = check_catalogs(self.space, now=700, read=self.read)
        entry = self.space.show(posted[0])
        self.assertEqual(entry["kind"], "release")
        self.assertIn("a v1 -> v2 (s2)", entry["body"])
        self.assertIn("added b v1 (t1)", entry["body"])
        self.assertEqual(check_catalogs(self.space, now=1400, read=self.read), [])  # Same head: nothing new.

    def test_only_one_supervisor_looks_per_interval(self):
        self.assertTrue(take_lease(self.space, now=0))
        self.assertFalse(take_lease(self.space, now=10))
        self.assertEqual(check_catalogs(self.space, now=20, read=self.read), [])
        self.assertEqual(self.calls, 0)
        self.assertTrue(take_lease(self.space, now=601))

    def test_failures_and_empty_policy_post_nothing(self):
        def broken(url):
            raise RoomError("network down", "native")
        self.assertEqual(check_catalogs(self.space, now=0, read=broken), [])
        (self.space.root / "policy.json").write_text(json.dumps({"descriptions_only_prefixes": []}))
        self.assertEqual(check_catalogs(self.space, now=10_000, read=self.read), [])
        self.assertEqual(self.calls, 0)

    def test_change_text(self):
        self.assertEqual(describe_change({"a": ["v1", "s1"], "c": ["v1", "x"]}, {"a": ["v1", "s1"], "b": ["main", None]}),
                         ["added b main (unpinned)", "removed c"])

    def test_source_metadata_is_retained_without_installing_or_announcing_a_baseline(self):
        source = {"source": "git-subdir", "url": "https://example.invalid/plugin.git",
                  "path": "plugins/p", "ref": "v1.0.0", "sha": "a" * 40}
        snapshot = {"head": "b" * 40, "plugins": {"p": ["v1.0.0", "a" * 40]},
                    "catalogs": {"claude": {"name": "ihav", "plugins": [{"name": "p", "source": source}]},
                                 "codex": {"name": "ihav", "plugins": [{"name": "p", "source": dict(source)}]}}}
        self.assertEqual(check_catalogs(self.space, now=0, read=lambda _: snapshot), [])
        db = self.space.connect()
        try:
            row = db.execute("SELECT value FROM meta WHERE key=?",
                             ("catalog_seen:https://example.invalid/ihav.git",)).fetchone()
            self.assertIsNotNone(row)
            saved = json.loads(row[0])
            self.assertEqual(saved["catalogs"], snapshot["catalogs"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)
        finally:
            db.close()

    def test_same_head_can_gain_metadata_without_another_notice(self):
        check_catalogs(self.space, now=0, read=lambda _: ("h1", {"p": ["v1", "s1"]}))
        snapshot = {"head": "h1", "plugins": {"p": ["v1", "s1"]},
                    "catalogs": {"claude": {"name": "ihav", "plugins": []}, "codex": None}}
        self.assertEqual(check_catalogs(self.space, now=700, read=lambda _: snapshot), [])
        db = self.space.connect()
        try:
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key=?",
                                         ("catalog_seen:https://example.invalid/ihav.git",)).fetchone()[0])
            self.assertEqual(saved["catalogs"], snapshot["catalogs"])
        finally:
            db.close()

    def test_reads_a_real_local_catalog_repository(self):
        repo = Path(self.temp.name) / "catalog"
        (repo / ".claude-plugin").mkdir(parents=True)
        (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"name": "ihav", "plugins": [{"name": "p", "source": {"source": "url", "url": "u", "ref": "v1", "sha": "abc"}}]}))
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        head, plugins = read_catalog(repo.as_uri())
        self.assertEqual((len(head), plugins), (40, {"p": ["v1", "abc"]}))

    def test_snapshot_keeps_both_host_manifests_at_the_same_actual_commit(self):
        repo = Path(self.temp.name) / "two-host-catalog"
        source = {"source": "git-subdir", "url": "https://example.invalid/plugin.git",
                  "path": "plugins/p", "ref": "v1.0.0", "sha": "a" * 40}
        catalog = {"name": "ihav", "plugins": [{"name": "p", "source": source}]}
        for relative in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(catalog))
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        snapshot = read_catalog_snapshot(repo.as_uri())
        self.assertEqual(snapshot["url"], repo.as_uri())
        self.assertEqual(snapshot["catalogs"], {"claude": catalog, "codex": catalog})
        self.assertEqual(snapshot["plugins"], {"p": ["v1.0.0", "a" * 40]})

    def test_manifest_symlink_outside_checkout_is_not_read(self):
        repo = Path(self.temp.name) / "manifest-root"
        repo.mkdir()
        outside = Path(self.temp.name) / "private.json"
        outside.write_text(json.dumps({"plugins": []}))
        (repo / "manifest.json").symlink_to(outside)
        with self.assertRaises(RoomError):
            read_manifest(repo, "manifest.json")

    def test_suspended_watcher_cannot_overlap_an_expired_lease_and_regress_metadata(self):
        check_catalogs(self.space, now=0, read=lambda _: ("h0", {"p": ["v0", "s0"]}))
        started, release = threading.Event(), threading.Event()
        second_calls = []

        def slow_read(_):
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release its owned watcher")
            return "h1", {"p": ["v1", "s1"]}

        def later_read(_):
            second_calls.append(True)
            return "h2", {"p": ["v2", "s2"]}

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(check_catalogs, self.space, 700, slow_read)
            self.assertTrue(started.wait(5))
            try:
                second = check_catalogs(self.space, now=1400, read=later_read)
                self.assertEqual(second, [])
                self.assertEqual(second_calls, [])
            finally:
                release.set()
            self.assertEqual(len(first.result(timeout=5)), 1)
        self.assertEqual(len(check_catalogs(self.space, now=1400, read=later_read)), 1)
        db = self.space.connect()
        try:
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key=?",
                                         ("catalog_seen:https://example.invalid/ihav.git",)).fetchone()[0])
            bodies = [row[0] for row in db.execute("SELECT body FROM entries ORDER BY seq")]
            self.assertEqual(saved["head"], "h2")
            self.assertEqual(len(bodies), 2)
            self.assertTrue(all("v2 -> v1" not in body for body in bodies))
        finally:
            db.close()

    def test_large_diff_has_one_bounded_notice_and_keeps_the_full_snapshot(self):
        check_catalogs(self.space, now=0, read=lambda _: ("h1", {}))
        plugins = {"ihav-plugin-" + str(i): ["v1.0.0", "a" * 40] for i in range(500)}
        catalog = {"name": "ihav", "plugins": [{"name": name, "source": {"ref": pin[0], "sha": pin[1]}}
                                             for name, pin in plugins.items()]}
        snapshot = {"head": "h2", "plugins": plugins, "catalogs": {"claude": catalog, "codex": catalog}}
        posted = check_catalogs(self.space, now=700, read=lambda _: snapshot)
        self.assertEqual(len(posted), 1)
        body = self.space.show(posted[0])["body"]
        self.assertLessEqual(len(body.encode()), MAX_BODY_BYTES)
        self.assertIn("500 changes", body)
        self.assertIn("shortened", body)
        db = self.space.connect()
        try:
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key=?",
                                         ("catalog_seen:https://example.invalid/ihav.git",)).fetchone()[0])
            self.assertEqual(saved["catalogs"], snapshot["catalogs"])
            self.assertEqual(saved["head"], "h2")
        finally:
            db.close()

    def test_shortened_utf8_notice_preserves_explicit_missing_host_metadata(self):
        check_catalogs(self.space, now=0, read=lambda _: ("h1", {}))
        plugins = {"ihav-" + str(i) + "-đội": ["v1.0.0", "a" * 40] for i in range(500)}
        catalog = {"name": "ihav", "plugins": [{"name": name, "source": {"ref": pin[0], "sha": pin[1]}}
                                             for name, pin in plugins.items()]}
        snapshot = {"head": "h2", "plugins": plugins, "catalogs": {"claude": catalog, "codex": None}}
        posted = check_catalogs(self.space, now=700, read=lambda _: snapshot)
        body = self.space.show(posted[0])["body"]
        self.assertLessEqual(len(body.encode("utf-8")), MAX_BODY_BYTES)
        self.assertIn("500 changes", body)
        self.assertIn("shortened", body)
        self.assertIn("available metadata", body)
        self.assertNotIn("both catalogs", body)
        db = self.space.connect()
        try:
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key=?",
                                         ("catalog_seen:https://example.invalid/ihav.git",)).fetchone()[0])
            self.assertEqual(saved["catalogs"], snapshot["catalogs"])
            self.assertIsNone(saved["catalogs"]["codex"])
        finally:
            db.close()

    def test_committed_notice_is_recovered_after_snapshot_write_interruption(self):
        check_catalogs(self.space, now=0, read=lambda _: ("h1", {"p": ["v1", "s1"]}))
        committed_post = self.space.post

        def interrupt_after_commit(*args, **kwargs):
            committed_post(*args, **kwargs)
            raise RuntimeError("injected interruption after notice commit")

        with patch.object(self.space, "post", side_effect=interrupt_after_commit):
            with self.assertRaisesRegex(RuntimeError, "after notice commit"):
                check_catalogs(self.space, now=700, read=lambda _: ("h2", {"p": ["v2", "s2"]}))
        self.assertEqual(check_catalogs(self.space, now=1400,
                                       read=lambda _: ("h2", {"p": ["v2", "s2"]})), [])
        db = self.space.connect()
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key=?",
                                         ("catalog_seen:https://example.invalid/ihav.git",)).fetchone()[0])
            self.assertEqual(saved["head"], "h2")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
