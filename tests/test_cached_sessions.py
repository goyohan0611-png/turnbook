import _isolate  # noqa: F401
from pathlib import Path
import importlib.util
import json
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("cached_sessions", Path(__file__).resolve()
                                             .parents[1] / "bench/cached_sessions.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class CachedSessionsTests(unittest.TestCase):
    def test_unicode_separator_inside_a_passage_is_not_a_jsonl_boundary(self):
        row = {"question_id": "q", "passages": [{"text": "before\u2028after"}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "packets.jsonl"
            path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(BENCH.load_packets(path), [row])

    def test_duplicate_turns_are_one_session_and_missing_packets_count_as_misses(self):
        questions = [{"question_id": qid, "question_type": "user", "answer_session_ids": ["gold"]}
                     for qid in ("hit", "miss", "unanswered_abs")]
        packets = [{"question_id": "hit", "estimated_tokens": 20,
                    "passages": [{"session_id": "wrong"}] * 10 + [{"session_id": "gold"}]}]
        report = BENCH.measure(questions, packets)
        self.assertEqual(report["all"]["questions"], 2)
        self.assertEqual(report["abstention_excluded"], 1)
        self.assertEqual(report["all"]["packet_session_hit@5"], 0.5)
        self.assertIn("miss", report["missing_packets"])

    def test_duplicate_records_and_unmatched_packets_are_rejected(self):
        q = {"question_id": "a", "question_type": "user", "answer_session_ids": ["gold"]}
        with self.assertRaises(ValueError):
            BENCH.measure([q, q], [])
        with self.assertRaises(ValueError):
            BENCH.measure([q], [{"question_id": "other"}])


if __name__ == "__main__":
    unittest.main()
