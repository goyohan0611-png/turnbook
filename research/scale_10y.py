"""Ten years of memory: does recall hold, and how long does a question take?

One store holds all ten LoCoMo conversations (5,882 turns) and then more and more other
conversations (LongMemEval_S haystack sessions, every distinct one, turn ids prefixed), up to the
size a heavy user collects in years. At each size every LoCoMo question goes through the shipped
gather() with its defaults (30 turns), and we record whether an answer turn is in what it returns
and how long the call took. Arms: plain fusion (0.9.45), the 0.9.46 re-ranker, re-ranker v2
(weights in research/runs/graph-v9.20-learning/rerank2_weights.json, if present).

    python research/scale_10y.py            # in CI (bench workflow): needs LongMemEval_S
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "bench"))

from evidence import DATA as LME  # noqa: E402
from evidence import iso  # noqa: E402
from locomo_pools import CACHE, DATA, evidence_ids, fetch, turns_of  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402

from turnbook import mcp_server  # noqa: E402
from turnbook.conversation_memory import ConversationMemoryStore  # noqa: E402
from turnbook.local_embedder import LOCAL_MODEL, LocalEmbedder  # noqa: E402
from turnbook.warmup import warm_turns  # noqa: E402

SIZES = [0, 50_000, 150_000, 400_000]          # other turns added beside LoCoMo's 5,882
if "--big" in sys.argv:                         # only the large sizes, the best arms (time)
    SIZES = [150_000, 400_000]
RUN = ROOT / "research/runs/graph-v9.20-learning"


def arms():
    """name -> (weights or None for plain fusion, candidates per search)"""
    out = {"fusion (0.9.45)": (None, 50), "re-ranker 0.9.46": (dict(mcp_server.RERANK), 50)}
    path = RUN / "rerank2_weights.json"
    if path.exists():
        v2 = json.loads(path.read_text(encoding="utf-8"))
        for pool in (50, 100, 200):
            out[f"v2, {pool} per search"] = (v2, pool)
    if "--big" in sys.argv:
        out = {k: v for k, v in out.items() if k in ("fusion (0.9.45)", "v2, 100 per search")}
    if "--shipped" in sys.argv:                 # the search model's own shipped weights
        out = {"fusion (0.9.45)": (None, 50), "shipped": ("shipped", mcp_server.RERANK_POOL)}
    return out


def distractors():
    """Every distinct LongMemEval_S session, in file order: (session id, date, turns)."""
    seen = set()
    for row in stream_instances(LME):
        for date, sid, turns in zip(row["haystack_dates"], row["haystack_session_ids"],
                                    row["haystack_sessions"]):
            if sid not in seen:
                seen.add(sid)
                yield sid, iso(date), turns


def shard(part: int, parts: int) -> None:
    """Only embed (into the cache) every parts-th session of the 150k-400k tier, so several CI
    jobs can share that tier's embedding; merge their caches (cache-merge) and run the script."""
    slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")
    embedder = LocalEmbedder(CACHE / f"embed-{slug}.json", model=LOCAL_MODEL)
    with tempfile.TemporaryDirectory() as directory:
        store = Path(directory) / "shard.sqlite"
        added = mine = 0
        with ConversationMemoryStore(store) as memory:
            for number, (sid, at, turns) in enumerate(distractors()):
                if added >= SIZES[-1]:
                    break
                take = added >= 150_000 and number % parts == part
                for index, turn in enumerate(turns):
                    if not turn["content"].strip():
                        continue
                    added += 1
                    if take:
                        memory.record({"turn_id": f"lme/{sid}/{index}", "client": "longmemeval",
                                       "client_session_id": f"lme/{sid}", "scope": "global",
                                       "role": turn["role"], "content_redacted": turn["content"],
                                       "raw_sha256": f"{sid}/{index}", "source_path": sid,
                                       "happened_at": at, "known_at": at,
                                       "source_ordinal": mine})
                        mine += 1
        print(f"shard {part}/{parts}: {mine} turns", flush=True)
        began, batches = time.perf_counter(), 0
        while warm_turns(store, limit=4096, embedder=embedder):
            batches += 1
            print(f"  embedded {batches * 4096} in {time.perf_counter() - began:.0f} s", flush=True)
            if batches % 5 == 0:
                embedder.cache.flush()
        embedder.cache.flush()


def main() -> None:
    for arg in sys.argv:
        if arg.startswith("--shard="):                  # --shard=2/6
            return shard(*map(int, arg[8:].split("/")))
    fetch()
    samples = json.loads(DATA.read_text(encoding="utf-8"))
    slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")
    embedder = LocalEmbedder(CACHE / f"embed-{slug}.json", model=LOCAL_MODEL)
    original = mcp_server.rerank
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        store = Path(directory) / "ten-years.sqlite"
        ordinal = 0
        with ConversationMemoryStore(store) as memory:
            for sample in samples:
                for tid, text, at in turns_of(sample):
                    memory.record({"turn_id": f"{sample['sample_id']}/{tid}", "client": "locomo",
                                   "client_session_id": f"{sample['sample_id']}/"
                                   + tid.split(":")[0], "scope": "global", "role": "user",
                                   "content_redacted": text, "raw_sha256": tid,
                                   "source_path": sample["sample_id"], "happened_at": at,
                                   "known_at": at, "source_ordinal": ordinal})
                    ordinal += 1
        others = distractors()
        added = 0
        for size in SIZES:
            began = time.perf_counter()
            with ConversationMemoryStore(store) as memory:
                while added < size:
                    try:
                        sid, at, turns = next(others)
                    except StopIteration:
                        break
                    for index, turn in enumerate(turns):
                        if not turn["content"].strip():
                            continue
                        memory.record({"turn_id": f"lme/{sid}/{index}", "client": "longmemeval",
                                       "client_session_id": f"lme/{sid}", "scope": "global",
                                       "role": turn["role"], "content_redacted": turn["content"],
                                       "raw_sha256": f"{sid}/{index}", "source_path": sid,
                                       "happened_at": at, "known_at": at,
                                       "source_ordinal": ordinal})
                        ordinal += 1
                        added += 1
            stored = time.perf_counter()
            print(f"{5882 + added} turns: stored in {stored - began:.0f} s", flush=True)
            batches = 0
            while warm_turns(store, limit=4096, embedder=embedder):
                batches += 1
                print(f"  embedded {batches * 4096} in {time.perf_counter() - stored:.0f} s",
                      flush=True)
                if batches % 10 == 0:               # keep progress if the job times out
                    embedder.cache.flush()
            embedder.cache.flush()
            print(f"{5882 + added} turns: stored in {stored - began:.0f} s, embedded in "
                  f"{time.perf_counter() - stored:.0f} s", flush=True)
            total = 5882 + added
            for arm, (weights, pool) in arms().items():
                mcp_server.RERANK_POOL = pool
                if weights is None:
                    mcp_server.rerank = lambda q, m, w, *, limit, weights=None: mcp_server.fuse(
                        m, w, limit=limit)
                else:
                    x = None if weights == "shipped" else weights
                    mcp_server.rerank = (lambda q, m, w, *, limit, weights=None, x=x:
                                         original(q, m, w, limit=limit, weights=x))
                hits = {5: 0, 10: 0, 30: 0}
                seconds, asked = [], 0
                for sample in samples:
                    for qa in sample["qa"][::2]:            # every other question: time
                        gold = {f"{sample['sample_id']}/{e}" for e in evidence_ids(qa)}
                        if qa["category"] == 5 or not gold:
                            continue
                        start = time.perf_counter()
                        found = mcp_server.gather(store, qa["question"],
                                                  as_of=datetime.now().isoformat(),
                                                  limit=mcp_server.CONTEXT_LIMIT,
                                                  embedder=embedder)
                        seconds.append(time.perf_counter() - start)
                        order = [t["turn_id"] for t in found["conversation_turns"]]
                        first = next((i for i, t in enumerate(order) if t in gold), None)
                        asked += 1
                        for k in hits:
                            hits[k] += first is not None and first < k
                row = {f"hit@{k}": round(v / asked, 4) for k, v in hits.items()}
                row["ms_median"] = round(statistics.median(seconds) * 1000)
                row["ms_p95"] = round(sorted(seconds)[int(len(seconds) * 0.95)] * 1000)
                results[f"{total}/{arm}"] = row
                print(f"{total:>8} turns  {arm:<18} " + "  ".join(
                    f"{k} {v:.1%}" if k.startswith("hit") else f"{k} {v}"
                    for k, v in row.items()), flush=True)
            mcp_server.rerank, mcp_server.RERANK_POOL = original, 50
            mcp_server._PIECES.pop(str(store), None)
    slug = "" if "--shipped" not in sys.argv else "-" + LOCAL_MODEL.split("@")[0].rsplit("/")[-1]
    (RUN / f"scale_10y{slug}.json").write_text(json.dumps(results, indent=1) + "\n",
                                               encoding="utf-8")


if __name__ == "__main__":
    main()
