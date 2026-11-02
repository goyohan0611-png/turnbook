"""Codex round 10 sharing findings: a replaced log, a large backlog, a forgotten predecessor,
a hub left unscrubbed, and plain lines on a sealed hub."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from unittest import mock
import os
import tempfile
import unittest

from turnbook import sharing


def memory(mid, text, supersedes=None):
    return {"memory_id": mid, "scope": "global", "memory_type": "fact", "title": text,
            "content": text, "effective_at": "2026-03-01T09:00:00",
            "known_at": "2026-03-01T09:00:00", "actor": "assistant", "tags": [],
            "entities": ["사는 곳"], "provenance": {"source_type": "test", "source_ref": "t"},
            "supersedes_memory_id": supersedes}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.shared, self.index = self.root / "shared", self.root / "index.sqlite"
        os.environ["TURNBOOK_DEVICE"] = "desk"

    def tearDown(self):
        os.environ.pop("TURNBOOK_DEVICE", None)

    def test_a_replaced_log_of_the_same_length_is_read_again(self):
        sharing.append(self.shared, "memory", memory("m1", "aaaa"))
        self.assertEqual(sharing.sync(self.shared, self.index)["imported"], 1)
        log = self.shared / "log" / "desk.jsonl"
        before = log.stat().st_size
        log.write_text("", encoding="utf-8")
        sharing.append(self.shared, "memory", memory("m2", "bbbb"))
        self.assertEqual(log.stat().st_size, before)
        self.assertEqual(sharing.sync(self.shared, self.index)["imported"], 1)

    def test_a_backlog_is_read_in_batches(self):
        for n in range(3):
            sharing.append(self.shared, "memory", memory(f"m{n}", f"fact {n}"))
        with mock.patch.object(sharing, "BATCH_BYTES", 1):
            self.assertEqual(sharing.sync(self.shared, self.index),
                             {"imported": 3, "conflicts": 0, "waiting": 0})

    def test_a_replacement_of_a_forgotten_memory_still_arrives(self):
        sharing.append(self.shared, "memory", memory("b", "사는 곳: 부산", supersedes="a"))
        sharing.append(self.shared, "forget", {"turn_ids": [], "memory_ids": ["a"]})
        self.assertEqual(sharing.sync(self.shared, self.index),
                         {"imported": 1, "conflicts": 0, "waiting": 0})

    def test_a_received_forget_notice_scrubs_the_shared_log(self):
        sharing.append(self.shared, "turn", {
            "turn_id": "t1", "client": "claude-code", "client_session_id": "s",
            "scope": "global", "role": "user", "content_redacted": "비밀 이야기",
            "raw_sha256": "x", "source_path": "p", "happened_at": "2026-03-01T09:00:00",
            "known_at": "2026-03-01T09:00:00", "source_ordinal": 1})
        os.environ["TURNBOOK_DEVICE"] = "laptop"   # forgot it while the folder was away
        sharing.append(self.shared, "forget", {"turn_ids": ["t1"], "memory_ids": []})
        sharing.sync(self.shared, self.index)
        self.assertNotIn("비밀", (self.shared / "log" / "desk.jsonl").read_text(encoding="utf-8"))

    def test_a_sealed_hub_refuses_plain_lines(self):
        from cryptography.fernet import Fernet
        key = Fernet(Fernet.generate_key())
        sealed = sharing.SEALED + key.encrypt(b"line").decode()
        hub = "postgresql://graphmind:pw@192.0.2.1:54329/graphmind"
        with mock.patch.object(sharing, "_line_key", return_value=key):
            self.assertIsNone(sharing._unseal(hub, '{"kind": "forget"}'))
            self.assertEqual(sharing._unseal(hub, sealed), "line")
        with mock.patch.object(sharing, "_line_key", return_value=None):
            self.assertEqual(sharing._unseal(hub, "plain"), "plain")


if __name__ == "__main__":
    unittest.main()
