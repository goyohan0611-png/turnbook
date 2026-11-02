"""Opening the conversation around a cited turn.

LongMemEval 1903aded: the packet held the user's "brainstorm work-from-home jobs for seniors",
but not the assistant's list right after it, where item 7 was the answer (Transcriptionist).
The model said it did not know. brain_recall(source=...) hands it that exchange.
"""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import asyncio
import re
import tempfile
import unittest

from mcp import Client

from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.mcp_server import build_server, wait_for_index

TALK = [("user", "Morning! Can you help me plan my week?"),
        ("assistant", "Sure. Start with the three things that must happen."),
        ("user", "Brainstorm work from home jobs for seniors, my dad wants something part time."),
        ("assistant", "Here are ideas: 1. Online tutor 2. Virtual assistant 3. Bookkeeper "
                      "4. Customer support 5. Proofreader 6. Online surveys "
                      "7. Transcriptionist 8. Craft seller"),
        ("user", "Thanks, I'll show him the list."),
        ("assistant", "Good luck to him!"),
        ("user", "Unrelated: what's a good pasta recipe?")]


class OpenSourceTests(unittest.TestCase):
    def test_the_reply_next_to_a_cited_request_can_be_read(self):
        async def run(db):
            async with Client(build_server(db)) as client:
                packet = (await client.call_tool("brain_context", {
                    "query": "work from home jobs for seniors my dad"})).structured_content
                found = re.search(r"Source: claude-code / (\S+:2)\b", packet["context"])
                self.assertIsNotNone(found, packet["context"])
                opened = (await client.call_tool("brain_recall", {
                    "source": found.group(1), "limit": 2})).structured_content
                bad = (await client.call_tool("brain_recall", {"source": "no-number"})
                       ).structured_content
                scoped = (await client.call_tool("brain_recall", {
                    "source": found.group(1), "limit": 2, "scopes": ["global"]})
                    ).structured_content
            return opened, bad, scoped

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                for i, (role, text) in enumerate(TALK):
                    when = f"2026-10-01T09:0{i}:00"
                    store.record({"turn_id": f"t{i}", "client": "claude-code",
                                  "client_session_id": "s", "role": role,
                                  "scope": "private" if i == 4 else "global",
                                  "content_redacted": text, "raw_sha256": f"t{i}",
                                  "source_path": "C:/talk.jsonl", "happened_at": when,
                                  "known_at": when, "source_ordinal": i})
            wait_for_index()
            opened, bad, scoped = asyncio.run(run(db))
        self.assertEqual([t["turn_id"] for t in opened["turns"]], ["t0", "t1", "t2", "t3", "t4"])
        self.assertEqual([t["turn_id"] for t in opened["turns"] if t["here"]], ["t2"])
        self.assertIn("Transcriptionist", opened["turns"][3]["content"])
        self.assertEqual(bad["status"], "REFUSED")
        self.assertEqual([t["turn_id"] for t in scoped["turns"]], ["t0", "t1", "t2", "t3"])


if __name__ == "__main__":
    unittest.main()
