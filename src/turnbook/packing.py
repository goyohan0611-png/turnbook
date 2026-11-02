"""Conversation text, compressed where it is stored.

A turn's text is kept zlib-compressed in `content_z` once it is long enough for that to pay
(`content_redacted` is then ''); short turns stay plain. Measured on 20,000 LongMemEval turns:
about half the size, and unpacking the 30 turns of one question takes under a millisecond.
The word-search index keeps the plain words, so search is unchanged.
"""
import sqlite3
import zlib

COMPRESS_FROM = 200          # bytes; below this zlib saves little or nothing


def pack(text: str) -> tuple[str, bytes | None]:
    """(plain, compressed): one of them carries the text."""
    raw = text.encode("utf-8")
    if len(raw) >= COMPRESS_FROM:     # always, even the odd one zlib cannot shrink (+11 bytes):
        return "", zlib.compress(raw, 6)   # "compressed or short" is then all a scan must check
    return text, None


def unpack(plain, packed) -> str:
    return plain if packed is None else zlib.decompress(packed).decode("utf-8")


def row_text(row) -> str:
    """A sqlite3.Row of conversation_turns (a store from before 0.9.27 has no content_z)."""
    return unpack(row["content_redacted"],
                  row["content_z"] if "content_z" in row.keys() else None)


def text_columns(db: sqlite3.Connection) -> str:
    """The two columns to select for unpack(): a store written before 0.9.27 has no content_z."""
    names = {row[1] for row in db.execute("PRAGMA table_info(conversation_turns)")}
    return "content_redacted, content_z" if "content_z" in names else "content_redacted, NULL"


# graph_mind, graph-mind: the names before 0.11
MARKS = ("turnbook", "graph_mind", "graph-mind", "automatic_capture_cli")   # how ours start


def older_processes() -> bool:
    """Is any Turnbook process still running code from before compression came in? Such a
    process reads a packed turn as '' (forgetting it then left its words in the index, sharing
    sent it empty), so nothing is packed while one runs. "Older" means started before this
    file was installed or before the process asking (the capture service, restarted by every
    upgrade); an app's memory server stays on old code until the app restarts. A Python process
    whose command line or start time cannot be read (another user's, an elevated one) counts
    as older: packing waits rather than guess. Not covered: a second, older installation started
    later against the same memory folder."""
    import os

    import psutil
    me = os.getpid()
    since = max(os.path.getmtime(__file__), psutil.Process(me).create_time())
    # names first: reading every process's command line took 4 s on Windows
    for process in psutil.process_iter(["pid", "name"]):
        info = process.info
        if info["pid"] == me or not (info["name"] or "").lower().startswith(
                ("python", "graph-mind", "turnbook")):
            continue
        try:
            line, started = " ".join(process.cmdline() or []), process.create_time()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            line, started = "", None
        if not line or started is None:
            return True                      # unreadable: assume the worst
        if started < since and any(mark in line for mark in MARKS):
            return True
    return False


def compact(db_path) -> dict:
    """Give the space freed by packing back to the disk (VACUUM): packing frees pages inside
    the file, which new turns reuse, but the file keeps its size until this runs. SQLite copies
    the whole database to do it, so it needs that much free disk and refuses otherwise; it is
    atomic (an interruption leaves the old file intact) and waits while another program writes."""
    from pathlib import Path
    import os
    import shutil
    path = Path(db_path)
    before = path.stat().st_size
    if shutil.disk_usage(path.parent).free < before * 1.2:
        raise ValueError("NOT_ENOUGH_FREE_DISK: needs about the database's size free")
    db = sqlite3.connect(str(path), timeout=120)
    try:
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    after = os.path.getsize(path)
    return {"database": str(path), "before_mb": round(before / 1e6, 1),
            "after_mb": round(after / 1e6, 1)}


if __name__ == "__main__":
    for text in ("short", "long enough to be worth packing " * 20, "한국어 문장도 " * 40):
        plain, packed = pack(text)
        assert unpack(plain, packed) == text
        assert (packed is None) == (len(text.encode()) < COMPRESS_FROM)
    print("ok")
