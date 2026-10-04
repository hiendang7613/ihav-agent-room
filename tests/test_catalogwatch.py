"""Catalog changes reach every room through the agents space (plan N-ac8dc5ab, admin I1.a and R1.a)."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ihav_agent_room.catalogwatch import check_catalogs, describe_change, read_catalog, take_lease
from ihav_agent_room.common import RoomError
from ihav_agent_room.globalspace import GlobalSpace


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

    def test_reads_a_real_local_catalog_repository(self):
        repo = Path(self.temp.name) / "catalog"
        (repo / ".claude-plugin").mkdir(parents=True)
        (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"name": "ihav", "plugins": [{"name": "p", "source": {"source": "url", "url": "u", "ref": "v1", "sha": "abc"}}]}))
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        head, plugins = read_catalog(repo.as_uri())
        self.assertEqual((len(head), plugins), (40, {"p": ["v1", "abc"]}))


if __name__ == "__main__":
    unittest.main()
