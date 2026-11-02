"""How well does the SHIPPED recall path find the evidence? Offline, free, deterministic.

Every number published so far (83.3% held-out, the rival comparisons, the scaling test) came from
the benchmark pipeline, longmemeval_retrieval_executor.py. The MCP server people actually run shares
none of that code: brain_context searches captured conversation turns with SQLite full-text search
and packs them into 6,000 characters. This measures that path on the same dev2 questions, so the two
can be compared on one metric.

For each question a fresh store is filled with its haystack exactly as the capture service would
fill it (every user and assistant turn, verbatim, dated by its session), the question is put through
the same functions brain_context runs, and we check whether the answer-bearing turns reach the
packet the model would read.

Reported, per configuration:
    turn coverage     share of answer-bearing turns that reach the packet
    question recall   share of questions with at least one answer-bearing turn in the packet
    skipped           questions the router declined to search at all (policy=auto)

    python product_recall_eval.py [--limit N]
"""
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
import json
import tempfile

from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.mcp_server import DEFAULT_CONTEXT_POLICY, gather
from longmemeval_m_fetch import stream_instances
from turnbook.context_packet import build_context_pack, decide_recall

DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
IDS = Path("runs/graph-v3.2-untouched-eval-v2/preregistration.json")       # dev2, as elsewhere
OUT = Path("runs/graph-v6.1-product-recall")
METHOD = "fused-semantic-v2"  # word search + meaning over user-turn pieces; 20/10000 default
CONFIGS = {                                   # name -> (policy, limit, packet characters)
    "previous packet (12 items, 6000 chars)": (DEFAULT_CONTEXT_POLICY, 12, 6000),
    "shipped defaults (20 items, 10000 chars)": (DEFAULT_CONTEXT_POLICY, 20, 10000),
}
# The benchmark already embedded every user-turn piece of these 120 haystacks with the same model
# and the same 500-character slicing, so the product path reuses those vectors. Read only: never
# flushed, so the benchmark's artefacts are not modified.
CACHE = Path("runs/graph-v3.8-dev2-k16/embed-cache-local.json")


def iso(longmemeval_date):
    return datetime.strptime(longmemeval_date, "%Y/%m/%d (%a) %H:%M").isoformat()


def fill(store_path, row):
    """The haystack as the capture service would store it; returns the answer-bearing turn ids."""
    wanted = set()
    with ConversationMemoryStore(store_path) as store:
        for date, sid, turns in zip(row["haystack_dates"], row["haystack_session_ids"],
                                    row["haystack_sessions"]):
            for index, turn in enumerate(turns):
                tid = f"{sid}::{index}"
                if not turn["content"].strip():        # the capture service skips empty turns too
                    continue
                store.record({"turn_id": tid, "client": "longmemeval", "client_session_id": sid,
                              "scope": "global", "role": turn["role"],
                              "content_redacted": turn["content"], "raw_sha256": tid,
                              "source_path": sid, "happened_at": iso(date),
                              "known_at": iso(date), "source_ordinal": index})
                if turn.get("has_answer"):
                    wanted.add(tid)
    return wanted


def main():
    parser = ArgumentParser()
    parser.add_argument("--limit", type=int, default=120)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    ids = json.loads(IDS.read_text(encoding="utf-8"))["question_ids"][:args.limit]
    wanted_ids, rows = set(ids), {}
    for row in stream_instances(DATA):
        if row["question_id"] in wanted_ids:
            rows[row["question_id"]] = row
            if len(rows) == len(wanted_ids):
                break

    from semantic_grounding_v073 import LocalEmbedder
    LocalEmbedder.FLUSH_EVERY = 10 ** 9
    embedder = LocalEmbedder(CACHE)
    # One line per finished question, so a bounded run resumes where the last one stopped. The file
    # is named for the method: the word-search-only baseline stays in per_question.jsonl.
    progress = OUT / f"per_question-{METHOD}.jsonl"
    done = {json.loads(line)["question_id"] for line in progress.open(encoding="utf-8")}         if progress.exists() else set()
    with tempfile.TemporaryDirectory() as directory, progress.open("a", encoding="utf-8") as log:
        for position, qid in enumerate(ids, 1):
            if qid in done:
                continue
            row = rows[qid]
            store = Path(directory) / f"{qid}.sqlite"
            wanted = fill(store, row)
            if not wanted:
                continue
            record = {"question_id": qid, "type": row["question_type"], "wanted": len(wanted)}
            for name, (policy, limit, chars) in CONFIGS.items():
                found, skipped = set(), decide_recall(row["question"], policy)["mode"] == "SKIP"
                if not skipped:
                    recalled = gather(store, row["question"], as_of=iso(row["question_date"]),
                                      limit=limit, embedder=embedder)
                    packet = build_context_pack(recalled, max_chars=chars)
                    found = {item["id"] for item in packet["items"]} & wanted
                record[name] = {"found": len(found), "skipped": skipped}
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            print(f"\r  {position}/{len(ids)}", end="", flush=True)
    print()

    records = [r for r in (json.loads(line) for line in progress.open(encoding="utf-8"))
               if r["question_id"] in set(ids)]
    report = {}
    for name in CONFIGS:
        turns = sum(r["wanted"] for r in records)
        report[name] = {
            "turn_coverage": round(sum(r[name]["found"] for r in records) / turns, 4),
            "question_recall": round(sum(r[name]["found"] > 0 for r in records) / len(records), 4),
            "skipped": sum(r[name]["skipped"] for r in records), "questions": len(records)}
    (OUT / f"report-{METHOD}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False),
                                               encoding="utf-8")
    for name, r in report.items():
        print(f"{name:<46} turn coverage {r['turn_coverage']:6.1%}   "
              f"question recall {r['question_recall']:6.1%}   skipped {r['skipped']}   "
              f"({r['questions']} questions)")

if __name__ == "__main__":
    main()
