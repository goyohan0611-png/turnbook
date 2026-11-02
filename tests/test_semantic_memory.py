import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.local_brain import LocalBrainStore
from turnbook.semantic_memory import SemanticMemoryIngestion, classify_durable_turn


T = "2026-09-16T12:00:00"


def turn(turn_id, role, content):
    return {"turn_id": turn_id, "client": "codex", "client_session_id": "s1",
        "scope": "project:test", "cwd": None, "happened_at": T, "known_at": T,
        "role": role, "content_redacted": content, "raw_sha256": turn_id,
        "redaction_count": 0, "source_path": "test.jsonl", "source_ordinal": 1}


class SemanticMemoryTests(unittest.TestCase):
    def test_current_tasks_and_general_questions_are_not_promoted(self):
        for text in ("이 코드 리뷰해줘", "이 에러 수정해줘", "이거 뭐야?",
                     "파이썬 리스트가 뭐야?"):
            category, _ = classify_durable_turn("user", text)
            self.assertIsNone(category)
        self.assertEqual(classify_durable_turn("assistant",
            "앞으로 이걸 사용하기로 했다")[1], "NON_USER_TURN")
        self.assertEqual(classify_durable_turn("user",
            "앞으로는 무조건 기억을 조회하는 거야?")[1],
            "CURRENT_OR_GENERAL_QUESTION")

    def test_explicit_decision_and_preference_are_promoted_with_source(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record(turn("t1", "user",
                    "앞으로는 Local Brain을 필요한 과거 질문에만 사용하기로 하자"))
                store.record(turn("t2", "user", "이 코드 리뷰해줘"))
                store.record(turn("t3", "user", "나는 짧은 답변을 선호한다"))
                store.record(turn("t4", "assistant", "앞으로 전부 저장하겠습니다"))
            with SemanticMemoryIngestion(db, now=lambda: T) as ingestion:
                first = ingestion.process_new()
                second = ingestion.process_new()
            with LocalBrainStore(db) as brain:
                decision = brain.recall("Local Brain 과거 질문", as_of=T)
                preference = brain.recall("짧은 답변 선호", as_of=T)
                count = brain.db.execute("SELECT count(*) FROM local_brain_memories").fetchone()[0]
            with ConversationMemoryStore(db) as conversations:
                small_detail = conversations.search("코드 리뷰")
            self.assertEqual(first["processed_turns"], 4)
            self.assertEqual(first["promoted_count"], 2)
            self.assertEqual(first["evidence_only_count"], 2)
            self.assertEqual(second["status"], "NO_NEW_TURNS")
            self.assertEqual(count, 2)
            self.assertEqual(decision["status"], "KNOWN")
            self.assertEqual(preference["status"], "KNOWN")
            self.assertEqual(small_detail["status"], "KNOWN")
            self.assertEqual(decision["matches"][0]["provenance"]["source_type"],
                             "captured-conversation-turn")

    def test_keyword_made_memories_are_dropped_once_but_remember_requests_stay(self):
        """0.10.5: the gate no longer runs; what it made by keyword goes, the turns stay."""
        from turnbook.semantic_memory import drop_keyword_memories
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record(turn("t1", "user", "근데 저건 안하기로 하지않았나"))     # a question
                store.record(turn("t2", "user", "기억해줘: 사물함은 북쪽 계단 아래"))
            with SemanticMemoryIngestion(db, now=lambda: T) as ingestion:
                self.assertEqual(ingestion.process_new()["promoted_count"], 2)
            self.assertEqual(drop_keyword_memories(db), 1)
            self.assertEqual(drop_keyword_memories(db), 0, "once per store")
            with LocalBrainStore(db) as brain:
                kinds = [row[0] for row in brain.db.execute(
                    "SELECT memory_type FROM local_brain_memories")]
            with ConversationMemoryStore(db) as conversations:
                self.assertEqual(conversations.search("안하기로")["status"], "KNOWN")
            self.assertEqual(kinds, ["FACT"])

    def test_redacted_content_is_never_automatically_promoted(self):
        category, reason = classify_durable_turn(
            "user", "앞으로 키는 [REDACTED_OPENAI_KEY]로 기억해")
        self.assertIsNone(category)
        self.assertEqual(reason, "REDACTED_CONTENT_REQUIRES_REVIEW")


if __name__ == "__main__":
    unittest.main()
