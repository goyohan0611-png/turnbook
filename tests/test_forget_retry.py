"""A local deletion and its durable propagation intent survive independently of text."""
import _isolate  # noqa: F401
from pathlib import Path
from unittest import mock
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from turnbook import forget as module, sharing
from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.local_brain import LocalBrainStore
from turnbook.semantic_memory import SemanticMemoryIngestion
from test_forget import memory, turn


class DurableForgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gm-forget-retry-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.index, self.peer = self.root / "local.sqlite", self.root / "peer.sqlite"
        self.shared = self.root / "shared"
        env = mock.patch.dict(os.environ, TURNBOOK_HOME=str(self.root / "home"),
                              TURNBOOK_DEVICE="synthetic-desk")
        env.start()
        self.addCleanup(env.stop)
        sharing._HUB_DOWN.clear()
        self.addCleanup(sharing._HUB_DOWN.clear)

    def seed(self):
        item = memory("m1", "synthetic cobalt decision")
        with LocalBrainStore(self.index) as store:
            store.remember(item)
        sharing.append(self.shared, "memory", item)
        sharing.sync(self.shared, self.peer)

    def rows(self, sql):
        db = sqlite3.connect(self.index)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def worker(self, code):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        done = subprocess.run([sys.executable, "-c", code, str(self.index), str(self.shared)],
                              env=env, capture_output=True, timeout=30)
        return done.returncode, done.stderr.decode(errors="replace")

    def assert_propagated(self):
        sharing.sync(self.shared, self.peer)
        self.assertEqual(module.find(self.peer, "cobalt")["memory_ids"], [])
        self.assertEqual(self.rows("SELECT count(*) FROM forget_jobs"), [(0,)])

    def test_exit_after_local_commit_is_retried_by_sync_without_phrase(self):
        self.seed()
        status, error = self.worker("""
import os, sys
from pathlib import Path
from turnbook import sharing
from turnbook.forget import forget
sharing.append = lambda *a, **kw: os._exit(75)
forget('cobalt', Path(sys.argv[1]), Path(sys.argv[2]))
""")
        self.assertEqual(status, 75, error)
        self.assertEqual(module.find(self.index, "cobalt")["memory_ids"], [])
        self.assertEqual(self.rows("SELECT count(*) FROM forget_jobs"), [(1,)])
        sharing.sync(self.shared, self.index)
        self.assert_propagated()

    def test_phrase_retry_works_after_text_is_already_erased(self):
        self.seed()
        with mock.patch.object(sharing, "append", side_effect=OSError("synthetic down")):
            result = module.forget("cobalt", self.index, self.shared)
        self.assertEqual(result["shared_delete_pending"], 1)
        result = module.forget("cobalt", self.index, self.shared)
        self.assertEqual(result["shared_delete_pending"], 0)
        self.assert_propagated()

    def test_exit_after_publication_before_ack_allows_idempotent_retry(self):
        self.seed()
        status, error = self.worker("""
import os, sys
from pathlib import Path
from turnbook import forget as module
module.scrub_shared = lambda *a: os._exit(76)
module.forget('cobalt', Path(sys.argv[1]), Path(sys.argv[2]))
""")
        self.assertEqual(status, 76, error)
        self.assertEqual(self.rows("SELECT count(*) FROM forget_jobs"), [(1,)])
        self.assertEqual(module.retry_deletions(self.index)["shared_delete_pending"], 0)
        self.assert_propagated()
        notices = [json.loads(line) for path in (self.shared / "log").glob("*.jsonl")
                   for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        # the notice was marked sent before the scrub: the retry only scrubs, it does not resend
        self.assertEqual(sum(item["kind"] == "forget" for item in notices), 1)

    def test_failed_job_persistence_rolls_back_local_erase_and_tombstone(self):
        self.seed()
        with mock.patch.object(module, "_destination", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                module.forget("cobalt", self.index, self.shared)
        self.assertEqual(module.find(self.index, "cobalt")["memory_ids"], ["m1"])
        with LocalBrainStore(self.index) as store:
            self.assertEqual(store.remember(memory("m1", "synthetic cobalt decision"))
                             ["disposition"], "ALREADY_RECORDED")

    def test_promoted_memory_ids_are_persisted_with_the_turn(self):
        with ConversationMemoryStore(self.index) as store:
            store.record(turn(1, "기억해: synthetic cobalt decision"))
        with SemanticMemoryIngestion(self.index) as ingestion:
            self.assertEqual(ingestion.process_new(limit=10)["promoted_count"], 1)
        promoted = module.find(self.index, "cobalt")["memory_ids"]
        self.assertTrue(promoted)
        sharing.export_history(self.shared, self.index)
        sharing.sync(self.shared, self.peer)
        with mock.patch.object(sharing, "append", side_effect=OSError("synthetic down")):
            module.forget_ids({"turn_ids": ["t1"]}, self.index, self.shared)
        payload = json.loads(self.rows("SELECT ids_json FROM forget_jobs")[0][0])
        self.assertEqual(payload["memory_ids"], promoted)
        self.assertEqual(payload["turn_ids"], ["t1"])
        module.retry_deletions(self.index)
        self.assert_propagated()
        self.assertEqual(module.find(self.peer, "cobalt")["turn_ids"], [])

    def test_journal_and_error_metadata_do_not_store_deleted_text(self):
        self.seed()
        with mock.patch.object(sharing, "append",
                               side_effect=PermissionError("synthetic cobalt contents")):
            module.forget("cobalt", self.index, self.shared)
        payload, error = self.rows("SELECT ids_json,last_error FROM forget_jobs")[0]
        self.assertNotIn("cobalt", payload)
        self.assertEqual(error, "PermissionError")
        self.assertEqual(json.loads(payload)["memory_ids"], ["m1"])

    def test_postgres_job_waits_for_actual_commit_instead_of_outbox_ack(self):
        self.seed()
        url = "postgresql://synthetic@127.0.0.1:1/scratch"
        with mock.patch.object(sharing, "_pg_append", side_effect=OSError("offline")):
            result = module.forget("cobalt", self.index, url)
        self.assertEqual(result["shared_delete_pending"], 1)
        self.assertFalse(sharing._outbox().exists())
        sharing._HUB_DOWN.clear()
        with mock.patch.object(sharing, "_pg_append") as committed, \
                mock.patch.object(module, "scrub_shared", return_value=0):
            result = module.retry_deletions(self.index)
        self.assertEqual(result["shared_delete_pending"], 0)
        self.assertEqual(json.loads(committed.call_args.args[1][0])["kind"], "forget")

    def test_failed_folder_delivery_uses_existing_retry_backoff(self):
        self.seed()
        with mock.patch.object(sharing, "_folder_append", side_effect=OSError("offline")) as send:
            result = module.forget("cobalt", self.index, self.shared)
            self.assertEqual(result["shared_delete_pending"], 1)
            self.assertEqual(module.retry_deletions(self.index)["shared_delete_pending"], 1)
            self.assertEqual(send.call_count, 1)
        sharing._HUB_DOWN.clear()
        module.retry_deletions(self.index)
        self.assert_propagated()

    def test_scrub_failure_keeps_job_even_after_notice_was_published(self):
        self.seed()
        with mock.patch.object(module, "scrub_shared", side_effect=PermissionError("offline")):
            result = module.forget("cobalt", self.index, self.shared)
        self.assertEqual(result["shared_delete_pending"], 1)
        module.retry_deletions(self.index)
        self.assert_propagated()

    def test_old_database_without_journal_is_unchanged_by_retry(self):
        self.seed()
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='forget_jobs'"), [])
        self.assertEqual(module.retry_deletions(self.index)["shared_delete_pending"], 0)
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='forget_jobs'"), [])
        self.assertEqual(module.find(self.index, "cobalt")["memory_ids"], ["m1"])

    def test_switching_current_folder_does_not_redirect_a_pending_job(self):
        self.seed()
        with mock.patch.object(sharing, "append", side_effect=OSError("offline")):
            module.forget("cobalt", self.index, self.shared)
        other = self.root / "other"
        sharing.sync(other, self.index)
        self.assertFalse((other / "log").exists())
        module.retry_deletions(self.index)       # sync waits out the backoff; this does not
        self.assert_propagated()

    def test_a_scrub_that_keeps_failing_sends_once_and_backs_off(self):
        self.seed()
        locked = PermissionError("locked")
        with mock.patch.object(module, "scrub_shared", side_effect=locked) as scrub:
            module.forget("cobalt", self.index, self.shared)
            for _ in range(5):                   # the capture service's polls, 5 s apart
                self.assertEqual(module.retry_deletions(self.index, wait=True)
                                 ["shared_delete_pending"], 1)
            self.assertEqual(scrub.call_count, 1)
            module.retry_deletions(self.index)   # an explicit retry: scrubs again, sends nothing
            self.assertEqual(scrub.call_count, 2)
        notices = [line for path in (self.shared / "log").glob("*.jsonl")
                   for line in path.read_text(encoding="utf-8").splitlines()
                   if '"kind": "forget"' in line]
        self.assertEqual(len(notices), 1)
        (sent, next_try), = self.rows("SELECT sent, next_try FROM forget_jobs")
        self.assertEqual(sent, 1)
        self.assertGreater(next_try, 0)
        module.retry_deletions(self.index)
        self.assert_propagated()

    def test_cli_retries_previous_request_even_with_no_local_phrase_match(self):
        self.seed()
        with mock.patch.object(sharing, "append", side_effect=OSError("offline")):
            module.forget("cobalt", self.index, self.shared)
        with mock.patch("turnbook.paths.default_development_db", return_value=self.index), \
                mock.patch("builtins.print"):
            module.main(["cobalt", "--yes"])
        self.assert_propagated()


if __name__ == "__main__":
    unittest.main()
