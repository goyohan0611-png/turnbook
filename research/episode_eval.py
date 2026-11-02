"""Phase 5 (episodes): does choosing turns by conversation, not one by one, bring more evidence?

The packet takes the top 30 turns of one fused ranking. A multi-session question needs turns from
several conversations, but the top 30 can come mostly from one: on the 14 October baseline only
66.7% of multi-session answer turns reached the packet (94.4% for knowledge-update). Here every
question's haystack is filled and searched as bench/evidence.py does, the two rankings are taken
deeper (100), and several ways of picking 30 turns from them are compared on the same questions:

    base       today's path (gather with the shipped defaults), the reference
    deep       the top 30 of the deeper rankings, to separate depth from episodes
    capK       at most K turns per episode, the rest filled down the ranking
    episode    turn score + the episode's score (its best two turns), so a conversation that
               matches as a whole lifts its turns

An episode is one conversation: in LongMemEval every session is one conversation on one day, so
the session is the episode. No model is called; it costs nothing but CPU.

    python episode_eval.py               # all 500 (vectors shared with bench/.cache)
    python episode_eval.py --limit 40
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import json
import os
import sys
import tempfile

# Never the developer's own memory: gather() syncs a configured shared folder into every store.
os.environ["TURNBOOK_HOME"] = tempfile.mkdtemp(prefix="graph-mind-eval-home-")
for _name in ("TURNBOOK_CONFIG", "TURNBOOK_FOLDER", "TURNBOOK_DB"):
    os.environ.pop(_name, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))

from evidence import CACHE, DATA, TUNED, fill, iso, report  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402

from turnbook.context_packet import build_context_pack  # noqa: E402
from turnbook.conversation_memory import ConversationMemoryStore  # noqa: E402
from turnbook.packing import unpack  # noqa: E402
from turnbook.local_embedder import LocalEmbedder  # noqa: E402
from turnbook.mcp_server import (_PIECES, CONTEXT_CHARS, CONTEXT_LIMIT, excerpt, gather,  # noqa: E402
                                   semantic_turns)
from turnbook.warmup import warm_turns  # noqa: E402

OUT = Path(__file__).resolve().parent / "runs/graph-v9.7-episodes-clean"
DEPTH = 100


def scored(*rankings):
    """fuse() with its scores kept: reciprocal-rank fusion, best first."""
    scores, keep = {}, {}
    for ranking in rankings:
        for rank, turn in enumerate(ranking):
            scores[turn["turn_id"]] = scores.get(turn["turn_id"], 0.0) + 1.0 / (60 + rank)
            keep.setdefault(turn["turn_id"], turn)
    return [(scores[t], keep[t]) for t in sorted(scores, key=scores.get, reverse=True)]


def episode(turn):
    return turn["source_path"]


def cap(ranked, k):
    picked, count, rest = [], {}, []
    for score, turn in ranked:
        if count.get(episode(turn), 0) < k:
            count[episode(turn)] = count.get(episode(turn), 0) + 1
            picked.append(turn)
        else:
            rest.append(turn)
    return (picked + rest)[:CONTEXT_LIMIT]


def lifted(ranked, weight):
    best = {}
    for score, turn in ranked:
        best.setdefault(episode(turn), []).append(score)
    lift = {e: sum(s[:2]) for e, s in best.items()}
    order = sorted(ranked, key=lambda st: st[0] + weight * lift[episode(st[1])], reverse=True)
    return [turn for _, turn in order[:CONTEXT_LIMIT]]


def measure(row, directory, embedder):
    store = directory / f"{row['question_id']}.sqlite"
    wanted = fill(store, row)
    if not wanted:
        return None
    while warm_turns(store, limit=2048, embedder=embedder):
        pass
    as_of = iso(row["question_date"])
    arms = {"base": gather(store, row["question"], as_of=as_of, limit=CONTEXT_LIMIT,
                           embedder=embedder)["conversation_turns"]}
    meaning = semantic_turns(store, row["question"], limit=DEPTH, embedder=embedder)
    with ConversationMemoryStore(store) as conversations:
        words = [excerpt(t) for t in conversations.search(row["question"], limit=50)["turns"]]
    ranked = scored(meaning, words)
    arms["deep"] = [t for _, t in ranked[:CONTEXT_LIMIT]]
    for k in (2, 3, 5):
        arms[f"cap{k}"] = cap(ranked, k)
    for w in (0.5, 1.0):
        arms[f"episode{w}"] = lifted(ranked, w)
    position = {t["turn_id"]: i for i, (_, t) in enumerate(ranked)}
    with ConversationMemoryStore(store) as conversations:
        roles = {turn_id: f"{role}:{len(unpack(plain, packed))}" for turn_id, role, plain, packed
                 in conversations.db.execute("SELECT turn_id, role, content_redacted, content_z "
                                             "FROM conversation_turns")}
    out = {"question_id": row["question_id"], "type": row["question_type"], "wanted": len(wanted),
           # where each answer turn sits in the deeper fused ranking (None: in neither list)
           "ranks": {t: [position.get(t), roles.get(t)] for t in sorted(wanted)}}
    for name, turns in arms.items():
        turns = [{k: v for k, v in t.items() if k != "full"} for t in turns]
        packet = build_context_pack({"matches": [], "conversation_turns": turns},
                                    max_chars=CONTEXT_CHARS)
        out[name] = {"found": len({i["id"] for i in packet["items"]} & wanted),
                     "tokens": packet["estimated_tokens"],
                     "episodes": len({i["provenance"]["source_ref"].rsplit(":", 1)[0]
                                      for i in packet["items"]})}
    _PIECES.pop(str(store), None)
    store.unlink()
    return out


def main():
    parser = ArgumentParser()
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "records.jsonl"
    done = {json.loads(l)["question_id"]: json.loads(l) for l in path.open(encoding="utf-8")} \
        if path.exists() else {}
    embedder = LocalEmbedder(CACHE / "embed-cache.json")
    records, seen = [], 0
    with tempfile.TemporaryDirectory() as directory, path.open("a", encoding="utf-8") as log:
        for row in stream_instances(DATA):
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
            print(f"\r  {seen} questions, {len(records)} with evidence", end="", flush=True)
            if seen % 50 == 0:
                embedder.cache.flush()
    embedder.cache.flush()
    tuned = set(json.loads(TUNED.read_text(encoding="utf-8"))["question_ids"])
    arms = [k for k in records[0] if isinstance(records[0][k], dict) and k != "ranks"]
    result = {arm: report([{"question_id": r["question_id"], "type": r["type"],
                            "wanted": r["wanted"], **r[arm]} for r in records], tuned)
              for arm in arms}
    (OUT / "report.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\n{'':<12}" + "".join(f"{g[:13]:>15}" for g in ("all", "untuned", "multi-session",
                                                           "knowledge-update", "temporal-reasoning")))
    for arm in arms:
        print(f"{arm:<12}" + "".join(
            f"{result[arm][g]['question_recall']:>7.1%}/{result[arm][g]['turn_coverage']:<7.1%}"
            for g in ("all", "untuned", "multi-session", "knowledge-update", "temporal-reasoning")
            if g in result[arm]))


if __name__ == "__main__":
    main()
