"""How far could a small re-ranking model go? The ceiling for a "smarter re-ranker".

A cross-encoder reads the question and each candidate turn together and scores the pair. This
runs cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (multilingual, Apache-2.0 card; trained on mMARCO,
check the data licence before shipping) through onnxruntime, the runtime Turnbook already ships,
on the LoCoMo pools (memory 1x, 50 per search), and reports recall in the first 5/10/30 and the
time per question on this machine. Arms: fusion, the shipped re-ranker, the cross-encoder alone,
and the cross-encoder added to the shipped score.

    python research/cross_rerank.py            # in CI: builds the pools itself (cached vectors)
"""
from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from locomo_pools import CACHE, DATA, conversation, fetch  # noqa: E402
import numpy as np  # noqa: E402

from turnbook.local_embedder import LOCAL_MODEL, LocalEmbedder  # noqa: E402
from turnbook.mcp_server import RERANK, words_of  # noqa: E402

REPO = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
OUT = ROOT / "research/runs/graph-v9.20-learning/cross_rerank.txt"


def load(variant: str):
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer
    import onnxruntime
    tokenizer = Tokenizer.from_file(hf_hub_download(REPO, "tokenizer.json"))
    tokenizer.enable_truncation(256)
    tokenizer.enable_padding()
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    session = onnxruntime.InferenceSession(hf_hub_download(REPO, f"onnx/{variant}"), options,
                                           providers=["CPUExecutionProvider"])
    names = {i.name for i in session.get_inputs()}

    def score(query: str, texts: list[str]) -> np.ndarray:
        out = []
        for start in range(0, len(texts), 32):
            batch = tokenizer.encode_batch([(query, t) for t in texts[start:start + 32]])
            feed = {"input_ids": np.array([b.ids for b in batch], dtype=np.int64),
                    "attention_mask": np.array([b.attention_mask for b in batch], dtype=np.int64)}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.array([b.type_ids for b in batch], dtype=np.int64)
            out.append(session.run(None, feed)[0].reshape(-1))
        return np.concatenate(out) if out else np.zeros(0)
    return score


def shipped(question, texts):
    meaning = {t: (r, s) for r, (t, s) in enumerate(question["meaning"])}
    words = {t: r for r, t in enumerate(question["words"])}
    pool = list(dict.fromkeys([t for t, _ in question["meaning"]] + question["words"]))
    asked = words_of(question["question"])
    scores = []
    for t in pool:
        m, w = meaning.get(t), words.get(t)
        scores.append(RERANK["meaning"] * (60 / (60 + m[0]) if m else 0)
                      + RERANK["words"] * (60 / (60 + w) if w is not None else 0)
                      + RERANK["similarity"] * (m[1] if m else 0)
                      + RERANK["both"] * (m is not None and w is not None)
                      + RERANK["overlap"] * len(asked & words_of(texts[t]["text"]))
                      / max(len(asked), 1))
    return pool, np.array(scores)


def first(order, gold):
    return next((i for i, t in enumerate(order) if t in gold), None)


def main() -> None:
    fetch()
    samples = json.loads(DATA.read_text(encoding="utf-8"))
    slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")
    embedder = LocalEmbedder(CACHE / f"embed-{slug}.json", model=LOCAL_MODEL)
    built = []
    with tempfile.TemporaryDirectory() as directory:
        for sample in samples:
            built.append(conversation(sample, Path(directory), embedder))
            print("pooled", sample["sample_id"], flush=True)
    embedder.cache.flush()
    lines = []
    for variant in ("model_quint8_avx2.onnx", "model.onnx"):
        score = load(variant)
        arms = {a: {5: 0, 10: 0, 30: 0} for a in ("fusion", "shipped", "cross", "shipped+cross")}
        seconds, asked = 0.0, 0
        if True:
            for texts, questions in built:
                for q in questions:
                    if not q["evidence"]:
                        continue
                    gold = set(q["evidence"])
                    pool, base = shipped(q, texts)
                    fused = np.array([(60 / (60 + r) if r is not None else 0) for r in
                                      [next((i for i, (t, _) in enumerate(q["meaning"])
                                             if t == p), None) for p in pool]])
                    fused += np.array([60 / (60 + q["words"].index(p)) if p in q["words"] else 0
                                       for p in pool])
                    start = time.perf_counter()
                    cross = score(q["question"], [texts[t]["text"] for t in pool])
                    seconds += time.perf_counter() - start
                    asked += 1
                    z = (cross - cross.mean()) / (cross.std() + 1e-6)
                    for arm, s in (("fusion", fused), ("shipped", base), ("cross", cross),
                                   ("shipped+cross", base + 0.5 * z)):
                        order = [pool[i] for i in sorted(range(len(pool)),
                                                         key=lambda i: (-s[i], i))]
                        f = first(order, gold)
                        for k in (5, 10, 30):
                            arms[arm][k] += f is not None and f < k
                print(variant, asked, flush=True)
        lines.append(f"\n{variant}: {asked} questions, {seconds / asked * 1000:.0f} ms per "
                     f"question (~{len(pool)} pairs, 2 threads)")
        for arm, h in arms.items():
            lines.append(f"  {arm:<15} hit@5 {h[5] / asked:.1%}  hit@10 {h[10] / asked:.1%}  "
                         f"hit@30 {h[30] / asked:.1%}")
        print("\n".join(lines[-5:]), flush=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
