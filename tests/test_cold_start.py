"""The first question after a busy day: answered by meaning, not by word search alone.

A memory server that has been open since morning holds the vectors it loaded then. Meanwhile the
capture service stores a day of turns. Before, the server met hundreds of unembedded pieces, sent
them to a background fill and searched none of them by meaning. Now the service embeds what it
stores, and the server picks those vectors up from the shared cache before it searches.
"""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from unittest import mock
import tempfile
import unittest

from turnbook import mcp_server as server
from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.warmup import warm_turns
from turnbook.local_embedder import default_embedder

FILLER = ["오늘 회의에서 분기 예산 이야기를 길게 했다", "점심 메뉴 고르느라 시간이 걸렸다",
          "빌드가 깨져서 의존성 버전을 다시 맞췄다", "주말에 읽을 책 목록을 정리했다",
          "운동 루틴을 바꿔볼까 고민 중이다", "프린터가 또 용지 걸림을 일으켰다"]
TARGET = "우리 강아지 콩이는 파도 소리만 들어도 벌벌 떨면서 바닷가에 안 가려고 해"
QUESTION = "반려견이 무서워하는 장소가 어디였지?"           # shares no word with the turn


def turn(i, text):
    when = f"2026-10-08T{9 + i // 60:02d}:{i % 60:02d}:00"
    return {"turn_id": f"t{i}", "client": "claude-code", "client_session_id": "s",
            "scope": "global", "role": "user", "content_redacted": text, "raw_sha256": f"t{i}",
            "source_path": "p", "happened_at": when, "known_at": when, "source_ordinal": i}


class ColdStartTests(unittest.TestCase):
    def test_turns_captured_after_the_server_started_are_searched_by_meaning(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            open_since_morning = default_embedder(db)          # the memory server's view
            with ConversationMemoryStore(db) as store:            # the day's capture
                for i in range(150):
                    store.record(turn(i, f"{FILLER[i % len(FILLER)]} (note {i})"))
                store.record(turn(150, TARGET))
            question = QUESTION

            with mock.patch.object(server, "embed_in_background"):
                cold = server.semantic_turns(db, question, embedder=open_since_morning)
            self.assertEqual(cold, [], "the old behaviour: nothing searched by meaning")

            self.assertEqual(warm_turns(db, embedder=default_embedder(db)), 151)   # the service

            with mock.patch.object(server, "embed_in_background",
                                   side_effect=AssertionError("no backlog left to fill")):
                warm = server.semantic_turns(db, question, embedder=open_since_morning, limit=3)
            self.assertIn("t150", [hit["turn_id"] for hit in warm])


class ModelStillLoadingTests(unittest.TestCase):
    def test_a_question_does_not_wait_for_the_model(self):
        """The app's first question: the model is still loading, so meaning search is skipped
        (word search answers) instead of waiting out the torch import."""
        class Loading:
            WAIT_FOR_MODEL = 0.5
            cache = {}
            def _key(self, text):
                return text
            def ready(self, timeout):
                return False
            def embed(self, texts):
                raise AssertionError("must not wait for the model")
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record(turn(0, TARGET))
            with mock.patch.object(server, "embed_in_background"):
                self.assertEqual(server.semantic_turns(db, "반려견", embedder=Loading()), [])


class SharedModelTests(unittest.TestCase):
    def test_a_server_uses_the_capture_services_model_and_never_loads_its_own(self):
        """Every app's server searching by meaning from its first question, at no memory cost:
        the capture service has the model loaded and lends it."""
        from turnbook import local_embedder

        class ServiceModel:                       # the capture service's loaded model
            model_name = local_embedder.LOCAL_MODEL

            def _load(self):
                pass

            def _encode(self, texts):
                return [[float(len(text)), 1.0] for text in texts]

            def embed(self, texts):
                return self._encode(texts)

        def no_local_model(*_):
            raise AssertionError("the server loaded its own model")

        local_embedder.serve(ServiceModel())
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(local_embedder.LocalEmbedder, "USE_SERVICE", True), \
                mock.patch.object(local_embedder.LocalEmbedder, "_load", no_local_model), \
                mock.patch.object(local_embedder, "_service", local_embedder._Service()):
            embedder = default_embedder(Path(directory) / "brain.sqlite")
            self.assertTrue(embedder.ready(0))
            self.assertEqual([list(v) for v in embedder.embed(["abc", "hello"])],
                             [[3.0, 1.0], [5.0, 1.0]])

    def test_without_the_service_the_server_loads_its_own_model(self):
        from turnbook import local_embedder
        local_embedder.service_file().unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(local_embedder.LocalEmbedder, "USE_SERVICE", True), \
                mock.patch.object(local_embedder.LocalEmbedder, "_load", lambda self: None), \
                mock.patch.object(local_embedder.LocalEmbedder, "_encode",
                                  lambda self, texts: [[7.0, 0.0] for _ in texts]), \
                mock.patch.object(local_embedder, "_service", local_embedder._Service()):
            embedder = default_embedder(Path(directory) / "brain.sqlite")
            self.assertEqual([list(v) for v in embedder.embed(["abc"])], [[7.0, 0.0]])


    def test_a_backlog_rests_is_embedded_once_and_a_question_does_not_wait(self):
        """After a model change every stored turn is embedded again, beside the user's work, and
        every app's server asks for the same backlog at once."""
        import threading
        import time
        from turnbook import local_embedder
        encoded = []

        def slow(self, texts):
            encoded.extend(texts)
            time.sleep(0.1)
            return [[1.0, float(len(t))] for t in texts]

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(local_embedder.LocalEmbedder, "PACE", True), \
                mock.patch.object(local_embedder.LocalEmbedder, "CHUNK", 2), \
                mock.patch.object(local_embedder.LocalEmbedder, "_load", lambda self: None), \
                mock.patch.object(local_embedder.LocalEmbedder, "_encode", slow):
            embedder = default_embedder(Path(directory) / "brain.sqlite")
            backlog = ["a", "bb", "ccc", "dddd"]
            began = time.monotonic()
            twice = [threading.Thread(target=embedder.embed, args=(backlog,)) for _ in range(2)]
            for thread in twice:
                thread.start()
            time.sleep(0.05)
            asked = time.monotonic()
            self.assertEqual(list(embedder.embed(["question"])[0]), [1.0, 8.0])
            # beside the backlog: waiting it out is >= 0.35 s; macOS CI took 0.24 s without waiting
            self.assertLess(time.monotonic() - asked, 0.3)
            for thread in twice:
                thread.join()
            # 2 chunks, each rested: ~0.4 s, 0.2 s without the rests; Windows' clock ticks 15.6 ms
            self.assertGreaterEqual(time.monotonic() - began, 0.35)
            self.assertEqual(sorted(encoded), sorted(backlog + ["question"]))   # once each


class PastMomentTests(unittest.TestCase):
    def test_asking_about_a_past_moment_leaves_out_later_turns(self):
        """"Where did the release stand on Monday?" must not read Tuesday's answer."""
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                for i, text in enumerate(["release planned for friday",
                                          "release moved to next month"]):
                    when = f"2026-10-0{5 + i}T10:00:00"           # Monday, then Tuesday
                    store.record({**turn(i, text), "happened_at": when, "known_at": when})

            class NoMeaning:
                def ready(self, timeout):
                    return False

            found = server.gather(db, "release", as_of="2026-10-05T23:00:00", limit=5,
                                  embedder=NoMeaning(), until="2026-10-05T23:00:00")
            self.assertEqual([t["content"] for t in found["conversation_turns"]],
                             ["release planned for friday"])


if __name__ == "__main__":
    unittest.main()
