"""agentmemory's own LongMemEval retrieval, answered by OUR reader and graded by the official judge.

agentmemory (rohitg00/agentmemory) publishes 95.2% on LongMemEval_S, and says plainly that it is
retrieval recall (recall_any@5: is an answer session among the five it returns?), not answer
accuracy. It also publishes, per question, the sessions its hybrid search returned. This hands the
first K of those sessions, every turn of each in full, to the reader used for every other number
here (gpt-5-mini, same answer prompt) and grades the answer with the official judge, so the two
systems are compared on the official metric with only the retrieval differing.

Fairness, which matters more than the number:
  * Their results file, not a re-run: pinned by commit and sha256 below. Nothing is tuned.
  * Whole sessions are their best case (their unit of retrieval). Five of them are about 4x the
    text Graph-MIND hands the reader (3.4k tokens a question): the reader sees more, not less.
  * Graph-MIND was developed against 120 of these questions; the report separates them.

    OPENAI_API_KEY=... python rival_agentmemory.py            # all 500, K=5 (about US$3)
    OPENAI_API_KEY=... python rival_agentmemory.py --k 10 --limit 40
"""
from argparse import ArgumentParser
from pathlib import Path
import hashlib
import json
import os
import urllib.request

from longmemeval_m_fetch import stream_instances

COMMIT = "565c2388a2ac3ef86b9d5c355c2b630db8e4357d"      # last change to the file, 2026-04-08
SHA256 = "94f9b00a54205adeeaf76b26b655993a98a8012111b4ac903791f0fbb44d2596"
URL = (f"https://raw.githubusercontent.com/rohitg00/agentmemory/{COMMIT}"
       "/benchmark/data/longmemeval_results_hybrid.json")
RESULTS = Path("external/agentmemory/longmemeval_results_hybrid.json")
DATA = Path("external/longmemeval/longmemeval_s_cleaned.json")
OURS = Path("runs/graph-v7.0-full500-30/official-judge.jsonl")       # the 88.8% run
TUNED = Path("runs/graph-v3.2-untouched-eval-v2/preregistration.json")  # dev2: tuned on


def retrieved():
    if not RESULTS.exists():
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(URL, RESULTS)
    if hashlib.sha256(RESULTS.read_bytes()).hexdigest() != SHA256:
        raise SystemExit(f"{RESULTS} is not the pinned file")
    return {r["question_id"]: r["retrieved_session_ids"]
            for r in json.loads(RESULTS.read_text(encoding="utf-8"))["per_question"]}


def passages(row, session_ids):
    sessions = dict(zip(row["haystack_session_ids"], zip(row["haystack_dates"],
                                                         row["haystack_sessions"])))
    out = []
    for sid in session_ids:
        date, turns = sessions[sid]
        out += [{"session_id": sid, "session_date": date, "speaker": t["role"],
                 "text": t["content"]} for t in turns if t["content"].strip()]
    return out


def mcnemar(a: dict, b: dict) -> dict:
    """Exact two-sided McNemar on the questions both answered."""
    from math import comb
    both = set(a) & set(b)
    only_a = sum(a[q] and not b[q] for q in both)
    only_b = sum(b[q] and not a[q] for q in both)
    n, k = only_a + only_b, min(only_a, only_b)
    p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0
    return {"questions": len(both), "only_turnbook": only_a, "only_agentmemory": only_b,
            "p": float(f"{p:.2e}")}


def main():
    import longmemeval_retrieval_executor as pipe
    import official_judge_v073 as judge
    parser = ArgumentParser()
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    key = os.environ["OPENAI_API_KEY"]
    out = Path(f"runs/graph-v9.6-rival-agentmemory-k{args.k}")
    out.mkdir(parents=True, exist_ok=True)
    found = retrieved()
    rows = [r for r in stream_instances(DATA) if r["question_id"] in found][:args.limit]
    packets = {r["question_id"]: passages(r, found[r["question_id"]][:args.k]) for r in rows}
    public = [{k: r[k] for k in ("question_id", "question_type", "question", "question_date",
                                 "answer", "answer_session_ids")} for r in rows]
    (out / "questions.json").write_text(json.dumps(public, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    chars = sorted(sum(len(p["text"]) for p in v) for v in packets.values())
    (out / "manifest.json").write_text(json.dumps({
        "source": URL, "sha256": SHA256, "k": args.k, "reader": pipe.READER_MODEL,
        "judge": judge.JUDGE_MODEL,
        "packet_chars_median": chars[len(chars) // 2]}, indent=1), encoding="utf-8")
    path = out / "answers.jsonl"
    done = pipe.finished(pipe.load_jsonl(path, "question_id"))

    def send(question):
        supplied = packets[question["question_id"]]
        body = pipe.answer_body(question, {}, [], None, None, supplied)   # same prompt, reader
        return pipe.safe_call(key, body, {"question_id": question["question_id"],
                                          "passages_supplied": len(supplied)},
                              lambda raw: json.loads(pipe.response_text(raw)))
    pipe.run_parallel([q for q in public if q["question_id"] not in done], path, 4, send)
    judge.SRC, judge.OUT = out, out / "official-judge.jsonl"
    report = judge.run()

    def labels(p):
        return {r["question_id"]: r["label"] for r in map(json.loads, p.open(encoding="utf-8"))
                if "label" in r}
    theirs, ours = labels(out / "official-judge.jsonl"), labels(OURS)
    tuned = set(json.loads(TUNED.read_text(encoding="utf-8"))["question_ids"])
    both = set(theirs) & set(ours)
    untuned = both - tuned
    result = {
        "agentmemory": f"{sum(theirs[q] for q in both)}/{len(both)}",
        "turnbook": f"{sum(ours[q] for q in both)}/{len(both)}",
        "untuned": {"agentmemory": f"{sum(theirs[q] for q in untuned)}/{len(untuned)}",
                    "turnbook": f"{sum(ours[q] for q in untuned)}/{len(untuned)}"},
        "mcnemar": mcnemar({q: ours[q] for q in both}, {q: theirs[q] for q in both}),
        "packet_chars_median": chars[len(chars) // 2], "by_type": report["by_type"]}
    (out / "comparison.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
