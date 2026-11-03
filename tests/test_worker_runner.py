"""The one brain's hand (worker_runner.py): off by default, only granted tasks, a clean tree, one
worker at a time, a daily limit, a timeout; a run that ends unfinished releases only its own
claim. A fake codex stands in: no model, no network, no real memory."""
import _isolate  # noqa: F401
from pathlib import Path
from unittest import mock
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from turnbook import worker_runner
from turnbook.work_state import WorkState
from turnbook.worker_runner import WorkerRunner

FAKE = r'''
import json, os, re, sys, time
sys.path.insert(0, os.environ["FAKE_SRC"])
prompt = sys.stdin.read()
with open(os.environ["FAKE_OUT"], "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\n")
mode = os.environ.get("FAKE_MODE", "exit")
if mode == "claim_and_die":
    from turnbook.work_state import WorkState
    owner = re.search(r"Your owner name: (\S+)", prompt)[1]
    workspace = re.search(r"Workspace: (.+)", prompt)[1].strip()
    task = re.search(r"Task: (\S+)", prompt)[1]
    with WorkState(os.environ["FAKE_DB"]) as work:
        packet = work.read(workspace, task)
        work.change("claim", workspace, agent=owner, expected_revision=packet["revision"],
                    task=task)
    sys.exit(3)
if mode == "sleep":
    time.sleep(60)
'''


class WorkerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.state, self.db = root / "state", root / "brain.sqlite"
        self.repo, self.out = root / "repo", root / "calls.jsonl"
        self.repo.mkdir()
        (self.repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run([*git, "add", "-A"], cwd=self.repo, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "x"], cwd=self.repo, check=True)
        fake = root / "fake_codex.py"
        fake.write_text(FAKE, encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "FAKE_OUT": str(self.out), "FAKE_DB": str(self.db),
            "FAKE_SRC": str(Path(worker_runner.__file__).parents[1])})
        self.env.start()
        self.runner = WorkerRunner(self.state, self.db, command=(sys.executable, str(fake)))
        with WorkState(self.db) as work:
            work.change("assign", str(self.repo), agent="claude-code:s", expected_revision=0,
                        objective="review the change", assigned_to="codex", task="verify")

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def calls(self):
        if not self.out.exists():
            return []
        return [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]

    def run_until_done(self):
        for _ in range(100):
            self.runner.tick()
            if not self.runner.load().get("running"):
                return
            time.sleep(0.1)
        self.fail("the worker never ended")

    def enable_and_grant(self):
        worker_runner.cli("workers-enable", None, self.state, self.db)
        worker_runner.cli("workers-grant", mock.Mock(workspace=str(self.repo), task="verify"),
                          self.state, self.db)

    def test_nothing_starts_unless_enabled_and_granted(self):
        self.runner.tick()
        worker_runner.cli("workers-enable", None, self.state, self.db)
        self.runner.tick()                                   # enabled, but nothing granted
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.runner.load().get("running"))

    def test_a_granted_task_starts_one_read_only_codex_once(self):
        self.enable_and_grant()
        self.runner.tick()
        running = self.runner.load()["running"]
        self.assertTrue(running["owner"].startswith("codex:auto-"))
        self.runner.tick()                                   # one at a time
        self.run_until_done()
        (call,) = self.calls()
        at = call["argv"].index("-C")
        self.assertEqual(call["argv"][at:at + 4], ["-C", str(self.repo), "-s", "read-only"])
        self.assertIn(f"Your owner name: {running['owner']}", call["prompt"])
        self.assertIn("Task: verify", call["prompt"])
        self.assertIn("First call brain_work_status", call["prompt"])
        self.runner.tick()                                   # the grant was used
        self.assertEqual(len(self.calls()), 1)
        status = worker_runner.cli("workers-status", None, self.state, self.db)
        self.assertEqual(status["launches_today"], 1)

    def test_uncommitted_changes_wait(self):
        (self.repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        self.enable_and_grant()
        self.runner.tick()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.runner.load()["grants"][0]["waiting"], "UNCOMMITTED_CHANGES")

    def test_a_task_for_someone_else_or_waiting_on_another_is_not_started(self):
        with WorkState(self.db) as work:
            work.change("assign", str(self.repo), agent="claude-code:s", expected_revision=0,
                        objective="later", assigned_to="claude-code", task="mine")
        worker_runner.cli("workers-enable", None, self.state, self.db)
        worker_runner.cli("workers-grant", mock.Mock(workspace=str(self.repo), task="mine"),
                          self.state, self.db)
        self.runner.tick()
        self.assertEqual(self.runner.load()["grants"][0]["waiting"], "ASSIGNED_TO_ANOTHER_AGENT")
        self.assertEqual(self.calls(), [])

    def test_a_run_that_dies_holding_its_task_releases_it(self):
        self.enable_and_grant()
        with mock.patch.dict(os.environ, {"FAKE_MODE": "claim_and_die"}):
            self.runner.tick()
            self.run_until_done()
        with WorkState(self.db) as work:
            packet = work.read(str(self.repo), "verify")
        self.assertEqual(packet["state"], "PAUSED")
        self.assertIn("unfinished (3)", packet["next_action"])
        self.assertEqual(self.runner.load()["runs"][-1]["exit"], 3)

    def test_timeout_and_disable_stop_the_worker(self):
        self.enable_and_grant()
        with mock.patch.dict(os.environ, {"FAKE_MODE": "sleep"}), \
                mock.patch.object(worker_runner, "TIMEOUT", 0):
            self.runner.tick()
            time.sleep(0.5)
            self.run_until_done()
        self.assertEqual(self.runner.load()["runs"][-1]["exit"], "timeout")
        worker_runner.cli("workers-grant", mock.Mock(workspace=str(self.repo), task="verify"),
                          self.state, self.db)
        with mock.patch.dict(os.environ, {"FAKE_MODE": "sleep"}):
            self.runner.tick()
            worker_runner.cli("workers-disable", mock.Mock(stop=True), self.state, self.db)
            self.run_until_done()
        self.assertEqual(self.runner.load()["runs"][-1]["exit"], "stopped")

    def test_the_worker_uses_the_services_store_and_untracked_files_count(self):
        self.enable_and_grant()
        self.runner.tick()
        self.run_until_done()
        argv = self.calls()[0]["argv"]
        # the whole server (a config without one would stop Codex), on this service's store
        settings = dict(a.split("=", 1) for a in argv if a.startswith("mcp_servers.turnbook."))
        for tool in ("brain_work", "brain_work_status"):
            self.assertEqual(settings[f"mcp_servers.turnbook.tools.{tool}.approval_mode"],
                             '"approve"')
        self.assertEqual(json.loads(settings["mcp_servers.turnbook.command"]), sys.executable)
        self.assertIn('TURNBOOK_DB=' + json.dumps(str(self.db)),
                      settings["mcp_servers.turnbook.env"])
        start = json.loads(settings["mcp_servers.turnbook.args"])[1]
        self.assertIn(repr(str(Path(worker_runner.__file__).resolve().parents[1])), start)
        # an untracked file waits even when the user's git hides untracked files
        subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=self.repo,
                       check=True)
        (self.repo / "new.py").write_text("y = 1\n", encoding="utf-8")
        worker_runner.cli("workers-grant", mock.Mock(workspace=str(self.repo), task="verify"),
                          self.state, self.db)
        self.runner.tick()
        self.assertEqual(self.runner.load()["grants"][-1]["waiting"], "UNCOMMITTED_CHANGES")

    def test_a_reused_pid_is_never_taken_for_the_worker(self):
        """After a restart the pid may be another program's: not watched, never killed."""
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            policy = self.runner.load()
            policy["running"] = {"pid": other.pid, "created": 1.0, "owner": "codex:auto-x",
                                 "workspace": str(self.repo), "task": "verify",
                                 "deadline": 0, "started": "2026-11-02T00:00:00"}
            policy["runs"] = [{"started": "2026-11-02T00:00:00"}]
            self.runner.save(policy)
            self.runner.tick()                       # deadline passed: would kill a worker
            self.assertIsNone(other.poll())          # the other program still runs
            self.assertIsNone(self.runner.load().get("running"))
        finally:
            other.kill()
            other.wait()

    def test_a_batch_shim_is_never_the_program(self):
        """npm's codex.cmd passes its arguments through cmd.exe ("C:/R&D" runs "D"): node
        runs the codex.js it wraps; an unknown shim is not started."""
        bin_dir = Path(self.folder.name) / "npm"
        script = bin_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        script.parent.mkdir(parents=True)
        (bin_dir / "codex.cmd").write_text("@echo off\n", encoding="utf-8")
        runner = WorkerRunner(self.state, self.db, command=("codex",))
        node = shutil.which("node") or "node"
        with mock.patch.object(shutil, "which", lambda name, path=None: str(bin_dir / "codex.cmd")
                               if name == "codex" else node):
            self.assertIsNone(runner._program())             # no codex.js yet
            script.write_text("", encoding="utf-8")
            self.assertEqual(runner._program(), [node, str(script)])

    def test_a_stale_dependency_keeps_the_grant(self):
        """A review completed, then the implementation it reviewed changed: the review is
        COMPLETE but stale, so a task after it cannot be claimed. Not started, grant kept."""
        ws = str(self.repo)
        with WorkState(self.db) as work:
            def run(task, rev, **kw):
                work.change("claim", ws, agent="c:s", expected_revision=rev, task=task, **kw)
                return work.change("complete", ws, agent="c:s", expected_revision=rev + 1,
                                   task=task)
            run("build", 0, objective="build")
            run("review", 0, objective="review", depends_on=["build"])
            work.change("assign", ws, agent="c:s", expected_revision=0, objective="ship check",
                        assigned_to="codex", depends_on=["review"], task="check")
            run("build", 2, objective="build again")                # reviewed code changed
        worker_runner.cli("workers-enable", None, self.state, self.db)
        worker_runner.cli("workers-grant", mock.Mock(workspace=ws, task="check"),
                          self.state, self.db)
        self.runner.tick()
        grant = self.runner.load()["grants"][0]
        self.assertNotIn("used_at", grant)
        self.assertEqual(grant["waiting"], "DEPENDENCY_CHANGED")
        self.assertEqual(self.calls(), [])

    def test_nothing_runs_unrecorded(self):
        self.enable_and_grant()
        saved = []
        real_save = WorkerRunner.save

        def failing_save(runner, policy):
            saved.append(1)
            raise OSError("disk full")
        with mock.patch.object(WorkerRunner, "save", failing_save):
            with self.assertRaises(OSError):
                self.runner.tick()
        self.assertEqual(self.calls(), [])                   # the record failed: no launch
        WorkerRunner.save = real_save
        with mock.patch.object(self.runner, "_program",
                               lambda: [str(Path(self.folder.name) / "no-such-codex.exe")]):
            self.runner.tick()                               # the program cannot start
        run = self.runner.load()["runs"][-1]
        self.assertEqual(run["exit"], "launch_failed")
        self.assertIsNone(self.runner.load().get("running"))

    def test_an_unconfirmed_stop_keeps_the_worker_tracked_and_its_claim(self):
        """Desktop Codex review 11-02: a stop that cannot be confirmed (identity unknown,
        access denied) released the task of a worker that was still running."""
        owner = "codex:auto-x"
        with WorkState(self.db) as work:
            work.change("claim", str(self.repo), agent=owner, expected_revision=1,
                        task="verify")
        child = mock.Mock(pid=424242)
        child.poll.return_value = None                       # still running
        self.runner._child = child
        running = {"pid": child.pid, "created": None, "owner": owner, "task": "verify",
                   "workspace": str(self.repo), "started": "x", "deadline": 0}
        policy = {"enabled": True, "grants": [], "runs": [{"started": "x"}],
                  "running": running}
        self.runner._watch(policy, running)
        self.assertIn("stop_unconfirmed", self.runner.load()["running"])
        with WorkState(self.db) as work:
            self.assertEqual(work.read(str(self.repo), "verify")["state"], "ACTIVE")
        self.runner.tick()                                   # still blocks a new launch
        self.assertEqual(self.calls(), [])
        # once it is shown to be gone, it is recorded and its task released
        with mock.patch.object(WorkerRunner, "_kill", lambda runner, running: True):
            self.runner.tick()
        self.assertIsNone(self.runner.load().get("running"))
        with WorkState(self.db) as work:
            self.assertEqual(work.read(str(self.repo), "verify")["state"], "PAUSED")

    def test_kill_checks_identity_on_the_object_it_kills(self):
        """One lookup: the object whose creation time matched is the one killed. A pid that
        now belongs to another program (other creation time) is left alone."""
        import psutil
        lookups = []

        def process(pid, created):
            found = mock.Mock()
            found.create_time.return_value = created
            found.children.return_value = []
            lookups.append(found)
            return found
        with mock.patch("psutil.Process", lambda pid: process(pid, 18.0)), \
                mock.patch("psutil.wait_procs", return_value=([], [])):
            self.assertTrue(self.runner._kill({"pid": 1, "created": 17.0}))
        self.assertEqual(len(lookups), 1)
        lookups[0].kill.assert_not_called()
        lookups.clear()
        with mock.patch("psutil.Process", lambda pid: process(pid, 17.0)), \
                mock.patch("psutil.wait_procs", return_value=([], [])):
            self.assertTrue(self.runner._kill({"pid": 1, "created": 17.0}))
        self.assertEqual(len(lookups), 1)
        lookups[0].kill.assert_called_once()

        def refused(pid):                # a kill the system refuses is not a confirmed stop
            found = process(pid, 17.0)
            found.kill.side_effect = psutil.AccessDenied(pid)
            return found
        with mock.patch("psutil.Process", refused):
            self.assertFalse(self.runner._kill({"pid": 1, "created": 17.0}))

    def test_after_a_restart_an_unknown_identity_is_not_an_end(self):
        """Desktop Codex cross-check 11-02: after a restart (_child None) a failed identity
        lookup (access denied) was taken as an end and the live worker's task released. Only
        no such process, or another creation time, ends it; the user can release it."""
        import psutil
        owner = "codex:auto-r"
        with WorkState(self.db) as work:
            work.change("claim", str(self.repo), agent=owner, expected_revision=1,
                        task="verify")
        self.runner._child = None
        policy = self.runner.load()
        policy["running"] = {"pid": 424242, "created": 17.0, "owner": owner, "task": "verify",
                             "workspace": str(self.repo), "started": "x",
                             "deadline": time.time() + 600}
        policy["runs"] = [{"started": "x"}]
        self.runner.save(policy)

        def lookup(error):
            def process(pid):
                if isinstance(error, type) and issubclass(error, psutil.Error):
                    raise error(pid)
                found = mock.Mock()
                found.create_time.return_value = error
                return found
            return process

        def state():
            with WorkState(self.db) as work:
                return work.read(str(self.repo), "verify")["state"]
        with mock.patch("psutil.Process", lookup(psutil.AccessDenied)):
            self.runner.tick()
        self.assertIn("identity_unknown", self.runner.load()["running"])
        self.assertEqual(state(), "ACTIVE")
        for no_pid in ({"pid": None}, {"created": None}):               # never recorded
            policy = self.runner.load()
            policy["running"].update(no_pid)
            self.runner.save(policy)
            self.runner.tick()
            self.assertIsNotNone(self.runner.load().get("running"))
            policy["running"].update(pid=424242, created=17.0)
            self.runner.save(policy)
        with mock.patch("psutil.Process", lookup(17.0)):                # still the worker
            self.runner.tick()
        self.assertEqual(state(), "ACTIVE")
        with mock.patch("psutil.Process", lookup(psutil.NoSuchProcess)):  # shown gone
            self.runner.tick()
        self.assertIsNone(self.runner.load().get("running"))
        self.assertEqual(state(), "PAUSED")

    def test_the_user_can_release_a_worker_the_service_cannot_identify(self):
        owner = "codex:auto-u"
        with WorkState(self.db) as work:
            work.change("claim", str(self.repo), agent=owner, expected_revision=1,
                        task="verify")
        policy = self.runner.load()
        policy["running"] = {"pid": None, "created": None, "owner": owner, "task": "verify",
                             "workspace": str(self.repo), "started": "x", "deadline": 0}
        policy["runs"] = [{"started": "x"}]
        self.runner.save(policy)
        worker_runner.cli("workers-release", None, self.state, self.db)
        self.assertIsNone(self.runner.load().get("running"))
        self.assertEqual(self.runner.load()["runs"][-1]["exit"], "released by the user")
        with WorkState(self.db) as work:
            self.assertEqual(work.read(str(self.repo), "verify")["state"], "PAUSED")

    def test_an_unattended_run_ignores_the_users_codex_config(self):
        self.enable_and_grant()
        self.runner.tick()
        self.run_until_done()
        self.assertEqual(self.calls()[0]["argv"][:2], ["exec", "--ignore-user-config"])

    def test_a_standing_understand_grant_runs_when_there_is_something_new(self):
        from turnbook.conversation_memory import ConversationMemoryStore
        worker_runner.cli("workers-enable", None, self.state, self.db)
        worker_runner.cli("workers-grant", mock.Mock(understand=True, scope="global"),
                          self.state, self.db)
        self.runner.tick()
        self.assertEqual(self.runner.load()["grants"][0]["waiting"], "NOTHING_NEW")
        self.assertEqual(self.calls(), [])
        with ConversationMemoryStore(self.db) as store:
            store.record({"turn_id": "u1", "client": "codex", "client_session_id": "s",
                          "scope": "global", "role": "user", "content_redacted": "I moved",
                          "raw_sha256": "u1", "source_path": "p", "source_ordinal": 1,
                          "happened_at": "2026-11-01T10:00:00",
                          "known_at": "2026-11-01T10:00:00"})
        self.runner.tick()
        self.run_until_done()
        (call,) = self.calls()
        self.assertIn("brain_understand", call["prompt"])
        self.assertIn('mcp_servers.turnbook.tools.brain_understand.approval_mode="approve"',
                      call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("-C") + 1], str(self.state))
        self.assertIn("--skip-git-repo-check", call["argv"])      # the state folder: no git
        grant = self.runner.load()["grants"][0]
        self.assertNotIn("used_at", grant)                  # standing
        self.assertIn("last_run", grant)
        self.runner.tick()                                  # not again within six hours
        self.assertEqual(len(self.calls()), 1)
        # a long turn read only in part counts as something new
        import sqlite3
        db = sqlite3.connect(self.db)
        db.execute("CREATE TABLE IF NOT EXISTS understand_progress(scope TEXT PRIMARY KEY, "
                   "last_revision INTEGER, generation INTEGER, last_offset INTEGER)")
        revision = db.execute("SELECT MAX(revision) FROM conversation_turns").fetchone()[0]
        db.execute("INSERT OR REPLACE INTO understand_progress VALUES('global', ?, 1, 0)",
                   (revision,))
        db.commit()
        self.assertEqual(self.runner._nothing_new("global"), "NOTHING_NEW")
        db.execute("UPDATE understand_progress SET last_offset=6000")
        db.commit()
        db.close()
        self.assertIsNone(self.runner._nothing_new("global"))

    def test_daily_limit(self):
        self.enable_and_grant()
        policy = self.runner.load()
        today = worker_runner._now()
        policy["runs"] = [{"started": today}] * worker_runner.DAILY
        self.runner.save(policy)
        self.runner.tick()
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
