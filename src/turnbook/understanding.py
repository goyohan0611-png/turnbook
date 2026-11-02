"""The brain's current understanding: short state lines every attached agent reads the same way.

docs/design/state-layer.md. An agent asks for the turns not yet folded in (`next`), works out
what they say about the user, and saves it (`save`). A state line is a saved memory
(memory_type STATE) citing its turns in attributes.from_turns, so forgetting those turns takes
it too (forget.py), and a line citing a forgotten turn is never stored (local_brain.remember).

- Progress is this PC's: turns are taken in the order they arrived here (conversation_turns
  revision), per scope, so a past conversation imported late is not skipped. One PC should do
  the understanding of a scope; other PCs receive its lines through sync.
- A batch is fixed when it is handed out: its turns (ids and hashes), the progress generation it
  started from. `save` writes every line and moves the progress in one transaction. Two agents
  on one batch: the first save wins, the other gets STALE_BATCH. The same batch saved again with
  the same lines answers the first result (a lost reply); with other lines, a conflict.
- Lines carry a stable state_key; a later version is a new memory with the same key (grouped
  like a saved fact, attributes.fact). History is built from versions, never written into the
  text. No supersedes link: recall drops superseded memories, which would drop the history.
- Understood facts are grouped apart from facts the user saved ("understood:<key>"), so an
  inference never replaces what the user said.
- Kinds: value (a current value), event (something that happened), item (one member of a group
  that spans sessions). Status: fact | plan | uncertain. `date` is when it happened or became
  true, null when unknown; when it was said comes from the turns.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import uuid

KINDS = {"value", "event", "item"}
STATUSES = {"fact", "plan", "uncertain"}
MAX_TEXT = 220                       # one line in a packet; the turns hold the rest
MAX_TURNS, MAX_CHARS = 40, 24000
PIECE = 6000                         # a user turn longer than this comes in pieces, in order
ASSISTANT_CHARS = 1500               # the assistant's words are context: the start of each
MAX_LINES, STATE_SHOWN = 120, 40
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

GUIDE = (
    "Work out what these turns say about the user and save it as state lines. One JSON object "
    "per line: kind value|event|item, subject, attribute (value only), value (a few words, "
    "as the user said it), status fact|plan|uncertain (a plan is not a fact; contradicting "
    "statements are uncertain), date YYYY-MM-DD when it happened or became true (null if not "
    "said: never the day it was mentioned), sources [refs]. To update a line in `state`, give "
    "its state_key. A new value line may list replaced [[earlier value, YYYY-MM-DD]] when the "
    "turns give the history. Only what the user says about themselves, their life, work and "
    "plans; not the assistant's suggestions unless the user did them. Nothing worth keeping: "
    "save with no lines, so these turns are marked done.")


def compact(line: dict) -> str:
    """`old sneaker storage: shoe rack (plan)` - one short line for a packet."""
    subject = str(line.get("subject") or "").strip()
    attribute = str(line.get("attribute") or "").strip()
    value = " ".join(str(line.get("value") or "").split())
    kind = line.get("kind")
    if kind == "value":
        head = attribute or subject
    else:
        head = subject if not attribute or subject.lower() in {"user", "me", "i"} \
            else f"{subject} {attribute}"
    text = f"{head}: {value}" if head else value
    notes = [line["status"]] if line.get("status") in {"plan", "uncertain"} else []
    if kind == "event" and line.get("date"):
        notes.append(line["date"])
    if line.get("planned_for"):
        notes.append(f"for {line['planned_for']}")
    if notes:
        text += f" ({', '.join(notes)})"
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT - 1].rstrip() + "…"
    return text


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def _words(text):
    return {w for w in re.findall(r"[^\W_]{3,}", str(text).lower())}


class Understanding:
    def __init__(self, database):
        from turnbook.local_brain import LocalBrainStore
        self.brain = LocalBrainStore(database)   # its schema; one connection for everything
        self.db = self.brain.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS understand_progress(
          scope TEXT PRIMARY KEY, last_revision INTEGER NOT NULL DEFAULT 0,
          generation INTEGER NOT NULL DEFAULT 0, last_offset INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS understand_batches(
          batch_id TEXT PRIMARY KEY, scope TEXT NOT NULL, generation INTEGER NOT NULL,
          end_revision INTEGER NOT NULL, refs_json TEXT NOT NULL, status TEXT NOT NULL,
          payload_sha TEXT, result_json TEXT, created_at TEXT NOT NULL,
          end_offset INTEGER NOT NULL DEFAULT 0);""")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.brain.close()

    def _progress(self, scope):
        """(last revision fully read, generation, offset read into the next user turn)."""
        row = self.db.execute("SELECT last_revision, generation, last_offset "
                              "FROM understand_progress WHERE scope=?", (scope,)).fetchone()
        return (row[0], row[1], row[2]) if row else (0, 0, 0)

    def state(self, scope):
        """The current version of every state line in the scope, newest first."""
        from turnbook.local_brain import current_facts
        rows = self.db.execute(
            "SELECT m.memory_id, m.scope, m.content, m.effective_at, m.known_at, "
            "p.attributes_json FROM local_brain_memories m JOIN local_brain_memory_profiles p "
            "USING (memory_id) WHERE m.scope=? AND m.memory_type='STATE'", (scope,)).fetchall()
        lines = [{"memory_id": r[0], "scope": r[1], "content": r[2], "effective_at": r[3],
                  "known_at": r[4], "attributes": json.loads(r[5])} for r in rows]
        return sorted(current_facts(lines), key=lambda m: m["known_at"], reverse=True)

    def next(self, scope="global"):
        """The next turns to fold in, fixed as a batch, with the state lines they may touch."""
        from turnbook.packing import unpack
        last, generation, offset = self._progress(scope)
        if not self.db.execute("SELECT 1 FROM sqlite_master "
                               "WHERE name='conversation_turns'").fetchone():
            return {"status": "UP_TO_DATE", "scope": scope}      # nothing captured yet
        # a user turn read in pieces is taken up again where the last batch stopped
        rows = self.db.execute(
            "SELECT revision, turn_id, raw_sha256, happened_at, role, client, "
            "content_redacted, content_z FROM conversation_turns WHERE scope=? AND revision>? "
            "AND role IN ('user','assistant') AND client<>'document' ORDER BY revision LIMIT ?",
            (scope, last - 1 if offset else last, MAX_TURNS)).fetchall()
        if not rows:
            return {"status": "UP_TO_DATE", "scope": scope}
        turns, size, end_offset = [], 0, 0
        for revision, turn_id, sha, when, role, client, text, packed in rows:
            text = unpack(text, packed)
            start = offset if revision == last and offset else 0
            if role == "assistant":
                piece, more = text[:ASSISTANT_CHARS], False      # context, not the subject
            else:
                piece, more = text[start:start + PIECE], start + PIECE < len(text)
            if turns and size + len(piece) > MAX_CHARS:
                break
            size += len(piece)
            turns.append({"revision": revision, "turn_id": turn_id, "sha": sha, "when": when,
                          "role": role, "client": client, "text": piece,
                          "part": f"{start + 1}-{start + len(piece)} of {len(text)}"
                          if start or more else None})
            if more:                         # the rest of this turn comes in the next batch
                end_offset = start + PIECE
                break
        refs = {f"t{n}": {"turn_id": t["turn_id"], "sha": t["sha"], "when": t["when"]}
                for n, t in enumerate(turns, 1)}
        batch = "ub-" + uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO understand_batches VALUES(?,?,?,?,?,?,?,?,?,?)", (
                batch, scope, generation, turns[-1]["revision"], json.dumps(refs), "open",
                None, None, _now(), end_offset))
        # shown in the order they happened; the refs keep the arrival order
        shown = sorted(zip(refs, turns), key=lambda pair: pair[1]["when"])
        # the lines sharing words with these turns first, then the newest: "I moved to
        # Incheon" shares no word with "city: Seoul", and a line not shown gets a duplicate
        said = set().union(*(_words(t["text"]) for t in turns))
        lines = self.state(scope)
        related = sorted(lines, key=lambda m: not said & _words(
            m["content"] + " " + json.dumps(m["attributes"])))
        return {"status": "BATCH", "batch": batch, "scope": scope, "guide": GUIDE,
                "turns": [{"ref": ref, "when": t["when"][:16], "role": t["role"],
                           "app": t["client"], "text": t["text"],
                           **({"part": t["part"]} if t["part"] else {})}
                          for ref, t in shown],
                "state": [{"state_key": m["attributes"].get("state_key"),
                           "kind": m["attributes"].get("kind"), "line": m["content"]}
                          for m in related[:STATE_SHOWN]]}

    def save(self, batch, lines):
        """Write the batch's lines and move the progress past it, all or nothing."""
        if len(lines or []) > MAX_LINES:
            raise ValueError(f"at most {MAX_LINES} lines per batch")  # nothing moves
        lines = [_validate(line) for line in (lines or [])]
        payload = hashlib.sha256(json.dumps(lines, sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT scope, generation, end_revision, refs_json, status, "
                                  "payload_sha, result_json, end_offset FROM understand_batches "
                                  "WHERE batch_id=?", (batch,)).fetchone()
            if row is None:
                raise _Conflict("UNKNOWN_BATCH")
            scope, generation, end, refs, status, saved_sha, saved, end_offset = row
            refs = json.loads(refs)
            if status == "saved":
                self.db.rollback()
                if saved_sha == payload:
                    return json.loads(saved)                  # a lost reply, answered again
                return {"status": "CONFLICT", "reason": "BATCH_SAVED_WITH_OTHER_LINES"}
            if self._progress(scope)[1] != generation:
                raise _Conflict("STALE_BATCH")               # another agent saved first
            self._published = []
            written, skipped = self._write(scope, batch, refs, lines)
            result = {"status": "OK", "batch": batch, "saved": written, "skipped": skipped}
            # a turn read only in part: its revision is not done yet, the offset says how far
            self.db.execute(
                "INSERT INTO understand_progress VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE "
                "SET last_revision=excluded.last_revision, generation=excluded.generation, "
                "last_offset=excluded.last_offset",
                (scope, end, generation + 1, end_offset))
            self.db.execute("UPDATE understand_batches SET status='saved', payload_sha=?, "
                            "result_json=? WHERE batch_id=?",
                            (payload, json.dumps(result), batch))
            self.db.commit()
            self._publish()
            return result
        except _Conflict as conflict:
            self.db.rollback()
            return {"status": "CONFLICT", "reason": str(conflict)}
        except BaseException:
            self.db.rollback()
            raise

    def _publish(self):
        """Every version saved, to the shared log, as brain_remember does: other PCs import
        the understanding this PC worked out. Unreachable: queued by sharing, not dropped."""
        if not self._published:
            return
        try:
            from turnbook.sharing import append_many, folder
            shared = folder()
            if shared:
                append_many(shared, [("memory", memory) for memory in self._published])
        except Exception:
            # ponytail: kept on this PC; brain_folder(share_existing=true) sends it later
            pass

    def _write(self, scope, batch, refs, lines):
        """Each line as one or more memory versions, in the caller's transaction."""
        current = {m["attributes"].get("state_key"): m for m in self.state(scope)}
        written, skipped = [], []
        self.brain.batch = True
        try:
            for n, line in enumerate(lines):
                cited = [refs[r] for r in line["sources"] if r in refs]
                alive = [c for c in cited if self.db.execute(
                    "SELECT 1 FROM conversation_turns WHERE turn_id=? AND raw_sha256=?",
                    (c["turn_id"], c["sha"])).fetchone()]
                if not alive or len(alive) != len(line["sources"]):
                    skipped.append({"line": n, "reason": "SOURCES_NOT_IN_BATCH_OR_GONE"})
                    continue
                key = line.get("state_key")
                if key and key not in current:
                    skipped.append({"line": n, "reason": "UNKNOWN_STATE_KEY"})
                    continue
                key = key or "sk-" + uuid.uuid4().hex[:10]
                said = max(c["when"] for c in alive)
                previous = current[key]["memory_id"] if key in current else None
                history = [] if previous else line.get("replaced") or []
                for value, when in history:          # older values, as their own versions
                    previous = self._version(scope, batch, key, {**line, "value": value,
                                             "date": when, "status": "fact"},
                                             alive, min(when, said[:10]), previous)
                memory = self._version(scope, batch, key, line, alive, said, previous)
                current[key] = {"memory_id": memory, "attributes": {"state_key": key}}
                written.append({"line": n, "state_key": key, "memory_id": memory})
        finally:
            self.brain.batch = False
        return written, skipped

    def _version(self, scope, batch, key, line, cited, said, previous):
        date = line.get("date")
        happened = date if date and date <= said[:10] else None
        when = (happened or said[:10]) + "T00:00:00" if line["kind"] != "item" else said
        memory_id = "state-" + uuid.uuid4().hex[:16]
        shown = {**line}
        if date and not happened:            # a date still ahead: what is planned for it
            shown["planned_for"] = date
            shown.pop("date", None)
        attributes = {"kind": line["kind"], "state_key": key, "fact": "understood:" + key,
                      "version_of": previous,
                      "from_turns": [c["turn_id"] for c in cited], "status": line["status"],
                      "subject": line["subject"], "date": happened,
                      "planned_for": shown.get("planned_for")}
        if line.get("attribute"):
            attributes["attribute"] = line["attribute"]
        memory = {
            "memory_id": memory_id, "scope": scope, "memory_type": "STATE",
            "title": compact({**shown, "value": ""}).rstrip(": ") or line["subject"],
            "content": compact(shown), "effective_at": min(when, _now()),
            "known_at": _now(), "actor": "agent", "tags": ["state", line["kind"]],
            "entities": [line["subject"]], "supersedes_memory_id": None,
            "provenance": {"source_type": "understanding", "source_ref": batch},
            "attributes": attributes, "confidence": 0.7}
        result = self.brain.remember(memory)
        if result.get("disposition") not in {"RECORDED", "ALREADY_RECORDED"}:
            raise _Conflict("SOURCE_FORGOTTEN")
        self._published.append(memory)
        return memory_id


class _Conflict(Exception):
    pass


def _clean(text, size):
    """An agent's words for a line: <private> parts out, secrets masked (as brain_remember
    masks a memory), one line, bounded. An agent can copy a key from the chat into a value."""
    from turnbook.conversation_memory import _redact
    from turnbook.privacy import strip_private
    return " ".join(_redact(strip_private(str(text or "")))[0].split())[:size]


def _validate(line):
    if not isinstance(line, dict):
        raise ValueError("each line is an object")
    kind = line.get("kind")
    if kind not in KINDS:
        raise ValueError("kind is value, event or item")
    subject = _clean(line.get("subject"), 80)
    value = _clean(line.get("value"), 300)
    if not subject or not value:
        raise ValueError("subject and value are required")
    sources = line.get("sources")
    if not isinstance(sources, list) or not sources or not all(
            isinstance(s, str) for s in sources):
        raise ValueError("sources: the refs of the turns it came from")
    date = line.get("date")
    if date is not None and not (isinstance(date, str) and DATE.fullmatch(date)):
        raise ValueError("date is YYYY-MM-DD or null")
    status = line.get("status") or "fact"
    if status not in STATUSES:
        raise ValueError("status is fact, plan or uncertain")
    clean = {"kind": kind, "subject": subject, "value": value, "status": status,
             "date": date, "sources": sources[:10]}
    if kind == "value" and line.get("attribute"):
        clean["attribute"] = _clean(line["attribute"], 80)
    if isinstance(line.get("state_key"), str) and line["state_key"]:
        clean["state_key"] = line["state_key"][:40]
    replaced = line.get("replaced")
    if kind == "value" and isinstance(replaced, list):
        clean["replaced"] = [[_clean(v, 300), w] for v, w in
                             (pair for pair in replaced[:5] if isinstance(pair, list)
                              and len(pair) == 2) if isinstance(w, str) and DATE.fullmatch(w)]
    return clean


if __name__ == "__main__":           # the check this module leaves behind
    assert compact({"subject": "User", "kind": "value", "attribute": "old sneaker storage",
                    "value": "shoe rack in the closet", "status": "plan"}) == \
        "old sneaker storage: shoe rack in the closet (plan)"
    assert compact({"subject": "trips", "kind": "event", "value": "Jeju, 3 nights",
                    "date": "2023-04-11"}) == "trips: Jeju, 3 nights (2023-04-11)"
    assert compact({"subject": "bikes owned", "kind": "item", "value": "road bike"}) == \
        "bikes owned: road bike"
    assert len(compact({"kind": "item", "subject": "x", "value": "y" * 500})) == MAX_TEXT
    print("ok")
