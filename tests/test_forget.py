"""Forget a phrase on one PC: gone from its store and indexes, from the shared log, and from the
other PC after its next sync, and the phrase itself is never written anywhere to make it so."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from turnbook import sharing
from turnbook.associative_memory import AssociativeMemoryIndex
from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.forget import find, forget
from turnbook.local_brain import LocalBrainStore

SECRET = "hunter2-correct-horse"
T = "2026-10-08T10:00:00"


def turn(n, text):
    return {"turn_id": f"t{n}", "client": "claude-code", "client_session_id": "s",
            "scope": "global",
            "role": "user", "content_redacted": text, "raw_sha256": f"h{n}", "source_path": "p",
            "happened_at": T, "known_at": T, "source_ordinal": n}


def memory(mid, text):
    return {"memory_id": mid, "scope": "global", "memory_type": "fact", "title": text[:40],
            "content": text, "effective_at": T, "known_at": T, "actor": "assistant", "tags": [],
            "entities": [], "provenance": {"source_type": "test", "source_ref": "forget"},
            "supersedes_memory_id": None}


def words(index, word):
    with ConversationMemoryStore(index) as store:
        return [t["turn_id"] for t in store.search(word, limit=10)["turns"]]


class ForgetTests(unittest.TestCase):
    def test_forgotten_everywhere_and_the_phrase_never_shared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared, desk, lap = root / "Shared", root / "desk.sqlite", root / "lap.sqlite"
            turns = [turn(1, "the build is green again"),
                     turn(2, f"my wifi password is {SECRET} by the way"),
                     turn(3, "lunch was kimchi stew")]
            secret_memory = memory("m-secret", f"router login {SECRET}")
            try:
                os.environ["TURNBOOK_DEVICE"] = "desk"
                with ConversationMemoryStore(desk) as store:
                    for item in turns:
                        store.record(item)
                with LocalBrainStore(desk) as store:
                    store.remember(secret_memory)
                sharing.append_many(shared, [("turn", t) for t in turns]
                                      + [("memory", secret_memory)])
                with AssociativeMemoryIndex(desk) as index:
                    index.sync_sources()
                os.environ["TURNBOOK_DEVICE"] = "lap"
                sharing.sync(shared, lap)
                self.assertEqual(words(lap, "password"), ["t2"])

                os.environ["TURNBOOK_DEVICE"] = "desk"
                result = forget(SECRET, desk, shared)
                self.assertEqual((result["turns"], result["memories"]), (1, 1))
                self.assertEqual(find(desk, SECRET), {"turn_ids": [], "memory_ids": []})
                self.assertEqual(words(desk, "password"), [], "the word index forgot it too")
                self.assertEqual(words(desk, "kimchi"), ["t3"], "nothing else went")
                db = sqlite3.connect(desk)
                engrams = db.execute("SELECT count(*) FROM associative_engrams "
                                     "WHERE source_id IN ('t2','m-secret')").fetchone()[0]
                db.close()
                self.assertEqual(engrams, 0)
                for log in (shared / "log").glob("*.jsonl"):
                    self.assertNotIn(SECRET, log.read_text(encoding="utf-8"))

                os.environ["TURNBOOK_DEVICE"] = "lap"
                sharing.sync(shared, lap)               # the other PC follows on its next sync
                self.assertEqual(find(lap, SECRET), {"turn_ids": [], "memory_ids": []})
                self.assertEqual(words(lap, "password"), [])
                self.assertEqual(sorted(words(lap, "build") + words(lap, "kimchi")), ["t1", "t3"])
            finally:
                os.environ.pop("TURNBOOK_DEVICE", None)

    def test_forgotten_from_a_postgres_hub(self):
        import psycopg
        from test_shared_brain import plain_directory, scratch_postgres
        with plain_directory() as directory, scratch_postgres(Path(directory), 54394) as hub:
            root = Path(directory)
            desk, lap = root / "desk.sqlite", root / "lap.sqlite"
            try:
                os.environ["TURNBOOK_DEVICE"] = "desk"
                items = [turn(1, "the build is green again"), turn(2, f"password {SECRET}")]
                with ConversationMemoryStore(desk) as store:
                    for item in items:
                        store.record(item)
                sharing.append_many(hub, [("turn", t) for t in items])
                os.environ["TURNBOOK_DEVICE"] = "lap"
                sharing.sync(hub, lap)
                os.environ["TURNBOOK_DEVICE"] = "desk"
                forget(SECRET, desk, hub)
                with psycopg.connect(hub) as connection:
                    lines = [row[0] for row in connection.execute("SELECT line FROM sharing")]
                self.assertFalse(any(SECRET in line for line in lines))
                self.assertTrue(any('"kind": "forget"' in line for line in lines))
                os.environ["TURNBOOK_DEVICE"] = "lap"
                sharing.sync(hub, lap)
                self.assertEqual(find(lap, SECRET), {"turn_ids": [], "memory_ids": []})
                self.assertEqual(words(lap, "build"), ["t1"])
            finally:
                os.environ.pop("TURNBOOK_DEVICE", None)

    def test_the_forgotten_text_leaves_the_file_itself(self):
        """Not just the rows: SQLite only marks deleted space free and FTS5 keeps a deleted
        word until a merge, so the phrase was still in the file's bytes."""
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "m.sqlite"
            with ConversationMemoryStore(index) as store:
                for n in range(300):
                    store.record(turn(n, f"wifi is {SECRET} here" if n == 7 else
                                      f"ordinary turn {n} " * 20))
            with AssociativeMemoryIndex(index) as associative:
                associative.sync_sources(limit=1000)
            forget(SECRET, index)
            left = sum(path.read_bytes().count(SECRET.encode()) for path
                       in Path(directory).iterdir() if path.name.startswith("m.sqlite"))
            self.assertEqual(left, 0)

    def test_short_phrases_are_refused(self):
        from turnbook import forget as module
        with self.assertRaises(SystemExit):
            module.main(["abc", "--yes"])


class PromotedCopyTests(unittest.TestCase):
    def test_forgetting_a_turn_also_forgets_the_memory_promoted_from_it(self):
        """"Remember ..." turns are copied into a memory word for word: erasing the turn by id
        left that copy behind."""
        from turnbook.forget import erase
        from turnbook.semantic_memory import SemanticMemoryIngestion
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(index) as store:
                store.record(turn(1, "기억해줘: 사물함 비밀 위치는 북쪽 계단 아래야"))
            with SemanticMemoryIngestion(index) as ingestion:
                self.assertEqual(ingestion.process_new(limit=10)["promoted_count"], 1)
            self.assertTrue(find(index, "북쪽 계단")["memory_ids"])
            erase(index, {"turn_ids": find(index, "북쪽 계단")["turn_ids"]})
            self.assertEqual(find(index, "북쪽 계단"), {"turn_ids": [], "memory_ids": []})


class OutboxTests(unittest.TestCase):
    def test_a_promoted_memory_leaves_the_outbox_with_its_turn(self):
        """forget_ids erased the memory promoted from a turn here, but kept sending it."""
        import json
        from turnbook.forget import forget_ids
        from turnbook.semantic_memory import SemanticMemoryIngestion
        from turnbook.sharing import _outbox
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(index) as store:
                store.record(turn(1, "기억해줘: 금고 비밀번호 힌트는 고양이 이름이야"))
            with SemanticMemoryIngestion(index) as ingestion:
                ingestion.process_new(limit=10)
            promoted = find(index, "고양이 이름")["memory_ids"]
            self.assertTrue(promoted)
            outbox = _outbox()
            outbox.parent.mkdir(parents=True, exist_ok=True)
            outbox.write_text(json.dumps({"kind": "memory", "record": {"memory_id": promoted[0]}},
                                         sort_keys=True) + "\n", encoding="utf-8")
            try:
                forget_ids({"turn_ids": ["t1"]}, index)
                left = outbox.read_text(encoding="utf-8")
            finally:
                outbox.unlink()
        self.assertNotIn(promoted[0], left)


class StagingTests(unittest.TestCase):
    def test_forgetting_blanks_the_hooks_own_copy(self):
        """A hook keeps what was typed, unmasked, until its session has been idle an hour."""
        import json
        from turnbook.forget import forget_ids
        from turnbook.paths import turnbook_home
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(index) as store:
                store.record(turn(1, "the vault door code is purple-otter-42"))
            staged = turnbook_home() / "hooks" / "gemini-cli" / "s.jsonl"
            staged.parent.mkdir(parents=True, exist_ok=True)
            keep = json.dumps({"type": "user", "message": {"content": "hello"}})
            gone = json.dumps({"type": "user",
                               "message": {"content": "the vault door code is purple-otter-42"}})
            staged.write_text(keep + "\n" + gone + "\n", encoding="utf-8")
            try:
                result = forget_ids({"turn_ids": ["t1"]}, index)
                text = staged.read_text(encoding="utf-8")
            finally:
                staged.unlink()
        self.assertEqual(result["staging_lines_removed"], 1)
        self.assertNotIn("purple-otter", text)
        self.assertIn("hello", text)


class TombstoneTests(unittest.TestCase):
    def test_a_forgotten_turn_is_not_brought_back_by_a_later_import(self):
        """Another PC's log, an outbox sent late or a transcript read again carried the same turn
        back in after it was forgotten."""
        from turnbook.forget import forget_ids
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(index) as store:
                store.record(turn(1, "my locker code is 4821"))
            with LocalBrainStore(index) as brain:
                brain.remember(memory("m1", "locker code note 4821"))
            forget_ids({"turn_ids": ["t1"], "memory_ids": ["m1"]}, index)
            with ConversationMemoryStore(index) as store:
                self.assertEqual(store.record(turn(1, "my locker code is 4821"))["disposition"],
                                 "FORGOTTEN")
            with LocalBrainStore(index) as brain:
                self.assertEqual(brain.remember(memory("m1", "locker code note 4821"))
                                 ["disposition"], "FORGOTTEN")
            self.assertEqual(find(index, "4821"), {"turn_ids": [], "memory_ids": []})


if __name__ == "__main__":
    unittest.main()
