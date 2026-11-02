"""Does gathering by ENTITY beat gathering by similarity? Offline, no API.

The product now indexes entity -> its memories (entity_timeline.py). The claim behind it is that
questions about one thing ("what happened to the auth module", "what did I buy before the air
fryer") need that thing's whole history, which similarity ranking only partly returns.

This measures the claim on dev2 with the labels we already have: what share of the answer-bearing
turns reaches the reader's packet under

    current   top-15 question-relevant events -> their source turns (what ships today)
    entity    events whose subject/object matches an entity the planner named -> their source turns
    union     both

Same packet mechanics either way, so the only difference is how the events were chosen.

    python entity_packet_ablation.py
"""
from pathlib import Path
import json

import longmemeval_retrieval_executor as pipe
from turnbook.entity_timeline import normalize
from longmemeval_adapter import load_instances

RUN = Path("runs/graph-v3.8-dev2-k16")
IDS = Path("runs/graph-v3.2-untouched-eval-v2")
OUT = Path("runs/graph-v4.7-entity-packet")
PACKET = 15


def entity_events(plan: dict, events: list[dict]) -> list[int]:
    """Indices of events that mention one of the planner's entities, oldest first."""
    keys = [normalize(e) for e in (plan.get("entities") or []) if normalize(e)]
    if not keys:
        return []
    hits = []
    for i, event in enumerate(events):
        text = normalize(f"{event.get('subject') or ''} {event.get('object_text') or ''} "
                         f"{event.get('relation_detail') or ''}")
        if any(key in text or text in key for key in keys):
            hits.append(i)
    return sorted(hits, key=lambda i: str(events[i].get("date_value")
                                          or events[i].get("session_date") or ""))


def turns_of(indices, events, sessions, question, cap=PACKET):
    mine = {s["session_id"]: s["content"] for s in sessions
            if s["question_id"] == question["question_id"]}
    out, seen = [], set()
    for i in indices:
        session_id = events[i]["session_id"]
        for turn_id in events[i].get("source_turn_ids") or []:
            if (session_id, turn_id) not in seen and pipe._turn_text(mine.get(session_id, ""), turn_id):
                seen.add((session_id, turn_id))
                out.append((session_id, turn_id))
        if len(out) >= cap:
            break
    return out[:cap]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pipe.OUTPUT = RUN
    ids = set(json.loads((IDS / "preregistration.json").read_text(encoding="utf-8"))["question_ids"])
    questions = json.loads((RUN / "questions.json").read_text(encoding="utf-8"))
    sessions = json.loads((RUN / "sessions.json").read_text(encoding="utf-8"))
    ingested = pipe.load_jsonl(RUN / "ingestion.jsonl", "custom_id")
    plans = {json.loads(l)["question_id"]: (json.loads(l).get("plan") or {})
             for l in (RUN / "plans.jsonl").open(encoding="utf-8")}
    raw = {r["question_id"]: r for r in load_instances(pipe.DATA) if r["question_id"] in ids}

    totals = {"evidence": 0, "current": 0, "entity": 0, "union": 0,
              "questions_with_entities": 0, "questions": 0}
    for question in questions:
        qid = question["question_id"]
        have = {s["session_id"] for s in sessions if s["question_id"] == qid}
        wanted = {(sid, f"TURN_{i:03d}")
                  for sid, turns in zip(raw[qid]["haystack_session_ids"], raw[qid]["haystack_sessions"])
                  if sid in have for i, t in enumerate(turns) if t.get("has_answer")}
        if not wanted:
            continue
        events = pipe.accepted_events(qid, sessions, ingested,
                                      include_assistant=pipe.asks_assistant_history(question["question"]))
        ranked = pipe._rank_events(question, events)[:PACKET]
        by_entity = entity_events(plans.get(qid, {}), events)
        current = set(turns_of(ranked, events, sessions, question))
        entity = set(turns_of(by_entity, events, sessions, question))
        union = set(turns_of(list(dict.fromkeys(list(ranked) + by_entity)), events, sessions,
                             question, cap=PACKET * 2))
        totals["questions"] += 1
        totals["questions_with_entities"] += bool(by_entity)
        totals["evidence"] += len(wanted)
        totals["current"] += len(wanted & current)
        totals["entity"] += len(wanted & entity)
        totals["union"] += len(wanted & union)

    report = {**totals, "coverage": {k: round(totals[k] / totals["evidence"], 4)
                                     for k in ("current", "entity", "union")}}
    (OUT / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
