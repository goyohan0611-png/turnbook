"""Postgres's tools fail at random on some Windows PCs (AhnLab V3): hub setup tries again."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from turnbook import sharing


class HubRetryTests(unittest.TestCase):
    def test_a_flaky_step_is_tried_again_and_cleaned_up_between(self):
        calls, undone = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise subprocess.CalledProcessError(1, "initdb")
            return "ok"

        with mock.patch("time.sleep"):
            self.assertEqual(sharing._retry(flaky, attempts=10, undo=lambda: undone.append(1)),
                             "ok")
        self.assertEqual((len(calls), len(undone)), (3, 2))

    def test_gives_up_after_the_last_attempt(self):
        def broken():
            raise subprocess.CalledProcessError(1, "pg_ctl")
        with mock.patch("time.sleep"), self.assertRaises(subprocess.CalledProcessError):
            sharing._retry(broken, attempts=3)

    def test_a_start_that_failed_after_initdb_still_gets_the_memory_database(self):
        """The first start fails (as cmd.exe denials do) after initdb made the cluster: the next
        call starts it and still creates the role and database the memory lives in."""
        from pathlib import Path
        import os
        import tempfile
        import time

        import psycopg
        real, tries = sharing._start, []

        def first_fails(data, port):
            tries.append(1)
            if len(tries) == 1:
                raise subprocess.CalledProcessError(1, "pg_ctl")
            real(data, port)
        # not mkdtemp: on Windows its folder is the owner's alone, and initdb run by an
        # administrator (CI) drops to a restricted token that cannot enter it
        home = Path(tempfile.gettempdir()) / f"graph-mind-retry-{os.getpid()}-{time.time_ns()}"
        home.mkdir()
        with mock.patch.dict(os.environ, {"TURNBOOK_HOME": str(home)}), \
                mock.patch.object(sharing, "PORT", 54395), \
                mock.patch.object(sharing, "_start", first_fails):
            try:
                with self.assertRaises(subprocess.CalledProcessError):
                    sharing.local_postgres()
                with psycopg.connect(sharing.local_postgres(), connect_timeout=10) as db:
                    self.assertEqual(db.execute("SELECT current_user").fetchone()[0], "graphmind")
            finally:
                sharing.stop_postgres()

    def test_the_hubs_postgres_is_fetched_and_speaks_tls(self):
        """Any Python, any supported machine: the pinned build is fetched, runs, has OpenSSL."""
        self.assertIn("PostgreSQL) 16.", sharing._pg_tool("postgres", ["-V"]))
        # built with OpenSSL: the server calls it (zonky's Linux build has no pg_config)
        program = sharing.postgres_bin() / sharing._exe("postgres")
        self.assertIn(b"SSL_CTX_new", program.read_bytes())


class DestinationTests(unittest.TestCase):
    def test_a_queue_for_one_hub_is_not_sent_to_the_next(self):
        import os
        import tempfile
        outbox = sharing._outbox()
        outbox.parent.mkdir(parents=True, exist_ok=True)
        # the choice must not outlive the test: later tests would share through that folder
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second, \
                mock.patch.object(sharing, "config_path", lambda: Path(second) / "config.json"):
            sharing.set_folder(first)
            outbox.write_text('{"kind": "turn"}' + chr(10), encoding="utf-8")
            sharing.set_folder(first)                  # the same place: the queue stays
            self.assertTrue(outbox.exists())
            sharing.set_folder(second)
            self.assertFalse(outbox.exists())
            if os.name != "nt":                        # it holds the share password
                self.assertEqual(sharing.config_path().stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
