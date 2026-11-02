"""Embed memories when they are STORED, not when they are asked about.

Recall embeds each candidate's text at query time. The vectors are cached, so the cost is
paid once — but it is paid by whoever asks the first question after a batch of memories arrives, and
on CPU that is seconds to minutes. Writing is the right moment: it happens once per memory, in the
background of a conversation the user is already having.

The text embedded here is exactly what the recall path will hash (the engram's FTS text, first 500
characters), so a warmed memory is a cache hit rather than a near miss.

    python warmup.py [db_path]      # warm a store, or self-check with no arguments
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

from turnbook.packing import text_columns, unpack

EXCERPT = 500          # semantic_recall embeds text_excerpt = text[:500]
BATCH = 128


def pending_texts(db: sqlite3.Connection, embedder, limit: int) -> list[str]:
    """Engram excerpts that have no vector yet, newest first."""
    rows = db.execute(
        "SELECT f.text FROM associative_engrams_fts f "
        "JOIN associative_engrams e ON e.engram_id = f.engram_id "
        "ORDER BY e.revision DESC LIMIT ?", (limit,)).fetchall()
    texts, seen = [], set()
    for (text,) in rows:
        excerpt = (text or "")[:EXCERPT].strip()[:2000]
        if excerpt and excerpt not in seen and embedder._key(excerpt) not in embedder.cache:
            seen.add(excerpt)
            texts.append(excerpt)
    return texts


def warm(db_path, *, limit: int = 500, embedder=None) -> int:
    """Embed everything unembedded in the newest `limit` engrams. Returns how many were added."""
    from turnbook.local_embedder import default_embedder

    embedder = embedder or default_embedder(db_path)
    db = sqlite3.connect(db_path)
    try:
        db.execute("SELECT 1 FROM associative_engrams LIMIT 1")
    except sqlite3.OperationalError:
        db.close()
        return 0                       # no associative index yet: nothing to warm
    try:
        texts = pending_texts(db, embedder, limit)
        for start in range(0, len(texts), BATCH):
            embedder.embed(texts[start:start + BATCH])
        if texts and hasattr(embedder, "flush"):
            embedder.flush()
        return len(texts)
    finally:
        db.close()


PIECE = 500           # conversation turns are searched by meaning in 500-character pieces


def turn_pieces(text: str) -> list[tuple[int, str]]:
    """(offset, piece) for one turn, exactly as the recall path cuts and searches it."""
    return [(start, text[start:start + PIECE]) for start in range(0, len(text), PIECE)
            if text[start:start + PIECE].strip()]


_VERIFIED = set()


def warm_turns(db_path, *, limit: int = 512, embedder=None) -> int:
    """Embed up to `limit` unembedded pieces of conversation turns, the user's first, newest first.

    The capture service calls this after every pass, so the turns it has just stored are
    searchable by meaning before anyone asks about them. Without it the first question after a
    busy day met thousands of unembedded pieces, and the server, which never makes a question
    wait for that, answered it from word search alone (79.2% against 86.0% on LongMemEval)."""
    from turnbook.local_embedder import default_embedder
    embedder = embedder or default_embedder(db_path)
    if hasattr(embedder.cache, "refresh"):
        embedder.cache.refresh()                 # what the memory servers embedded meanwhile
    progress = getattr(embedder, "_turn_progress", {})
    token = (str(Path(db_path).resolve()), id(embedder.cache),
             getattr(embedder.cache, "generation", 0))
    after = high = progress.get(token, 0)
    complete = True
    db = sqlite3.connect(db_path, timeout=30)
    try:
        # the user's turns first (every search uses them), then the assistant's (searched when
        # the question is about what it said: left to the server, a fresh server spent minutes
        # embedding all of them on the first such question, and every query waited meanwhile)
        rows = db.execute(f"SELECT rowid,{text_columns(db)} FROM conversation_turns "
                          "WHERE rowid>? AND role IN ('user','assistant','document') "
                          "ORDER BY role='assistant', happened_at DESC", (after,))
        texts, seen, cached = [], set(), []
        for revision, plain, packed in rows:
            high = max(high, revision)
            for _, piece in turn_pieces(unpack(plain, packed) or ""):
                key = piece.strip()[:2000]
                if key not in seen and embedder._key(key) not in embedder.cache:
                    seen.add(key)
                    texts.append(piece)
                elif len(cached) < 400:
                    cached.append(piece)
            if len(texts) >= limit:
                complete = False
                break
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        return 0                                 # no conversations captured yet
    finally:
        db.close()
    texts = texts[:limit]
    # once per process and cache: are the vectors already there right? (see LocalEmbedder.verify)
    checked = str(getattr(embedder.cache, "dir", ""))
    if hasattr(embedder, "verify") and checked not in _VERIFIED:
        _VERIFIED.add(checked)
        if embedder.verify(cached):
            return warm_turns(db_path, limit=limit, embedder=embedder)
    for start in range(0, len(texts), BATCH):
        embedder.embed(texts[start:start + BATCH])
    if texts and hasattr(embedder, "flush"):
        embedder.flush()
    if complete:
        progress[token] = high
        embedder._turn_progress = progress
    return len(texts)


def _self_check():
    import tempfile

    from turnbook.associative_memory import AssociativeMemoryIndex
    from turnbook.local_brain import LocalBrainStore

    path = Path(tempfile.mkdtemp()) / "brain.sqlite"
    with LocalBrainStore(path) as store:
        for i, title in enumerate(["JWT expiry cut to 1h", "Bought an air fryer"], start=1):
            store.remember({"memory_id": f"m{i}", "scope": "global", "memory_type": "fact",
                            "title": title, "content": title + " — details follow",
                            "effective_at": "2026-04-02T09:00:00",
                            "known_at": "2026-04-02T09:00:00", "actor": "assistant",
                            "tags": [], "entities": ["auth module"],
                            "provenance": {"source_type": "test", "source_ref": "unit"},
                            "supersedes_memory_id": None})
    with AssociativeMemoryIndex(path) as index:
        index.sync_sources()

    class FakeEmbedder:
        def __init__(self):
            self.cache, self.calls = {}, 0

        def _key(self, text):
            return "k:" + text[:40]

        def embed(self, texts):
            self.calls += 1
            for text in texts:
                self.cache.setdefault(self._key(text), [0.1, 0.2])
            return [self.cache[self._key(t)] for t in texts]

    fake = FakeEmbedder()
    first = warm(path, embedder=fake)
    assert first >= 2, first                      # both memories embedded
    assert warm(path, embedder=fake) == 0         # nothing left to do
    calls = fake.calls
    assert warm(path, embedder=fake) == 0 and fake.calls == calls, "warm must not re-embed"

    db = sqlite3.connect(path)
    texts = pending_texts(db, fake, 500)
    db.close()
    assert texts == [], texts
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE conversation_turns(content_redacted TEXT, role TEXT, "
                   "happened_at TEXT)")
        db.executemany("INSERT INTO conversation_turns VALUES(?,?,?)", [
            ("".join(f"{i:04d}" for i in range(300)), "user", "2026-01-01"),
            ("answer " * 300, "assistant", "2026-01-02"),
            ("newest question", "user", "2026-01-03")])
    turns = FakeEmbedder()
    assert warm_turns(path, embedder=turns, limit=2) == 2          # newest first, capped
    assert turns._key("newest question") in turns.cache
    assert not any("answer" in key for key in turns.cache), "the user's turns come first"
    assert warm_turns(path, embedder=turns) == 7                    # 2 user + 5 assistant pieces
    assert warm_turns(path, embedder=turns) == 0
    print(f"warmup self-check ok ({first} vectors warmed, second pass free)")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print("warmed", warm(sys.argv[1]), "vectors")
    else:
        _self_check()
