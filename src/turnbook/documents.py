"""Project documents (specs, design notes, READMEs) kept beside the conversations.

`turnbook docs add <folder>` lists a folder in ~/.turnbook/documents.json; the capture service
reads its Markdown and text files, cuts them at headings, and stores each section as a turn
(client "document", role "document"), so the word and meaning searches, the re-ranker and the
packets need nothing new. A section is stored once per distinct text: an edited section is a new
turn, and the version it replaced stays, marked with when it was replaced (document_replaced),
so "what did the spec say before?" has an answer and the newer text wins.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import re

from turnbook.conversation_memory import _redact
from turnbook.paths import turnbook_home
from turnbook.privacy import strip_private

SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "dist", "build", "target", "site-packages"}
WALK_SECONDS = 60
_WALKED: dict = {}
MAX_BYTES = 2_000_000          # a larger "document" is a dump or a log, not something written
SECTION_CHARS = 4000           # longer sections are cut at blank lines
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def folders_file() -> Path:
    return turnbook_home() / "documents.json"


def folders() -> list[str]:
    try:
        return list(json.loads(folders_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def set_folders(items) -> None:
    folders_file().parent.mkdir(parents=True, exist_ok=True)
    folders_file().write_text(json.dumps(sorted(set(items)), indent=1), encoding="utf-8")


def sections(text: str) -> list[tuple[str, str]]:
    """(heading path, body) per section: "Login > Kakao" for a ### under a ##. Text before the
    first heading is its own section. A section over SECTION_CHARS is cut at blank lines."""
    out, trail, body = [], [], []

    def close():
        joined = "\n".join(body).strip()
        if joined:
            path = " > ".join(title for _, title in trail)
            for piece in _cut(joined):
                out.append((path, piece))
        body.clear()
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        match = None if fenced else HEADING.match(line)
        if match:
            close()
            level = len(match.group(1))
            trail[:] = [h for h in trail if h[0] < level] + [(level, match.group(2))]
        else:
            body.append(line)
    close()
    return out


def _cut(text):
    if len(text) <= SECTION_CHARS:
        return [text]
    pieces, current = [], ""
    for block in re.split(r"\n\s*\n", text):
        if current and len(current) + len(block) > SECTION_CHARS:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    return pieces + [current] if current else pieces


class DocumentCapture:
    """One source of the capture service: every listed folder's documents. Its results carry no
    scanned_rows: an unchanged folder is not a transcript that stopped yielding turns."""

    CLIENT = "document"

    def __init__(self, roots, store, *, state_path, workspace_scopes=None, now=None,
                 publish=None, forget=None):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.store = store
        self.state_path = Path(state_path)
        self.workspace_scopes = {str(Path(k).resolve()): v
                                 for k, v in (workspace_scopes or {}).items()}
        self.now = now
        self.publish = publish
        store.db.execute("CREATE TABLE IF NOT EXISTS document_replaced("
                         "turn_id TEXT PRIMARY KEY, replaced_at TEXT NOT NULL)")

    def _files(self):
        for root in self.roots:
            for folder, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
                for name in names:
                    path = Path(folder) / name
                    if path.suffix.lower() in SUFFIXES:
                        yield root, path

    def _scope(self, path):
        for parent in path.parents:
            if str(parent) in self.workspace_scopes:
                return self.workspace_scopes[str(parent)]
        return "global"

    def initialize_baseline(self):
        return self.scan()              # unlike a transcript, a document is stored as it is now

    def scan(self):
        # walking the folders is the cost: once a minute, not on every 5 s pass (the service
        # builds its sources anew each pass, so the time is kept here, per state file)
        import time
        if time.monotonic() - _WALKED.get(self.state_path, -1e9) < WALK_SECONDS:
            return {"status": "NO_NEW_TURNS", "recorded_turns": 0, "redactions": 0,
                    "record_results": []}
        _WALKED[self.state_path] = time.monotonic()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        seen, recorded, redactions, changed = set(), [], 0, False
        known_at = self.now()
        replaced = []
        for root, path in self._files():
            key = str(path)
            seen.add(key)
            try:
                info = path.stat()
            except OSError:
                continue
            stamp = [info.st_mtime_ns, info.st_size]
            if info.st_size > MAX_BYTES or state.get(key, {}).get("stamp") == stamp:
                continue
            try:
                text = _decode(path.read_bytes())
            except OSError:
                # locked by another program, or a cloud file not on this PC yet: it stopped
                # every document after it; tried again next minute
                continue
            changed = True
            name = path.relative_to(root.parent).as_posix()
            ids = []
            for ordinal, (heading, body) in enumerate(sections(strip_private(text))):
                content = f"{name} > {heading}\n\n{body}" if heading else f"{name}\n\n{body}"
                cleaned, count = _redact(content)
                if not cleaned:
                    continue
                raw_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                turn_id = "document-" + hashlib.sha256(
                    f"{key}\0{raw_hash}".encode("utf-8")).hexdigest()
                ids.append(turn_id)
                turn = {"turn_id": turn_id, "client": self.CLIENT, "client_session_id": key,
                        "scope": self._scope(path), "cwd": str(root),
                        # first stored: the file's time; an edit: when it was seen
                        "happened_at": (known_at if key in state else
                                        _local(info.st_mtime)),
                        "known_at": known_at, "role": "document",
                        "content_redacted": cleaned, "raw_sha256": raw_hash,
                        "redaction_count": count, "source_path": key,
                        "source_ordinal": ordinal}
                outcome = self.store.record(turn)
                if outcome.get("disposition") == "RECORDED":
                    redactions += count
                    recorded.append(outcome)
                    if self.publish:
                        try:
                            self.publish(turn)
                        except Exception:
                            pass       # ponytail: shared folder unreachable; kept on this PC
            replaced += [i for i in state.get(key, {}).get("ids", []) if i not in ids]
            state[key] = {"stamp": stamp, "ids": ids}
        for key in [k for k in state if k not in seen]:     # deleted or no longer listed
            replaced += state.pop(key)["ids"]
            changed = True
        if not changed:
            return {"status": "NO_NEW_TURNS", "recorded_turns": 0, "files": len(seen),
                    "redactions": 0, "record_results": []}
        if replaced:
            with self.store.db:
                self.store.db.executemany(
                    "INSERT OR IGNORE INTO document_replaced(turn_id,replaced_at) VALUES(?,?)",
                    [(i, self.store._time(known_at)) for i in replaced])
        # a section that came back (an edit undone) is current again
        current = [i for entry in state.values() for i in entry["ids"]]
        if current:
            with self.store.db:
                self.store.db.executemany("DELETE FROM document_replaced WHERE turn_id=?",
                                          [(i,) for i in current])
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        return {"status": "TURNS_RECORDED" if recorded else "NO_NEW_TURNS",
                "recorded_turns": len(recorded), "files": len(seen),
                "redactions": redactions, "replaced_sections": len(replaced),
                "record_results": recorded}


def _decode(data: bytes) -> str:
    """UTF-8, else this PC's own encoding (Notepad's ANSI on a Korean Windows is CP949: read as
    UTF-8, every Korean letter became U+FFFD)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        import locale
        try:
            return data.decode(locale.getpreferredencoding(False))
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", "replace")


def _local(seconds):
    from datetime import datetime
    return datetime.fromtimestamp(seconds).isoformat(timespec="microseconds")


def replaced_at(db, turn_ids) -> dict[str, str]:
    """When each of these document sections was replaced by a newer text (absent: current)."""
    ids = [i for i in turn_ids if str(i).startswith("document-")]
    if not ids:
        return {}
    try:
        rows = db.execute("SELECT turn_id,replaced_at FROM document_replaced WHERE turn_id IN ("
                          + ",".join("?" for _ in ids) + ")", ids).fetchall()
    except Exception:           # no documents were ever added: no table
        return {}
    return {r[0]: r[1] for r in rows}
