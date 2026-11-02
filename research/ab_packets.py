"""LongMemEval_S packets from two versions of recall, for an answer-level A/B (ab_answers.py).

    A  0.9.45: the shipped mpnet model, plain rank fusion, question words as typed
    B  new:    multilingual-e5-small, re-ranker v2 (rerank2_weights_e5.json), Korean particles off

Each question's haystack is stored as bench/evidence.py stores it, once per arm (each arm its own
vectors), and brain_context's packet is built with the shipped defaults. Output: one line per
question per arm with the packet's passages, as product_answer_eval.py writes them.

    python research/ab_packets.py            # in CI (bench workflow)
"""
from __future__ import annotations

from pathlib import Path
import json
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "bench"))

from evidence import CACHE, DATA, fill, iso  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402
from longmemeval_retrieval_executor import asks_assistant_history  # noqa: E402

from turnbook import conversation_memory, mcp_server  # noqa: E402
from turnbook.context_packet import build_context_pack  # noqa: E402
from turnbook.local_embedder import LOCAL_MODEL, LocalEmbedder  # noqa: E402
from turnbook.warmup import warm_turns  # noqa: E402

RUN = ROOT / "research/runs/graph-v9.22-ab"
E5 = "intfloat/multilingual-e5-small@onnx/model.onnx"
ORIGINAL = (mcp_server.rerank, conversation_memory.search_terms)


def old_terms(query):
    return list(dict.fromkeys(re.findall(r"[^\W_]{2,}", query.casefold())))


def arm(name):
    """Set the module state for one arm; returns its embedding model."""
    if name == "A":
        mcp_server.rerank = lambda q, m, w, *, limit, weights=None: mcp_server.fuse(
            m, w, limit=limit)
        conversation_memory.search_terms = old_terms
        return LOCAL_MODEL
    weights = json.loads((ROOT / "research/runs/graph-v9.20-learning/rerank2_weights_e5.json")
                         .read_text(encoding="utf-8"))
    mcp_server.rerank = lambda q, m, w, *, limit, weights=None, x=weights: ORIGINAL[0](
        q, m, w, limit=limit, weights=x)
    conversation_memory.search_terms = ORIGINAL[1]
    return E5


def main() -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    embedders = {}
    for name in ("A", "B"):
        model = arm(name)
        slug = model.replace("/", "_").replace("@", "_")
        embedders[name] = LocalEmbedder(CACHE / f"embed-{slug}.json", model=model)
    with tempfile.TemporaryDirectory() as directory:
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        for name in sys.argv[1] if len(sys.argv) > 1 else "AB":
            path = RUN / f"packets-{name}.jsonl"
            done = {json.loads(line)["question_id"] for line in path.open(encoding="utf-8")} \
                if path.exists() else set()
            arm(name)
            embedder = embedders[name]
            with path.open("a", encoding="utf-8") as log:
                for n, row in enumerate(stream_instances(DATA)):
                    if limit and n >= limit:
                        break
                    if row["question_id"] in done:
                        continue
                    store = Path(directory) / f"{name}-{row['question_id']}.sqlite"
                    fill(store, row)
                    while warm_turns(store, limit=2048, embedder=embedder):
                        pass
                    recalled = mcp_server.gather(
                        store, row["question"], as_of=iso(row["question_date"]),
                        limit=mcp_server.CONTEXT_LIMIT, embedder=embedder,
                        about_assistant=asks_assistant_history(row["question"]))
                    packet = build_context_pack(recalled, max_chars=mcp_server.CONTEXT_CHARS)
                    passages = [{"session_id": item["provenance"]["source_ref"].rsplit(":", 1)[0],
                                 "session_date": item["effective_at"],
                                 "speaker": item["title"].split(" ", 1)[0],
                                 "text": item["content"]} for item in packet["items"]]
                    log.write(json.dumps({"question_id": row["question_id"],
                                          "passages": passages,
                                          "estimated_tokens": packet["estimated_tokens"]},
                                         ensure_ascii=False) + "\n")
                    log.flush()
                    mcp_server._PIECES.pop(str(store), None)
                    store.unlink()
                    print(f"\r  {name} {n + 1}", end="", flush=True)
            embedder.cache.flush()
            print()
    mcp_server.rerank, conversation_memory.search_terms = ORIGINAL


if __name__ == "__main__":
    main()
