"""Phase 6 (consolidation): after answering from old turns, the model writes a note; does the next
question on the same thing need a smaller packet, and is it still answered right?

On the 125 multi-session questions (where answers are pieced together from several turns):

    pass 1   today's packet for the question; the reader answers; then the same model, playing the
             assistant that just answered, writes the note it would save (brain_remember, with the
             passages it used as `from_turns`) and asks the same thing in other words
    pass 2   the reworded question, three ways:
               plain       no note (today), the reference: rewording alone moves accuracy
               note        the note in the store; the packet drops the turns it cites and keeps
                           NOTE_TURNS other turns (the shipped rule)
               note_all    the same without the cap: only the cited turns go

Gate (research plan): pass-2 packet tokens -30% against plain, accuracy no lower. Also counted:
questions pass 1 got wrong whose wrong answer the note then carries into pass 2.

The packet stages need the bench's cached vectors and run in CI (`research` input of bench.yml);
the stages that call the model run locally, and the key never leaves this PC.

    python consolidation_eval.py spans1                      # CI
    OPENAI_API_KEY=... python consolidation_eval.py answer1  # reader + judge, pass 1
    OPENAI_API_KEY=... python consolidation_eval.py notes    # note + rewording
    python consolidation_eval.py spans2                      # CI
    OPENAI_API_KEY=... python consolidation_eval.py answer2  # reader + judge, three arms
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import sys
import tempfile
import uuid

import episode_eval  # noqa: F401  (first: isolates TURNBOOK_HOME, sets up the paths)

from evidence import CACHE, DATA, fill, iso  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402

from turnbook import context_packet  # noqa: E402
from turnbook.context_packet import build_context_pack  # noqa: E402
from turnbook.local_brain import LocalBrainStore  # noqa: E402
from turnbook.mcp_server import _PIECES, CONTEXT_CHARS, CONTEXT_LIMIT, gather  # noqa: E402
from turnbook.warmup import warm_turns  # noqa: E402

OUT = Path(__file__).resolve().parent / "runs/graph-v9.7-consolidation-clean"
TYPE = "multi-session"
NOTE_PROMPT = """You are the assistant that just answered the user's question below from these \
passages of their past conversations. Do two things.
1. note: the memory you would save now so that next time you need not dig through the \
conversations again. State what the user told you, with numbers, names and dates, in a few \
sentences; not the reasoning. If your answer was UNKNOWN, note only what the passages do say.
2. cited: the numbers of the passages the note rests on.
3. reworded: the same question as the user might ask it another day, in different words, \
asking for the same information."""
NOTE_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["note", "cited", "reworded"],
               "properties": {"note": {"type": "string"}, "reworded": {"type": "string"},
                              "cited": {"type": "array", "items": {"type": "integer"}}}}


def rows():
    return [r for r in stream_instances(DATA) if r["question_type"] == TYPE
            and not r["question_id"].endswith("_abs")]


def packet(row, store, question, embedder):
    recalled = gather(store, question, as_of=iso(row["question_date"]), limit=CONTEXT_LIMIT,
                      embedder=embedder)
    pack = build_context_pack(recalled, max_chars=CONTEXT_CHARS)
    return {"question_id": row["question_id"], "estimated_tokens": pack["estimated_tokens"],
            "passages": [{"id": i["id"],
                          "session_id": i["provenance"]["source_ref"].rsplit(":", 1)[0],
                          "session_date": i["effective_at"],
                          "speaker": i["title"].split(" ", 1)[0],
                          "text": i["content"]} for i in pack["items"]]}


def build(name, arms):
    """arms: {arm: fn(row, store) -> question or None}; one packet per arm per question."""
    from turnbook.local_embedder import LocalEmbedder
    embedder = LocalEmbedder(CACHE / "embed-cache.json")
    logs = {}
    for arm in arms:
        (OUT / arm).mkdir(parents=True, exist_ok=True)
        logs[arm] = (OUT / arm / "spans.jsonl").open("w", encoding="utf-8")
    with tempfile.TemporaryDirectory() as directory:
        for n, row in enumerate(rows(), 1):
            store = Path(directory) / f"{row['question_id']}.sqlite"
            fill(store, row)
            while warm_turns(store, limit=2048, embedder=embedder):
                pass
            for arm, prepare in arms.items():
                question = prepare(row, store)
                if question:
                    logs[arm].write(json.dumps(packet(row, store, question, embedder),
                                               ensure_ascii=False) + "\n")
            _PIECES.pop(str(store), None)
            store.unlink()
            print(f"  {name} {n}", flush=True)
    for log in logs.values():
        log.close()
    embedder.cache.flush()


def spans1():
    build("spans1", {"pass1": lambda row, store: row["question"]})


def spans2():
    notes = {r["question_id"]: r for r in map(json.loads, (OUT / "notes.jsonl").open(encoding="utf-8"))
             if r["status"] == "ok"}

    def plain(row, store):
        return notes.get(row["question_id"], {}).get("reworded")

    def noted(row, store):
        note = notes.get(row["question_id"])
        if not note:
            return None
        if not note.get("saved"):
            when = iso(row["question_date"])
            with LocalBrainStore(store) as brain:
                brain.remember({
                    "memory_id": "note-" + uuid.uuid4().hex, "scope": "global",
                    "memory_type": "summary", "title": "note " + note["note"][:75],
                    "content": note["note"], "effective_at": when, "known_at": when,
                    "actor": "assistant", "tags": [], "entities": [],
                    "provenance": {"source_type": "mcp-client",
                                   "source_ref": row["question_id"] + ":note"},
                    "supersedes_memory_id": None,
                    "attributes": {"from_turns": note["from_turns"]}})
            note["saved"] = True               # one note per question's store
        return note["reworded"]

    original = context_packet.NOTE_TURNS

    def arm(name):
        def prepare(row, store):           # plain runs first, before the note is saved
            context_packet.NOTE_TURNS = CONTEXT_LIMIT if name == "note_all" else original
            return (plain if name == "plain" else noted)(row, store)
        return prepare

    build("spans2", {name: arm(name) for name in ("plain", "note", "note_all")})
    context_packet.NOTE_TURNS = original


def ask(arm, reworded=None, baseline=None):
    import product_answer_eval as pae
    ids = OUT / "ids.json"
    original = pae.questions

    def questions():
        out = original()
        return [{**r, "question": reworded[r["question_id"]]} for r in out
                if r["question_id"] in reworded] if reworded else out

    pae.questions, pae.OUT, pae.IDS_FILE = questions, OUT / arm, ids
    if baseline:
        pae.BASELINE = OUT / baseline / "official-judge.jsonl"
    pae.answer()
    pae.questions = original


def answer1():
    ids = [json.loads(l)["question_id"] for l in (OUT / "pass1" / "spans.jsonl").open(encoding="utf-8")]
    (OUT / "ids.json").write_text(json.dumps({"question_ids": ids}, indent=1), encoding="utf-8")
    ask("pass1")


def notes():
    import longmemeval_retrieval_executor as pipe
    key = os.environ["OPENAI_API_KEY"]
    packets = {r["question_id"]: r for r in map(json.loads, (OUT / "pass1" / "spans.jsonl")
                                                .open(encoding="utf-8"))}
    answers = pipe.load_jsonl(OUT / "pass1" / "answers.jsonl", "question_id")
    asked = {r["question_id"]: r for r in rows()}
    path = OUT / "notes.jsonl"
    done = pipe.finished(pipe.load_jsonl(path, "question_id")) if path.exists() else set()

    def send(qid):
        passages = packets[qid]["passages"]
        text = "\n\n".join(f"[{i}] {p['session_date'][:10]} {p['speaker']}: {p['text']}"
                           for i, p in enumerate(passages))
        body = {"model": pipe.READER_MODEL, "store": False, "reasoning": {"effort": "low"},
                "input": [{"role": "system", "content": NOTE_PROMPT},
                          {"role": "user", "content": f"Question ({asked[qid]['question_date']}): "
                           f"{asked[qid]['question']}\nYour answer: "
                           f"{answers[qid]['result']['answer']}\n\nPassages:\n{text}"}],
                "text": {"format": {"type": "json_schema", "name": "note", "strict": True,
                                    "schema": NOTE_SCHEMA}},
                "max_output_tokens": 4000}
        record = pipe.safe_call(key, body, {"question_id": qid},
                                lambda raw: json.loads(pipe.response_text(raw)))
        if record["status"] == "ok":
            result = record.pop("result")
            record.update(note=result["note"], reworded=result["reworded"],
                          from_turns=[passages[i]["id"] for i in result["cited"]
                                      if 0 <= i < len(passages)])
        return record

    pipe.run_parallel([q for q in packets if q in answers and q not in done], path, 8, send)


def answer2():
    reworded = {r["question_id"]: r["reworded"]
                for r in map(json.loads, (OUT / "notes.jsonl").open(encoding="utf-8"))
                if r["status"] == "ok"}
    ask("plain", reworded)
    for arm in ("note", "note_all"):
        ask(arm, reworded, baseline="plain")


if __name__ == "__main__":
    {"spans1": spans1, "answer1": answer1, "notes": notes, "spans2": spans2,
     "answer2": answer2}[sys.argv[1]]()
