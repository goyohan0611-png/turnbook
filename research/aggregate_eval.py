"""Counting and totals: does a wider packet for "how many / how much / total" questions help?

On clean vectors 37 of the 327 multi-session answer turns miss the packet, and 32 of them rank
30-100: the packet keeps 30 turns (15,000 characters), and a question that adds things up across
many conversations needs more of them than one that asks for one fact. Here the shipped path
(gather + build_context_pack) answers every question whose wording asks for a count or a total
twice: as shipped (30 turns, 15,000 chars) and wide (50 turns, 25,000 chars). Questions without
such a cue are not touched by the change and are not run.

    python aggregate_eval.py               # CI: packets for both arms (bench's cached vectors)
    OPENAI_API_KEY=... python aggregate_eval.py --answer   # reader + official judge, wide vs base
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import json
import re
import tempfile

import episode_eval  # noqa: F401  (first: isolates TURNBOOK_HOME, sets up the paths)

from evidence import CACHE, DATA, fill, iso  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402

from turnbook.context_packet import build_context_pack  # noqa: E402
from turnbook.local_embedder import LocalEmbedder  # noqa: E402
from turnbook.mcp_server import _PIECES, CONTEXT_CHARS, CONTEXT_LIMIT, gather  # noqa: E402
from turnbook.warmup import warm_turns  # noqa: E402

OUT = Path(__file__).resolve().parent / "runs/graph-v9.10-aggregate"
AGGREGATE = re.compile(r"\b(how many|how much|total|in all|altogether|combined|sum of)\b", re.I)
ARMS = {"base": (CONTEXT_LIMIT, CONTEXT_CHARS), "wide": (50, 25000)}
TYPE = None        # --type: every question of one type instead of the wording cue


def build():
    embedder = LocalEmbedder(CACHE / "embed-cache.json")
    for arm in ARMS:
        (OUT / arm).mkdir(parents=True, exist_ok=True)
    logs = {arm: (OUT / arm / "spans.jsonl").open("w", encoding="utf-8") for arm in ARMS}
    found = []
    with tempfile.TemporaryDirectory() as directory:
        for row in stream_instances(DATA):
            if (row["question_type"] != TYPE) if TYPE else not AGGREGATE.search(row["question"]):
                continue
            store = Path(directory) / f"{row['question_id']}.sqlite"
            wanted = fill(store, row)
            while warm_turns(store, limit=2048, embedder=embedder):
                pass
            record = {"question_id": row["question_id"], "type": row["question_type"],
                      "wanted": len(wanted)}
            for arm, (limit, chars) in ARMS.items():
                recalled = gather(store, row["question"], as_of=iso(row["question_date"]),
                                  limit=limit, embedder=embedder)
                pack = build_context_pack(recalled, max_chars=chars)
                record[arm] = {"found": len({i["id"] for i in pack["items"]} & wanted),
                               "tokens": pack["estimated_tokens"]}
                logs[arm].write(json.dumps({
                    "question_id": row["question_id"],
                    "estimated_tokens": pack["estimated_tokens"],
                    "passages": [{"id": i["id"],
                                  "session_id": i["provenance"]["source_ref"].rsplit(":", 1)[0],
                                  "session_date": i["effective_at"],
                                  "speaker": i["title"].split(" ", 1)[0],
                                  "text": i["content"]} for i in pack["items"]]},
                    ensure_ascii=False) + "\n")
            found.append(record)
            _PIECES.pop(str(store), None)
            store.unlink()
            print(f"  {len(found)} {record}", flush=True)
    for log in logs.values():
        log.close()
    embedder.cache.flush()
    with (OUT / "records.jsonl").open("w", encoding="utf-8") as log:
        for record in found:
            log.write(json.dumps(record) + "\n")
    for arm in ARMS:
        with_evidence = [r for r in found if r["wanted"]]
        tokens = sorted(r[arm]["tokens"] for r in found)
        print(arm, {"turn_coverage": round(sum(r[arm]["found"] for r in with_evidence)
                                           / sum(r["wanted"] for r in with_evidence), 4),
                    "tokens_median": tokens[len(tokens) // 2], "questions": len(found)})


def answer():
    import product_answer_eval as pae
    with (OUT / "base" / "spans.jsonl").open(encoding="utf-8") as kept:
        ids = [json.loads(line)["question_id"] for line in kept]
    (OUT / "ids.json").write_text(json.dumps({"question_ids": ids}, indent=1), encoding="utf-8")
    pae.IDS_FILE = OUT / "ids.json"
    for arm in ARMS:
        pae.OUT = OUT / arm
        if arm != "base":
            pae.BASELINE = OUT / "base" / "official-judge.jsonl"
        pae.answer()


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--answer", action="store_true")
    parser.add_argument("--type", help="all questions of this type (folder of its own)")
    args = parser.parse_args()
    if args.type:
        TYPE, OUT = args.type, OUT.with_name(f"graph-v9.11-wide-{args.type}")
    answer() if args.answer else build()
