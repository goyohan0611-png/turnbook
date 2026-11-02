"""Several processes saving one vector cache at once (the capture service and every app's memory
server do) must never pair a key with another text's vector. Before the fix, on Linux, a process
could load an index whose blob another process had just removed, and its next save wrote every
listed key against the wrong rows: the bench's cached vectors had cosine ~0.07 with the model's.

    python test_vector_cache_race.py [path/to/vector_cache.py | git:<rev>]   # another version
"""
import importlib.util
from array import array
import multiprocessing
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest

MODULE = Path(__file__).resolve().parent.parent / "src/turnbook/vector_cache.py"


def load(path):
    spec = importlib.util.spec_from_file_location("vector_cache_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vector(key):
    rng = random.Random(key)
    return list(array("f", [rng.random() for _ in range(8)]))


def writer(path, directory, name, rounds):
    cache_class = load(path).VectorCache
    for i in range(rounds):
        cache = cache_class(directory)              # a server starting, or reloading
        for j in range(5):
            cache.put(f"{name}-{i}-{j}", vector(f"{name}-{i}-{j}"))
        # a question's refresh() while a background thread embeds into the same cache
        side = threading.Thread(target=lambda: [cache.put(f"{name}-{i}-t{k}",
                                                          vector(f"{name}-{i}-t{k}"))
                                                for k in range(20)])
        side.start()
        for _ in range(5):
            cache.refresh()
        side.join()
        cache.flush()


def wrong_vectors(path=MODULE, processes=4, rounds=40):
    directory = tempfile.mkdtemp()
    workers = [multiprocessing.Process(target=writer, args=(str(path), directory, f"w{n}", rounds))
               for n in range(processes)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    cache = load(path).VectorCache(directory)
    # held as float16 (rounding < 1e-3 here); another text's vector is off by ~0.3
    wrong = sum(1 for key in cache.keys
                if max(abs(a - b) for a, b in zip(cache.get(key), vector(key))) > 1e-3)
    return wrong, len(cache.keys), [w.exitcode for w in workers]


class VectorCacheRaceTests(unittest.TestCase):
    def test_concurrent_saves_never_mix_up_vectors(self):
        wrong, keys, exits = wrong_vectors()
        self.assertEqual(exits, [0, 0, 0, 0])            # no save crashed (Windows: index open)
        self.assertEqual(wrong, 0)
        self.assertEqual(keys, 4 * 40 * 25)              # and none lost: saves merge


class RepairTests(unittest.TestCase):
    def test_a_cache_holding_wrong_vectors_is_found_and_refilled(self):
        import _isolate  # noqa: F401
        from turnbook.conversation_memory import ConversationMemoryStore
        from turnbook.local_embedder import LocalEmbedder
        from turnbook import warmup
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "m.sqlite"
            with ConversationMemoryStore(store) as turns:
                for i in range(12):
                    turns.record({"turn_id": f"t{i}", "client": "c", "client_session_id": "s",
                                  "scope": "global", "role": "user",
                                  "content_redacted": f"turn {i}: we talked about topic {i * 7}",
                                  "raw_sha256": f"x{i}", "source_path": "p",
                                  "happened_at": "2026-10-01T09:00:00",
                                  "known_at": "2026-10-01T09:00:00", "source_ordinal": i})
            embedder = LocalEmbedder(Path(directory) / "cache.json")
            warmup.warm_turns(store, embedder=embedder)
            cache = embedder.cache                  # the old bug: keys paired with others' rows
            keys = list(cache.keys)
            cache.keys = dict(zip(keys, [cache.keys[k] for k in keys[1:] + keys[:1]]))
            cache._dirty = 1
            cache.flush()
            warmup._VERIFIED.clear()                # a capture service starting up
            again = LocalEmbedder(Path(directory) / "cache.json")
            warmup.warm_turns(store, embedder=again)
            self.assertGreater(again.cache.generation, 0)
            self.assertFalse(again.verify([f"turn {i}: we talked about topic {i * 7}"
                                           for i in range(12)]))


class QueryTests(unittest.TestCase):
    def test_a_question_is_answered_without_staying_in_the_cache(self):
        from unittest import mock
        from turnbook import local_embedder
        from turnbook.local_embedder import LocalEmbedder
        with tempfile.TemporaryDirectory() as directory:
            embedder = LocalEmbedder(Path(directory) / "cache.json")
            vector = [0.6, 0.8]
            with (mock.patch.object(local_embedder._service, "encode", return_value=None),
                  mock.patch.object(embedder, "_encode_now", return_value=[vector])):
                self.assertEqual(embedder.embed_query("어제 뭐 고쳤어?"), vector)
                self.assertEqual(len(embedder.cache), 0)
                embedder.embed(["stored turn"])                  # stored text is still kept
                self.assertEqual(len(embedder.cache), 1)

    def test_the_service_is_asked_a_question_only_in_the_form_it_knows(self):
        from turnbook import local_embedder
        sent = []
        conn = type("C", (), {"send": lambda self, x: sent.append(x),
                              "poll": lambda self, t: True, "recv": lambda self: [[1.0]]})()
        service = local_embedder._Service()
        service.connected = lambda model: True
        service.local.conn, service.local.asks = conn, False      # an older service
        service.encode("m", ["q"], keep=False)
        service.local.asks = True
        service.encode("m", ["q"], keep=False)
        service.encode("m", ["t"])
        self.assertEqual(sent, [["q"], ("ask", ["q"]), ["t"]])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if target.startswith("git:"):
            import subprocess
            source = subprocess.run(["git", "show", target[4:] + ":src/turnbook/vector_cache.py"],
                                    capture_output=True, text=True, check=True,
                                    cwd=MODULE.parent).stdout
            target = Path(tempfile.mkdtemp()) / "vector_cache.py"
            target.write_text(source, encoding="utf-8")
        for _ in range(3):
            print(sys.argv[1], "wrong %d of %d, exits %s" % wrong_vectors(Path(target)), flush=True)
    else:
        unittest.main()
