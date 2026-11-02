"""What the per-message hook attaches once a store holds understood lines, and how fast.

Rebuilds one e2e store (research/understand_e2e.py output): the history's turns plus its saved
state lines, in a throwaway home. Then, for the history's own question and for unrelated
messages, the hook's packet (turnbook.inject.context) and its time. Runs below normal priority.

    python understand_packets.py 07741c45
Output: runs/graph-v9.25-state-layer/e2e/<id>/packets.json
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
UNRELATED = ["write a quicksort in rust", "ㅇㅋ ㄱㄱ", "what's the capital of France?",
             "fix the failing test in test_login.py"]


def main():
    qid = sys.argv[1]
    out = HERE / "runs/graph-v9.25-state-layer/e2e" / qid
    home = Path(tempfile.mkdtemp(prefix="understand-packets-"))
    os.environ["TURNBOOK_HOME"] = str(home)
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass
    from state_layer_eval import rows

    from turnbook.conversation_memory import ConversationMemoryStore
    from turnbook.local_brain import LocalBrainStore
    row = rows()[qid]
    db = home / "brain.sqlite"
    with ConversationMemoryStore(db) as store:
        order = sorted(range(len(row["haystack_sessions"])),
                       key=lambda i: row["haystack_dates"][i])
        for n, i in enumerate(order, 1):
            when = row["haystack_dates"][i]
            iso = f"{when[:4]}-{when[5:7]}-{when[8:10]}T{when[-5:]}:00"
            for t, turn in enumerate(row["haystack_sessions"][i]):
                if turn["content"].strip():
                    store.record({"turn_id": f"s{n:02d}-{t}", "client": "chat",
                                  "client_session_id": f"s{n:02d}", "scope": "global",
                                  "role": turn["role"], "content_redacted": turn["content"],
                                  "raw_sha256": f"s{n:02d}-{t}", "source_path": f"s{n:02d}",
                                  "source_ordinal": t, "happened_at": iso, "known_at": iso})
    lines = [json.loads(x) for x in (out / "state.jsonl").read_text(
        encoding="utf-8").splitlines() if x.strip()]
    with LocalBrainStore(db) as brain:
        for n, line in enumerate(lines):
            attrs = line["attributes"]
            brain.remember({"memory_id": f"state-{n}", "scope": "global",
                            "memory_type": "STATE", "title": line["content"][:60],
                            "content": line["content"], "effective_at": "2023-05-30T00:00:00",
                            "known_at": "2023-05-30T00:00:00", "actor": "agent", "tags": [],
                            "entities": [attrs.get("subject", "")],
                            "supersedes_memory_id": None,
                            "provenance": {"source_type": "understanding", "source_ref": "e2e"},
                            "attributes": attrs, "confidence": 0.7})
    from turnbook import inject
    from turnbook.associative_memory import AssociativeMemoryIndex
    from turnbook.mcp_server import embedder_for
    from turnbook.warmup import warm, warm_turns
    embedder = embedder_for(db)
    began = time.time()
    with AssociativeMemoryIndex(db) as index:        # what the capture service keeps
        index.sync_sources(limit=5000)
    while warm_turns(db, limit=512, embedder=embedder):
        pass
    while warm(db, limit=2000, embedder=embedder):
        pass
    indexed = round(time.time() - began, 1)
    results = []
    for message in [row["question"], *UNRELATED]:
        began = time.time()
        packet = inject.context(message, database=db, embedder=embedder)
        seconds = round(time.time() - began, 2)
        state = [x for x in packet.split("\n") if x.startswith("[STATE")]
        results.append({"message": message, "seconds": seconds, "chars": len(packet),
                        "state_items": len(state),
                        "answer_in_packet": "shoe rack" in packet.lower(),
                        "packet": packet[:1500]})
        print(json.dumps({k: v for k, v in results[-1].items() if k != "packet"},
                         ensure_ascii=False))
    (out / "packets.json").write_text(json.dumps({"indexing_seconds": indexed,
                                                  "results": results}, indent=1,
                                                 ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
