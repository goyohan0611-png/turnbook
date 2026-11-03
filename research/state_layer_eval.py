"""Does a state layer (the brain's current understanding) answer what raw turns miss?
docs/design/state-layer.md. Every model call goes through the Codex CLI (the user's own agent),
not an API key: consolidator, reader and judge are the same model in both arms, so the
comparison is records vs state+records with everything else fixed.

    python state_layer_eval.py pick          # pilot ids: the 30 shipped misses of the three
                                             # types + 90 random right ones of the same types
    python state_layer_eval.py consolidate   # per haystack, question-blind -> state/<id>.jsonl
    python state_layer_eval.py answer        # both arms -> answers.jsonl
    python state_layer_eval.py judge         # official LongMemEval judge prompt -> judged.jsonl
    python state_layer_eval.py report        # -> report.json
    (each stage resumes: what exists is kept; --jobs N parallel Codex runs, default 3)

Records arm: the shipped packet of the 91.8% run (runs/graph-v9.14-full500-mpnet8-gemma/
spans.jsonl), unchanged. State arm: the same packet plus the top state lines for the question.
The consolidator never sees a question, an answer or LongMemEval's session ids (some name the
answer session): sessions are renamed s01..sNN in date order. It gets the user's turns whole and
the assistant's first 300 characters (context; the questions of these types are about the user).

Codex runs with --ephemeral (no session file, so the capture service does not store this in the
real memory) and --ignore-user-config (no user hooks or MCP servers, so no real memory reaches it).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import collections
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OUT = HERE / "runs/graph-v9.25-state-layer"
BASE = HERE / "runs/graph-v9.14-full500-mpnet8-gemma"
DATA = Path(os.environ.get("LONGMEMEVAL_S",
                           HERE / "external/longmemeval/longmemeval_s_cleaned.json"))
if not DATA.exists():                         # the stable checkout keeps the downloaded file
    DATA = Path("C:/turnbook/research/external/longmemeval/longmemeval_s_cleaned.json")
TYPES = ("multi-session", "temporal-reasoning", "knowledge-update")
MODEL = os.environ.get("STATE_MODEL", "gpt-6.1-sol")
STATE_LINES = 30
ASSISTANT_CHARS = 300

CONSOLIDATE = """You maintain the current understanding of one person's memory, built from their
chat sessions with an assistant. Read the sessions below (oldest first; each line starts with
[session:turn]) and write what is worth knowing about the USER as state lines, one JSON object
per line, nothing else:

{"subject": "...", "kind": "value", "attribute": "...", "value": "...", "as_of": "YYYY-MM-DD",
 "replaced": [["older value", "YYYY-MM-DD"]], "sources": ["s03:2"]}
   a current value of something that can change (a job, a count, a preference, a plan, where
   something is); when a later session changes it, keep one line with the latest value and the
   older ones in "replaced", with the dates they were said.
{"subject": "...", "kind": "event", "value": "...", "date": "YYYY-MM-DD", "sources": [...]}
   something that happened, with the date it happened (work it out from the session date when
   the user says "yesterday", "last week", "two months ago"; give the session date if unknown).
{"subject": "...", "kind": "item", "value": "...", "sources": [...]}
   one member of a group that spans sessions (things bought, places visited, people met, books
   read, projects): one line each, so they can be counted and compared later.

Rules: only what the user says about themselves, their life and their plans (not the assistant's
suggestions, unless the user says they did it). Concrete words, numbers and names as the user
said them. Every line cites the [session:turn] it came from. Dates as YYYY-MM-DD. Be complete
about the user's facts: a question about any of them may come later. Output JSON lines only."""

READ_STATE = ("state_lines are facts already worked out from the same conversations (latest "
              "values with their history, dated events, members of groups), each citing the "
              "sessions it came from. Use them together with evidence_passages; when they "
              "disagree, the passages are the user's own words.")


def load(path, key="question_id"):
    return {row[key]: row for row in load_all(path)}


def append(path, row):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def codex(prompt, effort="high"):
    """One Codex run, prompt on stdin, its final message back."""
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "out.txt"
        argv = [shutil.which("codex"), "exec", "--ephemeral", "--ignore-user-config",
                "--skip-git-repo-check", "-s", "read-only", "-C", folder, "-m", MODEL,
                "-c", f'model_reasoning_effort="{effort}"', "-o", str(out), "-"]
        done = subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True,
                              timeout=1800)
        if done.returncode != 0 or not out.exists():
            raise RuntimeError(done.stderr.decode("utf-8", "replace")[-500:])
        return out.read_text(encoding="utf-8")


def rows():
    return {r["question_id"]: r for r in json.loads(DATA.read_text(encoding="utf-8"))}


def pick(args):
    judged = load(BASE / "official-judge.jsonl")
    wrong = sorted(q for q, j in judged.items() if j["type"] in TYPES and not j["label"])
    right = sorted(q for q, j in judged.items() if j["type"] in TYPES and j["label"])
    random.Random(0).shuffle(right)
    OUT.mkdir(parents=True, exist_ok=True)
    chosen = {"shipped_wrong": wrong, "shipped_right": sorted(right[:90]), "seed": 0,
              "types": TYPES, "source": str(BASE.relative_to(HERE))}
    (OUT / "ids.json").write_text(json.dumps(chosen, indent=1), encoding="utf-8")
    print(f"{len(wrong)} shipped misses + 90 right ones")


def ids():
    chosen = json.loads((OUT / "ids.json").read_text(encoding="utf-8"))
    return chosen["shipped_wrong"] + chosen["shipped_right"]


def haystack(row):
    """The sessions as the consolidator sees them: renamed, dated, oldest first."""
    order = sorted(range(len(row["haystack_sessions"])), key=lambda i: row["haystack_dates"][i])
    lines, names = [], {}
    for n, i in enumerate(order, 1):
        name = f"s{n:02d}"
        names[name] = row["haystack_session_ids"][i]
        lines.append(f"\n## {name} ({row['haystack_dates'][i]})")
        for t, turn in enumerate(row["haystack_sessions"][i]):
            text = " ".join(turn["content"].split())
            if turn["role"] != "user":
                text = text[:ASSISTANT_CHARS] + ("…" if len(text) > ASSISTANT_CHARS else "")
            lines.append(f"[{name}:{t}] {turn['role']}: {text}")
    return "\n".join(lines), names


def consolidate(args):
    data, folder = rows(), OUT / "state"
    folder.mkdir(parents=True, exist_ok=True)
    todo = [q for q in ids() if not (folder / f"{q}.jsonl").exists()]

    def work(qid):
        text, names = haystack(data[qid])
        try:
            reply = codex(CONSOLIDATE + "\n\n# Sessions\n" + text)
        except Exception as error:
            print(qid, "failed:", str(error)[:200], flush=True)
            return
        lines = []
        for line in reply.splitlines():
            line = line.strip().strip(",")
            if line.startswith("{"):
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    pass
        (folder / f"{qid}.names.json").write_text(json.dumps(names), encoding="utf-8")
        (folder / f"{qid}.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        print(qid, len(lines), "state lines", flush=True)
    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(work, todo))


def product(args):
    """The shipped layer instead of the CONSOLIDATE prompt: a real agent keeps each history with
    brain_understand (understand_e2e.py, question-blind), its lines become state/<id>.jsonl."""
    folder = OUT / "state"
    folder.mkdir(parents=True, exist_ok=True)
    chosen = json.loads((OUT / "ids.json").read_text(encoding="utf-8"))
    todo = [q for q in chosen["shipped_wrong"] + chosen["shipped_right"][:args.right]
            if not (folder / f"{q}.jsonl").exists()]

    def work(qid):
        e2e = OUT / "e2e" / qid / "state.jsonl"
        report = e2e.with_name("report.json")
        if not report.exists() or json.loads(report.read_text(encoding="utf-8")).get(
                "after") != "UP_TO_DATE":
            e2e.unlink(missing_ok=True)              # not folded in to the end: run it again
        if not e2e.exists():
            done = subprocess.run([sys.executable, str(HERE / "understand_e2e.py"), qid,
                                   "--model", MODEL],
                                  capture_output=True, timeout=3600)
            if done.returncode != 0 or not e2e.exists() or json.loads(report.read_text(
                    encoding="utf-8")).get("after") != "UP_TO_DATE":
                print(qid, "failed:", done.stderr.decode("utf-8", "replace")[-300:], flush=True)
                return
        lines = []
        for raw in e2e.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                line = json.loads(raw)
                attrs = line["attributes"]
                lines.append({"subject": attrs.get("subject"), "line": line["content"],
                              **{k: attrs[k] for k in ("kind", "status", "date", "planned_for")
                                 if attrs.get(k)}})
        (folder / f"{qid}.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        print(qid, len(lines), "state lines", flush=True)
    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(work, todo))


STOP = set("the a an of to in on for and or is was were did do does what which who when how many "
           "much my i me you your it this that with at by from have has had be been are as".split())


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in STOP and len(w) > 2}


def state_for(qid, question):
    """The state lines that share the most words with the question (subject counts double).
    ponytail: word overlap, not the product's hybrid search; a lower bound for the layer."""
    path = OUT / "state" / f"{qid}.jsonl"
    if not path.exists():
        return None
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    asked = words(question)

    def score(line):
        return 2 * len(asked & words(line.get("subject"))) + len(asked & words(json.dumps(line)))
    ranked = sorted(lines, key=score, reverse=True)
    return [x for x in ranked if score(x) > 0][:STATE_LINES]


def answer(args):
    import longmemeval_retrieval_executor as pipe
    data, spans = rows(), load(BASE / "spans.jsonl")
    path = OUT / "answers.jsonl"
    done = {(r["question_id"], r["arm"]) for r in load_all(path)}
    jobs = []
    for qid in ids():
        if not (OUT / "state" / f"{qid}.jsonl").exists():
            continue                                # only histories the layer was built for
        for arm in ("records", "state"):
            if (qid, arm) not in done:
                jobs.append((qid, arm))

    def work(job):
        qid, arm = job
        row = data[qid]
        supplied = {"question_date": row["question_date"], "question": row["question"],
                    "evidence_passages": spans[qid]["passages"]}
        system = pipe.ANSWER_PROMPT
        if arm == "state":
            lines = state_for(qid, row["question"])
            if lines is None:
                return
            supplied["state_lines"] = lines
            system += "\n" + READ_STATE
        prompt = (system + "\n\nReply with one JSON object {\"answer\": \"...\", \"abstained\": "
                  "true|false} and nothing else.\n\n" + json.dumps(supplied, ensure_ascii=False))
        try:
            reply = codex(prompt, effort="low")
        except Exception as error:
            print(qid, arm, "failed:", str(error)[:200], flush=True)
            return
        match = re.search(r"\{.*\}", reply, re.S)
        try:
            parsed = json.loads(match[0]) if match else {"answer": reply}
        except ValueError:
            parsed = {"answer": reply}
        append(path, {"question_id": qid, "arm": arm, "answer": parsed.get("answer", ""),
                      "abstained": parsed.get("abstained"),
                      "state_lines": len(supplied.get("state_lines", [])),
                      "chars": len(json.dumps(supplied, ensure_ascii=False))})
        print(qid, arm, "answered", flush=True)
    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(work, jobs))


def load_all(path):
    if not Path(path).exists():
        return []
    text = Path(path).read_text(encoding="utf-8")      # not splitlines: U+2028 in a text
    return [json.loads(x) for x in text.split(chr(10)) if x.strip()]


def judge(args):
    from official_judge_v073 import get_anscheck_prompt
    data = rows()
    path = OUT / "judged.jsonl"
    done = {(r["question_id"], r["arm"]) for r in load_all(path)}
    todo = [a for a in load_all(OUT / "answers.jsonl")
            if (a["question_id"], a["arm"]) not in done]

    def work(item):
        row = data[item["question_id"]]
        qtype = row["question_type"] + ("_abs" if row["question_id"].endswith("_abs") else "")
        prompt = get_anscheck_prompt(qtype, row["question"], row["answer"], item["answer"])
        try:
            reply = codex(prompt, effort="low")
        except Exception as error:
            print(item["question_id"], "judge failed:", str(error)[:200], flush=True)
            return
        append(path, {"question_id": item["question_id"], "arm": item["arm"],
                      "type": row["question_type"], "label": "yes" in reply.lower(),
                      "raw": reply.strip()[:40]})
    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(work, todo))


def report(args):
    chosen = json.loads((OUT / "ids.json").read_text(encoding="utf-8"))
    judged = {(r["question_id"], r["arm"]): r for r in load_all(OUT / "judged.jsonl")}
    answers = {(r["question_id"], r["arm"]): r for r in load_all(OUT / "answers.jsonl")}
    result = {"model": MODEL, "note": "pilot; same Codex model consolidates, reads and judges "
              "in both arms; not comparable to the gpt-5-mini/gpt-4o 91.8%"}
    for stratum in ("shipped_wrong", "shipped_right"):
        both = [q for q in chosen[stratum]
                if (q, "records") in judged and (q, "state") in judged]
        by = collections.defaultdict(lambda: [0, 0, 0])
        for q in both:
            t = judged[(q, "records")]["type"]
            by[t][0] += 1
            by[t][1] += judged[(q, "records")]["label"]
            by[t][2] += judged[(q, "state")]["label"]
        result[stratum] = {
            "n": len(both),
            "records_right": sum(judged[(q, "records")]["label"] for q in both),
            "state_right": sum(judged[(q, "state")]["label"] for q in both),
            "gained": sorted(q for q in both if judged[(q, "state")]["label"]
                             and not judged[(q, "records")]["label"]),
            "lost": sorted(q for q in both if judged[(q, "records")]["label"]
                           and not judged[(q, "state")]["label"]),
            "by_type": {t: {"n": n, "records": r, "state": s} for t, (n, r, s) in by.items()}}
    chars = [answers[k]["chars"] for k in answers if k[1] == "state"]
    base = [answers[k]["chars"] for k in answers if k[1] == "records"]
    if chars and base:
        result["packet_chars_median"] = {"records": sorted(base)[len(base) // 2],
                                         "state": sorted(chars)[len(chars) // 2]}
    (OUT / "report.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["pick", "consolidate", "product", "answer", "judge",
                                          "report"])
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--right", type=int, default=90, help="product: right ones to include")
    arguments = parser.parse_args()
    globals()[arguments.stage](arguments)
