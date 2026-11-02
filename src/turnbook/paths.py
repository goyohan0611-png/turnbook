"""Stable user-owned paths shared by every Turnbook client."""
from pathlib import Path
import hashlib
import os


def turnbook_home():
    """~/.turnbook, or TURNBOOK_HOME. A memory from before 0.11 lives in ~/.graph-mind and is
    used there until move_old_home() could move it (nothing may hold a file in it then)."""
    configured = os.environ.get("TURNBOOK_HOME")
    if configured:
        return Path(configured).expanduser()
    new, old = Path.home() / ".turnbook", Path.home() / ".graph-mind"
    return old if old.is_dir() and not new.exists() else new


def move_old_home() -> bool:
    """Move ~/.graph-mind to ~/.turnbook, and the paths written inside it, when nothing holds
    a file in it (at login, before any app; the installer stops the capture service first).
    False, and the memory stays where it is, when a file is open: tried again next time."""
    new, old = Path.home() / ".turnbook", Path.home() / ".graph-mind"
    if os.environ.get("TURNBOOK_HOME") or not old.is_dir() or new.exists():
        return False
    try:
        os.rename(old, new)
    except OSError:
        return False
    _repoint(old, new)
    link_old_home()
    return True


def link_old_home() -> None:
    """~/.graph-mind as a link to ~/.turnbook: an app's memory server started before the upgrade
    runs old code until the app restarts, and wrote to a new, empty ~/.graph-mind (a second
    memory). A directory junction on Windows needs no administrator rights. A real folder left
    there by such a server holding only its bookkeeping (start.lock, service.pid) is replaced."""
    new, old = Path.home() / ".turnbook", Path.home() / ".graph-mind"
    if not new.is_dir() or os.environ.get("TURNBOOK_HOME"):
        return
    if old.is_dir() and not old.is_symlink() and not _is_junction(old):
        stray = [p for p in old.rglob("*") if p.is_file()]
        if any(p.name not in ("start.lock", "service.pid", "service.lock") for p in stray):
            return                      # something real in it: never thrown away
        import shutil
        shutil.rmtree(old, ignore_errors=True)
    if old.exists() or old.is_symlink():
        return
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(new), str(old))
        else:
            old.symlink_to(new, target_is_directory=True)
    except OSError:
        pass


def _is_junction(path: Path) -> bool:
    return bool(getattr(os.path, "isjunction", lambda _: False)(path))


def _repoint(old: Path, new: Path) -> None:
    """Paths under the old folder kept in its files: the capture settings (database, code
    snapshot folders), each app's read positions (keys for the hook and OpenCode transcripts
    under it), the code snapshots' manifests and the code changes' patch files."""
    import json
    import sqlite3

    def swap(value):
        if isinstance(value, str):
            return str(new) + value[len(str(old)):] if value.startswith(str(old)) else value
        if isinstance(value, list):
            return [swap(item) for item in value]
        if isinstance(value, dict):
            return {swap(key): swap(item) for key, item in value.items()}
        return value
    for path in [*new.glob("*.json"), *new.glob("automatic-capture/*.json"),
                 *new.glob("coding/*/coding-manifest.json")]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            moved = swap(value)
            if moved != value:
                path.write_text(json.dumps(moved, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except (OSError, ValueError):
            continue
    for database in new.glob("*.sqlite"):
        try:
            db = sqlite3.connect(database, timeout=30)
            try:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='coding_changes'").fetchone():
                    with db:
                        for column in ("diff_ref", "before_ref", "after_ref"):
                            db.execute(f"UPDATE coding_changes SET {column} = ? || substr("
                                       f"{column}, ?) WHERE substr({column}, 1, ?) = ?",
                                       (str(new), len(str(old)) + 1, len(str(old)), str(old)))
            finally:
                db.close()
        except sqlite3.Error:
            continue


def default_development_db():
    return turnbook_home() / "development.sqlite"


def default_coding_state_dir(project_id):
    suffix = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    return turnbook_home() / "coding" / suffix


def use_wal(db):
    """Put a store in WAL mode, waiting for the other clients if need be. Changing the journal
    mode needs the database to itself, and SQLite does not wait for that the way it waits for a
    write: three clients opening a new store at once failed with "database is locked" (macOS CI).
    """
    import sqlite3
    import time
    # space freed by a delete or a rewrite is zeroed: with only the forgetting connection doing
    # it, a forgotten password was still in the file, in pages an earlier index merge had freed
    # (no measurable cost: 400 turns stored and indexed in the same time)
    db.execute("PRAGMA secure_delete = ON")
    for attempt in range(100):
        try:
            db.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or attempt == 99:
                raise
            time.sleep(0.1)
