"""Persistent background capture for Turnbook code and conversation memory."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import subprocess
import sys
import time

from turnbook.associative_memory import AssociativeMemoryIndex
from turnbook.coding_memory import CodingMemoryCapture, CodingMemoryStore
from turnbook.conversation_memory import (
    ClaudeCodeConversationCapture,
    ClaudeCoworkConversationCapture,
    CodexConversationCapture,
    ConversationMemoryStore,
    CopilotConversationCapture,
    CursorConversationCapture,
    HookConversationCapture,
    OpenCodeConversationCapture,
    copilot_root,
    cowork_root,
    cursor_root,
    opencode_root,
)
from turnbook.paths import default_coding_state_dir, default_development_db, turnbook_home

SERVICE_VERSION = "automatic-capture-v0.2"


def default_config_path():
    return turnbook_home() / "automatic-capture.json"


def default_service_state_dir():
    return turnbook_home() / "automatic-capture"


def running_service(state_dir=None):
    """The live capture service's pid, or None. A pid file left by a crash, or naming a pid the
    system has since given to another program, is not a running service."""
    import psutil
    try:
        path = Path(state_dir or default_service_state_dir()) / "service.pid"
        pid = int(path.read_text(encoding="ascii"))
        mine = (os.getpid(), os.getppid())      # a venv's pythonw runs the service as its child
        if pid in mine:
            return None
        process = psutil.Process(pid)
        # Just started, a child can still show its parent's command line (Linux, before exec):
        # a live pid written moments ago is the service, or a second one would start beside it.
        fresh = time.time() - path.stat().st_mtime < 10 and process.create_time() >= \
            path.stat().st_mtime - 10
        if fresh or "capture_cli" in " ".join(process.cmdline()):
            return pid
    except (OSError, ValueError, psutil.Error):
        pass
    return None


def ensure_service() -> bool:
    """Start the capture service unless it runs; True when it was started.

    It starts at login, and nothing else brought it back: a crash, a Task Manager kill or an
    update closing it meant no conversation was saved until the next login. Now every memory
    server of an app that relies on it checks it now and then and starts it again."""
    import fasteners
    state = default_service_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(state / "start.lock")):    # two apps, one service
        if running_service(state):
            return False
        python = Path(sys.executable)
        if sys.platform == "win32":
            python = python.with_name("pythonw.exe")                 # no console window
            detach = {"creationflags": subprocess.DETACHED_PROCESS}
        else:
            detach = {"start_new_session": True}
        child = subprocess.Popen([str(python), "-m", "turnbook.capture_cli", "run"],
                                 cwd=Path.home(), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
        (state / "service.pid").write_text(f"{child.pid}\n", encoding="ascii")
        return True


EMBED_BATCH = 512               # pieces embedded per pass
MIGRATE_BATCH = 64              # pieces read again with gemma per quiet pass (~15 s at half pace)
EMBED_RECHECK_SECONDS = 600     # look for unembedded turns at least this often
PULL_SECONDS = 60               # other PCs' lines brought in this often


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _workspace_key(path):
    return str(Path(path).expanduser().resolve())


def make_config(*, workspace, project_id, session_id, sessions_root,
                poll_seconds=5, code_poll_seconds=120, db_path=None):
    workspace = _workspace_key(workspace)
    sessions_root = _workspace_key(sessions_root)
    return {
        "service_version": SERVICE_VERSION,
        "db_path": str(Path(db_path or default_development_db()).expanduser().resolve()),
        "poll_seconds": max(1, int(poll_seconds)),
        "code_poll_seconds": max(int(poll_seconds), int(code_poll_seconds)),
        "codex_sessions_root": sessions_root,
        "workspace_scopes": {workspace: project_id},
        "code_workspaces": [{"workspace": workspace, "project_id": project_id,
                             "session_id": session_id,
                             "state_dir": str(default_coding_state_dir(project_id))}],
    }


def load_config(path=None):
    path = Path(path or default_config_path()).expanduser().resolve()
    if not path.is_file():
        raise ValueError("AUTOMATIC_CAPTURE_CONFIG_REQUIRED")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("service_version") != SERVICE_VERSION:
        raise ValueError("AUTOMATIC_CAPTURE_CONFIG_VERSION_MISMATCH")
    if not isinstance(value.get("code_workspaces"), list):
        raise ValueError("AUTOMATIC_CAPTURE_WORKSPACES_REQUIRED")
    return value


class AutomaticCaptureService:
    def __init__(self, config, *, state_dir=None, now=None):
        self.config = config
        self.state_dir = Path(state_dir or default_service_state_dir()).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.state_dir / "status.json"
        self.pid_path = self.state_dir / "service.pid"
        self.conversation_cursor = self.state_dir / "codex-session-cursor.json"
        self.claude_cursor = self.state_dir / "claude-code-session-cursor.json"
        self.cowork_cursor = self.state_dir / "claude-cowork-session-cursor.json"
        self.opencode_cursor = self.state_dir / "opencode-session-cursor.json"
        self.now = now or _now
        self.db_path = Path(config["db_path"]).expanduser().resolve()

    def _coding_capture(self, entry, store):
        return CodingMemoryCapture(entry["workspace"], store,
            project_id=entry["project_id"], session_id=entry["session_id"],
            state_dir=entry.get("state_dir") or default_coding_state_dir(
                entry["project_id"]), now=self.now)

    def _shared(self):
        """The shared folder or Postgres URL, if any: config, TURNBOOK_FOLDER, or the one
        chosen in conversation; the service's own wins."""
        from turnbook.sharing import folder
        shared = folder()
        if self.config.get("memory_folder"):
            from turnbook.sharing import is_database
            chosen = self.config["memory_folder"]
            shared = chosen if is_database(chosen) else Path(chosen).expanduser()
        return shared

    def pull_shared(self):
        """Other PCs' new lines into this PC's store. Only the memory servers did it, on a tool
        call; the per-message hook, which no longer waits on a hub, would miss them meanwhile.
        A size check per log file when nothing changed."""
        try:
            shared = self._shared()
            if shared:
                from turnbook.sharing import sync
                sync(shared, self.db_path)
        except Exception:                       # the hub PC is off: next time
            pass

    def _conversation_captures(self, store):
        """Every conversation source on this machine, each with its own read cursor.

        Codex is configured explicitly. Claude Code is picked up from ~/.claude/projects when
        that folder exists, unless claude_projects_root names another one, so that whatever the
        user talks to is stored in full and not only what a model chose to remember.
        """
        scopes = self.config.get("workspace_scopes", {})
        # With a shared folder, every captured turn also goes into the shared log, so a
        # conversation held on this PC can be recalled on any other (sharing.py).
        shared = self._shared()
        publish = None
        if shared:
            from turnbook.sharing import append

            def publish(turn):                  # to a folder or a Postgres URL
                append(shared, "turn", turn)
        from turnbook.forget import forget_ids

        def forget(turn_ids):
            forget_ids({"turn_ids": turn_ids}, self.db_path, shared)
        hooks = {"workspace_scopes": scopes, "now": self.now, "publish": publish,
                 "forget": forget}
        sources = []
        # A source whose folder does not exist yet (Codex never run on this PC) is skipped, not
        # fatal: it used to stop every source, so a Claude-Code-only user captured nothing.
        if Path(self.config["codex_sessions_root"]).is_dir():
            sources.append(("codex", CodexConversationCapture(
                self.config["codex_sessions_root"], store, state_path=self.conversation_cursor,
                **hooks), self.conversation_cursor))
        claude_root = Path(self.config.get("claude_projects_root")
                           or Path.home() / ".claude" / "projects")
        if claude_root.is_dir():
            sources.append(("claude-code", ClaudeCodeConversationCapture(
                claude_root, store, state_path=self.claude_cursor, **hooks), self.claude_cursor))
        cowork = self.config.get("claude_cowork_root") or cowork_root()
        if cowork and Path(cowork).is_dir():
            sources.append(("claude-cowork", ClaudeCoworkConversationCapture(
                cowork, store, state_path=self.cowork_cursor, **hooks), self.cowork_cursor))
        opencode = Path(self.config.get("opencode_root") or opencode_root())
        if opencode.is_dir():
            sources.append(("opencode", OpenCodeConversationCapture(
                opencode, store, state_path=self.opencode_cursor, **hooks), self.opencode_cursor))
        copilot = Path(self.config.get("copilot_root") or copilot_root())
        if copilot.is_dir():
            cursor = self.state_dir / "copilot-cli-session-cursor.json"
            sources.append(("copilot-cli", CopilotConversationCapture(
                copilot, store, state_path=cursor, **hooks), cursor))
        cursor_projects = Path(self.config.get("cursor_root") or cursor_root())
        if cursor_projects.is_dir():
            cursor = self.state_dir / "cursor-session-cursor.json"
            sources.append(("cursor", CursorConversationCapture(
                cursor_projects, store, state_path=cursor, **hooks), cursor))
        from turnbook.documents import DocumentCapture, folders
        if folders():                           # turnbook docs add <folder>
            cursor = self.state_dir / "documents-state.json"
            sources.append(("documents", DocumentCapture(folders(), store, state_path=cursor,
                                                         **hooks), cursor))
        from turnbook.hook import hooks_root
        agents = Path(self.config.get("hooks_root") or hooks_root())
        for agent_dir in sorted(agents.iterdir()) if agents.is_dir() else []:
            if agent_dir.is_dir():
                # "hook-": never the same file as a transcript source of the same agent
                cursor = self.state_dir / f"hook-{agent_dir.name}-session-cursor.json"
                sources.append((agent_dir.name, HookConversationCapture(
                    agent_dir.name, agent_dir, store, state_path=cursor, **hooks), cursor))
        return sources

    def baseline(self):
        coding = []
        with CodingMemoryStore(self.db_path) as store:
            for entry in self.config["code_workspaces"]:
                capture = self._coding_capture(entry, store)
                if capture.manifest_path.exists():
                    coding.append({"project_id": entry["project_id"],
                                   "status": "BASELINE_ALREADY_EXISTS",
                                   "manifest": str(capture.manifest_path)})
                else:
                    coding.append({"project_id": entry["project_id"],
                                   **capture.initialize_baseline()})
        with ConversationMemoryStore(self.db_path) as store:
            conversation = {
                name: ({"status": "BASELINE_ALREADY_EXISTS", "state_path": str(cursor)}
                       if cursor.exists() else capture.initialize_baseline())
                for name, capture, cursor in self._conversation_captures(store)}
        result = {"status": "BASELINE_READY", "service_version": SERVICE_VERSION,
                  "at": self.now(), "coding": coding,
                  "conversation": conversation.get("codex"), "conversation_sources": conversation}
        self._write_status(result)
        return result

    def run_once(self, *, capture_code=True):
        started = self.now()
        coding, errors = [], []
        from turnbook.forget import retry_deletions
        try:
            retry_deletions(self.db_path, wait=True)
        except Exception as error:
            errors.append({"source": "forget_retry", "error": type(error).__name__})
        # where a pass spends its time, in the status file: an idle service once took 8% of a
        # core and nothing said which step
        seconds, clock = {}, time.perf_counter()
        if capture_code:
            with CodingMemoryStore(self.db_path) as store:
                for entry in self.config["code_workspaces"]:
                    try:
                        capture = self._coding_capture(entry, store)
                        if not capture.manifest_path.exists():
                            outcome = capture.initialize_baseline()
                        else:
                            if not getattr(self, "_deletions_checked", False):
                                capture.drop_false_deletions()     # once per service start
                            try:
                                outcome = capture.scan(
                                    reason="Automatically observed workspace change; "
                                           "intent pending", related_tests=[])
                            except ValueError as error:
                                # another folder, file kinds or exclusions in the config: the
                                # old manifest failed every 2 minutes and code capture stopped
                                if str(error) != "CODING_MANIFEST_SCOPE_OR_VERSION_MISMATCH":
                                    raise
                                outcome = capture.initialize_baseline()
                        coding.append({"project_id": entry["project_id"], **outcome})
                    except Exception as error:  # keep other capture sources alive
                        errors.append({"source": "code",
                            "project_id": entry.get("project_id"),
                            "error": type(error).__name__, "detail": str(error)})
            self._deletions_checked = True
        else:
            coding.append({"status": "SKIPPED_NOT_DUE"})
        seconds["code"], clock = round(time.perf_counter() - clock, 3), time.perf_counter()
        conversation = {}
        try:
            with ConversationMemoryStore(self.db_path) as store:
                for name, capture, cursor in self._conversation_captures(store):
                    try:                       # one broken source must not stop the others
                        conversation[name] = (capture.scan() if cursor.exists()
                                              else capture.initialize_baseline())
                    except Exception as error:
                        errors.append({"source": f"conversation:{name}",
                                       "error": type(error).__name__, "detail": str(error)})
        except Exception as error:
            errors.append({"source": "conversation", "error": type(error).__name__,
                           "detail": str(error)})
        seconds["conversations"], clock = round(time.perf_counter() - clock, 3), time.perf_counter()
        # Idle passes re-read ~11k turns twice for nothing: skip them while the store's files
        # (the database and its write-ahead log) are exactly as the last idle pass left them.
        version = []
        for suffix in ("", "-wal"):
            try:
                info = os.stat(str(self.db_path) + suffix)
                version.append((info.st_mtime_ns, info.st_size))
            except OSError:
                version.append(None)
        unchanged = version is not None and version == getattr(self, "_maintenance_version", None)
        # No memories are made from turns by keyword any more (0.10.5): "앞으로"/"하기로" in a
        # question or a pasted chat made a "decision", and no English sentence ever did. The
        # model judges what to keep and saves it with brain_remember.
        associative = None
        if unchanged:
            associative = self._idle_indexes
        else:
            try:
                with AssociativeMemoryIndex(self.db_path) as index:
                    associative = index.sync_sources(limit=1000)
            except Exception as error:
                errors.append({"source": "associative", "error": type(error).__name__,
                               "detail": str(error)})
            if not errors and associative["indexed_total"] == 0:
                self._maintenance_version = version
                self._idle_indexes = associative
        seconds["indexes"] = round(time.perf_counter() - clock, 3)
        seconds["indexes_skipped"] = unchanged
        result = {"status": "CAPTURE_ERRORS" if errors else "CAPTURE_OK",
                  "service_version": SERVICE_VERSION, "started_at": started,
                  "completed_at": self.now(), "coding": coding,
                  "conversation": conversation.get("codex"),
                  "conversation_sources": conversation,
                  "associative": associative,
                  "errors": errors, "seconds": seconds,
                  "warnings": self._stall_warnings(conversation)}
        # every 5 s pass rewrote it (17k writes a day): when only the times moved, once a minute
        same = {k: v for k, v in result.items()
                if k not in ("started_at", "completed_at", "seconds")}
        if same != getattr(self, "_last_status", None) or (
                time.monotonic() - getattr(self, "_status_at", -1e9) >= 60):
            self._write_status(result)
            self._last_status, self._status_at = same, time.monotonic()
        return result

    STALL_ROWS = 2000

    def _stall_warnings(self, conversation):
        """A source whose transcripts keep growing while no conversation comes out of them: the
        app most likely changed its transcript format, and capture would otherwise stop without
        a word. Counted across passes in capture-health.json; reset by the next turn found."""
        path = self.state_dir / "capture-health.json"
        try:
            health = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            health = {}
        before = json.dumps(health, sort_keys=True)
        warnings = []
        for name, outcome in conversation.items():
            if not isinstance(outcome, dict) or "scanned_rows" not in outcome:
                continue
            entry = health.setdefault(name, {"rows_without_turns": 0, "last_turn_at": None})
            if outcome.get("recorded_turns"):
                entry.update(rows_without_turns=0, last_turn_at=self.now())
            else:
                entry["rows_without_turns"] += outcome["scanned_rows"]
            if entry["rows_without_turns"] >= self.STALL_ROWS:
                warnings.append(
                    f"{name}: {entry['rows_without_turns']} new transcript lines and no "
                    f"conversation found in them since {entry['last_turn_at'] or 'capture began'}"
                    " - the app may have changed its format; update Turnbook or report it")
        if json.dumps(health, sort_keys=True) != before:
            _atomic_json(path, health)
        return warnings

    def _write_status(self, result):
        try:
            _atomic_json(self.status_path, {**result, "pid": os.getpid(),
                                           "config_db": str(self.db_path)})
        except OSError:                 # a full disk or a locked file must not stop capture
            pass

    def embed_new_turns(self, limit=None):
        """Make what was just captured searchable by meaning before anyone asks about it (the
        memory servers pick the vectors up from the shared cache). Only the long-running service
        does this: a one-off `once` or a test should not load the embedding model."""
        try:
            from turnbook.local_embedder import default_embedder
            from turnbook.warmup import warm_turns
            if getattr(self, "_embedder", None) is None:
                self._embedder = default_embedder(self.db_path)
            return warm_turns(self.db_path, limit=limit or EMBED_BATCH, embedder=self._embedder)
        except Exception:                       # capture must go on if embedding cannot
            return 0

    def migrate_some(self):
        """A memory from 0.9.47 or before (local_embedder.migrating): read a few more pieces with
        gemma while nothing else is due. Search keeps mpnet meanwhile; once every piece has a
        gemma vector, model.txt names gemma and the service starts again with it. True when
        that moment came."""
        try:
            from turnbook.local_embedder import GEMMA, default_embedder, model_file
            from turnbook.warmup import warm_turns
            if getattr(self, "_gemma", None) is None:
                self._gemma = default_embedder(self.db_path, model=GEMMA)
            if warm_turns(self.db_path, limit=MIGRATE_BATCH, embedder=self._gemma):
                return False
            self._gemma.flush()
            model_file().parent.mkdir(parents=True, exist_ok=True)
            model_file().write_text("gemma\n", encoding="utf-8")
            return True
        except Exception:                       # capture must go on if the new model cannot
            return False

    def compress_stored(self, limit=2000):
        """True while there is more to look at; resumes after the last turn looked at. Turns
        added meanwhile are packed as they are stored (one written by a process still on older
        code waits for the next start)."""
        try:
            with ConversationMemoryStore(self.db_path) as store:
                if not store.packing():
                    # at most every 10 minutes: while turns arrive this ran every 5 s, and
                    # each look at the PC's processes costs CPU (8% of a core measured)
                    if time.monotonic() - getattr(self, "_older_checked", -1e9) < 600:
                        return False
                    self._older_checked = time.monotonic()
                    from turnbook.packing import older_processes
                    if older_processes():
                        return False             # asked again at the next recheck
                    store.start_packing()
                _, last = store.compress_some(limit, after=self._packed_upto)
        except Exception:                       # capture must go on if packing cannot
            return False
        more = last != self._packed_upto
        self._packed_upto = last
        self._packed_all = not more
        return more

    def lend_model(self):
        """Load the model now and let the memory servers use it (local_embedder.serve), so an
        app's search by meaning works from its first question without loading its own."""
        try:
            from turnbook.local_embedder import default_embedder, serve
            if getattr(self, "_embedder", None) is None:
                self._embedder = default_embedder(self.db_path)
            self._embedder.embed(["warm up"])
            serve(self._embedder, context=self.answer_context)
            # the index read now and kept (one copy, ~170 MB on an 11k-turn memory): a hook
            # waits DEADLINE seconds at most, and a cold read took up to 4 s
            import threading
            threading.Thread(target=self.answer_context, args=("where did we leave off?",),
                             daemon=True).start()
        except Exception:                       # no model on disk: the servers load their own
            pass

    def answer_context(self, message: str) -> str:
        """The memory for one message, for the per-message hook (turnbook.inject)."""
        from turnbook.inject import context
        return context(message, database=self.db_path, embedder=self._embedder)

    def run_forever(self, *, max_iterations=None):
        if running_service(self.state_dir):
            return                              # one service per PC (login item + a restart)
        # held for the service's life: a login start racing ensure_service() both passed the
        # pid check and ran side by side, scanning and embedding twice (the OS frees it on exit)
        import fasteners
        alone = fasteners.InterProcessLock(str(self.state_dir / "service.lock"))
        if not alone.acquire(blocking=False):
            return
        self.pid_path.write_text(str(os.getpid()) + "\n", encoding="ascii")
        migrate = False
        if max_iterations is None:
            from turnbook.local_embedder import LocalEmbedder, migrating
            LocalEmbedder.PACE = True       # it runs beside the user's work: backlogs at half pace
            try:                            # and below the user's own programs
                import psutil
                psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS
                                      if sys.platform == "win32" else 10)
            except Exception:
                pass
            self.lend_model()
            migrate = migrating()
            if not migrate:
                # models nothing reads any more: an upgrade by pip alone never ran the installer
                import threading

                from turnbook.install import remove_old_models
                threading.Thread(target=remove_old_models, daemon=True).start()
            try:                        # once per store: copies stored before 0.9.2, and the
                with ConversationMemoryStore(self.db_path) as store:      # keyword-made memories
                    store.drop_duplicates()
                from turnbook.semantic_memory import drop_keyword_memories
                drop_keyword_memories(self.db_path)
            except Exception:
                pass                    # tried again at the next start
        iteration, restart = 0, False
        last_code_scan = None
        pending, packing, last_embed = True, max_iterations is None, time.monotonic()
        last_pull = 0.0
        self._packed_upto, self._packed_all = 0, False
        from turnbook.worker_runner import WorkerRunner
        workers = WorkerRunner(self.state_dir, self.db_path)     # off unless the user enables it
        try:
            while max_iterations is None or iteration < max_iterations:
                monotonic_now = time.monotonic()
                try:                    # first, so a failing capture pass cannot leave a
                    workers.tick()      # worker past its deadline or a stop unheeded
                except Exception:       # the hand never takes the memory down
                    pass
                code_due = (last_code_scan is None or monotonic_now - last_code_scan
                            >= self.config.get("code_poll_seconds", 120))
                try:
                    result = self.run_once(capture_code=code_due)
                except Exception:       # one bad pass: try again next time, never exit
                    if max_iterations is not None:
                        raise
                    time.sleep(self.config["poll_seconds"] * 6)
                    continue
                recorded = any(isinstance(s, dict) and s.get("recorded_turns")
                               for s in result["conversation_sources"].values())
                # finding unembedded pieces reads every stored turn: on a 400 MB memory that
                # was half a CPU core and ~1 GB all day while nothing new came in. Only when
                # turns arrived, a batch left more behind, or every 10 minutes (turns synced in
                # from another PC)
                if pending or recorded or monotonic_now - last_embed >= EMBED_RECHECK_SECONDS:
                    pending = self.embed_new_turns() >= EMBED_BATCH
                    last_embed = time.monotonic()
                    packing = packing or (max_iterations is None and not self._packed_all)
                if packing:
                    # turns stored before 0.9.27, or by an app still on older code: one round
                    # over the store after each start, 2,000 turns a pass (~20 ms)
                    packing = self.compress_stored()
                elif migrate and not pending and not recorded and self.migrate_some():
                    restart = True              # the new model loads in a new service
                    break
                if code_due:
                    last_code_scan = time.monotonic()
                if time.monotonic() - last_pull >= PULL_SECONDS:
                    self.pull_shared()
                    last_pull = time.monotonic()
                iteration += 1
                if max_iterations is None or iteration < max_iterations:
                    time.sleep(self.config["poll_seconds"])
        finally:
            try:
                if self.pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                    self.pid_path.unlink()
            except (FileNotFoundError, OSError):
                pass
            alone.release()
        if restart:
            ensure_service()

