"""How strict should the per-message hook be about what it adds?

The hook adds up to 4 items to every message, so an unrelated message still gets the 4 nearest
memories: noise the user sees. This measures a similarity floor. LongMemEval_S, 500 questions,
each asked twice:
  own     against the person's own history: the answer turn should get in
  other   against the next person's history: nothing there answers it, so whatever gets in is
          noise
`pools` (CI, vectors cached) writes the 8 best candidates of each with their similarity;
`sweep` (local, seconds) tries floors: keep candidates at or above it, then the first 4.

    gh workflow run cache-merge --ref next -f runs="<full500 caches>" \
        -f research="hook_floor.py pools"
    python research/hook_floor.py sweep

Output: runs/graph-v9.24-hook-floor/{pools.jsonl,sweep.json}.
"""
from pathlib import Path
import json
import os
import sys
import tempfile

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE.parent / "src"))
OUT = HERE / "runs/graph-v9.24-hook-floor"
POOL = 8


def pools():
    from product_recall_eval import fill, iso
    from semantic_grounding_v073 import LocalEmbedder
    import product_answer_eval as pae

    from turnbook.local_embedder import LOCAL_MODEL
    from turnbook.mcp_server import gather
    slug = LOCAL_MODEL.replace("/", "_").replace("@", "_")
    embedder = LocalEmbedder(HERE.parent / "bench/.cache" / f"embed-{slug}.json")
    pae.IDS_FILE = Path("runs/graph-v7.0-full500-30/ids.json")
    rows = pae.questions()
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, \
            (OUT / "pools.jsonl").open("w", encoding="utf-8") as log:
        stores = {}
        for row in rows:
            path = Path(directory) / f"{row['question_id']}.sqlite"
            stores[row["question_id"]] = (path, fill(path, row))
        for i, row in enumerate(rows):
            other = rows[(i + 1) % len(rows)]
            for kind, history in (("own", row), ("other", other)):
                path, wanted = stores[history["question_id"]]
                turns = gather(path, row["question"], as_of=iso(row["question_date"]),
                               limit=POOL, embedder=embedder, share=False)["conversation_turns"]
                items = [{"similarity": t.get("similarity"), "role": t["role"],
                          "answer": kind == "own" and t["turn_id"] in wanted} for t in turns]
                log.write(json.dumps({"question_id": row["question_id"],
                                      "type": row["question_type"], "kind": kind,
                                      "items": items}) + "\n")
    print(f"{len(rows)} questions x 2")


def sweep():
    rows = [json.loads(line) for line in (OUT / "pools.jsonl").open(encoding="utf-8")]
    own = [r for r in rows if r["kind"] == "own"]
    other = [r for r in rows if r["kind"] == "other"]

    def packet(row, floor):
        # a candidate found by words only has no similarity: kept only with no floor
        return [t for t in row["items"]
                if floor is None or (t["similarity"] or 0.0) >= floor][:4]
    result = []
    for floor in [None] + [round(0.30 + 0.02 * k, 2) for k in range(21)]:
        result.append({
            "floor": floor,
            "own_answer_in_packet": round(sum(any(t["answer"] for t in packet(r, floor))
                                              for r in own) / len(own), 4),
            "own_items": round(sum(len(packet(r, floor)) for r in own) / len(own), 2),
            "other_empty": round(sum(not packet(r, floor) for r in other) / len(other), 4),
            "other_items": round(sum(len(packet(r, floor)) for r in other) / len(other), 2)})
    (OUT / "sweep.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    for line in result:
        print(line)


if __name__ == "__main__":
    {"pools": pools, "sweep": sweep}[sys.argv[1]]()
