"""Document retrieval on BEIR (SciFact, NFCorpus, ...): Turnbook against the usual RAG set-ups,
all with the same embedding model, so only the way of searching differs. nDCG@10 (the BEIR
metric) and recall@100, on each dataset's test queries.

  words      SQLite FTS5 BM25, Turnbook's word search alone
  meaning    the embedding alone (what a vector-store RAG does, LlamaIndex's default retriever)
  fusion     both lists, reciprocal rank fusion
  cross      fusion's first 100 re-scored by a cross-encoder (bge-reranker-base): the strong,
             slower RAG recipe
  turnbook   the shipped path: both searches and the learned re-ranker (mcp_server.rerank)

Every document goes in as a project-document section would (documents.py): one turn, role
"document", title and text.

    gh workflow run bench --ref feature/docs -f research="beir_eval.py scifact nfcorpus"
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import io
import json
import math
import sys
import time
import urllib.request
import zipfile

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from turnbook.conversation_memory import ConversationMemoryStore  # noqa: E402
from turnbook.local_embedder import LOCAL_MODEL, LocalEmbedder  # noqa: E402
from turnbook.mcp_server import _PIECES, excerpt, fuse, rerank, semantic_turns  # noqa: E402
from turnbook.warmup import warm_turns  # noqa: E402

DATA = ROOT / "bench/.cache/beir"
CACHE = ROOT / "bench/.cache"
OUT = ROOT / "research/runs/graph-v9.23-beir"
URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{}.zip"
POOL = 100
CROSS = ("Xenova/bge-reranker-base", "onnx/model_quantized.onnx", "tokenizer.json")


def load(name):
    folder = DATA / name
    if not (folder / "corpus.jsonl").exists():
        DATA.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL.format(name), timeout=600) as response:
            zipfile.ZipFile(io.BytesIO(response.read())).extractall(DATA)
    corpus = {}
    for line in (folder / "corpus.jsonl").open(encoding="utf-8"):
        row = json.loads(line)
        corpus[row["_id"]] = (row.get("title") or "", row["text"])
    queries = {json.loads(line)["_id"]: json.loads(line)["text"]
               for line in (folder / "queries.jsonl").open(encoding="utf-8")}
    qrels = {}
    for line in list((folder / "qrels" / "test.tsv").open(encoding="utf-8"))[1:]:
        qid, doc, score = line.rstrip("\n").split("\t")
        if int(score) > 0:
            qrels.setdefault(qid, {})[doc] = int(score)
    return corpus, {q: queries[q] for q in qrels}, qrels


def ndcg10(ranked, relevant):
    gain = sum(relevant.get(d, 0) / math.log2(i + 2) for i, d in enumerate(ranked[:10]))
    best = sum(r / math.log2(i + 2)
               for i, r in enumerate(sorted(relevant.values(), reverse=True)[:10]))
    return gain / best if best else 0.0


def recall100(ranked, relevant):
    return len(set(ranked[:100]) & set(relevant)) / len(relevant)


class CrossEncoder:
    def __init__(self):
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
        import onnxruntime
        repo, model, tok = CROSS
        self.tok = Tokenizer.from_file(hf_hub_download(repo, tok))
        self.tok.enable_truncation(512)
        self.tok.enable_padding()
        self.session = onnxruntime.InferenceSession(hf_hub_download(repo, model),
                                                    providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def scores(self, query, texts, batch=16):
        out = []
        for i in range(0, len(texts), batch):
            enc = self.tok.encode_batch([(query, t) for t in texts[i:i + batch]])
            feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            out.extend(self.session.run(None, feed)[0][:, 0].tolist())
        return out


def run(name, embedder, cross):
    corpus, queries, qrels = load(name)
    store_path = CACHE / f"beir-{name}.sqlite"
    store_path.unlink(missing_ok=True)
    started = time.time()
    with ConversationMemoryStore(store_path) as store:
        for doc, (title, text) in corpus.items():
            content = f"{title}\n\n{text}".strip()
            store.record({"turn_id": f"document-{name}-{doc}", "client": "document",
                          "client_session_id": doc, "scope": "global", "role": "document",
                          "content_redacted": content,
                          "raw_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                          "source_path": doc, "source_ordinal": 0,
                          "happened_at": "2026-01-01T00:00:00", "known_at": "2026-01-01T00:00:00"})
    while warm_turns(store_path, limit=2048, embedder=embedder):
        pass
    embedder.cache.flush()
    print(f"{name}: {len(corpus)} documents, {len(queries)} queries, stored and embedded in "
          f"{time.time() - started:.0f} s", flush=True)
    arms = {a: {"ndcg": [], "recall": []} for a in
            ("words", "meaning", "fusion", "cross", "turnbook")}
    doc_of = {}
    for n, (qid, query) in enumerate(queries.items()):
        with ConversationMemoryStore(store_path) as store:
            try:
                words = [excerpt(t) for t in store.search(query, limit=POOL)["turns"]]
            except ValueError:
                words = []
        meaning = semantic_turns(store_path, query, limit=POOL, embedder=embedder)
        fused = fuse(meaning, words, limit=POOL)
        ranked = {"words": words, "meaning": meaning, "fusion": fused,
                  "turnbook": rerank(query, meaning, words, limit=POOL)}
        for arm, turns in ranked.items():
            for t in turns:
                doc_of[t["turn_id"]] = t["source_path"]
        texts = [" ".join(corpus[doc_of[t["turn_id"]]]) for t in fused]
        if cross and fused:
            order = np.argsort(-np.array(cross.scores(query, texts)), kind="stable")
            ranked["cross"] = [fused[i] for i in order]
        else:
            ranked["cross"] = fused
        for arm, turns in ranked.items():
            docs = list(dict.fromkeys(doc_of[t["turn_id"]] for t in turns))
            arms[arm]["ndcg"].append(ndcg10(docs, qrels[qid]))
            arms[arm]["recall"].append(recall100(docs, qrels[qid]))
        if n % 50 == 0:
            print(f"  {n}/{len(queries)}", flush=True)
    _PIECES.pop(str(store_path), None)
    result = {arm: {"ndcg@10": round(float(np.mean(v["ndcg"])), 4),
                    "recall@100": round(float(np.mean(v["recall"])), 4)}
              for arm, v in arms.items()}
    for arm, m in result.items():
        print(f"  {name:<10} {arm:<9} nDCG@10 {m['ndcg@10']:.3f}  recall@100 "
              f"{m['recall@100']:.3f}", flush=True)
    return result


def main():
    names = sys.argv[1:] or ["scifact", "nfcorpus"]
    slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")
    embedder = LocalEmbedder(CACHE / f"embed-beir-{slug}.json", model=LOCAL_MODEL)
    cross = CrossEncoder()
    results = {"model": LOCAL_MODEL, "cross_encoder": CROSS[0]}
    for name in names:
        results[name] = run(name, embedder, cross)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "beir.json").write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
