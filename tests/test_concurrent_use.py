"""Claude, Codex and the capture service write one memory file from separate processes at once."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

WRITER = """
import sys
from turnbook.conversation_memory import ConversationMemoryStore
path, name = sys.argv[1], sys.argv[2]
for i in range(150):
    with ConversationMemoryStore(path) as store:
        store.record({"turn_id": f"{name}-{i}", "client": name, "client_session_id": name,
                      "scope": "global", "role": "user", "content_redacted": f"{name} said {i}",
                      "raw_sha256": f"{name}-{i}", "source_path": name,
                      "happened_at": "2026-10-07T10:00:00", "known_at": "2026-10-07T10:00:00",
                      "source_ordinal": i})
"""


class ConcurrentUseTests(unittest.TestCase):
    def test_three_writers_one_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "brain.sqlite")
            src = Path(__file__).parent.parent / "src"
            writers = [subprocess.Popen([sys.executable, "-c", WRITER, path, name], cwd=src,
                                        stderr=subprocess.PIPE, text=True)
                       for name in ("claude", "codex", "capture")]
            errors = [w.communicate(timeout=240)[1] for w in writers]
            self.assertEqual([w.returncode for w in writers], [0, 0, 0], errors)
            from turnbook.conversation_memory import ConversationMemoryStore
            with ConversationMemoryStore(path) as store:
                count = store.db.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0]
            self.assertEqual(count, 450)


if __name__ == "__main__":
    unittest.main()
