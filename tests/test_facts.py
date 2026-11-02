"""Facts that change: the value now first, what it was before beside it.

The model records a fact with `about` and `attribute`; a later value of the same fact replaces
the earlier one in recall without the model knowing the earlier memory's id, and the order is
when each became true, so a fact recorded late from an old conversation stays history.
"""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from turnbook.local_brain import LocalBrainStore
from turnbook.mcp_server import build_server, wait_for_index


class FactTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_newest_value_answers_and_the_old_ones_stay_as_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "memory.sqlite"
            async with Client(build_server(db)) as client:
                for when, text in [("2024-03-01", "I live in Busan"),
                                   ("2026-09-20", "We moved to Seoul, Mapo"),
                                   ("2025-05-10", "Moved to Daejeon for work")]:   # told late
                    await client.call_tool("brain_remember", {
                        "content": text, "about": "me", "attribute": "home_city",
                        "effective_at": when + "T09:00:00"})
                await client.call_tool("brain_remember", {"content": "I had ramen in Mapo",
                                                          "effective_at": "2026-10-01T12:00:00"})
                packet = (await client.call_tool("brain_context", {
                    "query": "where do I live now? me home city"})).structured_content
            wait_for_index()
            with LocalBrainStore(db) as store:
                now = store.recall("me", as_of="2099-01-01T00:00:00")["matches"]
                then = store.recall("me", as_of="2025-12-31T00:00:00",
                                    knowledge_cutoff="2099-01-01T00:00:00")["matches"]
        homes = [m for m in now if m.get("earlier") is not None]
        self.assertEqual(len(homes), 1)                              # one fact, not three
        self.assertEqual(homes[0]["content"], "We moved to Seoul, Mapo")
        self.assertEqual([e["content"] for e in homes[0]["earlier"]],
                         ["Moved to Daejeon for work", "I live in Busan"])
        self.assertIn("Moved to Daejeon for work", [m["content"] for m in then])   # as of then
        self.assertIn("before: 2025-05-10 Moved to Daejeon", packet["context"])


if __name__ == "__main__":
    unittest.main()
