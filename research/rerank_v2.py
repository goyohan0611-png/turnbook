"""A better re-ranker: new evidence about each candidate, and a small non-linear model.

The pool probe showed the answer is in a 100-turn pool for 92-95% of questions while the shipped
re-ranker brings it into the first 30 for 80-84%: ordering, not the pool, is the bottleneck.
Every feature here is one the product can compute from the pool alone (no history, no model):

    rrf_meaning, rrf_words, similarity, both, overlap   the shipped five
    rare_overlap   question words found, each weighted by how rare it is in the pool
    prefix_overlap the same with prefix matching ("콩이" matches "콩이를")
    neighbour      best meaning score of the turn just before or after, if pooled
    session_hits   how many pooled turns share this turn's session (log)
    session_best   best meaning score in this turn's session
    length         log length of the turn

Models: linear (pairwise logistic, as shipped) and a one-hidden-layer network (numpy). Each
conversation is scored with a model learned on the other nine. Memory 1x and 10x, pools of 50
and 100 per search.

    python research/rerank_v2.py
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402

NAMES = ["rrf_meaning", "rrf_words", "similarity", "both", "overlap", "rare_overlap",
         "prefix_overlap", "neighbour", "session_hits", "session_best", "length"]
SHIPPED = NAMES[:5]
WORD = re.compile(r"[^\W_]{2,}")


def tokens(text: str) -> list[str]:
    return [w for w in WORD.findall(text.casefold()) if w not in lc.STOP]


STEM = "--stem" in sys.argv or bool(__import__("os").environ.get("RERANK_STEM"))


def question_terms(text: str) -> list[str]:
    """The question's words; with STEM, Korean particles off as word search does."""
    if not STEM:
        return tokens(text)
    from turnbook.conversation_memory import search_terms
    return [w for w in search_terms(text) if w not in lc.STOP]


def session_of(tid: str) -> str:
    return tid.rsplit(":", 1)[0]


def neighbours(tid: str) -> list[str]:
    session, _, number = tid.rpartition(":")
    if not number.isdigit():
        return []
    return [f"{session}:{int(number) + step}" for step in (-1, 1)]


def features(question: dict, texts: dict, size: int) -> tuple[list[str], np.ndarray]:
    meaning = {t: (r, s) for r, (t, s) in enumerate(question["meaning"][:size])}
    words = {t: r for r, t in enumerate(question["words"][:size])}
    pool = list(dict.fromkeys([t for t, _ in question["meaning"][:size]]
                              + question["words"][:size]))
    asked = set(question_terms(question["question"]))
    bags = {t: set(tokens(texts.get(t, {}).get("text", ""))) for t in pool}
    def has(w, bag):
        return w in bag or (STEM and not w.isascii() and any(x.startswith(w) for x in bag))
    df = {w: sum(has(w, bag) for bag in bags.values()) for w in asked}
    rare = {w: math.log((len(pool) + 1) / (df[w] + 0.5)) for w in asked}
    total = sum(rare.values()) or 1.0
    sessions: dict[str, list[str]] = {}
    for t in pool:
        sessions.setdefault(session_of(t), []).append(t)
    rows = []
    for t in pool:
        rank_m, sim = meaning.get(t, (None, 0.0))
        rank_w = words.get(t)
        bag = bags[t]
        found = {w for w in asked if has(w, bag)}
        prefixed = {w for w in asked if any(b.startswith(w) for b in bag)}
        near = max((meaning[n][1] for n in neighbours(t) if n in meaning), default=0.0)
        mates = sessions[session_of(t)]
        rows.append([60 / (60 + rank_m) if rank_m is not None else 0.0,
                     60 / (60 + rank_w) if rank_w is not None else 0.0,
                     sim, float(rank_m is not None and rank_w is not None),
                     len(found) / max(len(asked), 1),
                     sum(rare[w] for w in found) / total,
                     len(prefixed) / max(len(asked), 1),
                     near, math.log(len(mates)),
                     max(meaning.get(m, (0, 0.0))[1] for m in mates),
                     math.log1p(len(texts.get(t, {}).get("text", "")))])
    return pool, np.array(rows, dtype=float).reshape(len(pool), len(NAMES))


def pairs(questions: list[dict], texts: dict, size: int, columns: list[int]):
    """Each answering turn against the 30 best-fused turns that do not answer."""
    out = []
    for q in questions:
        pool, x = features(q, texts[q["conv"]], size)
        gold = set(q["evidence"])
        pos = [i for i, t in enumerate(pool) if t in gold]
        if not pos:
            continue
        fused = np.argsort(-(x[:, 0] + x[:, 1]), kind="stable")
        neg = [i for i in fused if pool[i] not in gold][:30]
        out.append(x[pos][:, None, columns] - x[neg][None, :, columns])
    return np.concatenate([p.reshape(-1, len(columns)) for p in out])


def fit_linear(diff: np.ndarray, start: np.ndarray) -> np.ndarray:
    w = start.copy()
    for _ in range(400):
        margin = diff @ w
        grad = -(diff * (1 / (1 + np.exp(np.clip(margin, -30, 30))))[:, None]).mean(0)
        w -= 0.5 * (grad + 0.01 * (w - start))
    return w


class Net:
    """score(x) = v . tanh(W x + b) + u . x; trained on pairs (pos - neg can't be used directly
    for a non-linear model, so pairs are kept as two arrays)."""

    def __init__(self, n: int, hidden: int = 16, seed: int = 0):
        rng = np.random.default_rng(seed)
        self.W = rng.normal(0, 0.3, (hidden, n))
        self.b = np.zeros(hidden)
        self.v = rng.normal(0, 0.3, hidden)
        self.u = np.zeros(n)

    def score(self, x):
        return np.tanh(x @ self.W.T + self.b) @ self.v + x @ self.u

    def fit(self, pos, neg, steps=600, rate=0.05, l2=1e-3):
        for _ in range(steps):
            hp, hn = np.tanh(pos @ self.W.T + self.b), np.tanh(neg @ self.W.T + self.b)
            margin = hp @ self.v + pos @ self.u - hn @ self.v - neg @ self.u
            g = -1 / (1 + np.exp(np.clip(margin, -30, 30))) / len(pos)        # dloss/dmargin
            gv = hp.T @ g - hn.T @ g
            gu = pos.T @ g - neg.T @ g
            dp, dn = (g[:, None] * self.v) * (1 - hp ** 2), (g[:, None] * self.v) * (1 - hn ** 2)
            gW = dp.T @ pos - dn.T @ neg
            gb = dp.sum(0) - dn.sum(0)
            self.v -= rate * (gv + l2 * self.v)
            self.u -= rate * (gu + l2 * self.u)
            self.W -= rate * (gW + l2 * self.W)
            self.b -= rate * gb
        return self


def pair_arrays(questions, texts, size, columns):
    pos_all, neg_all = [], []
    for q in questions:
        pool, x = features(q, texts[q["conv"]], size)
        gold = set(q["evidence"])
        pos = [i for i, t in enumerate(pool) if t in gold]
        if not pos:
            continue
        fused = np.argsort(-(x[:, 0] + x[:, 1]), kind="stable")
        neg = [i for i in fused if pool[i] not in gold][:30]
        for p in pos:
            pos_all.append(np.repeat(x[[p]][:, columns], len(neg), 0))
            neg_all.append(x[neg][:, columns])
    return np.concatenate(pos_all), np.concatenate(neg_all)


def evaluate(questions, texts, size, scorer, columns):
    hits = {5: 0, 10: 0, 30: 0}
    for q in questions:
        pool, x = features(q, texts[q["conv"]], size)
        s = scorer(x[:, columns])
        order = [pool[i] for i in sorted(range(len(pool)), key=lambda i: (-s[i], i))]
        first = next((i for i, t in enumerate(order) if t in set(q["evidence"])), None)
        for k in hits:
            hits[k] += first is not None and first < k
    return {k: v / len(questions) for k, v in hits.items()}


def main() -> None:
    lc.background()
    train_data = json.loads(lc.POOLS.read_text(encoding="utf-8"))
    train_q = [q for q in train_data["questions"] if q["evidence"]]
    convs = sorted({q["conv"] for q in train_q})
    shipped_cols = [NAMES.index(n) for n in SHIPPED]
    all_cols = list(range(len(NAMES)))
    prior = np.zeros(len(NAMES))
    prior[:2] = 1
    # models per held-out conversation, trained at memory 1x, pool 50
    models = {}
    for c in convs:
        rest = [q for q in train_q if q["conv"] != c]
        lin5 = fit_linear(pairs(rest, train_data["turns"], 50, shipped_cols), prior[shipped_cols])
        lin = fit_linear(pairs(rest, train_data["turns"], 50, all_cols), prior)
        pos, neg = pair_arrays(rest, train_data["turns"], 50, all_cols)
        mean, std = pos.mean(0), pos.std(0) + 1e-6
        net = Net(len(all_cols)).fit((pos - mean) / std, (neg - mean) / std)
        models[c] = {"shipped": (lambda x, w=lin5: x @ w, shipped_cols),
                     "linear+new": (lambda x, w=lin: x @ w, all_cols),
                     "network+new": (lambda x, n=net, m=mean, s=std: n.score((x - m) / s),
                                     all_cols)}
        print("trained without", c, "linear weights:",
              {n: round(float(v), 2) for n, v in zip(NAMES, lin)}, flush=True)
    print(f"\n{'memory':<8}{'pool':>5}  {'model':<13}{'hit@5':>8}{'hit@10':>8}{'hit@30':>8}")
    results = {}
    for label, name in (("x1", "pools-pool200.json"), ("x10", "pools-mix9-pool200.json")):
        data = json.loads(lc.POOLS.with_name(name).read_text(encoding="utf-8"))
        qs = [q for q in data["questions"] if q["evidence"]]
        for size in (50, 100):
            fused = evaluate(qs, data["turns"], size, lambda x: x[:, 0] + x[:, 1], all_cols)
            rows = {"fusion": fused}
            for arm in ("shipped", "linear+new", "network+new"):
                total = {5: 0.0, 10: 0.0, 30: 0.0}
                for c in convs:
                    mine = [q for q in qs if q["conv"] == c]
                    scorer, cols = models[c][arm]
                    m = evaluate(mine, data["turns"], size, scorer, cols)
                    for k in total:
                        total[k] += m[k] * len(mine) / len(qs)
                rows[arm] = total
            for arm, m in rows.items():
                print(f"{label:<8}{size:>5}  {arm:<13}{m[5]:>8.1%}{m[10]:>8.1%}{m[30]:>8.1%}",
                      flush=True)
            results[f"{label}/{size}"] = rows
    lc.POOLS.with_name("rerank_v2.json").write_text(json.dumps(results, indent=1) + "\n",
                                                     encoding="utf-8")


if __name__ == "__main__":
    main()
