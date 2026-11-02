"""Does the answer reach the model? The shipped recall path on LongMemEval_S, offline and free.

For each question a fresh store is filled with its haystack exactly as the capture service would
fill it (every turn, verbatim, dated by its session, embedded before anyone asks: a server meeting
a backlog answers from word search while it embeds), the question goes through what brain_context
runs with its shipped defaults, and we check which answer-bearing turns reach the packet. No model
is called: this is the part of accuracy that Turnbook itself controls, measured on every change.
The answer accuracy behind it (88.8%) needs an answer model and a judge: research/REPORT.md §10.

Reported overall, per question type, and apart for the 120 questions the path was tuned on:
    question recall   share of questions with at least one answer-bearing turn in the packet
    turn coverage     share of all answer-bearing turns that reach the packet
    packet tokens     median estimated tokens of the packet the model reads

    python bench/evidence.py                 # all 500 (abstention questions have no evidence)
    python bench/evidence.py --limit 40      # a quick look
    python bench/evidence.py --baseline      # also write bench/baseline/evidence.json

The first full run embeds every haystack once (about an hour on a laptop CPU); the vectors are
kept in bench/.cache, so later runs only embed what changed. Progress is kept per commit, so an
interrupted run resumes.
"""
from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
import json
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from longmemeval_m_fetch import stream_instances  # noqa: E402
from longmemeval_retrieval_executor import asks_assistant_history  # noqa: E402

from turnbook.context_packet import build_context_pack  # noqa: E402
from turnbook.conversation_memory import ConversationMemoryStore  # noqa: E402
from turnbook.local_embedder import LOCAL_MODEL, LocalEmbedder  # noqa: E402
from turnbook.mcp_server import _PIECES, CONTEXT_CHARS, CONTEXT_LIMIT, gather  # noqa: E402
from turnbook.warmup import warm_turns  # noqa: E402

DATA = ROOT / "research/external/longmemeval/longmemeval_s_cleaned.json"
TUNED = ROOT / "research/runs/graph-v3.2-untouched-eval-v2/preregistration.json"   # dev2, 120
CACHE = ROOT / "bench/.cache"
BASELINE = ROOT / "bench/baseline/evidence.json"


def iso(longmemeval_date: str) -> str:
    return datetime.strptime(longmemeval_date, "%Y/%m/%d (%a) %H:%M").isoformat()


def fill(store_path: Path, row: dict) -> set[str]:
    """The haystack as the capture service stores it; returns the answer-bearing turn ids."""
    wanted = set()
    with ConversationMemoryStore(store_path) as store:
        for date, sid, turns in zip(row["haystack_dates"], row["haystack_session_ids"],
                                    row["haystack_sessions"]):
            for index, turn in enumerate(turns):
                if not turn["content"].strip():          # the capture service skips these too
                    continue
                tid = f"{sid}::{index}"
                store.record({"turn_id": tid, "client": "longmemeval", "client_session_id": sid,
                              "scope": "global", "role": turn["role"],
                              "content_redacted": turn["content"], "raw_sha256": tid,
                              "source_path": sid, "happened_at": iso(date),
                              "known_at": iso(date), "source_ordinal": index})
                if turn.get("has_answer"):
                    wanted.add(tid)
    return wanted


def measure(row: dict, directory: Path, embedder) -> dict | None:
    store = directory / f"{row['question_id']}.sqlite"
    wanted = fill(store, row)
    if not wanted:                                       # abstention: nothing to find
        return None
    while warm_turns(store, limit=2048, embedder=embedder):
        pass
    # about_assistant is the calling model's judgement in real use; the same English regex as
    # product_answer_eval.py plays it here (without it, assistant answers came only through
    # word search over every speaker)
    recalled = gather(store, row["question"], as_of=iso(row["question_date"]),
                      limit=CONTEXT_LIMIT, embedder=embedder,
                      about_assistant=asks_assistant_history(row["question"]))
    packet = build_context_pack(recalled, max_chars=CONTEXT_CHARS)
    found = {item["id"] for item in packet["items"]} & wanted
    # agentmemory's and MemPalace's LongMemEval number: recall_any@K, whether any answer session
    # is among the first K sessions retrieved (no answer, no judge). Sessions in the order their
    # first turn ranks.
    sessions = list(dict.fromkeys(t["turn_id"].split("::")[0]
                                  for t in recalled["conversation_turns"]))
    gold = set(row["answer_session_ids"])
    first = next((i for i, s in enumerate(sessions) if s in gold), None)
    _PIECES.pop(str(store), None)                        # one in-memory index per question
    store.unlink()
    return {"question_id": row["question_id"], "type": row["question_type"],
            "wanted": len(wanted), "found": len(found), "tokens": packet["estimated_tokens"],
            "first_session": first}


def summary(records: list[dict]) -> dict:
    if not records:
        return {}
    return {"questions": len(records),
            "question_recall": round(sum(r["found"] > 0 for r in records) / len(records), 4),
            "turn_coverage": round(sum(r["found"] for r in records)
                                   / sum(r["wanted"] for r in records), 4),
            "packet_tokens_median": int(statistics.median(r["tokens"] for r in records)),
            **{f"recall_any@{k}": round(sum(r.get("first_session") is not None
                                            and r["first_session"] < k for r in records)
                                        / len(records), 4) for k in (5, 10)}}


def report(records: list[dict], tuned: set[str]) -> dict:
    out = {"all": summary(records),
           "untuned": summary([r for r in records if r["question_id"] not in tuned]),
           "tuned": summary([r for r in records if r["question_id"] in tuned])}
    for kind in sorted({r["type"] for r in records}):
        out[kind] = summary([r for r in records if r["type"] == kind])
    return out


def show(result: dict, baseline: dict | None) -> None:
    print(f"\n{'':<28}{'questions':>10}{'recall':>9}{'turns':>9}{'tokens':>8}{'R@5':>8}{'R@10':>8}"
          "   vs baseline")
    for name, row in result.items():
        if not row:
            continue
        before = (baseline or {}).get(name)
        delta = (f"{(row['question_recall'] - before['question_recall']) * 100:+.1f}pt recall"
                 if before else "")
        print(f"{name:<28}{row['questions']:>10}{row['question_recall']:>9.1%}"
              f"{row['turn_coverage']:>9.1%}{row['packet_tokens_median']:>8}"
              f"{row.get('recall_any@5', 0):>8.1%}{row.get('recall_any@10', 0):>8.1%}   {delta}")


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, help="first N questions only")
    parser.add_argument("--baseline", action="store_true", help="write the result as baseline")
    parser.add_argument("--every", type=int, default=1, help="every Nth question only (a sample)")
    parser.add_argument("--model", help="another embedding model (repo or repo@onnx/file.onnx)")
    args = parser.parse_args()
    if not DATA.exists():
        sys.exit(f"LongMemEval_S not found at {DATA}: see research/REPORT.md §10")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True, cwd=ROOT).stdout.strip() or "unknown"
    dirty = subprocess.run(["git", "status", "--porcelain", "src"], capture_output=True,
                           text=True, cwd=ROOT).stdout.strip()
    label = commit + ("-dirty" if dirty else "")
    slug = args.model and args.model.replace("/", "_").replace("@", "_")
    if slug:                                         # its own vectors and progress, never mixed
        label += "-" + slug
    CACHE.mkdir(parents=True, exist_ok=True)
    progress = CACHE / f"evidence-{label}.jsonl"
    if dirty:
        progress.unlink(missing_ok=True)                 # uncommitted code: never resume
    done = {}
    if progress.exists():
        for line in progress.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            done[record["question_id"]] = record
    tuned = set(json.loads(TUNED.read_text(encoding="utf-8"))["question_ids"])
    model = args.model or LOCAL_MODEL                # each model its own vectors
    embedder = LocalEmbedder(CACHE / f"embed-{model.replace('/', '_').replace('@', '_')}.json",
                             model=model)
    records, seen = [], 0
    with tempfile.TemporaryDirectory() as directory, progress.open("a", encoding="utf-8") as log:
        for row in stream_instances(DATA):
            if args.limit and seen >= args.limit:
                break
            seen += 1
            if (seen - 1) % args.every:
                continue
            qid = row["question_id"]
            record = done.get(qid) or measure(row, Path(directory), embedder)
            if record is None:
                continue
            if qid not in done:
                log.write(json.dumps(record) + "\n")
                log.flush()
            records.append(record)
            print(f"\r  {seen} questions, {len(records)} with evidence", end="", flush=True)
    embedder.cache.flush()
    result = {"commit": label, "measured_at": datetime.now().isoformat(timespec="seconds"),
              "context_limit": CONTEXT_LIMIT, "context_chars": CONTEXT_CHARS,
              "questions_seen": seen, "report": report(records, tuned)}
    baseline = (json.loads(BASELINE.read_text(encoding="utf-8"))["report"]
                if BASELINE.exists() else None)
    show(result["report"], baseline)
    (CACHE / f"evidence-{label}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    if args.baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        print(f"\nbaseline written: {BASELINE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
