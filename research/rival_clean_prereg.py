"""Freeze the question list for the rival comparison BEFORE any of it is run.

The MemPalace and Mem0 comparisons both use the v3.2 question set, which 18 scripts in this repo read
during development (dev2_eval_v35 through v48, the ablations, the reader swap). Graph-MIND was tuned
against those questions and the rivals saw them once; the report says so, and it is the first thing a
skeptic should attack. This fixes it on the one set that is genuinely clean:

    v3.2  120 questions  read by 18 development scripts   <- what the current comparison uses
    v3.0  120 questions  read by its own eval only        <- what this pre-registers
    v3.5  100 questions  its own eval + reranker_ablation
    v4.2   72 questions  its own eval + date_fill_probe

The four sets are disjoint (412 of the 500 in LongMemEval_S). v3.0 was measured once, in the first
honest evaluation (65.8%), against a pipeline five versions old, and nothing has been tuned on it
since.

Forty questions are frozen IN ORDER, stratified by question type. Mem0 needs ~18 minutes per question
to build its store, so a 40-question run is ~12 hours and may not finish; because the ORDER is frozen
here, stopping early reports a prefix of a pre-registered list rather than a choice made afterwards.
The order is the sha256 of the question id, so it cannot be steered.

    python rival_clean_prereg.py          # write the pre-registration (refuses to overwrite)
    python rival_clean_prereg.py --show   # print what is already frozen
    python rival_clean_prereg.py --amend "reason"   # only before the run, and it keeps the old one
"""
from argparse import ArgumentParser
from datetime import datetime, timezone
import hashlib
import json
from collections import Counter
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from longmemeval_adapter import load_instances

OUT = Path("runs/graph-v6.0-rival-clean")
CLEAN_POOL = Path("runs/graph-v3.0-untouched-eval-v1/preregistration.json")
TUNED_POOL = Path("runs/graph-v3.2-untouched-eval-v2/preregistration.json")
FROZEN_FILES = ["longmemeval_retrieval_executor.py", "semantic_grounding_v073.py",
                "official_judge_v073.py", "ingestion_gate.py",
                "longmemeval_ingestion_calibration.py", "rival_mem0.py", "rival_mempalace.py",
                # the shipped recall path, added as a fourth arm (amendment 5)
                "mcp_server.py", "conversation_memory.py", "local_brain.py",
                "context_packet.py", "product_answer_eval.py", "product_recall_eval.py"]
N = 40


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


AMEND = None


def freeze():
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "preregistration.json"
    if path.exists() and not AMEND:
        raise SystemExit(f"{path} already exists; a pre-registration is never rewritten")
    superseded = None
    if path.exists():
        # Amending is legitimate only while no result exists for this pool, and only in the open:
        # the old manifest is kept beside the new one and the reason is recorded in it.
        # any arm, any depth; an empty file left by a run stopped before answering is not a result
        if any(f.stat().st_size for f in [*OUT.rglob("*answers*"), *OUT.rglob("*judge*")]):
            raise SystemExit("this pool already has results; the pre-registration is now fixed")
        previous = json.loads(path.read_text(encoding="utf-8"))
        kept = OUT / f"preregistration-superseded-{len(list(OUT.glob('preregistration-*'))) + 1}.json"
        kept.write_text(json.dumps(previous, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
        superseded = {"file": kept.name, "written_at": previous["written_at"],
                      "reason_for_amendment": AMEND,
                      "code_sha256": previous["code_sha256"],
                      # An amendment may change the method. It may never change which questions are
                      # measured, so the frozen list is carried over verbatim and checked below.
                      "question_ids": previous["question_ids"]}

    clean = set(json.loads(CLEAN_POOL.read_text(encoding="utf-8"))["question_ids"])
    tuned = set(json.loads(TUNED_POOL.read_text(encoding="utf-8"))["question_ids"])
    assert not (clean & tuned), "pools must be disjoint"
    rows = {r["question_id"]: r for r in load_instances(pipe.DATA) if r["question_id"] in clean}

    # proportional by type, so the mix is the pool's mix and not a flattering one
    by_type = Counter(r["question_type"] for r in rows.values())
    quota = {t: max(1, round(N * c / len(rows))) for t, c in by_type.items()}
    chosen = []
    for question_type in sorted(quota):
        pool = sorted((q for q, r in rows.items() if r["question_type"] == question_type),
                      key=lambda q: hashlib.sha256(q.encode()).hexdigest())
        chosen += pool[:quota[question_type]]
    chosen.sort(key=lambda q: hashlib.sha256(q.encode()).hexdigest())

    manifest = {
        "role": "rival comparison on questions Graph-MIND was never tuned against",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pool": {"source": str(CLEAN_POOL), "size": len(clean),
                 "why_clean": "read only by its own untouched eval; 18 scripts read v3.2 instead",
                 "measured_once_at": "2026-09-18, pipeline v3.0, 65.8%"},
        "n_frozen": len(chosen),
        "order": "sha256(question_id) ascending; a prefix may be reported if the run is cut short",
        "question_ids": chosen,
        "type_mix": {t: sum(1 for q in chosen if rows[q]["question_type"] == t)
                     for t in sorted(by_type)},
        "systems": {
            "turnbook": {"reader": "gpt-5-mini", "extraction": "gpt-5-mini, effort=low",
                           "retrieval_k": "max(16, ceil(1.5*sqrt(sessions)))",
                           "packet": "20 spans x 500 chars", "gate": "off"},
            "mem0": {"version": "2.2.1", "model": "gpt-4o-mini",
                     "parameters": "its own defaults: temperature 0.1, top_p 0.1, max_tokens 2000",
                     "deviations": ["model named explicitly as gpt-4o-mini. Mem0 2.2.1's defaults "
                                    "cannot all hold at once: its default model is gpt-5-mini, "
                                    "which its reasoning-model list omits, so it sends "
                                    "temperature=0.1 and OpenAI returns 400. Its parameter block is "
                                    "written for a model that accepts those parameters, so the "
                                    "parameters were kept and a model that takes them was named",
                                    "mem0ai[extras], mem0ai[nlp], en_core_web_sm installed so BM25 "
                                    "hybrid search and spaCy are ON, as its docs intend"],
                     "prompts": "untouched",
                     "write_tokens": "measured, not inferred: every OpenAI call it makes is "
                                     "intercepted and its usage summed (write-usage.jsonl)",
                     "input": "the FULL haystack, ~48 sessions per question, which is MORE than "
                              "Graph-MIND extracts in this harness (only the retrieved sessions)"},
            "mempalace": {"version": "3.10.0", "deviations": [],
                          "input": "the FULL haystack"},
            "turnbook_product": {
                "what": "the recall path the MCP server ships (brain_context), not the benchmark "
                        "pipeline: no write-time LLM; the haystack stored as captured turns",
                "retrieval": "USER turns cut into 500-char pieces ranked by local embeddings, fused "
                             "by reciprocal rank with SQLite full-text search; hits delivered as their "
                             "500-char piece; assistant answers delivered whole (<=3000 chars) when "
                             "the question is about what the assistant said",
                "about_assistant": "set by the calling model in real use; played here by the "
                                   "benchmark's own stand-in (asks_assistant_history regex)",
                "packet": "policy always, 20 items, 10000 chars",
                "vectors": "runs/graph-v6.0-rival-clean/graph-mind/embed-cache-local.json, read only",
                "dev2_reference": "88.3% on the tuned dev2 set vs benchmark pipeline 85.8%, "
                                  "McNemar p=0.68"}},
        "shared": {"answer_prompt": "ours, identical for every system",
                   "reader": "gpt-5-mini, identical", "judge": "official LongMemEval protocol, "
                   "gpt-4o-2024-08-06", "spans_to_reader": 15},
        "hypothesis": "Graph-MIND's evidence reaches the reader more often than Mem0's or "
                      "MemPalace's, on questions none of the three was tuned on.",
        "reported_regardless": "the accuracy of all three, per question type, with the per-question "
                               "judge verdicts, whatever the ordering turns out to be",
        "noise_floor": "9 of 120 answers move between identical runs (REPORT.md section 6), so at "
                       "n=40 a gap under about 8 points is not interpretable",
        "code_sha256": {f: sha(f) for f in FROZEN_FILES},
    }
    if superseded:
        manifest["supersedes"] = superseded
        assert superseded["question_ids"] == chosen, (
            "the selection changed; an amendment may not move the questions")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = ArgumentParser()
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--amend", help="reason; allowed only before this pool has any result")
    args = parser.parse_args()
    global AMEND
    AMEND = args.amend
    path = OUT / "preregistration.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if args.show else freeze()
    print(json.dumps({k: v for k, v in manifest.items() if k != "question_ids"},
                     ensure_ascii=False, indent=1))
    print(f"\n{manifest['n_frozen']} questions frozen, first 5: {manifest['question_ids'][:5]}")


if __name__ == "__main__":
    main()
