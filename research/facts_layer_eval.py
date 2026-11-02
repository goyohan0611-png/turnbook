"""Does the facts layer help, or hurt? The same questions answered without and with facts.

In real use the model the user talks to records a fact with `about` and `attribute` as the user
mentions it. Here gpt-5-mini plays that model: it reads each haystack's user turns in order, ten
sessions at a time, sees the facts it recorded so far (as brain_context would show them), and
records new ones the way brain_remember's docstring asks. The facts are saved dated to their
session, then the shipped packet, reader and official judge run twice over the same questions:
off (conversation only, as today) and on (conversation plus facts).

Only the question types the layer is for: knowledge-update (78, the value changed) and
single-session-preference (30). Knowledge-update already scores 68/72 on the full-500 run, so the
question is mostly whether wrongly recorded facts make answers worse.

    OPENAI_API_KEY=... python facts_layer_eval.py extract    # the stand-in model records facts
    python facts_layer_eval.py spans                          # packets off and on (offline)
    OPENAI_API_KEY=... python facts_layer_eval.py answer     # reader + judge, off vs on
"""
import os
import sys
import tempfile
from pathlib import Path

# Never the developer's own memory: a shared folder in the real config would be synced into every
# temporary store and its lines would reach the packets sent to the reader.
os.environ["TURNBOOK_HOME"] = tempfile.mkdtemp(prefix="graph-mind-eval-home-")
for _name in ("TURNBOOK_CONFIG", "TURNBOOK_FOLDER", "TURNBOOK_DB"):
    os.environ.pop(_name, None)

import json                                                  # noqa: E402
import uuid                                                  # noqa: E402

import longmemeval_retrieval_executor as pipe                # noqa: E402
import product_answer_eval as pae                            # noqa: E402
from turnbook.local_brain import LocalBrainStore, fact_key  # noqa: E402
from product_recall_eval import iso                          # noqa: E402

OUT = Path("runs/graph-v9.1-facts-layer")
SOURCE = Path("runs/graph-v7.0-full500-30/questions.json")
TYPES = ("knowledge-update", "single-session-preference")
CHUNK = 10                                                   # sessions per call
PROMPT = """You are an assistant with a memory tool. Below are the user's messages from past \
conversations, each under its session number and date. Record every fact about the user that can \
change later: where they live or work, people and pets, possessions, preferences, habits, plans, \
numbers they track, what they use or bought. Same rules as the tool:
- content: the user's own sentence, verbatim or nearly.
- about: who or what ("me" for the user, else a name like "Kongi" or "my sister").
- attribute: which property, a short English snake_case name ("home_city", "favorite_coffee").
  Reuse a name from the facts already recorded when it is the same property, so a new value \
replaces the old one. Never put the value in the name: "last_family_trip", not \
"last_family_trip_paris", or the next trip will not replace it.
- session: the session number it was said in.
Skip small talk, questions to the assistant and general knowledge."""
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["facts"],
          "properties": {"facts": {"type": "array", "items": {
              "type": "object", "additionalProperties": False,
              "required": ["session", "about", "attribute", "content"],
              "properties": {"session": {"type": "integer"}, "about": {"type": "string"},
                             "attribute": {"type": "string"}, "content": {"type": "string"}}}}}}


def selected():
    rows = json.loads(SOURCE.read_text(encoding="utf-8"))
    return [r["question_id"] for r in rows if r["question_type"] in TYPES]


def extract():
    key = os.environ["OPENAI_API_KEY"]
    OUT.mkdir(parents=True, exist_ok=True)
    ids = selected()
    (OUT / "ids.json").write_text(json.dumps({"question_ids": ids}, indent=1), encoding="utf-8")
    pae.IDS_FILE = OUT / "ids.json"
    path = OUT / "facts.jsonl"
    done = pipe.finished(pipe.load_jsonl(path, "question_id")) if path.exists() else set()

    def send(row):
        facts, usage = [], {}
        sessions = list(zip(row["haystack_dates"], row["haystack_sessions"]))
        for start in range(0, len(sessions), CHUNK):
            text = "\n\n".join(
                f"## Session {start + i} ({date})\n" + "\n".join(
                    "- " + t["content"] for t in turns if t["role"] == "user" and t["content"].strip())
                for i, (date, turns) in enumerate(sessions[start:start + CHUNK]))
            known = sorted({f"{f['about']} | {f['attribute']}" for f in facts})
            body = {"model": pipe.READER_MODEL, "store": False, "reasoning": {"effort": "low"},
                    "input": [{"role": "system", "content": PROMPT},
                              {"role": "user", "content": "Facts already recorded (about | "
                               "attribute):\n" + ("\n".join(known) or "none") + "\n\n" + text}],
                    "text": {"format": {"type": "json_schema", "name": "facts", "strict": True,
                                        "schema": SCHEMA}},
                    "max_output_tokens": 8000}
            record = pipe.safe_call(key, body, {}, lambda raw: json.loads(pipe.response_text(raw)))
            if record["status"] != "ok":
                return {"question_id": row["question_id"], "status": record["status"]}
            facts += [f for f in record["result"]["facts"] if start <= f["session"] < start + CHUNK]
            for name, value in record.get("usage", {}).items():
                if isinstance(value, int):
                    usage[name] = usage.get(name, 0) + value
        return {"question_id": row["question_id"], "status": "ok", "facts": facts, "usage": usage}

    pipe.run_parallel([r for r in pae.questions() if r["question_id"] not in done], path, 8, send)


def spans():
    facts = {r["question_id"]: r["facts"] for r in pipe.load_jsonl(OUT / "facts.jsonl",
                                                                    "question_id").values()
             if r["status"] == "ok"}
    original = pae.fill

    def fill_with_facts(store, row):
        wanted = original(store, row)
        with LocalBrainStore(store) as brain:
            for fact in facts[row["question_id"]]:
                sid = row["haystack_session_ids"][fact["session"]]
                when = iso(row["haystack_dates"][fact["session"]])
                brain.remember({
                    "memory_id": "fact-" + uuid.uuid4().hex, "scope": "global",
                    # the packet's speaker is the title's first word: "fact", not "I"
                    "memory_type": "fact", "title": "fact " + fact["content"][:75],
                    "content": fact["content"], "effective_at": when, "known_at": when,
                    "actor": "assistant", "tags": [], "entities": [fact["about"]],
                    "provenance": {"source_type": "mcp-client", "source_ref": sid + ":fact"},
                    "supersedes_memory_id": None,
                    "attributes": {"fact": fact_key(fact["about"], fact["attribute"])}})
        return wanted

    cache = str(OUT / "embed-cache-local.json")
    for arm, fill in (("off", original), ("on", fill_with_facts)):
        pae.fill = fill
        run(["spans", "--out", str(OUT / arm), "--writable", "--cache", cache])
    pae.fill = original


def answer():
    run(["answer", "--out", str(OUT / "off")])
    pae.BASELINE = OUT / "off" / "official-judge.jsonl"      # "on" is compared with "off"
    run(["answer", "--out", str(OUT / "on")])


def run(argv):
    sys.argv = ["product_answer_eval.py", *argv, "--ids", str(OUT / "ids.json")]
    pae.main()


if __name__ == "__main__":
    {"extract": extract, "spans": spans, "answer": answer}[sys.argv[1]]()
