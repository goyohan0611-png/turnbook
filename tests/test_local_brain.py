import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from turnbook.local_brain import LocalBrainStore


T1 = "2026-09-15T10:00:00"
T2 = "2026-09-15T11:00:00"


def memory(memory_id, *, scope="global", title="Turnbook 프로젝트",
           content="사용자 소유 로컬 뇌", tags=None, supersedes=None, when=T1):
    return {"memory_id": memory_id, "scope": scope, "memory_type": "PROJECT",
            "title": title, "content": content, "effective_at": when,
            "known_at": when, "actor": "user", "tags": tags or ["마인드"],
            "entities": ["Turnbook"],
            "provenance": {"source_type": "fixture", "source_ref": "test"},
            "supersedes_memory_id": supersedes}


class LocalBrainTests(unittest.TestCase):
    def test_natural_alias_recall_scope_and_supersession(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            with LocalBrainStore(path) as store:
                store.remember(memory("old", content="이전 설명"))
                store.remember(memory("new", content="시간과 관계를 실행하는 로컬 뇌",
                                      supersedes="old", when=T2))
                store.remember(memory("private", scope="private:demo",
                                      title="비공개 메모", tags=["비공개"]))
                recalled = store.recall("마인드 프로젝트가 뭐야?", as_of=T2)
                private = store.recall("비공개 메모", scopes=["global"], as_of=T2)
            self.assertEqual(recalled["status"], "KNOWN")
            self.assertEqual(recalled["matches"][0]["memory_id"], "new")
            self.assertNotIn("old", {item["memory_id"] for item in recalled["matches"]})
            self.assertEqual(private["status"], "UNKNOWN")

    def test_development_notes_from_before_0_8_are_not_answered_as_memories(self):
        """Nothing has written them since 0.8; read as memories, an agent's September task
        summaries took the curated slots of every hook packet."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            event = {"event_id": "objective", "project_id": "project:demo",
                "session_id": "session:1", "event_type": "OBJECTIVE_SET",
                "effective_at": T1, "known_at": T1, "actor": "user",
                "subject_id": "project",
                "payload": {"objective": "사용자 기억 엔진 만들기"},
                "provenance": {"source_type": "fixture", "source_ref": "test"},
                "supersedes_event_id": None}
            with closing(sqlite3.connect(path)) as db:  # written by versions before 0.8
                db.execute("""CREATE TABLE development_events(revision INTEGER PRIMARY KEY,
                  event_id TEXT, project_id TEXT, session_id TEXT, event_type TEXT,
                  effective_at TEXT, known_at TEXT, actor TEXT, subject_id TEXT,
                  payload_json TEXT, provenance_json TEXT, supersedes_event_id TEXT)""")
                db.execute("INSERT INTO development_events VALUES(1,?,?,?,?,?,?,?,?,?,?,?)",
                           (event["event_id"], event["project_id"], event["session_id"],
                            event["event_type"], event["effective_at"], event["known_at"],
                            event["actor"], event["subject_id"],
                            json.dumps(event["payload"], ensure_ascii=False),
                            json.dumps(event["provenance"]), None))
                db.commit()
            with LocalBrainStore(path) as brain:
                result = brain.recall("기억 엔진", scopes=["project:demo"], as_of=T2)
            self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main()
