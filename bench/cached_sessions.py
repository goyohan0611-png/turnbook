"""Session hit@K from a frozen answer packet, offline, without rerunning retrieval or an LLM.

    python bench/cached_sessions.py research/runs/graph-v9.14-full500-mpnet8-gemma \
        --out bench/baseline/cached-gemma-session-recall.json

This measures sessions PRESENT IN THE PACKET in first-occurrence order. It cannot recover
retriever candidates discarded during packing, nor measure answer-turn coverage from excerpts.
Abstention questions are excluded explicitly; missing packets count as misses, not omissions.
"""
from argparse import ArgumentParser
from pathlib import Path
import hashlib
import json
import statistics


def keyed(rows):
    out = {}
    for row in rows:
        qid = row["question_id"]
        if qid in out:
            raise ValueError(f"Duplicate question_id: {qid}")
        out[qid] = row
    return out


def load_packets(path):
    # str.splitlines also splits U+2028/U+0085 inside valid JSON strings.
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def measure(questions, packets):
    questions, packets = keyed(questions), keyed(packets)
    unknown = packets.keys() - questions.keys()
    if unknown:
        raise ValueError(f"Packets without a question: {sorted(unknown)}")
    records, excluded = [], 0
    for qid, question in questions.items():
        if qid.endswith("_abs") or question["question_type"] == "abstention":
            excluded += 1
            continue
        gold = set(question["answer_session_ids"])
        if not gold:
            raise ValueError(f"Non-abstention question has no gold session: {qid}")
        packet = packets.get(qid, {})
        sessions = list(dict.fromkeys(p["session_id"] for p in packet.get("passages", [])))
        first = next((i for i, sid in enumerate(sessions) if sid in gold), None)
        records.append({"type": question["question_type"], "first": first,
                        "tokens": packet.get("estimated_tokens")})

    def summary(rows):
        tokens = [r["tokens"] for r in rows if r["tokens"] is not None]
        n = len(rows)
        hits = {str(k): sum(r["first"] is not None and r["first"] < k for r in rows)
                for k in (5, 10)}
        return {"questions": n, "session_hits": hits,
                "packet_session_hit@5": hits["5"] / n if n else None,
                "packet_session_hit@10": hits["10"] / n if n else None,
                "packet_tokens_median": statistics.median(tokens) if tokens else None}

    return {"metric": "packet_session_hit@K", "ranking": "first session occurrence in packet",
            "questions_total": len(questions), "abstention_excluded": excluded,
            "missing_packets": sorted(questions.keys() - packets.keys()),
            "all": summary(records),
            "by_type": {kind: summary([r for r in records if r["type"] == kind])
                        for kind in sorted({r["type"] for r in records})},
            "limits": ["Frozen packet replay, not a run of the current checkout",
                       "No answer accuracy or answer-turn coverage measured",
                       "Packing may have discarded higher-ranked retriever candidates",
                       "Existing reranker used LongMemEval training data; not a blind result"]}


def main():
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    questions = args.run / "questions.json"
    packets = args.run / "spans.jsonl"
    result = measure(json.loads(questions.read_text(encoding="utf-8")),
                     load_packets(packets))
    result["run"] = args.run.name
    result["sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (questions, packets)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("all", "abstention_excluded", "missing_packets")}))


if __name__ == "__main__":
    main()
