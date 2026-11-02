"""The product's rerank() and research/rerank_v2.py must order a pool identically, or the research
numbers do not describe the product. Same weights, same pools; prints how many orders differ.

    RERANK_STEM=1 python research/rerank_parity.py [pools file] [weights as JSON]
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))
os.environ.setdefault("RERANK_STEM", "1")

import learning_curve as lc  # noqa: E402
import numpy as np  # noqa: E402
import rerank_v2 as v2  # noqa: E402

from turnbook.mcp_server import RERANK, rerank  # noqa: E402

PRODUCT_NAME = {"meaning": "rrf_meaning", "words": "rrf_words"}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "pools.json"
    weights = json.loads(sys.argv[2]) if len(sys.argv) > 2 else RERANK
    data = json.loads(lc.POOLS.with_name(name).read_text(encoding="utf-8"))
    cols = [v2.NAMES.index(PRODUCT_NAME.get(k, k)) for k in weights]
    w = np.array(list(weights.values()))
    differ = checked = 0
    for q in [q for q in data["questions"] if q["evidence"]][:300]:
        texts = data["turns"][q["conv"]]

        def turn(tid, **extra):
            return {"turn_id": tid, "content": texts[tid]["text"],
                    "client_session_id": v2.session_of(tid), **extra}
        meaning = [turn(t, similarity=s) for t, s in q["meaning"][:50]]
        words = [turn(t) for t in q["words"][:50]]
        product = [t["turn_id"] for t in rerank(q["question"], meaning, words, limit=30,
                                                weights=weights)]
        pool, x = v2.features(q, texts, 50)
        s = x[:, cols] @ w
        research = [pool[i] for i in sorted(range(len(pool)), key=lambda i: (-s[i], i))][:30]
        checked += 1
        differ += product != research
    print(f"{name}: {differ} of {checked} orders differ")


if __name__ == "__main__":
    main()
