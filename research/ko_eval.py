"""Recall on the Korean LoCoMo (locomo_ko.py, locomo_pools.py --ko), memory 1x and 4x, pool 50:
fusion, the shipped re-ranker (0.9.46 weights) and re-ranker v2 learned jointly on English
LoCoMo and LongMemEval (all features but length). Nothing here was trained on Korean.

    python research/ko_eval.py
"""
from __future__ import annotations

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402
import rerank_v2 as v2  # noqa: E402

from turnbook.mcp_server import RERANK  # noqa: E402

V2 = [n for n in v2.NAMES if n != "length"]


def joint_weights() -> np.ndarray:
    loco = json.loads(lc.POOLS.read_text(encoding="utf-8"))
    lme = json.loads(lc.POOLS.with_name("lme_pools.json").read_text(encoding="utf-8"))
    cols = [v2.NAMES.index(n) for n in V2]
    a = v2.pairs([q for q in loco["questions"] if q["evidence"]], loco["turns"], 50, cols)
    b = v2.pairs([q for q in lme["questions"] if q["evidence"]], lme["turns"], 50, cols)
    b = b[np.random.default_rng(0).choice(len(b), len(a))]
    prior = np.zeros(len(cols))
    prior[:2] = 1
    return v2.fit_linear(np.concatenate([a, b]), prior)


def main() -> None:
    lc.background()
    w = joint_weights()
    print("v2 weights:", {n: round(float(x), 3) for n, x in zip(V2, w)})
    shipped = np.array([RERANK[k] for k in ("meaning", "words", "similarity", "both", "overlap")])
    five = list(range(5))
    cols = [v2.NAMES.index(n) for n in V2]
    files = (("ko x1", "pools-ko-pool200.json"), ("ko x4", "pools-ko-mix3-pool200.json"),
             ("en x1 (same 4)", "pools-pool200.json"))
    if "--e5" in sys.argv:
        files = tuple((label + " e5", name.replace("-pool200", "-multilingual-e5-small-pool200"))
                      for label, name in files)
    for label, name in files:
        data = json.loads(lc.POOLS.with_name(name).read_text(encoding="utf-8"))
        qs = [q for q in data["questions"] if q["evidence"]
              and q["conv"] in ("conv-26", "conv-30", "conv-41", "conv-42")]
        for arm, scorer, c in (("fusion", lambda x: x[:, 0] + x[:, 1], five),
                               ("shipped 0.9.46", lambda x: x @ shipped, five),
                               ("v2 joint", lambda x: x @ w, cols)):
            m = v2.evaluate(qs, data["turns"], 50, scorer, c)
            print(f"{label:<16}{len(qs):>5} {arm:<15} {m[5]:.1%} / {m[10]:.1%} / {m[30]:.1%}",
                  flush=True)
    np.save(lc.POOLS.with_name("v2_joint_weights.npy"), w)


if __name__ == "__main__":
    main()
