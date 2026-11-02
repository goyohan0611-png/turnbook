"""Indexed coding activity memory with source-bound diffs and symbols."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import ast
import difflib
import hashlib
import json
import os
import re
import sqlite3
import stat

from turnbook.clock import timestamp
from turnbook.conversation_memory import _redact
from turnbook.paths import use_wal

CAPTURE_VERSION = "coding-memory-v0.1"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
DEFAULT_SUFFIXES = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".swift",
    ".go", ".rs", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".sql",
    ".toml", ".yaml", ".yml",
})
DEFAULT_EXCLUDED_PARTS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    "external", "runs",
})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _text(value, error):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value.strip()


def _string_list(value, error):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip()
                                              for item in value):
        raise ValueError(error)
    return list(dict.fromkeys(item.strip() for item in value))


def _decode_source(value):
    try:
        return value.decode("utf-8-sig", "strict")
    except UnicodeDecodeError:
        return value.decode("utf-8", "replace")


def _plain_directory(path):
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _python_symbols(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    result = []

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{node.name}" if prefix else node.name
                kind = "CLASS" if isinstance(node, ast.ClassDef) else "FUNCTION"
                result.append({"name": name, "kind": kind,
                               "start_line": node.lineno,
                               "end_line": getattr(node, "end_lineno", node.lineno)})
                visit(getattr(node, "body", []), name)
    visit(tree.body)
    return result


def _changed_lines(before, after):
    old_lines, new_lines = before.splitlines(), after.splitlines()
    old_changed, new_changed = set(), set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=True).get_opcodes():
        if tag == "equal":
            continue
        old_changed.update(range(i1 + 1, i2 + 1))
        new_changed.update(range(j1 + 1, j2 + 1))
    return old_changed, new_changed


def _affected_symbols(path, before, after):
    if Path(path).suffix.lower() != ".py":
        return []
    old_changed, new_changed = _changed_lines(before, after)
    found = {}
    for side, text, changed in (("BEFORE", before, old_changed),
                                ("AFTER", after, new_changed)):
        for symbol in _python_symbols(text):
            if any(symbol["start_line"] <= line <= symbol["end_line"] for line in changed):
                found[(side, symbol["name"])] = {**symbol, "side": side}
    if not found and (old_changed or new_changed):
        return [{"name": "<module>", "kind": "MODULE", "start_line": 1,
                 "end_line": max(len(after.splitlines()), 1), "side": "AFTER"}]
    return list(found.values())


class CodingMemoryStore:
    """Indexed structured code-change events; query never reads the workspace."""

    def __init__(self, path, *, clock="SOURCE_LOCAL"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        # Claude, Codex and the capture service each open this file from their own process.
        # WAL lets them read while one writes; the timeout makes a writer wait its turn
        # instead of failing with "database is locked".
        self.db = sqlite3.connect(str(self.path), timeout=30)
        use_wal(self.db)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS coding_changes(
          revision INTEGER PRIMARY KEY AUTOINCREMENT,
          change_id TEXT NOT NULL UNIQUE,
          project_id TEXT NOT NULL,
          session_id TEXT NOT NULL,
          workspace TEXT NOT NULL,
          happened_at TEXT NOT NULL,
          known_at TEXT NOT NULL,
          change_kind TEXT NOT NULL,
          file_path TEXT NOT NULL,
          language TEXT NOT NULL,
          before_sha256 TEXT,
          after_sha256 TEXT,
          reason TEXT NOT NULL,
          related_tests_json TEXT NOT NULL,
          diff_ref TEXT NOT NULL,
          before_ref TEXT,
          after_ref TEXT,
          provenance_json TEXT NOT NULL,
          fingerprint TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS coding_change_symbols(
          change_id TEXT NOT NULL,
          symbol_name TEXT NOT NULL,
          symbol_kind TEXT NOT NULL,
          side TEXT NOT NULL,
          start_line INTEGER NOT NULL,
          end_line INTEGER NOT NULL,
          PRIMARY KEY(change_id,symbol_name,side),
          FOREIGN KEY(change_id) REFERENCES coding_changes(change_id));
        CREATE INDEX IF NOT EXISTS coding_project_time
          ON coding_changes(project_id,happened_at DESC);
        CREATE INDEX IF NOT EXISTS coding_file_time
          ON coding_changes(file_path,happened_at DESC);
        CREATE INDEX IF NOT EXISTS coding_symbol_name
          ON coding_change_symbols(symbol_name,change_id);
        """)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()

    def _time(self, value):
        return timestamp(value, self.clock).isoformat(timespec="microseconds")

    def _query_time(self, value, *, end=False):
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            value += "T23:59:59.999999" if end else "T00:00:00"
        if isinstance(value, str) and self.clock == "SOURCE_LOCAL":
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                pass
            else:
                if parsed.utcoffset() is not None:
                    value = parsed.replace(tzinfo=None).isoformat(timespec="microseconds")
        return self._time(value)

    def record(self, change):
        required = ("change_id", "project_id", "session_id", "workspace",
                    "change_kind", "file_path", "language", "reason", "diff_ref")
        normalized = {key: _text(change.get(key), key.upper() + "_REQUIRED")
                      for key in required}
        normalized["happened_at"] = self._time(change.get("happened_at"))
        normalized["known_at"] = self._time(change.get("known_at"))
        if normalized["happened_at"] > normalized["known_at"]:
            raise ValueError("CODE_CHANGE_AFTER_RECORDING")
        normalized["before_sha256"] = change.get("before_sha256")
        normalized["after_sha256"] = change.get("after_sha256")
        normalized["before_ref"] = change.get("before_ref")
        normalized["after_ref"] = change.get("after_ref")
        normalized["related_tests"] = _string_list(
            change.get("related_tests"), "RELATED_TESTS_LIST_REQUIRED")
        symbols = change.get("symbols", [])
        if not isinstance(symbols, list):
            raise ValueError("CODE_SYMBOLS_LIST_REQUIRED")
        provenance = change.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("CODE_CHANGE_PROVENANCE_REQUIRED")
        normalized["provenance"] = {
            "source_type": _text(provenance.get("source_type"), "SOURCE_TYPE_REQUIRED"),
            "source_ref": _text(provenance.get("source_ref"), "SOURCE_REF_REQUIRED"),
        }
        fingerprint = _digest(_json({**normalized, "symbols": symbols}).encode("utf-8"))
        existing = self.db.execute(
            "SELECT revision,fingerprint FROM coding_changes WHERE change_id=?",
            (normalized["change_id"],)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("CODE_CHANGE_ID_COLLISION")
            return {"change_id": normalized["change_id"], "revision": existing["revision"],
                    "disposition": "ALREADY_RECORDED"}
        with self.db:
            cursor = self.db.execute("""INSERT INTO coding_changes(
              change_id,project_id,session_id,workspace,happened_at,known_at,
              change_kind,file_path,language,before_sha256,after_sha256,reason,
              related_tests_json,diff_ref,before_ref,after_ref,provenance_json,fingerprint)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                normalized["change_id"], normalized["project_id"],
                normalized["session_id"], normalized["workspace"],
                normalized["happened_at"], normalized["known_at"],
                normalized["change_kind"], normalized["file_path"],
                normalized["language"], normalized["before_sha256"],
                normalized["after_sha256"], normalized["reason"],
                _json(normalized["related_tests"]), normalized["diff_ref"],
                normalized["before_ref"], normalized["after_ref"],
                _json(normalized["provenance"]), fingerprint))
            for symbol in symbols:
                self.db.execute("""INSERT INTO coding_change_symbols VALUES(?,?,?,?,?,?)""", (
                    normalized["change_id"], _text(symbol.get("name"), "SYMBOL_NAME_REQUIRED"),
                    _text(symbol.get("kind"), "SYMBOL_KIND_REQUIRED"),
                    _text(symbol.get("side"), "SYMBOL_SIDE_REQUIRED"),
                    int(symbol.get("start_line")), int(symbol.get("end_line"))))
        return {"change_id": normalized["change_id"], "revision": cursor.lastrowid,
                "disposition": "RECORDED"}

    def query(self, *, project_id=None, happened_from=None, happened_to=None,
              file_path=None, symbol=None, change_kind=None, limit=20,
              include_diff_excerpt=True, max_diff_chars=6000):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("INVALID_CODE_QUERY_LIMIT")
        if type(max_diff_chars) is not int or not 200 <= max_diff_chars <= 20000:
            raise ValueError("INVALID_DIFF_EXCERPT_LIMIT")
        where, parameters = [], []
        if project_id:
            where.append("c.project_id=?")
            parameters.append(_text(project_id, "PROJECT_ID_REQUIRED"))
        if happened_from:
            where.append("c.happened_at>=?")
            parameters.append(self._query_time(happened_from))
        if happened_to:
            where.append("c.happened_at<=?")
            parameters.append(self._query_time(happened_to, end=True))
        if file_path:
            selected_path = _text(file_path, "FILE_PATH_REQUIRED").replace("\\", "/")
            where.append("(c.file_path=? OR c.file_path LIKE ?)")
            parameters.extend([selected_path, "%/" + selected_path])
        if change_kind:
            where.append("c.change_kind=?")
            parameters.append(_text(change_kind, "CHANGE_KIND_REQUIRED").upper())
        if symbol:
            where.append("EXISTS(SELECT 1 FROM coding_change_symbols s "
                         "WHERE s.change_id=c.change_id AND s.symbol_name LIKE ?)")
            parameters.append("%" + _text(symbol, "SYMBOL_REQUIRED") + "%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        parameters.append(limit)
        rows = self.db.execute("SELECT c.* FROM coding_changes c" + clause
                               + " ORDER BY c.happened_at DESC,c.revision DESC LIMIT ?",
                               parameters).fetchall()
        changes = []
        for row in rows:
            symbols = [dict(item) for item in self.db.execute("""SELECT symbol_name,symbol_kind,
              side,start_line,end_line FROM coding_change_symbols WHERE change_id=?
              ORDER BY symbol_name,side""", (row["change_id"],)).fetchall()]
            diff_excerpt, diff_truncated = None, False
            if include_diff_excerpt:
                try:
                    full_diff = Path(row["diff_ref"]).read_text(encoding="utf-8")
                    diff_excerpt = full_diff[:max_diff_chars]
                    diff_truncated = len(full_diff) > max_diff_chars
                except (OSError, UnicodeError):
                    diff_excerpt = None
            changes.append({"change_id": row["change_id"], "revision": row["revision"],
                "project_id": row["project_id"], "session_id": row["session_id"],
                "happened_at": row["happened_at"], "change_kind": row["change_kind"],
                "file_path": row["file_path"], "language": row["language"],
                "reason": row["reason"],
                "related_tests": json.loads(row["related_tests_json"]),
                "symbols": symbols, "diff_ref": row["diff_ref"],
                "diff_excerpt": diff_excerpt, "diff_truncated": diff_truncated,
                "before_ref": row["before_ref"], "after_ref": row["after_ref"],
                "provenance": json.loads(row["provenance_json"])})
        return {"status": "KNOWN" if changes else "UNKNOWN",
                "reason": "CODE_CHANGES_FOUND" if changes else "NO_MATCHING_CODE_CHANGE",
                "scope_completeness": "UNATTESTED", "changes": changes}


class CodingMemoryCapture:
    """Create structured code-change events from content-addressed local snapshots."""

    def __init__(self, workspace, store, *, project_id, session_id, state_dir,
                 suffixes=DEFAULT_SUFFIXES, excluded_parts=DEFAULT_EXCLUDED_PARTS,
                 now=None):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("CODING_WORKSPACE_NOT_FOUND")
        if not isinstance(store, CodingMemoryStore):
            raise ValueError("CODING_MEMORY_STORE_REQUIRED")
        self.store = store
        self.project_id = _text(project_id, "PROJECT_ID_REQUIRED")
        self.session_id = _text(session_id, "SESSION_ID_REQUIRED")
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.state_dir / "blobs"
        self.diff_dir = self.state_dir / "diffs"
        self.manifest_path = self.state_dir / "coding-manifest.json"
        self.suffixes = frozenset(item.lower() for item in suffixes)
        self.excluded_parts = frozenset(excluded_parts)
        self.now = now or (lambda: datetime.now().isoformat(timespec="microseconds"))

    def _blob(self, content):
        raw_digest = _digest(content)
        content = _redact(_decode_source(content), preserve_source=True)[0].encode("utf-8")
        digest = _digest(content)
        target = self.blob_dir / digest[:2] / (digest + ".blob")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # named by its hash, so a partial file (killed mid-write, disk full) was trusted forever
            staged = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            staged.write_bytes(content)
            os.replace(staged, target)
        return raw_digest, str(target)

    def snapshot(self, previous=None):
        """Every included file's content hash. Every 2 minutes on a whole workspace: excluded
        folders are not entered, and a file whose size and modification time are unchanged is
        not read again (as git does); reading and hashing every file each time, and walking
        node_modules before filtering it out, cost a CPU core on a large synced folder."""
        previous, files = previous or {}, {}
        for root, dirs, names in os.walk(self.workspace):       # symlinked folders: not entered
            root = Path(root)
            # a virtual environment by any name (.venv-rivals: 16,000 files walked every 2 min)
            dirs[:] = [d for d in dirs if d not in self.excluded_parts
                       and root / d != self.state_dir and _plain_directory(root / d)
                       and not (root / d / "pyvenv.cfg").exists()]
            for name in names:
                path = root / name
                if path.suffix.lower() not in self.suffixes or name in self.excluded_parts:
                    continue
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                        continue
                    relative = path.relative_to(self.workspace).as_posix()
                    known = previous.get(relative)
                    if (known and known.get("size") == info.st_size
                            and known.get("mtime_ns") == info.st_mtime_ns):
                        files[relative] = known
                        continue
                    content = path.read_bytes()                   # size checked above
                    if len(content) > MAX_SOURCE_BYTES or b"\0" in content:     # grew, or binary
                        continue
                except OSError:
                    continue
                digest, blob_ref = self._blob(content)
                files[relative] = {"sha256": digest, "size": len(content),
                                   "blob_ref": blob_ref, "mtime_ns": info.st_mtime_ns}
        return files

    def _manifest(self, files):
        return {"capture_version": CAPTURE_VERSION, "project_id": self.project_id,
                "workspace": str(self.workspace), "suffixes": sorted(self.suffixes),
                "excluded_parts": sorted(self.excluded_parts), "files": files,
                "written_at": self.now()}

    def _write_manifest(self, files):
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._manifest(files), ensure_ascii=False, indent=2)
                             + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def _drop_old_blobs(self, files):
        """A blob is read only as the "before" of the next change, so only the current version
        of each file is needed; the patch keeps the history. Every past version was kept (169 MB
        for a 182-file project in three weeks). Called once the manifest naming them is
        written: a scan whose manifest failed still finds its blobs."""
        keep = {Path(entry["blob_ref"]).name for entry in files.values()
                if entry.get("blob_ref")}
        for blob in self.blob_dir.glob("*/*.blob"):
            if blob.name not in keep:
                blob.unlink(missing_ok=True)

    def initialize_baseline(self):
        files = self.snapshot()
        self._write_manifest(files)
        self._drop_old_blobs(files)
        return {"status": "BASELINE_CREATED", "file_count": len(files),
                "manifest": str(self.manifest_path)}

    def _load_manifest(self):
        if not self.manifest_path.is_file():
            raise ValueError("CODING_BASELINE_REQUIRED")
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if (value.get("capture_version") != CAPTURE_VERSION
                or value.get("project_id") != self.project_id
                or Path(value.get("workspace", "")).resolve() != self.workspace
                or value.get("suffixes") != sorted(self.suffixes)
                or value.get("excluded_parts") != sorted(self.excluded_parts)
                or not isinstance(value.get("files"), dict)):
            raise ValueError("CODING_MANIFEST_SCOPE_OR_VERSION_MISMATCH")
        return value

    def drop_false_deletions(self):
        """0.9.31 recorded every file of a newly skipped virtual environment as deleted (7,394
        on one PC). Drops each DELETED record whose file is still there and has had no record
        since (a file deleted and then made again has an ADDED after it)."""
        db = self.store.db
        rows = db.execute("""SELECT c.change_id,c.file_path,c.diff_ref FROM coding_changes c
          WHERE c.workspace=? AND c.project_id=? AND c.change_kind='DELETED'
          AND NOT EXISTS(SELECT 1 FROM coding_changes l WHERE l.workspace=c.workspace
                         AND l.file_path=c.file_path AND l.revision>c.revision)""",
                          (str(self.workspace), self.project_id)).fetchall()
        false = [row for row in rows if (self.workspace / row["file_path"]).is_file()]
        with db:
            for row in false:
                db.execute("DELETE FROM coding_change_symbols WHERE change_id=?",
                           (row["change_id"],))
                db.execute("DELETE FROM coding_changes WHERE change_id=?", (row["change_id"],))
        for row in false:
            diff = Path(row["diff_ref"])
            if diff.parent == self.diff_dir:              # only our own patch files
                diff.unlink(missing_ok=True)
        return len(false)

    def scan(self, *, reason, related_tests=None):
        reason = _text(reason, "CODE_CHANGE_REASON_REQUIRED")
        related_tests = _string_list(related_tests, "RELATED_TESTS_LIST_REQUIRED")
        manifest = self._load_manifest()
        previous = manifest["files"]
        current = self.snapshot(previous)
        now = self.now()
        # the id names the state the change starts from, not this scan's clock: when the
        # manifest could not be replaced (or a crash came first), the next scan makes the same
        # ids and the store skips them, instead of a new copy of every change each poll
        since = manifest.get("written_at") or now
        recorded, changes = [], []
        self.diff_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(set(previous) | set(current)):
            before, after = previous.get(path), current.get(path)
            if any(item and item.get("size", 0) > MAX_SOURCE_BYTES
                   for item in (before, after)):
                continue
            if before and after and before["sha256"] == after["sha256"]:
                continue
            if after is None and (self.workspace / path).exists():
                # left out now (a folder newly excluded), not deleted: 7,394 "deleted" files
                # and 5 minutes of work when a virtual environment stopped being walked
                continue
            kind = "ADDED" if before is None else "DELETED" if after is None else "MODIFIED"
            before_bytes = Path(before["blob_ref"]).read_bytes() if before else b""
            after_bytes = Path(after["blob_ref"]).read_bytes() if after else b""
            before_text = _redact(_decode_source(before_bytes), preserve_source=True)[0]
            after_text = _redact(_decode_source(after_bytes), preserve_source=True)[0]
            symbols = _affected_symbols(path, before_text, after_text)
            binding = {"project_id": self.project_id, "session_id": self.session_id,
                       "file_path": path, "before": before and before["sha256"],
                       "after": after and after["sha256"], "reason": reason,
                       "since": since}
            change_id = "code-change-" + _digest(_json(binding).encode("utf-8"))
            known = self.store.db.execute("SELECT revision FROM coding_changes "
                                          "WHERE change_id=?", (change_id,)).fetchone()
            if known:                                     # kept by a scan whose manifest failed
                recorded.append({"change_id": change_id, "revision": known["revision"],
                                 "disposition": "ALREADY_RECORDED"})
                continue
            diff = "".join(difflib.unified_diff(
                before_text.splitlines(True), after_text.splitlines(True),
                fromfile="a/" + path, tofile="b/" + path, n=3))
            diff_path = self.diff_dir / (change_id + ".patch")
            diff_path.write_text(diff, encoding="utf-8")
            change = {"change_id": change_id, "project_id": self.project_id,
                "session_id": self.session_id, "workspace": str(self.workspace),
                "happened_at": now, "known_at": now, "change_kind": kind,
                "file_path": path, "language": Path(path).suffix.lower().lstrip(".") or "text",
                "before_sha256": before and before["sha256"],
                "after_sha256": after and after["sha256"], "reason": reason,
                "related_tests": related_tests, "symbols": symbols,
                "diff_ref": str(diff_path), "before_ref": before and before["blob_ref"],
                "after_ref": after and after["blob_ref"],
                "provenance": {"source_type": "workspace-snapshot-diff",
                               "source_ref": path}}
            result = self.store.record(change)
            recorded.append(result)
            changes.append({"change_id": change_id, "change_kind": kind,
                            "file_path": path,
                            "symbols": list(dict.fromkeys(item["name"] for item in symbols)),
                            "diff_ref": str(diff_path)})
        if current != previous:
            self._write_manifest(current)
            self._drop_old_blobs(current)
        return {"status": "CHANGES_RECORDED" if changes else "NO_CHANGES",
                "change_count": len(changes), "changes": changes,
                "record_results": recorded}
