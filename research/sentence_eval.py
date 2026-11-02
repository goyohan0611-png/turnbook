"""Phase 5, second try: search the user's sentences, not 500-character pieces.

episode_eval.py found that 47 of the 107 multi-session answer turns missing from the packet are in
neither top list: the fact is one "by the way" sentence in a turn about something else ("tips on
portrait photography? By the way, I attended a photography workshop..."), and the piece's vector
is about the main topic. Here every user sentence gets its own vector; a turn ranks by its best
sentence, and the packet shows the 500 characters around that sentence. Still no model at write
time: only more vectors (about four times as many).

    base       today's path (gather with the shipped defaults)
    sent       sentence ranking + word search, fused as today's two lists are
    mix        piece ranking + sentence ranking + word search

    python sentence_eval.py --types multi-session single-session-user
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import json
import re
import sys
import tempfile

import episode_eval  # noqa: F401  (first: isolates TURNBOOK_HOME, sets up the paths)
from episode_eval import scored

from evidence import CACHE, DATA, TUNED, fill, iso, report  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402

import numpy as np  # noqa: E402

from turnbook.context_packet import build_context_pack  # noqa: E402
from turnbook.conversation_memory import ConversationMemoryStore  # noqa: E402
from turnbook.packing import unpack  # noqa: E402
from turnbook.local_embedder import LocalEmbedder  # noqa: E402
from turnbook.mcp_server import (_PIECES, CONTEXT_CHARS, CONTEXT_LIMIT, PIECE, excerpt,  # noqa: E402
                                   gather, semantic_turns)
from turnbook.warmup import turn_pieces, warm_turns  # noqa: E402

OUT = Path(__file__).resolve().parent / "runs/graph-v9.7-sentences-clean"
SPANS = False      # --spans: also keep the base and mix packets, in a folder of their own
DEPTH = 100
SPLIT = re.compile(r"[^.!?\n]+[.!?]*")


def sentences(text):
    """(offset, sentence) for every sentence long enough to state something."""
    return [(m.start(), m.group().strip()[:300]) for m in SPLIT.finditer(text)
            if len(m.group().strip()) >= 15]


def sentence_turns(store, query, embedder):
    with ConversationMemoryStore(store) as conversations:
        turns = [conversations._turn(r) for r in conversations.db.execute(
            "SELECT * FROM conversation_turns WHERE role = 'user'")]
    units = [(turn, offset, text) for turn in turns for offset, text in sentences(turn["content"])]
    if not units:
        return []
    vectors = np.asarray(embedder.embed([text for _, _, text in units]), dtype=np.float32)
    question = np.asarray(embedder.embed([query])[0], dtype=np.float32)
    scores = vectors @ question / (np.linalg.norm(vectors, axis=1) * np.linalg.norm(question) + 1e-9)
    best = {}
    for i in np.argsort(-scores):
        turn, offset, _ = units[i]
        if turn["turn_id"] not in best:
            start = max(0, offset - 150)
            best[turn["turn_id"]] = {**turn, "content": turn["content"][start:start + PIECE]}
            if len(best) == DEPTH:
                break
    return list(best.values())


def measure(row, directory, embedder):
    store = directory / f"{row['question_id']}.sqlite"
    wanted = fill(store, row)
    if not wanted:
        return None
    while warm_turns(store, limit=2048, embedder=embedder):
        pass
    arms = {"base": gather(store, row["question"], as_of=iso(row["question_date"]),
                           limit=CONTEXT_LIMIT, embedder=embedder)["conversation_turns"]}
    meaning = semantic_turns(store, row["question"], limit=30, embedder=embedder)
    with ConversationMemoryStore(store) as conversations:
        words = [excerpt(t) for t in conversations.search(row["question"], limit=30)["turns"]]
    sent = sentence_turns(store, row["question"], embedder)
    arms["sent"] = [t for _, t in scored(sent[:30], words)[:CONTEXT_LIMIT]]
    arms["mix"] = [t for _, t in scored(meaning, sent[:30], words)[:CONTEXT_LIMIT]]
    out = {"question_id": row["question_id"], "type": row["question_type"], "wanted": len(wanted)}
    with ConversationMemoryStore(store) as conversations:      # the cost: vectors per memory
        contents = [(r[0], unpack(r[1], r[2]) or "") for r in conversations.db.execute(
            "SELECT role, content_redacted, content_z FROM conversation_turns")]
    out["vectors"] = {"pieces": sum(len(turn_pieces(c)) for _, c in contents),
                      "sentences": sum(len(sentences(c)) for role, c in contents
                                       if role == "user")}
    for name, turns in arms.items():
        turns = [{k: v for k, v in t.items() if k != "full"} for t in turns]
        packet = build_context_pack({"matches": [], "conversation_turns": turns},
                                    max_chars=CONTEXT_CHARS)
        out[name] = {"found": len({i["id"] for i in packet["items"]} & wanted),
                     "tokens": packet["estimated_tokens"]}
        if SPANS and name in ("base", "mix"):                 # for the reader and the judge
            (OUT / name).mkdir(parents=True, exist_ok=True)
            with (OUT / name / "spans.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({
                    "question_id": row["question_id"],
                    "estimated_tokens": packet["estimated_tokens"],
                    "passages": [{"id": i["id"],
                                  "session_id": i["provenance"]["source_ref"].rsplit(":", 1)[0],
                                  "session_date": i["effective_at"],
                                  "speaker": i["title"].split(" ", 1)[0],
                                  "text": i["content"]} for i in packet["items"]]},
                    ensure_ascii=False) + "\n")
    _PIECES.pop(str(store), None)
    store.unlink()
    return out


def main():
    parser = ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--types", nargs="*")
    parser.add_argument("--spans", action="store_true")
    parser.add_argument("--answer", action="store_true",
                        help="reader + official judge on kept packets (OPENAI_API_KEY)")
    args = parser.parse_args()
    global OUT, SPANS
    if args.spans or args.answer:
        OUT, SPANS = OUT.with_name("graph-v9.9-sentences-qa"), True
    if args.answer:
        return answer()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "records.jsonl"
    done = {json.loads(l)["question_id"]: json.loads(l) for l in path.open(encoding="utf-8")} \
        if path.exists() else {}
    embedder = LocalEmbedder(CACHE / "embed-cache.json")
    records, seen = [], 0
    with tempfile.TemporaryDirectory() as directory, path.open("a", encoding="utf-8") as log:
        for row in stream_instances(DATA):
            if args.types and row["question_type"] not in args.types:
                continue
            if args.limit and seen >= args.limit:
                break
            seen += 1
            record = done.get(row["question_id"]) or measure(row, Path(directory), embedder)
            if record is None:
                continue
            if row["question_id"] not in done:
                log.write(json.dumps(record) + "\n")
                log.flush()
            records.append(record)
            print(f"  {seen} questions, {len(records)} with evidence", flush=True)
            if seen % 10 == 0:
                embedder.cache.flush()
    embedder.cache.flush()
    tuned = set(json.loads(TUNED.read_text(encoding="utf-8"))["question_ids"])
    result = {arm: report([{"question_id": r["question_id"], "type": r["type"],
                            "wanted": r["wanted"], **r[arm]} for r in records], tuned)
              for arm in ("base", "sent", "mix")}
    (OUT / "report.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    for arm, groups in result.items():
        print(arm, {g: (s["question_recall"], s["turn_coverage"], s["packet_tokens_median"])
                    for g, s in groups.items() if s})


def answer():
    """Same reader, prompt and official judge as every accuracy number; mix against base."""
    import product_answer_eval as pae
    kept = (OUT / "base" / "spans.jsonl").open(encoding="utf-8")
    ids = [json.loads(line)["question_id"] for line in kept]
    (OUT / "ids.json").write_text(json.dumps({"question_ids": ids}, indent=1), encoding="utf-8")
    pae.IDS_FILE = OUT / "ids.json"
    for arm in ("base", "mix"):
        pae.OUT = OUT / arm
        if arm == "mix":
            pae.BASELINE = OUT / "base" / "official-judge.jsonl"
        pae.answer()
    records = [json.loads(l) for l in (OUT / "records.jsonl").open(encoding="utf-8")]
    pieces = sum(r["vectors"]["pieces"] for r in records)
    print({"pieces": pieces, "plus_sentences": sum(r["vectors"]["sentences"] for r in records),
           "questions": len(records)})


if __name__ == "__main__":
    main()
