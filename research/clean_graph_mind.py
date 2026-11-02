"""Graph-MIND's side of the pre-registered clean comparison (runs/graph-v6.0-rival-clean).

Same pipeline as every untouched evaluation \u2014 planner, retrieval, extraction, answer, official judge
\u2014 pointed at the 40 frozen questions in their frozen order. It refuses to run if any file whose hash
the pre-registration froze has changed since, because then the result would not be the result of the
system that was registered.

    OPENAI_API_KEY=... python clean_turnbook.py
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import longmemeval_retrieval_executor as pipe
import official_judge_v073 as judge
import untouched_eval_v1 as v1

RUN = Path("runs/graph-v6.0-rival-clean")
OUT = RUN / "graph-mind"
CACHE = Path.home() / "gm-rival-stores" / "instances-clean.json"   # written by the rival harness


def main():
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("OPENAI_API_KEY_REQUIRED")
    manifest = json.loads((RUN / "preregistration.json").read_text(encoding="utf-8"))
    drift = [f for f, h in manifest["code_sha256"].items()
             if hashlib.sha256(Path(f).read_bytes()).hexdigest() != h]
    if drift:
        sys.exit(f"REFUSED: frozen files changed since pre-registration: {drift}")

    # Persist vectors every 500 instead of 5000. On a loaded machine embedding ran at ~10 chunks/s,
    # so 5000 took longer than one bounded run and nothing was ever saved between runs. This
    # changes only how often the cache is written, never a vector, and leaves the frozen file be.
    from semantic_grounding_v073 import LocalEmbedder
    LocalEmbedder.FLUSH_EVERY = 500

    ids = manifest["question_ids"]
    cached = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = [cached[q] for q in ids]           # the frozen order, parsed once, not 277 MB per call

    OUT.mkdir(parents=True, exist_ok=True)
    v1.OUT = OUT
    v1.load_instances = lambda _path: rows
    plans = v1.make_plans(key, ids)
    pipe.OUTPUT, pipe.OPERATOR_RESPONSES = OUT, plans
    pipe.select_questions = lambda: rows
    questions, sessions = pipe.prepare()
    pipe.run_ingestion(key, 8, sessions)
    try:
        pipe.run_answers(key, 4, questions, sessions)
    finally:
        pipe._EMBEDDER and pipe._EMBEDDER.flush()
    judge.SRC, judge.OUT = OUT, OUT / "official-judge.jsonl"
    print(json.dumps(judge.run()["by_type"], indent=1))


if __name__ == "__main__":
    main()
