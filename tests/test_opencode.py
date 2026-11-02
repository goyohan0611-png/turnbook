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

    def test_a_message_with_no_sign_of_the_past_gets_nothing(self):
        self.assertEqual(context("write a quicksort in rust"), "")
        self.assertEqual(context("   "), "")


if __name__ == "__main__":
    unittest.main()
