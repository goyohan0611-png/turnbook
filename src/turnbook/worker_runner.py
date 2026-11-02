"""Opt-in: the capture service starts a read-only Codex review of a task the user granted.

The one brain's "hand" (docs/design/agent-continuity.md, level 2). Off until
`turnbook-capture workers-enable`; then only tasks granted one by one with `workers-grant`
(a grant is used once), only while the work tree is clean, one worker at a time, at most
DAILY launches a day, each stopped after TIMEOUT. The launched worker claims the task itself
under the owner name in its prompt; the launcher never claims for it. A run that ends without
completing pauses its own task (only if that run still owns it), so nothing is left held.

Not promised: waking an app window, knowing a queued prompt was read, other PCs.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

# a login item starts the service with a short PATH (macOS launchd: /usr/bin:/bin...), without
# where Homebrew, npm or pipx put a CLI
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.npm-global/bin",
              "~/AppData/Roaming/npm")
DAILY = 4
TIMEOUT = 600                        # seconds a launched worker may run
ROLE = "read-only review"


def _now():
    return datetime.now().isoformat(timespec="seconds")


class WorkerRunner:
    def __init__(self, state_dir, db_path, command=("codex",)):
        self.path = Path(state_dir) / "workers.json"
        self.db_path = db_path
        self.command = list(command)
        self._child = None           # the running worker, when this process started it

    # the policy file is shared with the CLI: every read-modify-write holds this lock, or a
    # tick that read it before `workers-disable` wrote it back enabled
    def lock(self):
        import fasteners
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return fasteners.InterProcessLock(str(self.path.with_suffix(".lock")))

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        value.setdefault("enabled", False)
        value.setdefault("grants", [])
        value.setdefault("runs", [])
        return value

    def save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
        os.replace(temporary, self.path)

    def tick(self):
        """Once per service pass: watch the running worker, else maybe start one."""
        with self.lock():
            self._tick()

    def _tick(self):
        policy = self.load()
        running = policy.get("running")
        if running:
            self._watch(policy, running)
            return
        if not policy["enabled"]:
            return
        today = _now()[:10]
        if sum(run.get("started", "").startswith(today) for run in policy["runs"]) >= DAILY:
            return
        for grant in policy["grants"]:
            if grant.get("used_at"):
                continue
            if not self._program():
                grant["waiting"] = "CODEX_NOT_FOUND"
                break
            why = self._not_ready(grant)
            if why:
                grant["waiting"] = why
                continue
            self._start(policy, grant)
            break
        self.save(policy)

    def _program(self):
        """The command without a shell in between. On Windows npm installs codex as
        codex.cmd, a batch file: its arguments go through cmd.exe even without shell=True, and
        a folder like C:/R&D would run "D" as a command. So node runs the codex.js it wraps."""
        found = shutil.which(self.command[0], path=os.pathsep.join(
            [os.environ.get("PATH", ""), *(str(Path(d).expanduser()) for d in EXTRA_PATH)]))
        if not found or not found.lower().endswith((".cmd", ".bat")):
            return [found, *self.command[1:]] if found else None
        folder = Path(found).parent
        script = folder / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = folder / "node.exe" if (folder / "node.exe").exists() else shutil.which("node")
        if not script.exists() or not node:
            return None                      # an unknown shim: not started at all
        return [str(node), str(script), *self.command[1:]]

    def _server(self):
        """The worker's memory server, whole: a plugin install or a Codex installed after
        turnbook-install has none in its config, and overrides of a missing entry stop Codex
        ("invalid transport"). The same Turnbook code and store this service runs on, so the
        worker reads the task the service just checked. Values in JSON, which is TOML here
        (characters as they are: an escaped emoji, \\ud83e\\udde0, is not valid TOML)."""
        def toml(value):
            return json.dumps(value, ensure_ascii=False)
        source = str(Path(__file__).resolve().parents[1])
        start = (f"import sys; sys.path.insert(0, {source!r}); "
                 "from turnbook.mcp_server import main; main()")
        return ["-c", "mcp_servers.turnbook.command=" + toml(sys.executable),
                "-c", "mcp_servers.turnbook.args=" + toml(["-c", start]),
                "-c", "mcp_servers.turnbook.env=" + "{" + ",".join(
                    f"{k}={toml(v)}" for k, v in (
                        ("TURNBOOK_DB", str(self.db_path)), ("PYTHONUTF8", "1"),
                        ("PYTHONIOENCODING", "utf-8"),
                        # a service on another home: the worker's server shares it
                        *((("TURNBOOK_HOME", os.environ["TURNBOOK_HOME"]),)
                          if os.environ.get("TURNBOOK_HOME") else ()))) + "}",
                "-c", "mcp_servers.turnbook.startup_timeout_sec=60",
                "-c", 'mcp_servers.turnbook.tools.brain_work.approval_mode="approve"']

    def _not_ready(self, grant):
        from turnbook.work_state import WorkState, workspace_key
        with WorkState(self.db_path) as work:
            packet = work.read(grant["workspace"], grant["task"])
            if not packet or packet["state"] not in {"READY", "PAUSED"}:
                return "TASK_NOT_WAITING"
            if packet.get("assigned_to", "any") not in {"any", "codex"}:
                return "ASSIGNED_TO_ANOTHER_AGENT"
            for dependency in packet.get("depends_on", []):
                dep = work.read(grant["workspace"], dependency)
                if not dep or dep["state"] != "COMPLETE":
                    return "DEPENDENCIES_NOT_COMPLETE"
                if not work._current_dependency(workspace_key(grant["workspace"]), dep):
                    return "DEPENDENCY_CHANGED"     # claim would refuse it: keep the grant
        try:
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                   cwd=grant["workspace"], capture_output=True, text=True,
                                   timeout=30)
        except (OSError, subprocess.SubprocessError):
            return "NOT_A_GIT_FOLDER"
        if dirty.returncode != 0:
            return "NOT_A_GIT_FOLDER"
        if dirty.stdout.strip():
            return "UNCOMMITTED_CHANGES"     # a review of a tree that can still change
        return None

    def _start(self, policy, grant):
        owner = f"codex:auto-{uuid.uuid4().hex[:8]}"
        prompt = (
            "Turnbook started you to continue one task of the user's work board, as a "
            f"{ROLE}: do not modify files.\n"
            f"Workspace: {grant['workspace']}\nTask: {grant['task']}\nYour owner name: {owner}\n"
            "First call brain_work status for this workspace and task, then claim it with "
            "that owner name and the revision you read. If the claim fails, stop without "
            "working. Use the inputs the claim returns. Check the referenced files and run "
            "the checks that apply. Complete with your findings and the checks you ran, or "
            "pause with what is left as the next action.")
        log = self.path.parent / "workers.log"
        flags = {}
        if sys.platform == "win32":
            flags["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                      | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        # an unattended run starts from Codex's defaults, not the user's config.toml: none of
        # their hooks, other MCP servers or profiles run in it (auth is kept: it is not in
        # config.toml). On Windows the read-only sandbox needs the elevated mode to run even a
        # directory listing (smoke 11-02), so that one setting is given.
        sandbox = ["-c", 'windows.sandbox="elevated"'] if sys.platform == "win32" else []
        argv = [*self._program(),
                "exec", "--ignore-user-config", *sandbox,
                "-C", grant["workspace"], "-s", "read-only",
                "-c", 'approval_policy="never"',
                *self._server(), "-"]
        # recorded first (the grant used, the day's count, a run with no pid yet): if this
        # cannot be saved nothing starts, so no worker ever runs unrecorded
        grant["used_at"] = _now()
        grant.pop("waiting", None)
        policy["running"] = {"pid": None, "created": None, "owner": owner,
                             "workspace": grant["workspace"], "task": grant["task"],
                             "started": _now(), "deadline": time.time() + TIMEOUT}
        policy["runs"].append({"started": _now(), "owner": owner, "task": grant["task"],
                               "workspace": grant["workspace"]})
        policy["runs"] = policy["runs"][-50:]
        self.save(policy)
        try:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n== {_now()} {owner} {grant['workspace']} {grant['task']}\n")
                handle.flush()
                child = subprocess.Popen(argv, cwd=grant["workspace"], stdin=subprocess.PIPE,
                                         stdout=handle, stderr=subprocess.STDOUT, **flags)
        except OSError:
            policy.pop("running")
            policy["runs"][-1]["exit"] = "launch_failed"
            return
        try:
            child.stdin.write(prompt.encode("utf-8"))
            child.stdin.close()
        except OSError:
            pass
        self._child = child
        policy["running"].update(pid=child.pid, created=self._created(child.pid))
        # the deadline holds even when capture passes are slow or far apart; the next tick
        # records it as a timeout
        import threading
        timer = threading.Timer(TIMEOUT, self._kill, [dict(policy["running"])])
        timer.daemon = True
        timer.start()
        # ponytail: if the service dies between Popen and the save below, the restarted one
        # sees pid None and cannot find this child; a Windows job object / process group tied
        # to the service would end it with the service
        try:
            self.save(policy)
        except OSError:                      # untracked, it could be started again: end it
            self._kill(policy["running"])
            self._child = None
            raise

    def _watch(self, policy, running):
        code = None
        if self._child is not None and self._child.pid == running["pid"]:
            code = self._child.poll()
            alive = code is None
        else:                        # the service restarted: the pid, if still the worker
            alive = self._is_worker(running)
        stop = policy.get("stop")    # disabling alone lets a running review finish
        if alive and (stop or time.time() > running["deadline"]):
            if not self._kill(running):
                # not shown to have ended (identity unknown, access denied, still running):
                # it stays tracked and keeps its claim, so nothing else starts or takes the
                # task; the next tick tries again
                running["stop_unconfirmed"] = _now()
                self.save(policy)
                return
            code, alive = "stopped" if stop else "timeout", False
        if alive:
            return
        if code not in (0, "stopped") and time.time() > running["deadline"]:
            code = "timeout"             # ended by the deadline timer before this tick
        self._child = None
        policy.pop("running")
        policy.pop("stop", None)
        policy["runs"][-1:] = [{**run, "ended": _now(), "exit": code}
                               for run in policy["runs"][-1:]]
        self._release(running, code)
        self.save(policy)

    @staticmethod
    def _created(pid):
        try:
            import psutil
            return psutil.Process(pid).create_time()
        except Exception:
            return None

    def _is_worker(self, running):
        """After a restart or reboot the pid may be another program's: the same creation time
        is what says it is still the worker."""
        created = running.get("created")
        return created is not None and self._created(running["pid"]) == created

    def _kill(self, running):
        """End the worker and everything it started (a shell, its tests), never another
        program. True only once it is shown to be gone. The identity is checked on the very
        Process object that is killed: a second lookup could be a new program that got the
        pid in between, and psutil's own reuse check would then guard the wrong process."""
        # ponytail: a child that detached from the tree survives; a Windows job object would not
        import psutil
        created = running.get("created")
        if created is None:
            return False                     # cannot tell it from another program
        try:
            worker = psutil.Process(running["pid"])
            if worker.create_time() != created:
                return True                  # the worker is gone; the pid is someone else's
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            return False
        try:
            family = worker.children(recursive=True)
        except psutil.Error:
            family = []
        for process in [*family, worker]:
            try:
                process.kill()               # psutil refuses if this object's pid was reused
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                return False
        try:
            _, left = psutil.wait_procs([worker], timeout=10)
        except psutil.Error:
            return False
        if left:
            return False
        if self._child is not None and self._child.pid == running["pid"]:
            try:
                self._child.wait(timeout=5)  # reaped: no handle left open
            except Exception:
                pass
        return True

    def _release(self, running, code):
        """A run that ended still holding its task pauses it, so the user or another worker
        can continue. Only this run's own claim: anything else is left as it is."""
        from turnbook.work_state import WorkState
        with WorkState(self.db_path) as work:
            packet = work.read(running["workspace"], running["task"])
            if packet and packet["state"] == "ACTIVE" and packet["owner"] == running["owner"]:
                work.change("pause", running["workspace"], agent=running["owner"],
                            expected_revision=packet["revision"], task=running["task"],
                            next_action=packet.get("next_action") or
                            f"The automatic review ended unfinished ({code}): continue it.")


def cli(command, args, state_dir, db_path):
    """turnbook-capture workers-enable | workers-disable [--stop] | workers-status |
    workers-grant --workspace W --task T"""
    runner = WorkerRunner(state_dir, db_path)
    with runner.lock():
        return _cli(runner, command, args)


def _cli(runner, command, args):
    policy = runner.load()
    if command == "workers-enable":
        policy["enabled"] = True
    elif command == "workers-disable":
        policy["enabled"] = False
        if args.stop and policy.get("running"):
            policy["stop"] = True
    elif command == "workers-grant":
        from turnbook.work_state import task_key, workspace_key
        workspace_key(args.workspace)
        policy["grants"].append({"workspace": os.path.abspath(args.workspace),
                                 "task": task_key(args.task), "granted_at": _now()})
    if command != "workers-status":
        runner.save(policy)
    today = _now()[:10]
    return {"enabled": policy["enabled"], "running": policy.get("running"),
            "grants": policy["grants"],
            "launches_today": sum(r.get("started", "").startswith(today) for r in policy["runs"]),
            "daily_limit": DAILY, "timeout_seconds": TIMEOUT, "role": ROLE}
