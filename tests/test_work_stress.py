"""Adversarial continuity checks, including independent processes and killed transactions."""
import _isolate  # noqa: F401
from contextlib import closing
from pathlib import Path
from unittest import mock
import json
import multiprocessing
import random
import sqlite3
import tempfile
import unittest

from turnbook import hook
from turnbook.work_state import WorkState


def compete(database, gate, replies, number):
    try:
        gate.wait(timeout=30)
        with WorkState(database) as work:
            result = work.change("claim", "/project", agent=f"worker:{number}",
                                 expected_revision=0, objective="One implementation")
        replies.put(result)
    except Exception as error:
        replies.put({"error": repr(error)})


def uncommitted_writer(database, ready):
    with WorkState(database) as work:
        work.db.execute("BEGIN IMMEDIATE")
        work.db.execute("UPDATE work_items SET packet=?", (json.dumps({"corrupt": True}),))
        ready.set()
        # The parent terminates only this disposable test process before COMMIT.
        ready.wait(60)
        import time
        time.sleep(60)


class WorkStressTests(unittest.TestCase):
    def test_first_prototype_is_upgraded_without_losing_packets(self):
        with tempfile.TemporaryDirectory() as folder:
            source, legacy = Path(folder) / "new.sqlite", Path(folder) / "legacy.sqlite"
            with WorkState(source) as work:
                packet = work.change("claim", "/project", agent="one", expected_revision=0,
                                     objective="Preserve this unfinished work")["work"]
                work.change("claim", "/project", task="finished", agent="two",
                            expected_revision=0, objective="Finished work")
                work.change("complete", "/project", task="finished", agent="two",
                            expected_revision=1)
                old_row = work.db.execute(
                    "SELECT workspace,task_id,revision,packet FROM work_items")
                rows = old_row.fetchall()
            with closing(sqlite3.connect(legacy)) as db, db:
                db.execute("CREATE TABLE work_items (workspace TEXT,task_id TEXT,revision INTEGER,"
                           "packet TEXT,PRIMARY KEY(workspace,task_id))")
                db.executemany("INSERT INTO work_items VALUES (?,?,?,?)", rows)
            for _ in range(2):
                with WorkState(legacy) as work:
                    self.assertEqual(work.read("/project"), packet)
                    self.assertEqual(work.tasks("/project")[0], packet)
                    self.assertEqual(len(work.tasks("/project")), 2)
                    plan = str(work.db.execute("EXPLAIN QUERY PLAN SELECT packet FROM work_items "
                               "WHERE workspace=? ORDER BY is_open DESC,updated_at DESC,task_id "
                               "LIMIT 30", ("posix:/project",)).fetchall())
                    self.assertIn("work_items_queue", plan)
                    self.assertNotIn("TEMP B-TREE", plan)

    def test_empty_next_action_cannot_erase_the_handover_on_pause(self):
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            work.change("claim", "/p", agent="one", expected_revision=0,
                        objective="Fix login", next_action="Run tests")
            for empty in ("", " \t", "<private>not public</private>"):
                result = work.change("pause", "/p", agent="one", expected_revision=1,
                                     next_action=empty)
                self.assertEqual(result["status"], "CONFLICT")
                self.assertEqual(result["reason"], "NEXT_ACTION_REQUIRED")
                self.assertEqual(work.read("/p")["next_action"], "Run tests")

    def test_inherited_objective_cannot_be_erased_when_claiming(self):
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            work.change("assign", "/p", agent="coordinator", expected_revision=0,
                        objective="Fix login", assigned_to="any")
            for empty in ("", " \t", "<private>not public</private>"):
                with self.assertRaises(ValueError):
                    work.change("claim", "/p", agent="one", expected_revision=1, objective=empty)
                self.assertEqual(work.read("/p")["objective"], "Fix login")

    def test_completed_review_with_a_changed_implementation_is_not_a_fresh_input(self):
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            work.change("claim", "/p", task="code", agent="one", expected_revision=0,
                        objective="Implement")
            work.change("complete", "/p", task="code", agent="one", expected_revision=1)
            work.change("claim", "/p", task="review", agent="two", expected_revision=0,
                        objective="Review", depends_on=["code"])
            work.change("complete", "/p", task="review", agent="two", expected_revision=1)
            work.change("assign", "/p", task="deliver", agent="coordinator", expected_revision=0,
                        objective="Deliver reviewed work", assigned_to="any", depends_on=["review"])
            work.change("claim", "/p", task="code", agent="one", expected_revision=2,
                        objective="Change implementation")
            work.change("complete", "/p", task="code", agent="one", expected_revision=3)
            result = work.change("claim", "/p", task="deliver", agent="three", expected_revision=1)
            self.assertEqual(result["status"], "CONFLICT")
            self.assertEqual(result["reason"], "DEPENDENCY_CHANGED")

    def test_finished_tasks_never_hide_active_work_from_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "brain.sqlite"
            with WorkState(database) as work:
                work.change("claim", "/p", task="ongoing", agent="one", expected_revision=0,
                            objective="Unfinished implementation")
                for number in range(40):
                    task = f"finished-{number}"
                    work.change("claim", "/p", task=task, agent="two", expected_revision=0,
                                objective="Independent task")
                    work.change("complete", "/p", task=task, agent="two", expected_revision=1)
                self.assertEqual(work.tasks("/p")[0]["task"], "ongoing")
            with mock.patch("turnbook.mcp_server.configured_db_path", return_value=database):
                self.assertIn("Unfinished implementation", hook.left_off(
                    {"cwd": "/p", "session_id": "new", "source": "startup"}))

    def test_eight_processes_have_only_one_winner(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as folder:
            database = str(Path(folder) / "brain.sqlite")
            gate, replies = context.Barrier(8), context.Queue()
            workers = [context.Process(target=compete, args=(database, gate, replies, i))
                       for i in range(8)]
            try:
                for worker in workers:
                    worker.start()
                results = [replies.get(timeout=45) for _ in workers]
                for worker in workers:
                    worker.join(timeout=10)
                    self.assertEqual(worker.exitcode, 0)
                self.assertEqual(sum(r.get("status") == "OK" for r in results), 1, results)
                self.assertEqual(sum(r.get("status") == "CONFLICT" for r in results), 7, results)
                with WorkState(database) as work:
                    self.assertEqual(work.read("/project")["revision"], 1)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=10)
                replies.close()

    def test_killed_uncommitted_writer_does_not_damage_the_last_checkpoint(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as folder:
            database = str(Path(folder) / "brain.sqlite")
            with WorkState(database) as work:
                original = work.change("claim", "/project", agent="one", expected_revision=0,
                                       objective="Keep the saved checkpoint")["work"]
            ready = context.Event()
            worker = context.Process(target=uncommitted_writer, args=(database, ready))
            try:
                worker.start()
                self.assertTrue(ready.wait(timeout=30))
                worker.terminate()
                worker.join(timeout=10)
                with WorkState(database) as work:
                    self.assertEqual(work.read("/project"), original)
                    self.assertEqual(work.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(work.change("checkpoint", "/project", agent="one",
                                                 expected_revision=1, summary="Recovered")
                                     ["status"], "OK")
            finally:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=10)

    def test_seeded_action_sequences_preserve_revisions_and_ownership(self):
        rng = random.Random(20261102)
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            for number in range(600):
                task, agent = rng.choice(["a", "b", "c"]), rng.choice(["one", "two"])
                before = work.read("/p", task)
                revision = before["revision"] if before else 0
                action = rng.choice(["claim", "checkpoint", "pause", "complete", "clear"])
                expected = revision if rng.random() < 0.8 else revision + 1
                result = work.change(action, "/p", task=task, agent=agent,
                                     expected_revision=expected,
                                     objective=f"Job {number}", next_action="Continue checking")
                after = work.read("/p", task)
                if result["status"] == "CONFLICT":
                    self.assertEqual(after, before)
                else:
                    self.assertEqual(after["revision"], revision + 1)
                    if after["state"] == "ACTIVE":
                        self.assertEqual(after["owner"], agent)
                    if after["state"] != "EMPTY":
                        self.assertTrue(after["objective"])
                    if after["state"] == "PAUSED":
                        self.assertTrue(after["next_action"])


if __name__ == "__main__":
    unittest.main()
