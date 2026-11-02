"""Long turns are stored compressed; everything that reads them still sees the words.

The store keeps a turn of 200+ bytes zlib-compressed (about half the size). Word search, the
turns handed to a model, forgetting, phrase lookup and export must not notice. A store written
by an older version is upgraded in place and packed later by the capture service.
"""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from turnbook import forget, sharing
from turnbook.conversation_memory import ConversationMemoryStore

LONG = ("I'm planning the spring offsite. By the way, the venue we picked is Lakeside Lodge, "
        "and the budget owner is Priya from finance. ") * 3
LONG = LONG.strip()                       # the store keeps text trimmed
OLD_SCHEMA = """
CREATE TABLE conversation_turns(
  revision INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL UNIQUE,
  client TEXT NOT NULL, client_session_id TEXT NOT NULL, scope TEXT NOT NULL, cwd TEXT,
  happened_at TEXT NOT NULL, known_at TEXT NOT NULL, role TEXT NOT NULL,
  content_redacted TEXT NOT NULL, raw_sha256 TEXT NOT NULL, redaction_count INTEGER NOT NULL,
  source_path TEXT NOT NULL, source_ordinal INTEGER NOT NULL, fingerprint TEXT NOT NULL);
CREATE VIRTUAL TABLE conversation_turns_fts USING fts5(
  content_redacted, content='conversation_turns', content_rowid='revision',
  tokenize='unicode61');
CREATE TRIGGER conversation_turns_ai AFTER INSERT ON conversation_turns BEGIN
  INSERT INTO conversation_turns_fts(rowid,content_redacted)
  VALUES(new.revision,new.content_redacted);
END;
"""


def turn(i, text):
    when = f"2026-10-01T09:{i:02d}:00"
    return {"turn_id": f"t{i}", "client": "claude-code", "client_session_id": "s",
            "scope": "global", "role": "user", "content_redacted": text, "raw_sha256": f"t{i}",
            "source_path": "C:/talk.jsonl", "happened_at": when, "known_at": when,
            "source_ordinal": i}


class PackingTests(unittest.TestCase):
    def test_nothing_is_packed_while_a_process_on_older_code_runs(self):
        """Such a process reads a packed turn as '': forgetting it left its words searchable."""
        from unittest import mock
        from turnbook.automatic_capture import AutomaticCaptureService
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record(turn(0, LONG))
            service = AutomaticCaptureService.__new__(AutomaticCaptureService)
            service.db_path, service._packed_upto, service._packed_all = db, 0, False
            with mock.patch("turnbook.packing.older_processes", return_value=True):
                self.assertFalse(service.compress_stored())
            with ConversationMemoryStore(db) as store:
                self.assertFalse(store.packing())
            with mock.patch("turnbook.packing.older_processes",
                            side_effect=AssertionError("looked again within 10 minutes")):
                self.assertFalse(service.compress_stored())       # not every 5 s
            service._older_checked -= 601
            with mock.patch("turnbook.packing.older_processes", return_value=False):
                self.assertTrue(service.compress_stored())
                self.assertFalse(service.compress_stored())       # one round, then done
            with ConversationMemoryStore(db) as store:
                self.assertTrue(store.packing())
                self.assertEqual(store.latest(limit=1)["turns"][0]["content"], LONG)


    def test_a_long_turn_is_compressed_and_still_found_by_its_words(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record(turn(9, LONG.replace("Lakeside", "Riverside")))   # flag still off
                store.start_packing()
                store.record(turn(0, LONG))
                store.record(turn(1, "short note about lunch"))
                found = store.search("Lakeside")["turns"]
                latest = {t["turn_id"]: t["content"] for t in store.latest(limit=5)["turns"]}
            raw = sqlite3.connect(db)
            stored = dict(raw.execute("SELECT turn_id, content_redacted FROM conversation_turns"))
            packed = raw.execute("SELECT length(content_z) FROM conversation_turns "
                                 "WHERE turn_id='t0'").fetchone()[0]
            raw.close()
        self.assertEqual(stored, {"t0": "", "t1": "short note about lunch",
                                  "t9": LONG.replace("Lakeside", "Riverside")})
        self.assertLess(packed, len(LONG) * 0.7)
        self.assertEqual([t["content"] for t in found], [LONG])
        self.assertEqual(latest["t0"], LONG)

    def test_an_old_store_is_upgraded_packed_forgotten_and_exported_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            old = sqlite3.connect(db)
            old.executescript(OLD_SCHEMA)
            for i, text in enumerate([LONG, LONG.replace("Priya", "Marco"), "tiny"]):
                t = turn(i, text)
                old.execute("INSERT INTO conversation_turns(turn_id,client,client_session_id,"
                            "scope,happened_at,known_at,role,content_redacted,raw_sha256,"
                            "redaction_count,source_path,source_ordinal,fingerprint) "
                            "VALUES(?,?,?,?,?,?,?,?,?,0,?,?,'f')",
                            (t["turn_id"], t["client"], t["client_session_id"], t["scope"],
                             t["happened_at"], t["known_at"], t["role"], text, t["raw_sha256"],
                             t["source_path"], i))
            old.commit()
            old.close()
            with ConversationMemoryStore(db) as store:            # upgraded on open
                self.assertEqual(store.compress_some(limit=10), (0, 0))   # not until allowed
                store.start_packing()
                self.assertEqual(store.compress_some(limit=10), (2, 3))
                self.assertEqual(store.compress_some(limit=10, after=3), (0, 3))
                store.record(turn(3, LONG.replace("Lakeside", "Hillcrest")))   # new, packed
                self.assertEqual(len(store.search("Lakeside")["turns"]), 2)
                self.assertEqual(len(store.search("Hillcrest")["turns"]), 1)
            self.assertEqual(forget.find(db, "Marco")["turn_ids"], ["t1"])
            forget.erase(db, {"turn_ids": ["t1"], "memory_ids": []})
            with ConversationMemoryStore(db) as store:
                self.assertEqual([t["turn_id"] for t in store.search("Lakeside")["turns"]],
                                 ["t0"])
                self.assertEqual(store.search("Marco")["turns"], [])
            out = Path(directory) / "export.jsonl"
            sharing.export_file(db, out)
            exported = {json.loads(line)["record"]["turn_id"]:
                        json.loads(line)["record"]["content_redacted"]
                        for line in out.read_text(encoding="utf-8").splitlines()}
        self.assertEqual(exported["t0"], LONG)
        self.assertEqual(exported["t2"], "tiny")
        self.assertNotIn("t1", exported)


    def test_compact_gives_the_freed_space_back_to_the_disk(self):
        from turnbook.packing import compact
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                for i in range(400):
                    store.record({**turn(i % 60, f"{LONG} ({i})"), "turn_id": f"x{i}",
                                  "raw_sha256": f"x{i}", "source_ordinal": i})
                store.start_packing()
                self.assertEqual(store.compress_some(limit=1000), (400, 400))
            result = compact(db)
            with ConversationMemoryStore(db) as store:
                self.assertEqual(len(store.search("Lakeside", limit=50)["turns"]), 50)
        self.assertLess(result["after_mb"], result["before_mb"])


if __name__ == "__main__":
    unittest.main()
