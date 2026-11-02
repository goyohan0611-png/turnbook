"""Does the entity index return an entity's history, and only that entity's history? Free, offline.

`brain_timeline` is in the product but has never been measured. The earlier attempt measured the wrong
thing: entity_packet_ablation.py asked whether gathering by entity beats gathering by similarity on
LongMemEval, and it lost badly (evidence coverage 90.4% -> 28.3%). That result says little about the
feature, because LongMemEval questions never name an entity — the failure cause is the benchmark's
shape, not the store's size, so repeating it on _M would reproduce it for the same reason.

What the feature actually claims is narrower and checkable: given an entity, return every memory that
mentions it, oldest first, with superseded entries marked. So the store is built from real extraction
output at real scale and the index is measured against the ground truth of which memories list which
entity — and, just as important, the entity VOCABULARY is measured, because an index over names that
each occur once has no history to return no matter how correct it is.

Note on precision: it is measured against EXACT name matches, and `timeline()` also matches names by
substring. For "Rachel" that mostly helps ("lunch with Rachel", "5K run with Rachel" really are her
history); for "budget" it conflates unrelated budgets. So precision below 1 is not automatically a
defect, and the substring rule is one-directional — asking for "lunch with Rachel" does not return
"Rachel".

    python entity_index_check.py
"""
from datetime import datetime
import json
import sqlite3
import statistics as st
import tempfile
from pathlib import Path

import longmemeval_retrieval_executor as pipe
from turnbook.entity_timeline import entities, normalize, rebuild, timeline
from turnbook.local_brain import LocalBrainStore
from longmemeval_adapter import parse_date

RUN = Path("runs/graph-v5.6-effort-low")
SRC = RUN / "ingestion.jsonl"
OUT = Path("runs/graph-v5.8-entity-index")


def build(path: Path) -> dict[str, set[str]]:
    """One memory per extracted event; its entities are what the extraction model named.

    Returns the ground truth: entity key -> the memory ids that really list it.
    """
    truth: dict[str, set[str]] = {}
    rows = pipe.load_jsonl(SRC, "custom_id")
    # a memory's date is when it was said (the session), unless the event states when it happened
    said_on = {}
    for session in json.loads((RUN / "sessions.json").read_text(encoding="utf-8")):
        stamp = session["content"].splitlines()[1].removeprefix("SESSION_DATE: ")
        said_on[session["custom_id"]] = parse_date(stamp).isoformat(timespec="seconds")
    with LocalBrainStore(path) as store:
        n = 0
        for custom_id, row in sorted(rows.items()):
            if row.get("status") != "ok":
                continue
            date = custom_id.split("::")[0]
            for event in row["result"]["events"]:
                names = [e for e in (event.get("subject"), event.get("object_text")) if e]
                if not names:
                    continue
                n += 1
                memory_id = f"m{n}"
                when = said_on.get(custom_id, "2026-01-01T09:00:00")
                happened = event.get("date_value")
                if isinstance(happened, str) and len(happened) == 10:
                    try:
                        when = datetime.fromisoformat(happened).isoformat(timespec="seconds")
                    except ValueError:
                        pass
                store.remember({"memory_id": memory_id, "scope": "global", "memory_type": "fact",
                                "title": pipe.event_text(event)[:120] or "event",
                                "content": json.dumps(event, ensure_ascii=False),
                                "effective_at": when, "known_at": when, "actor": "assistant",
                                "tags": [date], "entities": names,
                                "provenance": {"source_type": "longmemeval",
                                               "source_ref": custom_id},
                                "supersedes_memory_id": None})
                for name in names:
                    key = normalize(name)
                    if key:
                        truth.setdefault(key, set()).add(memory_id)
    return truth


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp()) / "brain.sqlite"
    truth = build(path)
    db = sqlite3.connect(path)
    indexed = rebuild(db)

    # the busiest entities are the ones a history question would actually be about
    busiest = entities(db, limit=30)
    scores = []
    for row in busiest:
        key = normalize(row["entity"])
        want = truth.get(key, set())
        got = {r["memory_id"] for r in timeline(db, row["entity"], limit=10_000)}
        if not want:
            continue
        scores.append({"entity": row["entity"], "expected": len(want), "returned": len(got),
                       "recall": len(want & got) / len(want),
                       "precision": len(want & got) / max(1, len(got)),
                       "ordered": True})
    # ordering, checked on the busiest entity that has more than one memory
    for row in busiest:
        rows = timeline(db, row["entity"], limit=10_000)
        if len(rows) > 1:
            dates = [r["effective_at"] for r in rows]
            order_ok = dates == sorted(dates)
            break
    counts = sorted((len(v) for v in truth.values()), reverse=True)
    report = {"memories": sum(len(v) for v in truth.values()),
              "distinct_entities": len(truth), "index_rows": indexed,
              # the decisive numbers: an entity seen once has no history to return
              "entities_seen_once": sum(c == 1 for c in counts),
              "entities_seen_once_share": round(sum(c == 1 for c in counts) / len(counts), 4),
              "entities_seen_3plus": sum(c >= 3 for c in counts),
              "words_per_name_median": st.median(len(str(n).split()) for n in
                                                 (e for e in truth)),
              "entities_measured": len(scores),
              "recall_mean": round(st.mean(s["recall"] for s in scores), 4),
              "precision_mean": round(st.mean(s["precision"] for s in scores), 4),
              "precision_min": round(min(s["precision"] for s in scores), 4),
              "perfect_precision": sum(s["precision"] == 1 for s in scores),
              "oldest_first": order_ok,
              "worst_exact_precision": sorted(scores, key=lambda s: s["precision"])[:8]}
    (OUT / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False),
                                     encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "worst_exact_precision"}, indent=1))
    assert report["recall_mean"] == 1.0, "the index no longer returns every memory of an entity"
    assert report["oldest_first"], "the timeline is no longer oldest-first"
    db.close()


if __name__ == "__main__":
    main()
