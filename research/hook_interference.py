"""Does the memory the per-message hook adds get in the way of ordinary work?

Every message now gets a small packet (inject: 4 items, 1,200 characters), relevant or not. This
measures the cost on tasks that need no memory: 200 GSM8K test problems (public, a checkable
number each), each paired with one LongMemEval_S user's history as "their memory". The packet is
what the hook would add for that problem from that history: unrelated personal chat. The same
reader (gpt-5-mini) answers every problem twice, with and without it.

    gh workflow run cache-merge --ref exp/gemma -f runs="<full500 caches>" \
        -f research="hook_interference.py packets"          # CI: the packets (vectors cached)
    OPENAI_API_KEY=... python hook_interference.py answer    # locally: 2 x 200 answers, scored

Output: runs/graph-v9.23-hook-interference/{packets,answers}.jsonl, report.json.
"""
from pathlib import Path
from urllib.request import urlopen
import json
import math
import os
import re
import sys
import tempfile

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE.parent / "src"))
OUT = HERE / "runs/graph-v9.23-hook-interference"
GSM8K = ("https://raw.githubusercontent.com/openai/grade-school-math/"
         "3101c7d5072418e28b9008a6636bde82a006892c/grade_school_math/data/test.jsonl")
N = 200
READER = "gpt-5-mini"
PROMPT = ("Solve the math word problem. Think it through, then end with a last line of the form "
          "'Answer: <number>'.")


def problems():
    rows = [json.loads(line) for line in urlopen(GSM8K, timeout=60).read().decode().splitlines()]
    return [{"task_id": f"gsm8k-{i}", "question": r["question"],
             "answer": r["answer"].rsplit("####", 1)[-1].strip().replace(",", "")}
            for i, r in enumerate(rows[:N])]


def packets():
    from product_recall_eval import fill, iso
    from semantic_grounding_v073 import LocalEmbedder
    import product_answer_eval as pae

    from turnbook import inject
    from turnbook.context_packet import build_context_pack
    from turnbook.local_embedder import LOCAL_MODEL
    from turnbook.mcp_server import gather
    slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")     # full500_mpnet.py's cache
    cache = HERE.parent / "bench/.cache" / f"embed-{slug}.json"
    pae.IDS_FILE = Path("runs/graph-v7.0-full500-30/ids.json")
    histories = pae.questions()
    embedder = LocalEmbedder(cache)
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, \
            (OUT / "packets.jsonl").open("w", encoding="utf-8") as log:
        for task, history in zip(problems(), histories):
            store = Path(directory) / f"{history['question_id']}.sqlite"
            fill(store, history)
            recalled = gather(store, task["question"], as_of=iso(history["question_date"]),
                              limit=inject.LIMIT, embedder=embedder, share=False)
            packet = build_context_pack(recalled, max_chars=inject.CHARS, short_sources=True)
            text = (inject.HEADER + packet["context"]) if packet["status"] == "KNOWN" else ""
            log.write(json.dumps({**task, "history": history["question_id"], "packet": text},
                                 ensure_ascii=False) + "\n")
            store.unlink(missing_ok=True)
    print(f"{N} packets")


def number(text):
    found = re.findall(r"Answer:\s*\$?(-?[\d,]*\.?\d+)", text or "")
    return found[-1].replace(",", "").rstrip(".") if found else None


def answer():
    import longmemeval_retrieval_executor as pipe
    key = os.environ["OPENAI_API_KEY"]
    rows = [json.loads(line) for line in (OUT / "packets.jsonl").open(encoding="utf-8")]
    path = OUT / "answers.jsonl"
    previous = pipe.load_jsonl(path, "id")

    def send(job):
        row, arm = job["row"], job["arm"]
        # as Claude Code shows a hook's additionalContext: after the user's message
        user = row["question"] + (f"\n\n<system-reminder>\n{row['packet']}\n</system-reminder>"
                                  if arm == "memory" and row["packet"] else "")
        body = {"model": READER, "instructions": PROMPT, "max_output_tokens": 4096,
                "input": [{"role": "user", "content": user}]}
        return pipe.safe_call(key, body, {"id": job["id"]}, pipe.response_text)
    jobs = [{"id": f"{r['task_id']}:{arm}", "row": r, "arm": arm}
            for r in rows for arm in ("plain", "memory")]
    done = pipe.finished(previous)
    pipe.run_parallel([j for j in jobs if j["id"] not in done], path, 4, send)
    got = {r["id"]: r for r in pipe.load_jsonl(path, "id").values()}
    correct = {arm: {r["task_id"]: number((got.get(f"{r['task_id']}:{arm}") or {})
                                          .get("result")) == r["answer"] for r in rows}
               for arm in ("plain", "memory")}
    only_plain = sum(correct["plain"][t] and not correct["memory"][t] for t in correct["plain"])
    only_memory = sum(correct["memory"][t] and not correct["plain"][t] for t in correct["plain"])
    n = only_plain + only_memory
    p = min(1.0, 2 * sum(math.comb(n, k) for k in range(min(only_plain, only_memory) + 1))
            / 2 ** n) if n else 1.0
    chars = [len(r["packet"]) for r in rows]
    report = {"tasks": len(rows), "reader": READER,
              "plain_correct": sum(correct["plain"].values()),
              "memory_correct": sum(correct["memory"].values()),
              "only_plain": only_plain, "only_memory": only_memory, "mcnemar_p": round(p, 4),
              "packets_nonempty": sum(1 for c in chars if c),
              "packet_chars_median": sorted(chars)[len(chars) // 2]}
    (OUT / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    {"packets": packets, "answer": answer}[sys.argv[1]]()
