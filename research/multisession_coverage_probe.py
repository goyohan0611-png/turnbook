"""Why do untuned multi-session questions fail? Offline, free: does the packet hold every answer
session, under a few packet shapes?

The full 500 run (runs/graph-v6.4-full500) scored multi-session 29/32 on the tuned dev2 questions and
41/89 on the rest. On dev2 every packet already held all its answer sessions, so retrieval changes
measured there could not show a gain. Here the untuned multi-session questions are split in two by
sha256 of the id: choices are made on the "tune" half and only then read on the "confirm" half.

    python multisession_coverage_probe.py
"""
from pathlib import Path
import hashlib
import json
import tempfile

from turnbook.mcp_server import gather
from longmemeval_m_fetch import stream_instances
from product_recall_eval import DATA, fill, iso
from turnbook.context_packet import build_context_pack

RUN = Path("runs/graph-v6.4-full500")
CACHE = RUN / "embed-cache-local.json"
SHAPES = {"shipped 20 items / 10k chars": (20, 10000),
          "30 items / 15k chars": (30, 15000),
          "40 items / 20k chars": (40, 20000)}


def half(qid):
    return "tune" if int(hashlib.sha256(qid.encode()).hexdigest(), 16) % 2 == 0 else "confirm"


def main():
    from semantic_grounding_v073 import LocalEmbedder
    LocalEmbedder.FLUSH_EVERY = 10 ** 9
    embedder = LocalEmbedder(CACHE)
    tuned = set(json.loads((RUN / "ids.json").read_text(encoding="utf-8"))["tuned_ids"])
    rows = [r for r in stream_instances(DATA)
            if r["question_type"] == "multi-session" and r["question_id"] not in tuned
            and "_abs" not in r["question_id"]]
    out = {}
    with tempfile.TemporaryDirectory() as directory:
        for position, row in enumerate(rows, 1):
            store = Path(directory) / f"{row['question_id']}.sqlite"
            fill(store, row)
            wanted = set(row["answer_session_ids"])
            record = {"half": half(row["question_id"]), "sessions": len(wanted)}
            for name, (limit, chars) in SHAPES.items():
                recalled = gather(store, row["question"], as_of=iso(row["question_date"]),
                                  limit=limit, embedder=embedder)
                packet = build_context_pack(recalled, max_chars=chars)
                got = {item["provenance"]["source_ref"].rsplit(":", 1)[0]
                       for item in packet["items"]} & wanted
                record[name] = [len(got), packet["estimated_tokens"]]
            out[row["question_id"]] = record
            print(f"\r  {position}/{len(rows)}", end="", flush=True)
    print()
    (RUN / "multisession-coverage.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    for part in ("tune", "confirm"):
        records = [r for r in out.values() if r["half"] == part]
        print(f"{part}: {len(records)} questions")
        for name in SHAPES:
            full = sum(r[name][0] == r["sessions"] for r in records)
            tokens = sorted(r[name][1] for r in records)[len(records) // 2]
            print(f"  {name:30} all evidence in packet {full}/{len(records)}   "
                  f"median packet {tokens} tokens")


if __name__ == "__main__":
    main()
