"""Faults that must heal without the user: a hub process that died, a capture service that died,
an embedding model that will not run. bench/recovery.py runs these with the other fault tests and
reports them together."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from unittest import mock
import os
import subprocess
import sys
import tempfile
import time
import unittest

from turnbook import automatic_capture
from turnbook import mcp_server as server
from turnbook import sharing
from turnbook.conversation_memory import ConversationMemoryStore


class HubProcessDiedTests(unittest.TestCase):
    def test_the_next_use_starts_a_killed_postgres_again(self):
        """A crash, an update killing it, a console closed on it: whoever needs the memory next
        starts it again, and what was written before is still there."""
        import psutil
        import psycopg
        # not mkdtemp: initdb run by an administrator (CI) cannot enter its owner-only folder
        home = Path(tempfile.gettempdir()) / f"graph-mind-recovery-{os.getpid()}-{time.time_ns()}"
        home.mkdir()
        # a free port, not a fixed one: Linux hands out 32768-60999 to outgoing connections, and
        # when one held 54396 Postgres listened on its socket file only ("Connection refused")
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with mock.patch.dict(os.environ, {"TURNBOOK_HOME": str(home)}), \
                mock.patch.object(sharing, "PORT", port):
            try:
                with psycopg.connect(sharing.local_postgres(), autocommit=True) as db:
                    db.execute("CREATE TABLE kept(note text)")
                    db.execute("INSERT INTO kept VALUES ('before the crash')")
                # nobody else on the PC logs in without the password (initdb made it "trust")
                hba = (sharing._pg_data() / "pg_hba.conf").read_text(encoding="utf-8")
                self.assertNotRegex(hba, r"(?m)^[^#\n]*\btrust\s*$")
                with mock.patch.dict(os.environ, {"PGPASSFILE": str(home / "none")}), \
                        self.assertRaises(psycopg.OperationalError):
                    psycopg.connect(f"postgresql://postgres@127.0.0.1:{sharing.PORT}/postgres",
                                    connect_timeout=10).close()
                pid = (sharing._pg_data() / "postmaster.pid").read_text(encoding="utf-8")
                postmaster = psutil.Process(int(pid.split()[0]))
                # the whole server, no clean stop; the postmaster first: killed after its
                # children, it sometimes saw them die and restarted them ("reinitializing")
                children = postmaster.children(recursive=True)
                for process in [postmaster, *children]:
                    try:
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
                time.sleep(1)
                self.assertIsNone(sharing._server_exe(sharing._pg_data()))
                with psycopg.connect(sharing.local_postgres(), connect_timeout=10) as db:
                    self.assertEqual(db.execute("SELECT note FROM kept").fetchone()[0],
                                     "before the crash")
            finally:
                sharing.stop_postgres()


class CaptureServiceDiedTests(unittest.TestCase):
    def test_a_dead_capture_service_is_started_again_once(self):
        """Killed, crashed, closed by an update: the next check starts it, and while it runs no
        second one is started (two apps check; one service)."""
        real_popen, started = subprocess.Popen, []

        def start(*_, **__):                     # a stand-in that looks like the service
            started.append(real_popen([sys.executable, "-c", "import time; time.sleep(120)",
                                       "capture_cli"]))
            return started[-1]

        state = automatic_capture.default_service_state_dir()
        state.mkdir(parents=True, exist_ok=True)
        (state / "service.pid").write_text("4194000", encoding="ascii")   # left by a crash
        with mock.patch.object(automatic_capture.subprocess, "Popen", start):
            try:
                self.assertTrue(automatic_capture.ensure_service())
                self.assertFalse(automatic_capture.ensure_service())   # running: left alone
                started[0].kill()
                started[0].wait()
                self.assertTrue(automatic_capture.ensure_service())    # died: started again
                self.assertEqual(len(started), 2)
            finally:
                for process in started:
                    process.kill()


class ModelBrokenTests(unittest.TestCase):
    def test_word_search_still_answers_when_the_model_cannot_run(self):
        """A model download cut short, a runtime DLL missing: recall answers by words."""
        class Broken:
            def embed(self, texts):
                raise OSError("model file is truncated")

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                store.record({"turn_id": "t1", "client": "claude-code", "client_session_id": "s",
                              "scope": "global", "role": "user",
                              "content_redacted": "the invoice for October is due on the 25th",
                              "raw_sha256": "t1", "source_path": "p", "source_ordinal": 1,
                              "happened_at": "2026-10-01T09:00:00",
                              "known_at": "2026-10-01T09:00:00"})
            recalled = server.gather(db, "when is the October invoice due",
                                     as_of="2026-10-14T00:00:00", embedder=Broken())
        self.assertIn("t1", [t["turn_id"] for t in recalled["conversation_turns"]])


if __name__ == "__main__":
    unittest.main()
