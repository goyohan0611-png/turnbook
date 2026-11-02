"""Freeze, capture, and evaluate target-blind LLM concept plans."""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from statistics import median
from time import perf_counter
import json
import os

from turnbook.associative_memory import AssociativeMemoryIndex
from turnbook.paths import default_development_db
from llm_concept_planner import (
    DEFAULT_MODEL, INSTRUCTIONS, MINIMUM_CONCEPT_COVERAGE, PLAN_SCHEMA,
    OpenAIResponsesConceptPlanner, digest,
)


RUN_NAME = "graph-v3.0-llm-concept-plan-v0.7.1"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def prepare(root, cases_path, model):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cases = _read(cases_path)
    questions = [{"id": row["id"], "query": row["query"]}
                 for row in cases]
    manifest = {
        "run": RUN_NAME,
        "model": model,
        "questions_sha256": digest(questions),
        "prompt_sha256": digest(INSTRUCTIONS),
        "schema_sha256": digest(PLAN_SCHEMA),
        "minimum_concept_coverage": MINIMUM_CONCEPT_COVERAGE,
        "planner_sees": ["id", "query"],
        "planner_never_sees": ["style", "expected", "concept_cues", "memory_contents"],
    }
    _write(root / "questions.json", questions)
    _write(root / "manifest.json", manifest)
    return manifest


def _verify_frozen(root, model=None):
    root = Path(root)
    questions, manifest = _read(root / "questions.json"), _read(root / "manifest.json")
    checks = {
        "questions_sha256": digest(questions),
        "prompt_sha256": digest(INSTRUCTIONS),
        "schema_sha256": digest(PLAN_SCHEMA),
        "minimum_concept_coverage": MINIMUM_CONCEPT_COVERAGE,
    }
    for key, actual in checks.items():
        if manifest.get(key) != actual:
            raise RuntimeError("FROZEN_CONCEPT_PLAN_CONTRACT_CHANGED:" + key)
    if model is not None and manifest.get("model") != model:
        raise RuntimeError("FROZEN_CONCEPT_PLAN_MODEL_CHANGED")
    return questions, manifest


def _records(path):
    records = {}
    path = Path(path)
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        previous = records.get(row["id"])
        # A browser double-submit can produce two valid captures.  Preserve the
        # first success so evaluation cannot cherry-pick between generations.
        if previous and previous.get("status") == "ok":
            continue
        records[row["id"]] = row
    return records


def capture(root, *, limit=0):
    root = Path(root)
    questions, manifest = _verify_frozen(root)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY_REQUIRED_NO_REQUEST_SENT")
    planner = OpenAIResponsesConceptPlanner(manifest["model"])
    output = root / "responses.jsonl"
    existing = _records(output)
    pending = [row for row in questions
               if existing.get(row["id"], {}).get("status") != "ok"]
    if limit:
        pending = pending[:limit]
    completed = 0
    with output.open("a", encoding="utf-8") as handle:
        for question in pending:
            try:
                plan = planner.plan(question["query"])
                row = {"id": question["id"], "status": "ok", "query_sha256":
                       digest(question["query"]), "plan": plan}
            except RuntimeError as exc:
                row = {"id": question["id"], "status": "api_error",
                       "query_sha256": digest(question["query"]),
                       "error": str(exc).split(":")[:3]}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            completed += 1
            if row["status"] != "ok":
                break
    current = _records(output)
    successful = sum(row.get("status") == "ok" for row in current.values())
    return {"attempted": completed, "successful": successful,
            "total_questions": len(questions),
            "remaining": len(questions) - successful}


def _metric(ranking, expected):
    if not expected:
        return {"false_accept": bool(ranking), "hit_at_1": None,
                "hit_at_5": None, "reciprocal_rank": None}
    rank = next((i for i, item in enumerate(ranking, 1) if item in set(expected)), None)
    return {"false_accept": None, "hit_at_1": rank == 1,
            "hit_at_5": rank is not None and rank <= 5,
            "reciprocal_rank": 1.0 / rank if rank else 0.0}


def _pct(values, p):
    return sorted(values)[round((len(values) - 1) * p)] if values else 0.0


def evaluate(root, cases_path, db_path, *, limit=8):
    root = Path(root)
    questions, manifest = _verify_frozen(root)
    records, cases = _records(root / "responses.jsonl"), _read(cases_path)
    question_by_id = {row["id"]: row for row in questions}
    case_by_id = {row["id"]: row for row in cases}
    if set(question_by_id) != set(case_by_id):
        raise RuntimeError("QUESTION_AND_LABEL_IDS_DIFFER")
    details, retrieval_ms, planner_ms = [], [], []
    with AssociativeMemoryIndex(db_path) as index:
        index.sync_sources()
        for item_id, question in question_by_id.items():
            case = case_by_id[item_id]
            if question["query"] != case["query"]:
                raise RuntimeError("QUESTION_TEXT_CHANGED:" + item_id)
            record = records.get(item_id)
            plan = (record or {}).get("plan", {}) if (record or {}).get("status") == "ok" else {}
            cues = plan.get("concept_cues", []) if plan.get("status") == "plan" else []
            started = perf_counter()
            if cues:
                result = index.activate(question["query"], concept_cues=cues,
                    seed_limit=12, hops=2, fanout=8, max_nodes=24, limit=limit,
                    minimum_concept_coverage=manifest["minimum_concept_coverage"])
            else:
                result = {"status": "UNKNOWN", "reason": "PLANNER_ABSTAINED",
                          "results": [], "maximum_concept_coverage": 0.0}
            elapsed = (perf_counter() - started) * 1000
            ranking = [row["engram_id"] for row in result["results"]]
            metric = _metric(ranking, case["expected"])
            retrieval_ms.append(elapsed)
            if plan.get("latency_ms") is not None:
                planner_ms.append(float(plan["latency_ms"]))
            details.append({"id": item_id, "style": case["style"],
                "query": question["query"], "expected": case["expected"],
                "generated_plan": {"status": plan.get("status", "missing"),
                                   "concept_cues": cues},
                "ranking": ranking, "metrics": metric,
                "trace": {"status": result.get("status"),
                          "reason": result.get("reason"),
                          "maximum_concept_coverage": result.get(
                              "maximum_concept_coverage", 0.0)}})
    positives = [row for row in details if row["expected"]]
    controls = [row for row in details if not row["expected"]]
    vague = [row for row in positives if row["style"] == "VAGUE"]
    exact = [row for row in positives if row["style"] == "EXACT"]
    def aggregate(rows):
        return {"count": len(rows),
            "hit_at_1": sum(r["metrics"]["hit_at_1"] for r in rows) / len(rows),
            "hit_at_5": sum(r["metrics"]["hit_at_5"] for r in rows) / len(rows),
            "mrr": sum(r["metrics"]["reciprocal_rank"] for r in rows) / len(rows)}
    usage = [records[row["id"]]["plan"].get("usage", {}) for row in details
             if row["id"] in records and records[row["id"]].get("status") == "ok"]
    successful_plans = sum(row.get("status") == "ok" for row in records.values())
    result = {"benchmark": RUN_NAME, "evaluation_status":
              "COMPLETE" if successful_plans == len(details) else "INCOMPLETE",
              "case_count": len(details), "successful_plans": len(usage),
              "contract": manifest,
              "summary": {"positive": aggregate(positives), "exact": aggregate(exact),
                  "vague": aggregate(vague), "control_count": len(controls),
                  "false_association_rate": sum(r["metrics"]["false_accept"]
                      for r in controls) / len(controls),
                  "planner_latency_ms_median": median(planner_ms) if planner_ms else 0.0,
                  "planner_latency_ms_p95": _pct(planner_ms, 0.95),
                  "retrieval_latency_ms_median": median(retrieval_ms),
                  "retrieval_latency_ms_p95": _pct(retrieval_ms, 0.95),
                  "input_tokens": sum(x.get("input_tokens", 0) for x in usage),
                  "output_tokens": sum(x.get("output_tokens", 0) for x in usage)},
              "details": details}
    _write(root / "report.json", result)
    (root / "RESULTS.md").write_text(render(result), encoding="utf-8")
    return result


def render(result):
    s = result["summary"]
    return "\n".join([
        "# Target-blind LLM Concept Planner v0.7.1", "",
        f"Status: **{result['evaluation_status']}**", "",
        "| Slice | Hit@1 | Hit@5 | MRR |", "|---|---:|---:|---:|",
        f"| Exact | {s['exact']['hit_at_1']:.2%} | {s['exact']['hit_at_5']:.2%} | {s['exact']['mrr']:.3f} |",
        f"| Vague | {s['vague']['hit_at_1']:.2%} | {s['vague']['hit_at_5']:.2%} | {s['vague']['mrr']:.3f} |",
        f"| All positive | {s['positive']['hit_at_1']:.2%} | {s['positive']['hit_at_5']:.2%} | {s['positive']['mrr']:.3f} |",
        "", f"- Control false association: {s['false_association_rate']:.2%}",
        f"- Planner latency median / p95: {s['planner_latency_ms_median']:.2f} / {s['planner_latency_ms_p95']:.2f} ms",
        f"- Graph retrieval latency median / p95: {s['retrieval_latency_ms_median']:.2f} / {s['retrieval_latency_ms_p95']:.2f} ms",
        f"- Planner input / output tokens: {s['input_tokens']} / {s['output_tokens']}",
        "", "## 연구 경계", "",
        "- API planner에는 query만 전달했다. style, expected, oracle cue, memory content는 전달하지 않았다.",
        "- 현재 24문항은 이전 v0.7 진단에 사용된 development set이므로 final blind 성능이 아니다.",
        "- prompt, schema, model, 0.75 coverage threshold는 manifest hash로 동결했다.",
        "- 다음 단계는 이 계약을 바꾸지 않고 새 untouched 질문을 한 번 평가하는 것이다.", ""])


def main():
    parser = ArgumentParser()
    parser.add_argument("action", choices=["prepare", "capture", "evaluate", "all"])
    parser.add_argument("--root", default="runs/" + RUN_NAME)
    parser.add_argument("--cases", default="associative_hybrid_benchmark_v07_cases.json")
    parser.add_argument("--db", default=str(default_development_db()))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.action in {"prepare", "all"}:
        prepare(args.root, args.cases, args.model)
    if args.action in {"capture", "all"}:
        print(json.dumps(capture(args.root, limit=args.limit), ensure_ascii=False))
    if args.action in {"evaluate", "all"}:
        result = evaluate(args.root, args.cases, Path(args.db))
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
