import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
import json
from pathlib import Path
import tempfile
import unittest

from turnbook.conversation_memory import ConversationMemoryStore, OpenCodeConversationCapture
from turnbook.inject import context


class OpenCodeTests(unittest.TestCase):
    def test_word_matches_stay_unchanged_while_the_model_is_loading(self):
        from unittest import mock
        import time
        from turnbook.inject import _standing_out

        memories = [{"memory_id": "dog", "title": "Kongi", "content": "golden retriever"}]
        embedder = mock.Mock(WAIT_FOR_MODEL=2.0)
        embedder.ready.return_value = False
        with mock.patch("turnbook.inject._all_memories", return_value=memories):
            began = time.monotonic()
            found = _standing_out("Kongi", memories, embedder, "unused", 0.36)
            elapsed = time.monotonic() - began
        self.assertIs(found, memories)
        embedder.ready.assert_called_once_with(0)
        embedder.embed.assert_not_called()
        self.assertLess(elapsed, 1.0)

    def test_memories_all_about_one_project_are_not_all_dropped(self):
        """Twelve memories all near the message: mean + 0.2 is above any similarity."""
        from unittest import mock
        from turnbook.inject import _standing_out
        memories = [{"memory_id": f"m{n}", "title": "kiosk", "content": str(n)}
                    for n in range(12)]
        embedder = mock.Mock(WAIT_FOR_MODEL=None)
        with mock.patch("turnbook.inject._all_memories", return_value=memories), \
                mock.patch("turnbook.mcp_server.memory_meaning",
                           lambda e, q, texts: [0.9] * len(texts)):
            self.assertEqual(len(_standing_out("kiosk", memories, embedder, "x", 0.36)), 12)

    def test_the_rest_is_every_newest_memory_not_those_a_word_names(self):
        from turnbook.local_brain import LocalBrainStore
        with tempfile.TemporaryDirectory() as directory:
            with LocalBrainStore(Path(directory) / "b.sqlite") as store:
                for n in range(15):
                    store.remember({"memory_id": f"m{n}", "scope": "global",
                                    "memory_type": "fact", "title": f"note {n}",
                                    "content": f"note {n}", "effective_at": "2026-01-01T00:00:00",
                                    "known_at": "2026-01-01T00:00:00", "actor": "user",
                                    "tags": ["memory"] if n == 0 else [], "entities": [],
                                    "provenance": {"source_type": "t", "source_ref": "t"},
                                    "supersedes_memory_id": None})
                self.assertEqual(len(store.newest(as_of="2026-02-01T00:00:00")), 15)
                store.remember({"memory_id": "city", "scope": "global", "memory_type": "fact",
                                "title": "city", "content": "Seoul",
                                "effective_at": "2026-01-02T00:00:00",
                                "known_at": "2026-01-02T00:00:00", "actor": "user", "tags": [],
                                "entities": [], "supersedes_memory_id": None,
                                "provenance": {"source_type": "t", "source_ref": "t"},
                                "attributes": {"fact": "me|city"}})
                newest = store.newest(as_of="2026-02-01T00:00:00", limit=5)
                self.assertEqual(newest[0]["memory_id"], "city")   # a fact keeps its place
                rule = {"scope": "global", "memory_type": "rule", "title": "r", "actor": "user",
                        "tags": [], "entities": [], "known_at": "2026-01-03T00:00:00",
                        "provenance": {"source_type": "t", "source_ref": "t"},
                        "attributes": {"fact": "me|reply_language"}}
                store.remember({**rule, "memory_id": "r1", "content": "영어로 답해",
                                "effective_at": "2026-01-03T00:00:00",
                                "supersedes_memory_id": None})
                store.remember({**rule, "memory_id": "r2", "content": "한국어로 답해",
                                "effective_at": "2025-01-01T00:00:00",   # dated earlier
                                "supersedes_memory_id": "r1"})
                self.assertEqual(store.rules(), ["한국어로 답해"])

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
        def memory(title):
            return {"memory_id": title, "title": title, "content": ""}
        found = [memory("price list"), memory("the answer")]
        seen = {}

        def pack(recalled, **_):
            seen.update(recalled)
            return {"status": "KNOWN", "context": "x"}

        def run(query, others, scores):
            with mock.patch("turnbook.mcp_server.gather", lambda *a, **k: {
                    "status": "KNOWN", "conversation_turns": list(turns),
                    "matches": list(found)}), \
                    mock.patch("turnbook.inject._all_memories", lambda database: others), \
                    mock.patch("turnbook.mcp_server.memory_meaning", lambda e, q, texts: None
                               if scores is None else [scores.get(t.strip(), 0.3)
                                                       for t in texts]), \
                    mock.patch("turnbook.context_packet.build_context_pack", pack), \
                    mock.patch("turnbook.local_embedder.LOCAL_MODEL",
                               "onnx-community/embeddinggemma-300m-ONNX@onnx/model_q4.onnx"):
                context(query, database="db", embedder=object())
            return [m["title"] for m in seen["matches"]]
        # no model yet: memories as found; turns: the floor, no repeats, words-only kept
        self.assertEqual(run("얼마나 걸리나", [], None), ["price list", "the answer"])
        self.assertEqual([t["turn_id"] for t in seen["conversation_turns"]], ["b", "d"])
        # among many memories, only one standing 0.2 above their mean (0.3 here) gets in, even
        # one found by shared words ("그러면", "이제") at 0.43
        many = [memory(f"other {i}") for i in range(12)]
        self.assertEqual(run("그러면 이제 2번 약점 고치자", many,
                             {"price list": 0.43, "the answer": 0.6}), ["the answer"])
        # with few memories there is no "rest": the floor (0.36)
        self.assertEqual(run("그러면 이제 2번 약점 고치자", [],
                             {"price list": 0.34, "the answer": 0.4}), ["the answer"])

    def test_a_message_with_no_sign_of_the_past_gets_nothing(self):
        self.assertEqual(context("write a quicksort in rust"), "")
        self.assertEqual(context("   "), "")


if __name__ == "__main__":
    unittest.main()
