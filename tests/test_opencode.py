import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
import json
from pathlib import Path
import tempfile
import unittest

from turnbook.conversation_memory import ConversationMemoryStore, OpenCodeConversationCapture
from turnbook.inject import context


class OpenCodeTests(unittest.TestCase):
    def test_what_the_plugin_writes_is_captured_once(self):
        """The plugin's transcript (Claude Code's record shape): both sides, stored once."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opencode"
            root.mkdir()
            records = [
                {"type": "user", "uuid": "m1", "sessionId": "ses_1", "cwd": directory,
                 "timestamp": "2026-10-16T05:00:00.000Z",
                 "message": {"content": "our golden retriever Kongi turned three"}},
                {"type": "assistant", "uuid": "m2", "sessionId": "ses_1", "cwd": directory,
                 "timestamp": "2026-10-16T05:00:09.000Z",
                 "message": {"content": "Happy birthday to Kongi!"}}]
            (root / "ses_1.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = OpenCodeConversationCapture(
                    root, store, state_path=Path(directory) / "cursor.json")
                capture.initialize_baseline()        # the service seeing the folder first
                capture.scan()
                capture.scan()                                   # nothing new: nothing twice
                rows = store.db.execute("SELECT client, role, content_redacted FROM "
                                        "conversation_turns ORDER BY happened_at").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("opencode", "user", "our golden retriever Kongi turned three"),
                          ("opencode", "assistant", "Happy birthday to Kongi!")])

    def test_a_finished_session_file_is_deleted_once_stored(self):
        """It holds what was typed before masking: gone once every line is stored and an hour
        passed; a session that goes on starts a new file, read from its start."""
        import os
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opencode"
            root.mkdir()
            path = root / "ses_1.jsonl"

            def say(uuid, text):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "user", "uuid": uuid, "sessionId": "ses_1",
                                             "cwd": directory, "message": {"content": text},
                                             "timestamp": "2026-10-16T05:00:00.000Z"}) + "\n")
            say("m1", "my password is Hunter2!xy")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = OpenCodeConversationCapture(
                    root, store, state_path=Path(directory) / "cursor.json")
                capture.initialize_baseline()
                capture.scan()
                self.assertTrue(path.exists())                     # just written: kept
                old = time.time() - 2 * capture.DONE_AFTER
                os.utime(path, (old, old))
                capture.scan()
                self.assertFalse(path.exists())
                say("m2", "the session goes on")
                capture.scan()
                said = [r[0] for r in store.db.execute(
                    "SELECT content_redacted FROM conversation_turns ORDER BY rowid")]
        self.assertEqual(said, ["my password is [REDACTED]", "the session goes on"])

    def test_the_hook_keeps_only_close_items_and_no_repeats(self):
        """research/hook_floor.py: under the floor is noise; a past message that repeats this
        one answers nothing; a turn found by its words (no similarity) stays."""
        from unittest import mock
        turns = [{"turn_id": "a", "role": "user", "content": "얼마나 걸려", "similarity": 0.66},
                 {"turn_id": "b", "role": "assistant", "content": "about an hour",
                  "similarity": 0.5},
                 {"turn_id": "c", "role": "user", "content": "kiosk price list",
                  "similarity": 0.3},
                 {"turn_id": "d", "role": "user", "content": "found by words"}]
        memories = [{"title": "close", "similarity": 0.4}, {"title": "far", "similarity": 0.2},
                    {"title": "by words", "score": 140}]
        seen = {}

        def pack(recalled, **_):
            seen.update(recalled)
            return {"status": "KNOWN", "context": "x"}
        with mock.patch("turnbook.mcp_server.gather", lambda *a, **k: {
                "status": "KNOWN", "conversation_turns": list(turns), "matches": list(memories)}), \
                mock.patch("turnbook.context_packet.build_context_pack", pack), \
                mock.patch("turnbook.local_embedder.LOCAL_MODEL",
                           "onnx-community/embeddinggemma-300m-ONNX@onnx/model_q4.onnx"):
            self.assertTrue(context("얼마나 걸리나", database="db", embedder=object()))
        self.assertEqual([t["turn_id"] for t in seen["conversation_turns"]], ["b", "d"])
        self.assertEqual([m["title"] for m in seen["matches"]], ["close", "by words"])

    def test_a_message_with_no_sign_of_the_past_gets_nothing(self):
        self.assertEqual(context("write a quicksort in rust"), "")
        self.assertEqual(context("   "), "")


if __name__ == "__main__":
    unittest.main()
