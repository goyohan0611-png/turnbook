"""End-to-end accuracy of the SHIPPED recall path: its packet, the same reader, the official judge.

product_recall_eval.py showed the MCP path now delivers the evidence (93.9% of answer-bearing turns
on dev2). Delivery is not answering. This hands each question's brain_context packet to the reader
used for every other number in REPORT.md (same answer prompt, gpt-5-mini) and grades it with the
official LongMemEval judge, then compares question by question with the benchmark pipeline on the
same 120 questions (runs/graph-v3.9-dev2-mv, 85.8%).

    python product_answer_eval.py spans      # offline, resumable: build every packet
    OPENAI_API_KEY=... python product_answer_eval.py answer   # reader + official judge
"""
from argparse import ArgumentParser
from pathlib import Path
import json
import math
import os
import tempfile

from turnbook.mcp_server import DEFAULT_CONTEXT_POLICY, PIECE, gather
from longmemeval_m_fetch import stream_instances
from product_recall_eval import CACHE, DATA, IDS, fill, iso
from longmemeval_retrieval_executor import asks_assistant_history
from turnbook.context_packet import build_context_pack

OUT = Path("runs/graph-v6.2-product-answers")
TYPE = None                                     # --type: only one question type (a re-run)
AROUND = False                                  # --around: resolve "two weeks ago" into a window
WRITABLE = False                                # --writable: keep new vectors (full-set runs)
BASELINE = Path("runs/graph-v3.9-dev2-mv/official-judge.jsonl")
LIMIT, CHARS = 30, 15000                        # brain_context's shipped defaults (v7.0)


IDS_FILE, CACHE_FILE = IDS, CACHE                # --ids / --cache: the clean pool and its vectors


def questions():
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))["question_ids"]
    rows, wanted = {}, set(ids)
    for row in stream_instances(DATA):
        if row["question_id"] in wanted:
            rows[row["question_id"]] = row
            if len(rows) == len(wanted):
                break
    return [rows[q] for q in ids if not TYPE or rows[q]["question_type"] == TYPE]


def spans():
    """Each question's packet, exactly as brain_context would assemble it, one line per question."""
    from semantic_grounding_v073 import LocalEmbedder
    # A benchmark's cache stays read-only; a run's own cache (--writable) saves every 500 vectors
    # so a bounded run that is stopped keeps what it embedded.
    LocalEmbedder.FLUSH_EVERY = 500 if WRITABLE else 10 ** 9
    embedder = LocalEmbedder(CACHE_FILE)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "spans.jsonl"
    done = {json.loads(l)["question_id"] for l in path.open(encoding="utf-8")} if path.exists() \
        else set()
    rows = questions()
    with tempfile.TemporaryDirectory() as directory, path.open("a", encoding="utf-8") as log:
        for position, row in enumerate(rows, 1):
            if row["question_id"] in done:
                continue
            store = Path(directory) / f"{row['question_id']}.sqlite"
            fill(store, row)
            # Steady state, as in real use where the capture service and the background indexer
            # embed every turn as it arrives. Without this a haystack new to the cache has more
            # than INLINE_EMBED_LIMIT unembedded pieces, the server answers from word search alone
            # while it backfills, and the packet measures a cold start instead: that happened to
            # 20 of 89 untuned multi-session packets in the first full-500 run.
            # exactly the pieces semantic_turns searches: user turns, and assistant turns only when
            # the question is about what the assistant said
            roles = {"user", "assistant"} if asks_assistant_history(row["question"]) else {"user"}
            embedder.embed([turn["content"][start:start + PIECE]
                            for session in row["haystack_sessions"] for turn in session
                            if turn["role"] in roles
                            for start in range(0, len(turn["content"]), PIECE)
                            if turn["content"][start:start + PIECE].strip()])
            assert DEFAULT_CONTEXT_POLICY == "always"
            # about_assistant is the calling model's judgement in real use; the benchmark's own
            # stand-in for that judgement (an English regex) plays the model here.
            recalled = gather(store, row["question"], as_of=iso(row["question_date"]),
                              limit=LIMIT, embedder=embedder,
                              about_assistant=asks_assistant_history(row["question"]),
                              around=around(row) if AROUND else None)
            packet = build_context_pack(recalled, max_chars=CHARS)
            passages = [{"session_id": item["provenance"]["source_ref"].rsplit(":", 1)[0],
                         "session_date": item["effective_at"],
                         "speaker": item["title"].split(" ", 1)[0],
                         "text": item["content"]} for item in packet["items"]]
            log.write(json.dumps({"question_id": row["question_id"], "passages": passages,
                                  "estimated_tokens": packet["estimated_tokens"]},
                                 ensure_ascii=False) + "\n")
            log.flush()
            print(f"\r  {position}/{len(rows)}", end="", flush=True)
    if WRITABLE:
        embedder.flush()
    print()


def around(row):
    """The calling model resolves "four weeks ago" itself in real use; the benchmark's own
    resolver (English, rule-based) plays it here."""
    import temporal_resolver
    found = temporal_resolver.resolve(row["question"], row["question_date"])
    return f"{found[0]}..{found[1]}" if found else None


def answer():
    import longmemeval_retrieval_executor as pipe
    import official_judge_v073 as judge
    key = os.environ["OPENAI_API_KEY"]
    packets = {json.loads(l)["question_id"]: json.loads(l)
               for l in (OUT / "spans.jsonl").open(encoding="utf-8")}
    rows = [r for r in questions() if r["question_id"] in packets]
    public = [{k: r[k] for k in ("question_id", "question_type", "question", "question_date",
                                 "answer", "answer_session_ids")} for r in rows]
    (OUT / "questions.json").write_text(json.dumps(public, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    path = OUT / "answers.jsonl"
    previous = pipe.load_jsonl(path, "question_id")

    def send(question):
        passages = packets[question["question_id"]]["passages"]
        body = pipe.answer_body(question, {}, [], None, None, passages)   # same prompt, reader
        return pipe.safe_call(key, body, {"question_id": question["question_id"],
                                          "passages_supplied": len(passages)},
                              lambda raw: json.loads(pipe.response_text(raw)))
    done = pipe.finished(previous)                    # failed rows are retried
    pipe.run_parallel([q for q in public if q["question_id"] not in done], path, 4, send)
    judge.SRC, judge.OUT = OUT, OUT / "official-judge.jsonl"
    report = judge.run()

    mine = {r["question_id"]: r["label"] for r in map(json.loads, (OUT / "official-judge.jsonl")
            .open(encoding="utf-8")) if "label" in r}     # a failed verdict is asked again next run
    base = {json.loads(l)["question_id"]: json.loads(l)["label"]
            for l in BASELINE.open(encoding="utf-8")}
    if not set(mine) & set(base):                  # the clean pool has no benchmark-path run
        print(json.dumps({"product_path": f"{sum(mine.values())}/{len(mine)}",
                          "by_type": report["by_type"]}, indent=1))
        return
    both = [q for q in mine if q in base]
    only_product = sum(1 for q in both if mine[q] and not base[q])
    only_bench = sum(1 for q in both if base[q] and not mine[q])
    n = only_product + only_bench
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(min(only_product, only_bench) + 1)) / 2 ** n) \
        if n else 1.0
    tokens = sorted(packets[q]["estimated_tokens"] for q in packets)
    summary = {"product_path": f"{sum(mine[q] for q in both)}/{len(both)}",
               "benchmark_path": f"{sum(base[q] for q in both)}/{len(both)}",
               "only_product_right": only_product, "only_benchmark_right": only_bench,
               "exact_mcnemar_p": round(p, 3),
               "packet_tokens_median": tokens[len(tokens) // 2],
               "by_type": report["by_type"]}
    (OUT / "comparison.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False),
                                         encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "by_type"}, indent=1))


OPEN_RULE = """
Each passage is one excerpt of one conversation turn. Never answer UNKNOWN without first calling
open_conversation on the passage most likely to hold the answer: it returns that turn in full
with the turns around it (the assistant's reply, the rest of a list, the line before). Also call
it when a passage you rely on is cut off. Up to 3 calls."""
OPEN_TOOL = {"type": "function", "name": "open_conversation",
             "description": "The turns around one evidence passage, in full and in order: two "
                            "before it, the passage's own turn, and three after.",
             "parameters": {"type": "object", "additionalProperties": False,
                            "required": ["passage"],
                            "properties": {"passage": {"type": "integer",
                                                       "description": "passage_number"}}},
             "strict": True}


def answer_open():
    """The reader may open the conversation around a passage, as a model in real use opens it
    with brain_recall(source=...). Same packets, prompt and judge as `answer`."""
    import longmemeval_retrieval_executor as pipe
    import official_judge_v073 as judge
    key = os.environ["OPENAI_API_KEY"]
    packets = {json.loads(l)["question_id"]: json.loads(l)
               for l in (OUT / "spans.jsonl").open(encoding="utf-8")}
    rows = {r["question_id"]: r for r in questions() if r["question_id"] in packets}
    public = [{k: r[k] for k in ("question_id", "question_type", "question", "question_date",
                                 "answer", "answer_session_ids")} for r in rows.values()]
    (OUT / "questions.json").write_text(json.dumps(public, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    path = OUT / "answers.jsonl"
    previous = pipe.load_jsonl(path, "question_id")

    def located(row, passages):
        """(session turns, index) of each passage: the excerpt is a slice of one turn."""
        sessions = dict(zip(row["haystack_session_ids"], row["haystack_sessions"]))
        found = []
        for p in passages:
            turns = sessions.get(p["session_id"], [])
            probe = p["text"].strip()[:200]
            index = next((i for i, t in enumerate(turns) if probe and probe in t["content"]), None)
            found.append((turns, index))
        return found

    def send(question):
        row, passages = rows[question["question_id"]], packets[question["question_id"]]["passages"]
        where = located(row, passages)
        numbered = [{**p, "passage_number": i} for i, p in enumerate(passages)]
        body = pipe.answer_body(question, {}, [], None, None, numbered)
        body["input"][0]["content"] += OPEN_RULE
        body["tools"] = [OPEN_TOOL]
        body["include"] = ["reasoning.encrypted_content"]   # replayed with store=false
        usage, opened = {"input_tokens": 0, "output_tokens": 0}, []
        for round_ in range(4):
            if round_ == 3:
                body["tool_choice"] = "none"
            record = pipe.safe_call(key, body, {"question_id": question["question_id"]},
                                    lambda raw: raw)
            if record["status"] != "ok":
                return {**record, "opened": opened}
            raw = record["result"]
            for k in usage:
                usage[k] += (raw.get("usage") or {}).get(k, 0)
            calls = [item for item in raw.get("output", []) if item.get("type") == "function_call"]
            if not calls:
                try:
                    result = json.loads(pipe.response_text(raw))
                except ValueError as error:
                    return {**record, "status": "parse_failure", "error": str(error),
                            "result": None, "opened": opened, "usage": usage}
                return {**record, "result": result, "usage": usage, "opened": opened,
                        "passages_supplied": len(passages)}
            body["input"] = body["input"] + raw["output"]
            for call in calls:
                try:
                    n = int(json.loads(call["arguments"])["passage"])
                    turns, index = where[n]
                except (ValueError, KeyError, IndexError, TypeError):
                    turns, index = [], None
                opened.append(n if index is not None else None)
                shown = [] if index is None else [
                    {"role": t["role"], "here": i == index, "content": t["content"][:4000]}
                    for i, t in enumerate(turns) if index - 2 <= i <= index + 3]
                body["input"].append({"type": "function_call_output", "call_id": call["call_id"],
                                      "output": json.dumps({"turns": shown}, ensure_ascii=False)})
        return {**record, "status": "parse_failure", "error": "NO_FINAL_ANSWER", "result": None}

    done = pipe.finished(previous)
    pipe.run_parallel([q for q in public if q["question_id"] not in done], path, 4, send)
    judge.SRC, judge.OUT = OUT, OUT / "official-judge.jsonl"
    judge.run()


def main():
    global OUT, TYPE, IDS_FILE, CACHE_FILE, AROUND, WRITABLE, LIMIT, CHARS
    parser = ArgumentParser()
    parser.add_argument("stage", choices=["spans", "answer", "answer-open"])
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--type")
    parser.add_argument("--around", action="store_true")
    parser.add_argument("--writable", action="store_true", help="the cache is this run's own")
    parser.add_argument("--ids", default=str(IDS), help="a preregistration.json; its order is kept")
    parser.add_argument("--cache", default=str(CACHE), help="vector cache, read only")
    parser.add_argument("--limit", type=int, default=LIMIT, help="packet items (a trial size)")
    parser.add_argument("--chars", type=int, default=CHARS, help="packet characters")
    args = parser.parse_args()
    LIMIT, CHARS = args.limit, args.chars
    OUT, TYPE, IDS_FILE, CACHE_FILE = Path(args.out), args.type, Path(args.ids), Path(args.cache)
    AROUND, WRITABLE = args.around, args.writable
    {"spans": spans, "answer": answer, "answer-open": answer_open}[args.stage]()


if __name__ == "__main__":
    main()
