"""Grounded concept bridge v0.7.2 — source-bound synonym enrichment ablation.

Isolated A/B on the single variable the v0.7.1 diagnosis identified: whether ingestion-time
source-bound alias cues lift concept coverage (and top-5 recall) for paraphrased recall,
without touching the deterministic UNKNOWN boundary.

Both arms build a shadow associative index over development_events ONLY (all benchmark targets
are development:* — personal memories and conversations are never materialized).  The BASELINE
arm indexes each event's own text; the TREATMENT arm additionally folds frozen, source-bound
alias cues into that engram's text+signature.  Everything else is held fixed:

  - the same frozen v0.7.1 concept plans (runs/graph-v3.0-llm-concept-plan-v0.7.1/responses.jsonl)
  - the same activation params: seed_limit=12 hops=2 fanout=8 max_nodes=24 coverage>=0.75

The live user index is never mutated; the raw memory DB is read-only and only derived engram
rows land in the shadow files.  Alias generation is the ONLY step that calls the API, writes a
frozen alias-map.jsonl, and never persists the key.

Usage (run locally where the memory DB is readable):
    OPENAI_API_KEY=... python grounded_concept_bridge_v072.py aliases   # API, once, frozen
    python grounded_concept_bridge_v072.py build                        # both shadow indexes
    python grounded_concept_bridge_v072.py replay                       # offline A/B report
    python grounded_concept_bridge_v072.py all
    python grounded_concept_bridge_v072.py selftest                     # no DB, no API
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from statistics import median
import json
import os
import sqlite3

from turnbook.associative_memory import AssociativeMemoryIndex
from turnbook.paths import default_development_db
from ingestion_synonyms import EXTRACTOR_VERSION, OpenAIResponsesAliasExtractor
from llm_concept_planner import (INSTRUCTIONS as PLAN_INSTRUCTIONS,
    PLAN_SCHEMA, OpenAIResponsesConceptPlanner, digest)

RUN_NAME = "graph-v3.0-grounded-concept-bridge-v0.7.2"
FROZEN_PLANS = Path("runs/graph-v3.0-llm-concept-plan-v0.7.1/responses.jsonl")
CASES_PATH = Path("associative_hybrid_benchmark_v07_cases.json")
BLIND_CASES = Path("blind_hard_benchmark_v072_cases.json")
BLIND_ROOT = Path("runs/graph-v3.0-grounded-concept-bridge-v0.7.2-blind")
MIN_COVERAGE = 0.75
ACTIVATE = dict(seed_limit=12, hops=2, fanout=8, max_nodes=24, limit=8)  # frozen v0.7.1 params


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def _event_text(row):
    """Exactly the text associative_memory.sync_sources builds for a development event."""
    return row["event_type"] + " " + row["subject_id"] + " " + row["payload_json"]


def _event_cues(row):
    return [row["event_type"], row["subject_id"], row["session_id"]]


def _dev_events(db_path):
    uri = "file:" + Path(db_path).resolve().as_posix() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='development_events'").fetchone():
            raise RuntimeError("NO_DEVELOPMENT_EVENTS_TABLE")
        return [dict(r) for r in db.execute(
            "SELECT * FROM development_events ORDER BY revision").fetchall()]
    finally:
        db.close()


# ---- alias-map frozen record I/O (mirrors the v0.7.1 capture resume discipline) ----

def _alias_records(path):
    records = {}
    path = Path(path)
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if records.get(row["id"], {}).get("status") == "ok":
            continue  # keep first success; never cherry-pick a re-run
        records[row["id"]] = row
    return records


def alias_map(root):
    """{engram_id: [alias_cues]} for successful, plan-status records only."""
    out = {}
    for eid, row in _alias_records(Path(root) / "alias-map.jsonl").items():
        if row.get("status") == "ok" and row.get("aliases", {}).get("status") == "plan":
            out[eid] = row["aliases"]["alias_cues"]
    return out


def generate_aliases(root, db_path, model=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY_REQUIRED_NO_REQUEST_SENT")
    extractor = OpenAIResponsesAliasExtractor(model) if model else OpenAIResponsesAliasExtractor()
    events = _dev_events(db_path)
    output = root / "alias-map.jsonl"
    done = {eid for eid, r in _alias_records(output).items() if r.get("status") == "ok"}
    pending = [e for e in events if "development:" + e["event_id"] not in done]
    completed = 0
    with output.open("a", encoding="utf-8") as handle:
        for event in pending:
            engram_id = "development:" + event["event_id"]
            try:
                aliases = extractor.extract(_event_text(event))
                row = {"id": engram_id, "status": "ok", "aliases": aliases}
            except RuntimeError as exc:
                row = {"id": engram_id, "status": "api_error",
                       "error": str(exc).split(":")[:3]}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            completed += 1
            if row["status"] != "ok":
                break  # fail-closed: stop so a transient API fault is visible, then resumable
    current = _alias_records(output)
    ok = sum(r.get("status") == "ok" for r in current.values())
    return {"extractor_version": EXTRACTOR_VERSION, "events": len(events),
            "attempted": completed, "successful": ok, "remaining": len(events) - ok}


# ---- shadow index construction (dev events only; live index untouched) ----

def build_index(root, db_path, *, with_aliases):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    arm = "treatment" if with_aliases else "baseline"
    shadow = root / (arm + "-index.sqlite")
    if shadow.exists():
        shadow.unlink()  # deterministic rebuild of this derived file
    aliases = alias_map(root) if with_aliases else {}
    indexed = 0
    with AssociativeMemoryIndex(shadow) as index:
        for event in _dev_events(db_path):
            engram_id = "development:" + event["event_id"]
            text = _event_text(event)
            if with_aliases and aliases.get(engram_id):
                # fold source-bound aliases into text (→ FTS seed retrieval) AND cues (→ signature
                # coverage). This is the single enriched variable.
                text = text + "\n" + " ".join(aliases[engram_id])
            index.add_engram(engram_id=engram_id, source_kind="DEVELOPMENT_EVENT",
                source_id=event["event_id"], scope=event["project_id"],
                happened_at=event["effective_at"], text=text,
                cues=_event_cues(event), strength=0.65)
            indexed += 1
    return {"arm": arm, "shadow_index": str(shadow), "indexed": indexed,
            "enriched": sum(1 for e in aliases if with_aliases)}


# ---- offline replay of frozen plans over each shadow index ----

def _metric(ranking, expected):
    if not expected:
        return {"false_accept": bool(ranking), "hit_at_1": None,
                "hit_at_5": None, "reciprocal_rank": None}
    rank = next((i for i, item in enumerate(ranking, 1) if item in set(expected)), None)
    return {"false_accept": None, "hit_at_1": rank == 1,
            "hit_at_5": rank is not None and rank <= 5,
            "reciprocal_rank": 1.0 / rank if rank else 0.0}


def _frozen_plans():
    # reuse the exact v0.7.1 capture-resume semantics
    from concept_plan_benchmark_v071 import _records
    records = _records(FROZEN_PLANS)
    plans = {}
    for cid, row in records.items():
        if row.get("status") != "ok":
            continue
        plan = row["plan"]
        plans[cid] = plan.get("concept_cues", []) if plan.get("status") == "plan" else []
    return plans


def _replay_arm(shadow, cases, plans, *, coverage_mode="single"):
    details = []
    with AssociativeMemoryIndex(shadow) as index:
        for case in cases:
            cues = plans.get(case["id"], [])
            if cues:
                result = index.activate(case["query"], concept_cues=cues,
                    minimum_concept_coverage=MIN_COVERAGE,
                    coverage_mode=coverage_mode, **ACTIVATE)
            else:
                result = {"status": "UNKNOWN", "reason": "PLANNER_ABSTAINED",
                          "results": [], "maximum_concept_coverage": 0.0}
            ranking = [r["engram_id"] for r in result["results"]]
            details.append({"id": case["id"], "style": case["style"],
                "expected": case["expected"], "ranking": ranking,
                "metrics": _metric(ranking, case["expected"]),
                "reason": result.get("reason"),
                "maximum_concept_coverage": result.get("maximum_concept_coverage", 0.0),
                "union_concept_coverage": result.get("union_concept_coverage", 0.0)})
    return details


def _aggregate(rows):
    if not rows:
        return {"count": 0, "hit_at_1": 0.0, "hit_at_5": 0.0, "mrr": 0.0}
    return {"count": len(rows),
        "hit_at_1": sum(r["metrics"]["hit_at_1"] for r in rows) / len(rows),
        "hit_at_5": sum(r["metrics"]["hit_at_5"] for r in rows) / len(rows),
        "mrr": sum(r["metrics"]["reciprocal_rank"] for r in rows) / len(rows)}


def _summary(details):
    positives = [r for r in details if r["expected"]]
    controls = [r for r in details if not r["expected"]]
    return {
        "exact": _aggregate([r for r in positives if r["style"] == "EXACT"]),
        "vague": _aggregate([r for r in positives if r["style"] == "VAGUE"]),
        "positive": _aggregate(positives),
        "control_count": len(controls),
        "false_association_rate": (sum(r["metrics"]["false_accept"] for r in controls)
                                   / len(controls)) if controls else 0.0,
        "vague_median_coverage": median([r["maximum_concept_coverage"]
            for r in positives if r["style"] == "VAGUE"]) if positives else 0.0}


def replay(root):
    root = Path(root)
    cases = _read_json(CASES_PATH)
    plans = _frozen_plans()
    arms = {}
    for arm in ("baseline", "treatment"):
        shadow = root / (arm + "-index.sqlite")
        if not shadow.exists():
            raise RuntimeError("MISSING_SHADOW_INDEX:" + arm + " (run build first)")
        details = _replay_arm(shadow, cases, plans)
        arms[arm] = {"summary": _summary(details), "details": details}
    report = {"benchmark": RUN_NAME, "isolated_variable": "source_bound_alias_cues",
              "held_fixed": {"plans": str(FROZEN_PLANS), "activation": ACTIVATE,
                             "minimum_concept_coverage": MIN_COVERAGE},
              "note": "dev-scope-only A/B; baseline is a dev-only re-baseline, not the "
                      "all-source v0.7.1 index. Both arms differ ONLY by alias enrichment.",
              "arms": {a: arms[a]["summary"] for a in arms},
              "per_question": {a: arms[a]["details"] for a in arms}}
    _write_json(root / "report.json", report)
    (root / "RESULTS.md").write_text(_render(report), encoding="utf-8")
    return report


def gate(root):
    """Isolated ablation: coverage gate single-seed MAX vs union-across-seeds.

    Same baseline dev-only index, same frozen plans, same activation params and 0.75 gate.
    The ONLY variable is how concept coverage is aggregated across seeds.  Control false
    association is the safety metric — a permissive gate must not erode the UNKNOWN boundary.
    """
    root = Path(root)
    cases = _read_json(CASES_PATH)
    plans = _frozen_plans()
    shadow = root / "baseline-index.sqlite"
    if not shadow.exists():
        raise RuntimeError("MISSING_SHADOW_INDEX:baseline (run build first)")
    modes = {}
    for mode in ("single", "union"):
        details = _replay_arm(shadow, cases, plans, coverage_mode=mode)
        modes[mode] = {"summary": _summary(details), "details": details}
    report = {"benchmark": RUN_NAME + "-gate-ablation",
              "isolated_variable": "coverage_gate_mode",
              "held_fixed": {"index": str(shadow), "plans": str(FROZEN_PLANS),
                             "activation": ACTIVATE, "minimum_concept_coverage": MIN_COVERAGE},
              "note": "dev-scope-only baseline index, no alias enrichment. single=max over "
                      "seeds (v0.7.1 behavior), union=fraction of cue terms covered by the "
                      "union of all seed signatures.",
              "modes": {m: modes[m]["summary"] for m in modes},
              "per_question": {m: modes[m]["details"] for m in modes}}
    _write_json(root / "gate-ablation.json", report)
    (root / "GATE-ABLATION.md").write_text(_render_gate(report), encoding="utf-8")
    return report


def _render_gate(report):
    s, u = report["modes"]["single"], report["modes"]["union"]
    def row(name, key):
        return (f"| {name} | {s[key]['hit_at_1']:.2%} → {u[key]['hit_at_1']:.2%} "
                f"| {s[key]['hit_at_5']:.2%} → {u[key]['hit_at_5']:.2%} "
                f"| {s[key]['mrr']:.3f} → {u[key]['mrr']:.3f} |")
    return "\n".join([
        "# Coverage gate ablation — single-seed MAX → union-across-seeds", "",
        "Same baseline index, same frozen plans; only the gate aggregation varies.", "",
        "| Slice | Hit@1 | Hit@5 | MRR |", "|---|---|---|---|",
        row("Exact", "exact"), row("Vague", "vague"), row("All positive", "positive"), "",
        f"- **Control false association: {s['false_association_rate']:.2%} → "
        f"{u['false_association_rate']:.2%}** (safety — must stay 0%)",
        f"- Vague median coverage used at gate: single≈max, union≥max by construction", "",
        "## 연구 경계", "",
        "- 변수는 gate 집계 방식 하나뿐. 인덱스·plan·파라미터·임계 0.75 고정.",
        "- union이 vague를 살리더라도 control false association이 오르면 순이득이 아니다.",
        "- dev 스코프 전용 baseline이라 v0.7.1 all-source 수치와 직접 비교는 아니다.", ""])


def _blind_records(path):
    records = {}
    path = Path(path)
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if records.get(row["id"], {}).get("status") == "ok":
            continue
        records[row["id"]] = row
    return records


def blind_freeze(root):
    """Commit the frozen contract BEFORE any evaluation, so the gate/threshold cannot be
    tuned to the blind scores. This is the pre-registration record."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cases = _read_json(BLIND_CASES)
    questions = [{"id": c["id"], "query": c["query"]} for c in cases]
    manifest = {
        "run": RUN_NAME + "-blind",
        "authored_by": "claude-opus-4-8 (independent of the 24-case dev targets)",
        "questions_sha256": digest(questions),
        "prompt_sha256": digest(PLAN_INSTRUCTIONS),
        "schema_sha256": digest(PLAN_SCHEMA),
        "frozen_gate": {"minimum_concept_coverage": MIN_COVERAGE,
                        "modes_evaluated": ["single", "union"]},
        "activation": ACTIVATE,
        "index": "runs/graph-v3.0-grounded-concept-bridge-v0.7.2/baseline-index.sqlite",
        "planner_sees": ["id", "query"],
        "planner_never_sees": ["style", "expected", "concept_cues", "memory_contents"],
        "rule": "evaluate exactly once; do NOT pick the threshold on this blind set either.",
    }
    _write_json(root / "questions.json", questions)
    _write_json(root / "manifest.json", manifest)
    return manifest


def blind_plans(root):
    root = Path(root)
    manifest = _read_json(root / "manifest.json")  # requires blind_freeze first
    cases = _read_json(BLIND_CASES)
    if digest([{"id": c["id"], "query": c["query"]} for c in cases]) != manifest["questions_sha256"]:
        raise RuntimeError("BLIND_QUESTIONS_CHANGED_AFTER_FREEZE")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY_REQUIRED_NO_REQUEST_SENT")
    planner = OpenAIResponsesConceptPlanner()
    output = root / "blind-responses.jsonl"
    done = {i for i, r in _blind_records(output).items() if r.get("status") == "ok"}
    pending = [c for c in cases if c["id"] not in done]
    with output.open("a", encoding="utf-8") as handle:
        for case in pending:
            try:
                plan = planner.plan(case["query"])
                row = {"id": case["id"], "status": "ok", "plan": plan}
            except RuntimeError as exc:
                row = {"id": case["id"], "status": "api_error", "error": str(exc).split(":")[:3]}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            if row["status"] != "ok":
                break
    current = _blind_records(output)
    ok = sum(r.get("status") == "ok" for r in current.values())
    return {"cases": len(cases), "successful_plans": ok, "remaining": len(cases) - ok}


def _blind_plan_map(root):
    plans = {}
    for cid, row in _blind_records(Path(root) / "blind-responses.jsonl").items():
        if row.get("status") != "ok":
            continue
        p = row["plan"]
        plans[cid] = p.get("concept_cues", []) if p.get("status") == "plan" else []
    return plans


def blind_eval(root):
    root = Path(root)
    manifest = _read_json(root / "manifest.json")
    cases = _read_json(BLIND_CASES)
    plans = _blind_plan_map(root)
    if set(plans) != {c["id"] for c in cases}:
        raise RuntimeError("BLIND_PLANS_INCOMPLETE (run blind-plans first)")
    shadow = Path(manifest["index"])
    if not shadow.exists():
        raise RuntimeError("MISSING_BASELINE_INDEX (run build first)")
    modes = {}
    for mode in ("single", "union"):
        details = _replay_arm(shadow, cases, plans, coverage_mode=mode)
        modes[mode] = {"summary": _summary(details), "details": details}
    report = {"benchmark": manifest["run"], "manifest": manifest,
              "modes": {m: modes[m]["summary"] for m in modes},
              "per_question": {m: modes[m]["details"] for m in modes}}
    _write_json(root / "blind-report.json", report)
    (root / "BLIND-RESULTS.md").write_text(_render_blind(report), encoding="utf-8")
    return report


def _render_blind(report):
    s, u = report["modes"]["single"], report["modes"]["union"]
    def row(name, key):
        return (f"| {name} | {s[key]['hit_at_1']:.2%} → {u[key]['hit_at_1']:.2%} "
                f"| {s[key]['hit_at_5']:.2%} → {u[key]['hit_at_5']:.2%} "
                f"| {s[key]['mrr']:.3f} → {u[key]['mrr']:.3f} |")
    return "\n".join([
        "# Blind hard eval v0.7.2 — frozen gate 0.75, single → union", "",
        "New author-independent hard questions, never used to tune the gate. Evaluated once.", "",
        "| Slice | Hit@1 | Hit@5 | MRR |", "|---|---|---|---|",
        row("Exact", "exact"), row("Vague", "vague"), row("All positive", "positive"), "",
        f"- Control false association: {s['false_association_rate']:.2%} → "
        f"{u['false_association_rate']:.2%} (must stay 0%)", "",
        "## 연구 경계", "",
        "- planner는 질문만 봄. gate 0.75와 파라미터는 평가 전 동결(manifest hash).",
        "- 임계값은 이 blind set에서도 고르지 않는다(한 번 평가, 진단만).",
        "- baseline dev-only 인덱스라 v0.7.1 all-source 수치와 직접 비교는 아니다.", ""])


def _render(report):
    b, t = report["arms"]["baseline"], report["arms"]["treatment"]
    def row(name, key):
        return (f"| {name} | {b[key]['hit_at_1']:.2%} → {t[key]['hit_at_1']:.2%} "
                f"| {b[key]['hit_at_5']:.2%} → {t[key]['hit_at_5']:.2%} "
                f"| {b[key]['mrr']:.3f} → {t[key]['mrr']:.3f} |")
    return "\n".join([
        "# Grounded Concept Bridge v0.7.2 — source-bound alias A/B", "",
        "Baseline → Treatment (dev-scope-only, only alias enrichment varies).", "",
        "| Slice | Hit@1 | Hit@5 | MRR |", "|---|---|---|---|",
        row("Exact", "exact"), row("Vague", "vague"), row("All positive", "positive"), "",
        f"- Vague median concept coverage: {b['vague_median_coverage']:.3f} → "
        f"{t['vague_median_coverage']:.3f} (gate at {report['held_fixed']['minimum_concept_coverage']})",
        f"- Control false association: {b['false_association_rate']:.2%} → "
        f"{t['false_association_rate']:.2%}", "",
        "## 연구 경계", "",
        "- 동일 frozen v0.7.1 plan, 동일 activation 파라미터. 변수는 alias 확장 하나뿐.",
        "- dev 스코프 전용 A/B이므로 baseline은 all-source v0.7.1 지수와 다르다(재-baseline).",
        "- alias는 각 engram 자기 원문만 보고 생성했고 질문·정답은 보지 않았다(source-bound).",
        "- control false association이 오르면 동의어가 UNKNOWN 경계를 침식한 것이므로 실패로 본다.", ""])


def selftest():
    # pure-logic checks that need neither the DB nor the API
    assert _metric(["a", "b"], ["b"]) == {"false_accept": None, "hit_at_1": False,
        "hit_at_5": True, "reciprocal_rank": 0.5}
    assert _metric([], ["b"])["hit_at_5"] is False
    assert _metric(["x"], [])["false_accept"] is True
    assert _metric([], [])["false_accept"] is False
    agg = _aggregate([{"metrics": {"hit_at_1": True, "hit_at_5": True, "reciprocal_rank": 1.0}},
                      {"metrics": {"hit_at_1": False, "hit_at_5": True, "reciprocal_rank": 0.5}}])
    assert agg["hit_at_1"] == 0.5 and agg["hit_at_5"] == 1.0 and agg["mrr"] == 0.75
    # alias-map resume keeps first success, ignores later duplicate
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "alias-map.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in [
            {"id": "development:x", "status": "api_error"},
            {"id": "development:x", "status": "ok",
             "aliases": {"status": "plan", "alias_cues": ["first", "one", "two"]}},
            {"id": "development:x", "status": "ok",
             "aliases": {"status": "plan", "alias_cues": ["later", "three", "four"]}},
        ]) + "\n", encoding="utf-8")
        assert alias_map(d) == {"development:x": ["first", "one", "two"]}
    print("selftest OK")


def main():
    parser = ArgumentParser()
    parser.add_argument("action",
                        choices=["aliases", "build", "replay", "gate",
                                 "blind-freeze", "blind-plans", "blind-eval",
                                 "all", "selftest"])
    parser.add_argument("--root", default="runs/" + RUN_NAME)
    parser.add_argument("--db", default=str(default_development_db()))
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    if args.action == "selftest":
        selftest()
        return
    if args.action in {"aliases", "all"}:
        print(json.dumps(generate_aliases(args.root, args.db, args.model), ensure_ascii=False))
    if args.action in {"build", "all"}:
        print(json.dumps(build_index(args.root, args.db, with_aliases=False), ensure_ascii=False))
        print(json.dumps(build_index(args.root, args.db, with_aliases=True), ensure_ascii=False))
    if args.action in {"replay", "all"}:
        report = replay(args.root)
        print(json.dumps({"arms": report["arms"]}, ensure_ascii=False, indent=2))
    if args.action == "gate":
        report = gate(args.root)
        print(json.dumps({"modes": report["modes"]}, ensure_ascii=False, indent=2))
    if args.action == "blind-freeze":
        print(json.dumps(blind_freeze(BLIND_ROOT), ensure_ascii=False, indent=2))
    if args.action == "blind-plans":
        print(json.dumps(blind_plans(BLIND_ROOT), ensure_ascii=False))
    if args.action == "blind-eval":
        report = blind_eval(BLIND_ROOT)
        print(json.dumps({"modes": report["modes"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
