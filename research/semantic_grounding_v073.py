"""Semantic concept grounding v0.7.3 — embedding cue↔memory matching (DEV calibration).

The v0.7.2 result proved literal token overlap (single OR union) cannot bridge Korean
paraphrase to terse/English memory: on both the dev set and an independent hard blind set,
vague Hit@5 stayed 0% at the frozen 0.75 gate.  Cross-lingual embedding similarity does carry
the missing signal (cos(KR "연상 기억 속도", EN "associative memory latency")≈0.41 vs an
unrelated pair ≈0.15).

This experiment replaces the literal coverage gate with a SEMANTIC one, post-hoc and without
touching associative_memory: for each query it takes the same FTS seeds and the same activation
ranking (gate off), then computes semantic coverage = mean over concept cues of the max cosine
similarity of that cue to any seed's text.  It sweeps the gate threshold on the DEV 24 set only,
to see whether a threshold separates vague recall from control false association.  This is
DIAGNOSTIC calibration on a spent dev set — no threshold is adopted and no performance is claimed
until a FRESH untouched blind set confirms it.  Embeddings are cached to disk so re-runs need no
API.

    OPENAI_API_KEY=... python semantic_grounding_v073.py run
    python semantic_grounding_v073.py selftest    # no API, no DB
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from urllib import error, request
import hashlib
import json
import math
import os
import threading

from turnbook.local_embedder import LOCAL_MODEL, LocalEmbedder, cosine  # noqa: F401

from turnbook.associative_memory import AssociativeMemoryIndex, _terms
from grounded_concept_bridge_v072 import (ACTIVATE, BLIND_CASES, BLIND_ROOT, CASES_PATH,
    _blind_plan_map, _frozen_plans, _metric, _read_json, _summary, _write_json)

SEED_LIMIT = ACTIVATE["seed_limit"]


def semantic_coverage(cue_vecs, seed_vecs):
    """Mean over cues of the best cosine similarity of that cue to any seed text."""
    if not cue_vecs or not seed_vecs:
        return 0.0
    return sum(max(cosine(c, s) for s in seed_vecs) for c in cue_vecs) / len(cue_vecs)


def _seed_texts(index, query, cues):
    terms = _terms(" ".join([query, *cues]))
    if not terms:
        return []
    expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    rows = index.db.execute(
        """SELECT associative_engrams_fts.text AS text
           FROM associative_engrams_fts JOIN associative_engrams e
             ON e.engram_id=associative_engrams_fts.engram_id
           WHERE associative_engrams_fts MATCH ?
           ORDER BY bm25(associative_engrams_fts), e.revision DESC LIMIT ?""",
        [expression, SEED_LIMIT]).fetchall()
    return [r["text"] for r in rows]


# Pre-registered from the DEV calibration safe window [0.25, 0.35] (control=0%, vague=33%,
# exact=89%). Fixed BEFORE observing any blind semantic score. Not re-tuned on the blind set.
PREREGISTERED_THRESHOLD = 0.30

RUN_ROOT = Path("runs/graph-v3.0-semantic-grounding-v0.7.3")
INDEX_PATH = Path("runs/graph-v3.0-grounded-concept-bridge-v0.7.2/baseline-index.sqlite")
EMBED_MODEL = "text-embedding-3-small"
SEED_LIMIT = ACTIVATE["seed_limit"]
THRESHOLDS = [round(0.20 + 0.05 * i, 2) for i in range(14)]  # 0.20 .. 0.85 (covers both scales)
_BACKEND = "openai"  # set by main(); local backend keeps all text on-device (product promise)


def _sha(text):
    return hashlib.sha256((EMBED_MODEL + "\0" + text).encode("utf-8")).hexdigest()


class OpenAIEmbedder:
    """Minimal /v1/embeddings client with an on-disk cache; key never persisted."""

    def __init__(self, cache_path, *, api_key=None, timeout=60):
        self.cache_path = Path(cache_path)
        self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) \
            if self.cache_path.exists() else {}
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.timeout = timeout

    def _fetch(self, texts):
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY_REQUIRED")
        body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
        req = request.Request("https://api.openai.com/v1/embeddings", data=body,
            method="POST", headers={"Authorization": "Bearer " + self.api_key,
                                    "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise RuntimeError("OPENAI_EMBED_HTTP_ERROR:" + str(exc.code)) from None
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError("OPENAI_EMBED_TRANSPORT:" + type(exc).__name__) from None
        return [row["embedding"] for row in payload["data"]]

    def embed(self, texts):
        texts = [t.strip()[:2000] for t in texts]
        missing = [t for t in dict.fromkeys(texts) if _sha(t) not in self.cache]
        for start in range(0, len(missing), 128):
            chunk = missing[start:start + 128]
            for text, vector in zip(chunk, self._fetch(chunk)):
                self.cache[_sha(text)] = vector
        if missing:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        return [self.cache[_sha(t)] for t in texts]


def make_embedder(root):
    if _BACKEND == "local":
        return LocalEmbedder(Path(root) / "embed-cache-local.json")
    return OpenAIEmbedder(Path(root) / "embed-cache.json")


def _backend_model():
    return LOCAL_MODEL if _BACKEND == "local" else EMBED_MODEL


def run(root=RUN_ROOT):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cases = _read_json(CASES_PATH)
    plans = _frozen_plans()
    embedder = make_embedder(root)
    rows = []
    with AssociativeMemoryIndex(INDEX_PATH) as index:
        for case in cases:
            cues = plans.get(case["id"], [])
            seed_texts = _seed_texts(index, case["query"], cues) if cues else []
            if cues and seed_texts:
                vecs = embedder.embed(cues + seed_texts)
                cue_vecs, seed_vecs = vecs[:len(cues)], vecs[len(cues):]
                sem_cov = semantic_coverage(cue_vecs, seed_vecs)
                result = index.activate(case["query"], concept_cues=cues,
                    minimum_concept_coverage=0.0, coverage_mode="single", **ACTIVATE)
                ranking = [r["engram_id"] for r in result["results"]]
            else:
                sem_cov, ranking = 0.0, []
            rows.append({"id": case["id"], "style": case["style"],
                "expected": case["expected"], "ranking": ranking,
                "semantic_coverage": round(sem_cov, 6)})
    sweep = _sweep(rows)
    report = {"benchmark": "graph-v3.0-semantic-grounding-v0.7.3-DEV-CALIBRATION",
              "embed_model": _backend_model(), "backend": _BACKEND, "index": str(INDEX_PATH),
              "note": "DEV-set calibration only. No threshold adopted; confirm on a FRESH "
                      "blind set before any claim.",
              "separation": _separation(rows), "sweep": sweep, "per_question": rows}
    _write_json(root / "report.json", report)
    (root / "RESULTS.md").write_text(_render(report), encoding="utf-8")
    return report


RERANK_POOL = 24  # = max_nodes; fixed a priori, not tuned


def _rerank_eval(index, embedder, cases, plans, threshold):
    """Same candidate pool, two orderings: activation-score vs query-semantic. Gate fixed."""
    act = {**ACTIVATE, "limit": RERANK_POOL}
    base, rer = [], []
    for case in cases:
        cues = plans.get(case["id"], [])
        seed_texts = _seed_texts(index, case["query"], cues) if cues else []
        if cues and seed_texts:
            vecs = embedder.embed(cues + seed_texts)
            sem_cov = semantic_coverage(vecs[:len(cues)], vecs[len(cues):])
            result = index.activate(case["query"], concept_cues=cues,
                minimum_concept_coverage=0.0, coverage_mode="single", **act)
            cand = [(r["engram_id"], r["text_excerpt"]) for r in result["results"]]
        else:
            sem_cov, cand = 0.0, []
        base_order = [cid for cid, _ in cand]
        if cand:
            qv = embedder.embed([case["query"]])[0]
            cvs = embedder.embed([t for _, t in cand])
            order = sorted(range(len(cand)),
                           key=lambda i: cosine(qv, cvs[i]), reverse=True)
            sem_order = [cand[i][0] for i in order]
        else:
            sem_order = []
        gated_base = base_order if sem_cov >= threshold else []
        gated_sem = sem_order if sem_cov >= threshold else []
        base.append({"style": case["style"], "expected": case["expected"],
                     "metrics": _metric(gated_base, case["expected"])})
        rer.append({"style": case["style"], "expected": case["expected"],
                    "metrics": _metric(gated_sem, case["expected"])})
    return base, rer


def rerank(root=RUN_ROOT, threshold=PREREGISTERED_THRESHOLD):
    """Isolated: activation-order vs query-semantic re-ranking over the SAME pool (size 24),
    same gate 0.30. Pre-fixed hyperparameters, no tuning. Reports dev and blind."""
    root = Path(root)
    embedder = make_embedder(root)
    out = {"preregistered_threshold": threshold, "rerank_pool": RERANK_POOL,
           "embed_model": _backend_model(), "backend": _BACKEND, "datasets": {}}
    datasets = {"dev": (_read_json(CASES_PATH), _frozen_plans()),
                "blind": (_read_json(BLIND_CASES), _blind_plan_map(BLIND_ROOT))}
    with AssociativeMemoryIndex(INDEX_PATH) as index:
        for name, (cases, plans) in datasets.items():
            base, rer = _rerank_eval(index, embedder, cases, plans, threshold)
            def slice_hit5(rows, style):
                g = [r for r in rows if r["expected"] and r["style"] == style]
                return sum(r["metrics"]["hit_at_5"] for r in g) / len(g) if g else 0.0
            controls = [r for r in base if not r["expected"]]
            out["datasets"][name] = {
                "vague_hit5": {"activation_order": slice_hit5(base, "VAGUE"),
                               "semantic_rerank": slice_hit5(rer, "VAGUE")},
                "exact_hit5": {"activation_order": slice_hit5(base, "EXACT"),
                               "semantic_rerank": slice_hit5(rer, "EXACT")},
                "control_false_association": sum(bool(r["metrics"]["false_accept"])
                    for r in [b for b in base if not b["expected"]]) / len(controls)
                    if controls else 0.0}
    _write_json(root / "rerank.json", out)
    return out


def _all_engram_vecs(index, embedder):
    rows = index.db.execute(
        "SELECT engram_id, text FROM associative_engrams_fts").fetchall()
    ids = [r["engram_id"] for r in rows]
    vecs = embedder.embed([r["text"] for r in rows])
    return ids, vecs, {r["engram_id"]: r["text"] for r in rows}


def retrieve(root=RUN_ROOT, threshold=PREREGISTERED_THRESHOLD):
    """Isolated: candidate retrieval source. Baseline = FTS-seeded activation pool then semantic
    rerank (v0.7.3 best). Treatment = global embedding retrieval (top-24 by query cosine over ALL
    engrams) then the same gate. Same gate 0.30, same pool size 24, no tuning. Control safety is
    measured because a looser retrieval could erode the UNKNOWN boundary."""
    root = Path(root)
    embedder = make_embedder(root)
    out = {"preregistered_threshold": threshold, "pool": RERANK_POOL,
           "embed_model": _backend_model(), "backend": _BACKEND, "datasets": {}}
    datasets = {"dev": (_read_json(CASES_PATH), _frozen_plans()),
                "blind": (_read_json(BLIND_CASES), _blind_plan_map(BLIND_ROOT))}
    act = {**ACTIVATE, "limit": RERANK_POOL}
    with AssociativeMemoryIndex(INDEX_PATH) as index:
        eids, evecs, etext = _all_engram_vecs(index, embedder)
        for name, (cases, plans) in datasets.items():
            fts, emb = [], []
            for case in cases:
                cues = plans.get(case["id"], [])
                seed_texts = _seed_texts(index, case["query"], cues) if cues else []
                sem_cov = 0.0
                if cues and seed_texts:
                    cvecs = embedder.embed(cues + seed_texts)
                    sem_cov = semantic_coverage(cvecs[:len(cues)], cvecs[len(cues):])
                qv = embedder.embed([case["query"]])[0]
                # baseline: FTS-seeded activation pool, semantic rerank by query cosine
                if cues and seed_texts:
                    res = index.activate(case["query"], concept_cues=cues,
                        minimum_concept_coverage=0.0, coverage_mode="single", **act)
                    cand = [(r["engram_id"], r["text_excerpt"]) for r in res["results"]]
                    order = sorted(cand, key=lambda c: cosine(qv, embedder.embed([c[1]])[0]),
                                   reverse=True)
                    fts_order = [c[0] for c in order]
                else:
                    fts_order = []
                # treatment: global embedding retrieval over ALL engrams by query cosine
                sims = sorted(range(len(eids)), key=lambda i: cosine(qv, evecs[i]),
                              reverse=True)[:RERANK_POOL]
                emb_order = [eids[i] for i in sims]
                # gate for treatment: cues vs the treatment candidate texts
                if cues:
                    cue_vecs = embedder.embed(cues)
                    emb_cov = semantic_coverage(cue_vecs, [evecs[i] for i in sims])
                else:
                    emb_cov = 0.0
                fts.append({"style": case["style"], "expected": case["expected"],
                    "metrics": _metric(fts_order if sem_cov >= threshold else [], case["expected"])})
                emb.append({"style": case["style"], "expected": case["expected"],
                    "metrics": _metric(emb_order if emb_cov >= threshold else [], case["expected"])})

            def hit5(rows, style):
                g = [r for r in rows if r["expected"] and r["style"] == style]
                return sum(r["metrics"]["hit_at_5"] for r in g) / len(g) if g else 0.0
            def fa(rows):
                c = [r for r in rows if not r["expected"]]
                return sum(bool(r["metrics"]["false_accept"]) for r in c) / len(c) if c else 0.0
            out["datasets"][name] = {
                "vague_hit5": {"fts_rerank": hit5(fts, "VAGUE"), "embedding_retrieval": hit5(emb, "VAGUE")},
                "exact_hit5": {"fts_rerank": hit5(fts, "EXACT"), "embedding_retrieval": hit5(emb, "EXACT")},
                "control_false_association": {"fts_rerank": fa(fts), "embedding_retrieval": fa(emb)}}
    _write_json(root / "retrieve.json", out)
    return out


def confirm(root=RUN_ROOT, threshold=PREREGISTERED_THRESHOLD):
    """One-shot confirmation on the hard blind set at the PRE-REGISTERED threshold.

    The blind questions were authored before this mechanism existed; their semantic scores were
    never observed when the threshold was fixed.  This is a pre-registered confirmation, not a
    tuning run.  A brand-new untouched set would be even cleaner; this reuses the frozen blind
    plans as an independent-content check.
    """
    root = Path(root)
    cases = _read_json(BLIND_CASES)
    plans = _blind_plan_map(BLIND_ROOT)
    if set(plans) != {c["id"] for c in cases}:
        raise RuntimeError("BLIND_PLANS_INCOMPLETE (run blind-plans first)")
    embedder = make_embedder(root)
    details = []
    with AssociativeMemoryIndex(INDEX_PATH) as index:
        for case in cases:
            cues = plans.get(case["id"], [])
            seed_texts = _seed_texts(index, case["query"], cues) if cues else []
            if cues and seed_texts:
                vecs = embedder.embed(cues + seed_texts)
                sem_cov = semantic_coverage(vecs[:len(cues)], vecs[len(cues):])
                result = index.activate(case["query"], concept_cues=cues,
                    minimum_concept_coverage=0.0, coverage_mode="single", **ACTIVATE)
                ranking = [r["engram_id"] for r in result["results"]]
            else:
                sem_cov, ranking = 0.0, []
            gated = ranking if sem_cov >= threshold else []
            details.append({"id": case["id"], "style": case["style"],
                "expected": case["expected"], "ranking": gated,
                "metrics": _metric(gated, case["expected"]),
                "semantic_coverage": round(sem_cov, 6),
                "maximum_concept_coverage": round(sem_cov, 6)})  # _summary reuses this key
    report = {"benchmark": "graph-v3.0-semantic-grounding-v0.7.3-BLIND-CONFIRM",
              "preregistered_threshold": threshold,
              "embed_model": LOCAL_MODEL if _BACKEND == "local" else EMBED_MODEL,
              "backend": _BACKEND,
              "note": "Threshold fixed from DEV calibration; blind semantic scores unseen when "
                      "fixed. One-shot confirmation on independent-content hard questions.",
              "summary": _summary(details), "per_question": details}
    _write_json(root / "blind-confirm.json", report)
    return report


def _sweep(rows):
    positives = [r for r in rows if r["expected"]]
    controls = [r for r in rows if not r["expected"]]
    vague = [r for r in positives if r["style"] == "VAGUE"]
    exact = [r for r in positives if r["style"] == "EXACT"]
    out = []
    for t in THRESHOLDS:
        def hit5(group):
            if not group:
                return 0.0
            n = 0
            for r in group:
                rank = _metric(r["ranking"] if r["semantic_coverage"] >= t else [],
                               r["expected"])
                n += bool(rank["hit_at_5"])
            return n / len(group)
        fa = sum(bool(r["ranking"]) and r["semantic_coverage"] >= t for r in controls)
        out.append({"threshold": t, "vague_hit5": hit5(vague), "exact_hit5": hit5(exact),
                    "control_false_association": fa / len(controls) if controls else 0.0})
    return out


def _separation(rows):
    def stat(style_ok):
        vals = [r["semantic_coverage"] for r in rows if style_ok(r)]
        return {"min": round(min(vals), 4), "max": round(max(vals), 4),
                "mean": round(sum(vals) / len(vals), 4)} if vals else {}
    return {"vague": stat(lambda r: r["expected"] and r["style"] == "VAGUE"),
            "exact": stat(lambda r: r["expected"] and r["style"] == "EXACT"),
            "control": stat(lambda r: not r["expected"])}


def _render(report):
    lines = ["# Semantic grounding v0.7.3 — DEV calibration (embedding coverage gate)", "",
             "Post-hoc semantic gate over the frozen v0.7.1 plans + baseline index. DEV only.", "",
             f"Separation (mean semantic coverage): vague {report['separation']['vague'].get('mean')} "
             f"/ control {report['separation']['control'].get('mean')} "
             f"/ exact {report['separation']['exact'].get('mean')}", "",
             "| gate | Vague Hit@5 | Exact Hit@5 | Control false assoc |",
             "|---:|---:|---:|---:|"]
    for s in report["sweep"]:
        lines.append(f"| {s['threshold']:.2f} | {s['vague_hit5']:.2%} | "
                     f"{s['exact_hit5']:.2%} | {s['control_false_association']:.2%} |")
    lines += ["", "## 연구 경계", "",
              "- DEV 24 calibration만. 임계값 채택·성능 주장 없음 — 새 blind 셋 확인 후에만.",
              "- 임베딩은 캐시됨. planner plan은 frozen v0.7.1 재사용(질문측 무변경).",
              "- control false association이 0%를 유지하는 최고 임계값 구간이 안전 후보다.", ""]
    return "\n".join(lines)


def selftest():
    assert abs(cosine([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(cosine([1, 0], [0, 1])) < 1e-9
    assert cosine([0, 0], [1, 1]) == 0.0
    # semantic_coverage = mean of per-cue best match
    cues = [[1, 0], [0, 1]]
    seeds = [[1, 0]]  # first cue matches (1.0), second cue orthogonal (0.0)
    assert abs(semantic_coverage(cues, seeds) - 0.5) < 1e-9
    assert semantic_coverage([], seeds) == 0.0 and semantic_coverage(cues, []) == 0.0
    print("selftest OK")


def main():
    global _BACKEND
    parser = ArgumentParser()
    parser.add_argument("action",
                        choices=["run", "confirm", "rerank", "retrieve", "selftest"])
    parser.add_argument("--backend", choices=["openai", "local"], default="openai")
    parser.add_argument("--threshold", type=float, default=None,
                        help="gate threshold; default = pre-registered 0.30 (openai scale)")
    args = parser.parse_args()
    _BACKEND = args.backend
    t = args.threshold if args.threshold is not None else PREREGISTERED_THRESHOLD
    if args.action == "selftest":
        selftest()
    elif args.action == "confirm":
        report = confirm(threshold=t)
        print(json.dumps({"backend": _BACKEND, "threshold": t,
                          "summary": report["summary"]}, ensure_ascii=False, indent=2))
    elif args.action == "rerank":
        print(json.dumps(rerank(threshold=t), ensure_ascii=False, indent=2))
    elif args.action == "retrieve":
        print(json.dumps(retrieve(threshold=t), ensure_ascii=False, indent=2))
    else:
        report = run()
        print(json.dumps({"separation": report["separation"], "sweep": report["sweep"]},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
