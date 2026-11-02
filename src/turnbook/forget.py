"""Forget everything that contains a phrase: on this PC, in the shared log, and on the other PCs.

    turnbook-forget                  # asks for the phrase without echoing it (keeps it out of
                                       # the shell history), shows what matches, asks to confirm
    turnbook-forget "hunter2" --yes

A captured turn or memory lives in its store, in that store's word index, and as an engram in the
associative index; all three go. In the shared log its lines go too: Postgres rows are deleted, a
folder's lines are blanked in place (same length, so other PCs' read offsets stay valid). Then a
"forget" record listing the ids, never the phrase, tells every other PC to erase its own copies.

Not covered: the AI app's own transcript (Claude Code keeps ~/.claude/projects/*.jsonl) and the
embedding vectors, which are numbers keyed by a hash and hold no text.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import getpass
import hashlib
import json
import sqlite3
import sys

from turnbook.packing import text_columns, unpack


def find(index: Path, phrase: str) -> dict:
    """Ids of the turns and memories whose text contains the phrase (exact, case-sensitive)."""
    db = sqlite3.connect(index, timeout=30)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        # a long turn is stored compressed: unpack each to look (100k turns take about 1.5 s)
        turns = [turn_id for turn_id, plain, packed in db.execute(
            f"SELECT turn_id, {text_columns(db)} FROM conversation_turns")
            if phrase in unpack(plain, packed)] if "conversation_turns" in tables else []
        memories = [row[0] for row in db.execute(
            "SELECT memory_id FROM local_brain_memories "
            "WHERE instr(content, ?) > 0 OR instr(title, ?) > 0",
            (phrase, phrase))] if "local_brain_memories" in tables else []
    finally:
        db.close()
    return {"turn_ids": turns, "memory_ids": memories}


def erase(index: Path, ids: dict) -> dict:
    """Delete these turns and memories, with every index entry derived from them."""
    db = sqlite3.connect(index, timeout=30)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            result = _erase(db, ids)
        _flush(db)
        return result
    finally:
        db.close()


def _wipe(db, tables):
    """Deleted text must leave the file too: SQLite only marks a deleted row's space free, and
    FTS5 keeps a deleted word in its index until a merge, so a forgotten password could still be
    read from the file with a hex editor. secure_delete zeroes freed space; FTS5's
    secure-delete removes the word at once (SQLite 3.44+; older ones keep the old behaviour)."""
    db.execute("PRAGMA secure_delete = ON")
    for table in ("conversation_turns_fts", "associative_engrams_fts"):
        if table in tables:
            try:
                db.execute(f"INSERT INTO {table}({table}, rank) VALUES('secure-delete', 1)")
            except sqlite3.OperationalError:
                pass


def _flush(db):
    """Copy the zeroed pages into the file and empty the write-ahead log, which still held the
    old ones. Another process reading at this moment keeps the log; it is emptied next time."""
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def _erase(db, ids):
    """Caller owns the transaction, including any durable propagation job."""
    turns, memories = list(ids.get("turn_ids", [])), list(ids.get("memory_ids", []))
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    _wipe(db, tables)
    if turns and "semantic_ingestion_audit" in tables:
        # a turn the capture service promoted to a memory lives on in that memory, word for
        # word: forgetting the turn by id left the copy behind
        marks = ",".join("?" * len(turns))
        memories += [row[0] for row in db.execute(
            f"SELECT memory_id FROM semantic_ingestion_audit WHERE turn_id IN ({marks}) "
            "AND memory_id IS NOT NULL", turns) if row[0] not in memories]
    if turns and "local_brain_memory_profiles" in tables:
        # a memory worked out from these turns (a note after answering, a state line) names
        # them in attributes.from_turns and holds what they said: it goes with them
        marks = ",".join("?" * len(turns))
        memories += [row[0] for row in db.execute(
            "SELECT DISTINCT p.memory_id FROM local_brain_memory_profiles p, "
            "json_each(p.attributes_json, '$.from_turns') j "
            f"WHERE j.value IN ({marks})", turns) if row[0] not in memories]
    # what was forgotten stays forgotten: a later import of the same turn (another PC's
    # log, an outbox sent late, a transcript read again) is refused by the stores
    db.execute("CREATE TABLE IF NOT EXISTS forgotten_ids(id TEXT PRIMARY KEY)")
    db.executemany("INSERT OR IGNORE INTO forgotten_ids VALUES(?)",
                   [(i,) for i in turns + memories])
    if turns and "conversation_turns" in tables:
        marks = ",".join("?" * len(turns))
        # the index must be handed the exact words it holds: unpacked
        rows = [(revision, unpack(plain, packed)) for revision, plain, packed in
                db.execute(f"SELECT revision, {text_columns(db)} FROM conversation_turns "
                           f"WHERE turn_id IN ({marks})", turns)]
        # an external-content FTS table keeps its words until told to drop them
        db.executemany("INSERT INTO conversation_turns_fts(conversation_turns_fts, rowid, "
                       "content_redacted) VALUES('delete', ?, ?)", rows)
        db.execute(f"DELETE FROM conversation_turns WHERE turn_id IN ({marks})", turns)
    if memories and "local_brain_memories" in tables:
        marks = ",".join("?" * len(memories))
        db.execute(f"DELETE FROM local_brain_memories WHERE memory_id IN ({marks})",
                   memories)
        if "local_brain_memory_profiles" in tables:
            db.execute(f"DELETE FROM local_brain_memory_profiles "
                       f"WHERE memory_id IN ({marks})", memories)
    sources = turns + memories
    if sources and "associative_engrams" in tables:
        marks = ",".join("?" * len(sources))
        engrams = [row[0] for row in db.execute(
            f"SELECT engram_id FROM associative_engrams WHERE source_id IN ({marks})",
            sources)]
        if engrams:
            em = ",".join("?" * len(engrams))
            for table, column in (("associative_engrams_fts", "engram_id"),
                                  ("associative_terms", "engram_id"),
                                  ("associative_edges", "source_engram_id"),
                                  ("associative_edges", "target_engram_id"),
                                  ("associative_pending_edges", "source_engram_id"),
                                  ("associative_pending_edges", "target_engram_id"),
                                  ("associative_engrams", "engram_id")):
                if table in tables:
                    db.execute(f"DELETE FROM {table} WHERE {column} IN ({em})", engrams)
    return {"turns": len(turns), "memories": len(memories), "memory_ids": memories}


def _blank(path: Path, doomed, *, strict=False) -> int:
    """Blank the lines `doomed` picks, in place and at the same length: readers' offsets stay
    valid, and only those bytes are written. Writing the whole snapshot back let two forgets at
    once restore each other's text, and a file deleted meanwhile (expired staging) raised."""
    spans, offset = [], 0
    try:
        with path.open("rb") as stream:               # a line at a time: logs grow large
            for line in stream:
                body = line[:-1] if line.endswith(b"\n") else line
                if doomed(body):
                    spans.append((offset, len(body)))
                offset += len(line)
    except FileNotFoundError:
        return 0
    except OSError:
        if strict:
            raise
        return 0
    if spans:
        try:
            with path.open("r+b") as stream:
                for start, size in spans:
                    stream.seek(start)
                    stream.write(b" " * size)
        except FileNotFoundError:
            return 0
        except OSError:
            if strict:
                raise
            return 0
    return len(spans)


def _keys(ids: dict) -> list[str]:
    """How a target's log line spells its id (sharing writes sorted keys, ": " separators)."""
    return ([f'"turn_id": {json.dumps(t)}' for t in ids.get("turn_ids", [])]
            + [f'"memory_id": {json.dumps(m)}' for m in ids.get("memory_ids", [])])


def scrub_shared(shared, ids: dict) -> int:
    """Remove the targets' lines from the shared log. Returns how many lines went."""
    from turnbook.sharing import _pg, is_database
    keys = _keys(ids)
    if not keys:
        return 0
    if is_database(shared):
        from turnbook.sharing import _opener
        with _pg(shared) as connection:          # lines are sealed: open each to find targets
            doomed, opened = [], _opener(shared)
            rows = connection.cursor(name="turnbook_scrub")   # streamed, not all in memory
            rows.execute("SELECT seq, line FROM sharing")
            for seq, line in rows:
                text = opened(line) or line         # a refused plain line goes too
                if any(key in text for key in keys) and '"kind": "forget"' not in text:
                    doomed.append(seq)
            rows.close()
            if doomed:
                connection.execute("DELETE FROM sharing WHERE seq = ANY(%s)", (doomed,))
            connection.commit()
        return len(doomed)
    from turnbook.sharing import _opener
    opened = _opener(shared)

    def doomed(line):
        text = line.decode("utf-8", "replace").strip()
        text = opened(text) or text           # a sealed folder: open the line to look
        return any(key in text for key in keys) and '"kind": "forget"' not in text
    return sum(_blank(log, doomed, strict=True)
               for log in (Path(shared) / "log").glob("*.jsonl"))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def scrub_staging(contents) -> int:
    """Blank the lines of the hooks' and the OpenCode plugin's own transcripts that hold these
    turns. They keep what was typed, before masking, until a session has been idle an hour: a
    forgotten password stayed there all that time. Same length, in place: the capture service's
    read offsets stay valid, and a line appended meanwhile is left alone."""
    from turnbook.conversation_memory import _redact
    from turnbook.paths import turnbook_home
    wanted = {c.strip() for c in contents if c and c.strip()}
    if not wanted:
        return 0
    def doomed(line):
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:
            return False
        return any(_redact(text)[0].strip() in wanted for text in _strings(record))
    home = turnbook_home()
    return sum(_blank(path, doomed) for path in
               [*(home / "hooks").glob("*/*.jsonl"), *(home / "opencode").glob("*.jsonl")])


def drop_from_outbox(ids: dict) -> int:
    """Lines waiting to be sent to a hub that was down: a forgotten turn must not leave later."""
    import fasteners

    from turnbook.sharing import _outbox
    keys = _keys(ids)
    outbox = _outbox()
    if not outbox.exists():
        return 0
    with fasteners.InterProcessLock(str(outbox.with_suffix(".lock"))):
        lines = outbox.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if '"kind": "forget"' in line
                or not any(key in line for key in keys)]
        if len(kept) != len(lines):
            outbox.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    return len(lines) - len(kept)


def forget_ids(ids: dict, index: Path, shared=None) -> dict:
    """Erase these turns and memories here, scrub the shared log, and tell the other PCs."""
    if not (ids.get("turn_ids") or ids.get("memory_ids")):
        return {"turns": 0, "memories": 0, **retry_deletions(index, shared=shared)}
    ids = {"turn_ids": list(ids.get("turn_ids", [])),
           "memory_ids": list(ids.get("memory_ids", []))}
    contents = []
    db = sqlite3.connect(index, timeout=30)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if ids["turn_ids"] and db.execute("SELECT 1 FROM sqlite_master "
                                             "WHERE name='conversation_turns'").fetchone():
                marks = ",".join("?" * len(ids["turn_ids"]))
                contents = [unpack(plain, packed) for plain, packed in db.execute(
                    f"SELECT {text_columns(db)} FROM conversation_turns WHERE turn_id IN ({marks})",
                    ids["turn_ids"])]
            result = _erase(db, ids)
            ids["memory_ids"] = result.pop("memory_ids")  # include promoted copies
            if shared:
                db.execute("""CREATE TABLE IF NOT EXISTS forget_jobs(
                    job_id TEXT PRIMARY KEY, destination TEXT NOT NULL, ids_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    sent INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0)""")
                destination = _destination(shared)
                payload = json.dumps({key: sorted(set(value)) for key, value in ids.items()},
                                     sort_keys=True)
                job_id = hashlib.sha256((destination + "\0" + payload).encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO forget_jobs(job_id,destination,ids_json) "
                           "VALUES(?,?,?)", (job_id, destination, payload))
        _flush(db)
    finally:
        db.close()
    drop_from_outbox(ids)          # also when a notice arrives: this PC's queue held them too
    result["staging_lines_removed"] = scrub_staging(contents)
    result.update(retry_deletions(index, shared=shared))
    return result


def _destination(shared):
    from turnbook.sharing import is_database
    return str(shared) if is_database(shared) else str(Path(shared).expanduser().resolve())


RETRY_MAX = 3600.0      # seconds: a job failing for good is tried hourly, not every poll


def retry_deletions(index: Path, *, shared=None, wait=False) -> dict:
    """Resume previously accepted requests without needing their deleted text again. `wait`
    (the capture service's poll, sync) skips a job until its backoff is over: a log file that
    stays unreadable (OneDrive holding it, a cloud-only file offline) failed the scrub every
    5 s, and each retry read every log again."""
    import time

    from turnbook.sharing import append
    outcome = {"shared_lines_removed": 0, "shared_delete_pending": 0}
    if not Path(index).is_file():
        return outcome
    db = sqlite3.connect(index, timeout=30)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='forget_jobs'").fetchone():
            return outcome
        where = " WHERE destination=?" if shared is not None else ""
        parameters = (_destination(shared),) if shared is not None else ()
        due = (" AND " if where else " WHERE ") + "next_try<=?" if wait else ""
        jobs = db.execute("SELECT job_id,destination,ids_json,sent,attempts FROM forget_jobs"
                          + where + due, parameters + ((time.time(),) if wait else ())).fetchall()
        for job_id, destination, payload, sent, attempts in jobs:
            ids = json.loads(payload)
            try:
                drop_from_outbox(ids)
                if not sent:
                    # This job is already durable; an outbox enqueue is not its delivery ack.
                    append(destination, "forget", ids, require_delivery=True)
                    with db:            # sent once: a failing scrub must not resend it each try
                        db.execute("UPDATE forget_jobs SET sent=1 WHERE job_id=?", (job_id,))
                outcome["shared_lines_removed"] += scrub_shared(destination, ids)
            except Exception as error:
                with db:
                    db.execute("UPDATE forget_jobs SET attempts=attempts+1,last_error=?,next_try=? "
                               "WHERE job_id=?", (type(error).__name__,
                                                  time.time() + min(RETRY_MAX,
                                                                    60.0 * 2 ** min(attempts, 6)),
                                                  job_id))
            else:
                with db:
                    db.execute("DELETE FROM forget_jobs WHERE job_id=?", (job_id,))
        outcome["shared_delete_pending"] = db.execute(
            "SELECT count(*) FROM forget_jobs" + where, parameters).fetchone()[0]
        return outcome
    finally:
        db.close()


def forget(phrase: str, index: Path, shared=None) -> dict:
    """Find everything containing the phrase, then forget it as forget_ids does."""
    return forget_ids(find(index, phrase), index, shared)


SECRET_HINTS = ("password", "passwd", "pw", "pin", "비밀번호", "비번", "암호", "key", "token",
                "secret", "api")
LINKS = {"is", "was", "=", ":", "->", "는", "은", "이", "가"}


def masked_preview(text: str, width: int = 90) -> str:
    """Enough of a turn to recognise it, with what could be a secret replaced by ****: the word
    after "password"/"비번"/"key"..., anything after "password:" or "key=", and any word that
    mixes letters with digits or symbols. Over-masks on purpose; the original is never shown, so
    listing candidates does not put a secret back into the chat it is being removed from."""
    out, hide_next = [], False
    for token in str(text).split():
        lowered = token.lower()
        bare = token.strip(".,!?\"'()[]{}")
        hint = any(word in lowered for word in SECRET_HINTS)
        if hint and (":" in token or "=" in token):
            separator = ":" if ":" in token else "="
            out.append(token.split(separator, 1)[0] + separator + "****")
            hide_next = not token.split(separator, 1)[1]
            continue
        if hide_next and lowered in LINKS:          # "password is ...", "비번은 = ..."
            out.append(token)
            continue
        mixed = any(c.isdigit() for c in bare) and any(c.isalpha() for c in bare)
        secretish = hide_next or mixed or any(c in "!@#$%^&*_=+<>|~`\\" for c in bare)
        out.append("****" if secretish else token)
        hide_next = hint
    preview = " ".join(out)
    return preview if len(preview) <= width else preview[:width] + "…"


def main(argv=None):
    from turnbook.paths import default_development_db
    from turnbook.sharing import folder
    parser = argparse.ArgumentParser(description="Forget every captured turn and memory "
                                                 "that contains a phrase, on every PC.")
    parser.add_argument("phrase", nargs="?", help="omit it to type it without echo")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    phrase = args.phrase or getpass.getpass("Phrase to forget (not shown): ")
    if len(phrase.strip()) < 4:
        sys.exit("Use at least 4 characters, so a short word does not wipe half the memory.")
    index = default_development_db()
    retried = retry_deletions(index)
    ids = find(index, phrase)
    count = len(ids["turn_ids"]) + len(ids["memory_ids"])
    if not count:
        if retried["shared_delete_pending"]:
            print("A previous shared deletion is still pending; it will be retried automatically.")
            return
        print("Nothing in this PC's memory contains that phrase.")
        return
    print(f"{len(ids['turn_ids'])} conversation turns and {len(ids['memory_ids'])} memories "
          "contain it.")
    if not args.yes and input("Forget them here and on your other PCs? [y/N] ").lower() != "y":
        print("Nothing changed.")
        return
    shared = folder()
    result = forget(phrase, index, shared)
    print(f"Forgotten: {result['turns']} turns, {result['memories']} memories"
          + ("; shared deletion pending, automatic retry enabled" if result["shared_delete_pending"]
             else f"; {result['shared_lines_removed']} lines removed from the shared memory, "
             "other PCs follow on their next sync" if shared else "") + ".")
    print("Your AI app may still have it in its own history (Claude Code: ~/.claude/projects).")


if __name__ == "__main__":
    main()
