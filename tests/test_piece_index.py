"""Meaning search keeps its pieces in memory between questions: turns added later are searched,
and a forgotten turn is never returned again."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from turnbook import mcp_server as server
from turnbook.conversation_memory import ConversationMemoryStore

TOPICS = ("dog", "cat", "beach", "food")


class Words:
    """A stand-in embedder: one dimension per topic word."""
    WAIT_FOR_MODEL = None

    def __init__(self):
        self.cache = {}

    def _key(self, text):
        return text

    def embed(self, texts):
        for text in texts:
            self.cache.setdefault(text, [float(word in text) + 0.01 for word in TOPICS])
        return [self.cache[text] for text in texts]


def turn(i, text):
    when = f"2026-10-08T10:{i:02d}:00"
    return {"turn_id": f"t{i}", "client": "codex", "client_session_id": "s", "scope": "global",
            "role": "user", "content_redacted": text, "raw_sha256": f"t{i}", "source_path": "p",
            "happened_at": when, "known_at": when, "source_ordinal": i}


class PieceIndexTests(unittest.TestCase):
    def test_new_turns_are_searched_and_forgotten_ones_are_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            db, words = Path(directory) / "brain.sqlite", Words()
            ask = lambda: [h["turn_id"] for h in  # noqa: E731
                           server.semantic_turns(db, "dog", embedder=words, limit=5)]
            with ConversationMemoryStore(db) as store:
                store.record(turn(0, "my dog at the park"))
                store.record(turn(1, "cat food prices"))
            self.assertEqual(ask()[0], "t0")
            with ConversationMemoryStore(db) as store:
                store.record(turn(2, "dog on the beach"))
            self.assertIn("t2", ask())
            with ConversationMemoryStore(db) as store, store.db:
                store.db.execute("DELETE FROM conversation_turns WHERE turn_id='t0'")
            self.assertNotIn("t0", ask())
            self.assertIn("t2", ask())

    def test_an_idle_server_lets_go_of_its_index_and_reads_it_back(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            db, words = Path(directory) / "brain.sqlite", Words()
            with ConversationMemoryStore(db) as store:
                store.record(turn(1, "my dog is called Bori"))

            def ask():
                return [t["turn_id"] for t in server.semantic_turns(db, "dog", embedder=words)]

            self.assertEqual(ask(), ["t1"])
            self.assertFalse(server.trim_if_idle(), "just used: kept")
            self.assertTrue(server.trim_if_idle(time.monotonic() + server.IDLE_TRIM + 1))
            self.assertEqual(server._PIECES, {})
            self.assertEqual(ask(), ["t1"], "the next question reads the index back")


if __name__ == "__main__":
    unittest.main()
