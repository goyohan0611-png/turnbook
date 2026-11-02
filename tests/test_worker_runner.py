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
        self.assertEqual(call["argv"][:5], ["exec", "-C", str(self.repo), "-s", "read-only"])
        self.assertIn(f"Your owner name: {running['owner']}", call["prompt"])
        self.assertIn("Task: verify", call["prompt"])
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
