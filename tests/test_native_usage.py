"""Provider counter capture is observation, not task completion or permission."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from ihav_agent_room.common import RoomError
from ihav_agent_room.native import codex_usage_snapshot
from ihav_agent_room.runtime import Supervisor
from ihav_agent_room.store import Store


def packet():
    counts = dict(inputTokens=100, cachedInputTokens=80, outputTokens=10,
                  reasoningOutputTokens=4, totalTokens=110)
    return dict(threadId="synthetic-thread", turnId="synthetic-turn",
                tokenUsage=dict(total=counts, last=dict(counts)))


class NativeUsageTests(unittest.TestCase):
    def test_preserves_counter_scopes_without_double_adding_reasoning_or_cache(self):
        params = packet()
        params["tokenUsage"]["last"]["inputTokens"] = 50
        result = codex_usage_snapshot(params, "synthetic-thread")
        self.assertEqual(result["token_usage"]["total"]["totalTokens"], 110)
        self.assertEqual(result["token_usage"]["last"]["inputTokens"], 50)
        self.assertNotIn("complete_event_observed", result)

    def test_untrusted_extra_text_is_not_logged_and_input_is_not_mutated(self):
        params = packet()
        params["prompt"] = "synthetic sensitive text"
        params["tokenUsage"]["total"]["unknown"] = "synthetic sensitive text"
        original = copy.deepcopy(params)
        result = codex_usage_snapshot(params, "synthetic-thread")
        self.assertNotIn("synthetic sensitive text", json.dumps(result))
        self.assertEqual(original, params)

    def test_optional_cache_write_is_preserved_only_when_observed(self):
        params = packet()
        self.assertNotIn("cacheWriteInputTokens", codex_usage_snapshot(params, "synthetic-thread")["token_usage"]["total"])
        params["tokenUsage"]["total"]["cacheWriteInputTokens"] = 3
        self.assertEqual(codex_usage_snapshot(params, "synthetic-thread")["token_usage"]["total"]["cacheWriteInputTokens"], 3)

    def test_wrong_identity_and_malformed_counters_refuse(self):
        invalid = [None, dict(packet(), threadId="other"), dict(packet(), turnId=None)]
        for value in (-1, True, 1.5, None, "100"):
            params = packet()
            params["tokenUsage"]["total"]["inputTokens"] = value
            invalid.append(params)
        params = packet()
        del params["tokenUsage"]["last"]["outputTokens"]
        invalid.append(params)
        for params in invalid:
            with self.subTest(params=params), self.assertRaises(RoomError):
                codex_usage_snapshot(params, "synthetic-thread")

    def test_supervisor_persists_usage_and_invalidity_without_state_or_message_effect(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as root:
                store = Store(Path(root))
                store.initialize("pair")
                store.member("CODEX_01", {"status": "working"})
                supervisor = Supervisor(store, "synthetic-generation")
                queue = asyncio.Queue()
                for params in (packet(), packet(), dict(packet(), threadId="foreign-thread")):
                    queue.put_nowait(dict(method="thread/tokenUsage/updated", params=params))
                supervisor.codex["CODEX_01"] = SimpleNamespace(events=queue,
                    thread_id="synthetic-thread", turn_id="synthetic-turn", process=SimpleNamespace(returncode=None))
                await supervisor.native_events()
                with store.tx() as db:
                    rows = db.execute("SELECT kind,data FROM events WHERE kind LIKE 'native.usage%' ORDER BY seq").fetchall()
                    self.assertEqual([r["kind"] for r in rows], ["native.usage", "native.usage", "native.usage_invalid"])
                    usage = json.loads(rows[0]["data"])
                    self.assertEqual(usage["thread"], "synthetic-thread")
                    self.assertEqual(usage["member"], "CODEX_01")
                    self.assertEqual(usage["generation"], "synthetic-generation")
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)
                self.assertEqual(store.member("CODEX_01")["status"], "working")
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
