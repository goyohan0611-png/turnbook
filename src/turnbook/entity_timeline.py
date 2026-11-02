"""Per-entity change history: "what happened to THIS thing, in order".

Retrieval by similarity answers "what is related to this question". It does not answer "what has
happened to the auth module", because the four decisions about it sit in four different sessions and
only the closest one or two come back. The failures that kept surviving every retrieval fix were of
exactly this shape — which came first, what is the current value, how many times in total.

So the store keeps a second index beside the memories: entity -> its memories, oldest first, with
superseded ones marked. No graph engine and no traversal: Zep's multi-hop machinery answers
questions we never actually failed. The entity NAMES come from the model that wrote the memory
(naming is a language judgement); this module only groups, orders and reads.

    python entity_timeline.py    # self-check on a temporary store
"""
from __future__ import annotations

import json
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_timeline(
  entity_key TEXT NOT NULL,
  entity_label TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  effective_at TEXT NOT NULL,
  PRIMARY KEY(entity_key, memory_id));
CREATE INDEX IF NOT EXISTS entity_timeline_order
  ON entity_timeline(entity_key, effective_at);
CREATE TABLE IF NOT EXISTS entity_timeline_state(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def normalize(name: str) -> str:
    """Group "Auth Module", "auth  module" and "auth module." together — punctuation and spacing
    only. Deciding that "인증 모듈" is the same entity is a language call and belongs to the model,
    which can pass both names on the memory."""
    return re.sub(r"[^\w가-힣]+", " ", str(name).casefold()).strip()


def rebuild(db: sqlite3.Connection) -> int:
    """(Re)build the index when the memory count changed. Cheap: one pass over the memories."""
    db.executescript(SCHEMA)
    total = tuple(db.execute(
        "SELECT COUNT(*), COALESCE(MAX(revision), 0) FROM local_brain_memories").fetchone())
    state = dict(db.execute("SELECT key, value FROM entity_timeline_state"))
    if state.get("memories") == str(total):
        return 0
    rows = []
    for memory_id, effective_at, entities_json in db.execute(
            "SELECT memory_id, effective_at, entities_json FROM local_brain_memories"):
        try:
            entities = json.loads(entities_json) or []
        except (TypeError, ValueError):
            entities = []
        for entity in entities:
            key = normalize(entity)
            if key:
                rows.append((key, str(entity), memory_id, effective_at))
    with db:
        db.execute("DELETE FROM entity_timeline")
        db.executemany("INSERT OR REPLACE INTO entity_timeline VALUES(?,?,?,?)", rows)
        db.execute("INSERT OR REPLACE INTO entity_timeline_state VALUES('memories',?)",
                   (str(total),))
    return len(rows)


def entities(db: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Every entity the store knows, busiest first — the map of what this memory is about."""
    rebuild(db)
    return [{"entity": label, "entries": count, "last_seen": last}
            for label, count, last in db.execute(
                # MIN() so a group reports one stable label instead of whichever spelling SQLite
                # happened to read last ("auth  module." vs "Auth Module").
                "SELECT MIN(entity_label), COUNT(*) c, MAX(effective_at) FROM entity_timeline "
                "GROUP BY entity_key ORDER BY c DESC LIMIT ?", (limit,))]


def timeline(db: sqlite3.Connection, entity: str, limit: int = 50) -> list[dict]:
    """This entity's memories oldest first, with the ones a later memory replaced marked."""
    rebuild(db)
    key = normalize(entity)
    rows = db.execute(
        "SELECT DISTINCT m.memory_id, m.effective_at, m.title, m.content, m.memory_type,"
        "       m.memory_id IN (SELECT supersedes_memory_id FROM local_brain_memories "
        "                       WHERE supersedes_memory_id IS NOT NULL) "
        "FROM entity_timeline t JOIN local_brain_memories m ON m.memory_id = t.memory_id "
        "WHERE t.entity_key = ? OR t.entity_key LIKE ? "
        "ORDER BY m.effective_at, m.revision LIMIT ?", (key, f"%{key}%", limit)).fetchall()
    return [{"memory_id": r[0], "effective_at": r[1], "title": r[2], "content": r[3],
             "memory_type": r[4], "superseded": bool(r[5])} for r in rows]


def _self_check():
    from pathlib import Path
    import tempfile

    from turnbook.local_brain import LocalBrainStore

    path = Path(tempfile.mkdtemp()) / "brain.sqlite"
    facts = [("JWT expiry set to 24h", ["auth module"], "2026-03-12T09:00:00", None),
             ("Refresh tokens added", ["Auth Module", "mobile"], "2026-03-20T09:00:00", None),
             ("JWT expiry cut to 1h", ["auth  module."], "2026-04-02T09:00:00", "m1"),
             ("Bought an air fryer", ["kitchen"], "2026-04-05T09:00:00", None)]
    with LocalBrainStore(path) as store:
        for i, (title, ents, when, supersedes) in enumerate(facts, start=1):
            store.remember({"memory_id": f"m{i}", "scope": "global", "memory_type": "fact",
                            "title": title, "content": title, "effective_at": when,
                            "known_at": when, "actor": "assistant", "tags": [],
                            "entities": ents,
                            "provenance": {"source_type": "test", "source_ref": "unit"},
                            "supersedes_memory_id": supersedes})
    db = sqlite3.connect(path)
    assert rebuild(db) == 5, "4 memories, one with two entities"
    assert rebuild(db) == 0, "no work when nothing changed"

    rows = timeline(db, "AUTH MODULE")
    assert [r["title"] for r in rows] == ["JWT expiry set to 24h", "Refresh tokens added",
                                          "JWT expiry cut to 1h"], rows
    assert rows[0]["superseded"] and not rows[-1]["superseded"], "m1 was replaced by m3"
    assert timeline(db, "kitchen")[0]["title"] == "Bought an air fryer"
    assert timeline(db, "nothing here") == []
    names = {e["entity"].casefold() for e in entities(db)}
    assert "auth module" in names and "kitchen" in names, names
    assert entities(db)[0]["entries"] == 3, "auth module is the busiest entity"
    db.close()
    print("entity_timeline self-check ok")


if __name__ == "__main__":
    _self_check()
