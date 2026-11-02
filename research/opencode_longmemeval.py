"""Does adding the memory to every message (the OpenCode plugin) make answers better, for weak
and strong models? LongMemEval questions asked through real OpenCode, three ways:

    tools   no injection: the model has the memory tools and must think of calling them
    small   the plugin as shipped: 8 items, 4,000 characters added before the model answers
    large   30 items, 15,000 characters (brain_context's size)

Each question's haystack goes into a throwaway memory, as the capture service would store it
(every turn, verbatim, embedded); each arm gets its own copy. The answer is graded by the
official LongMemEval judge (gpt-4o). The user's own memory and OpenCode settings are not touched.

    OPENAI_API_KEY=... python opencode_longmemeval.py --model opencode/nemotron-3.5-lightning-free
    OPENAI_API_KEY=... python opencode_longmemeval.py --model openai/gpt-5-mini
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "bench"), str(HERE.parent / "src")]

from evidence import DATA, TUNED, fill  # noqa: E402
import official_judge_v073 as judge  # noqa: E402
from opencode_models import session  # noqa: E402

OUT = HERE / "runs/graph-v9.5-opencode-injection"
ARMS = {"tools": None, "small": "8,4000", "large": "30,15000"}
TYPES = {"multi-session": 6, "temporal-reasoning": 4, "knowledge-update": 4,
         "single-session-user": 3, "single-session-preference": 3}


def questions(seed=7):
    rows = json.loads(DATA.read_text(encoding="utf-8"))
    tuned = set(json.loads(TUNED.read_text(encoding="utf-8"))["question_ids"])
    rng, by_type = random.Random(seed), []
    for kind, n in TYPES.items():
        pool = [r for r in rows if r["question_type"] == kind and r["question_id"] not in tuned
                and not r["question_id"].endswith("_abs")]
        by_type.append(rng.sample(pool, n))
    # types interleaved, so a --limit run still covers each one
    return [q for i in range(max(TYPES.values())) for picked in by_type
            if i < len(picked) for q in [picked[i]]]


def memory_for(row, base):
    """The haystack, stored and embedded once per question; copied for each arm."""
    home = base / "home"
    if not home.exists():
        home.mkdir(parents=True)
        os.environ["TURNBOOK_HOME"] = str(home)
        from turnbook.local_embedder import default_embedder
        from turnbook.mcp_server import configured_db_path
        from turnbook.warmup import warm_turns
        database = configured_db_path()
        fill(database, row)
        embedder = default_embedder(database)
        while warm_turns(database, limit=2048, embedder=embedder):
            pass
        embedder.flush()
    return home


def ask(model, row, arm, home, work):
    config = work / "config" / "opencode"
    config.mkdir(parents=True)
    os.environ.update(XDG_CONFIG_HOME=str(config.parent), TURNBOOK_HOME=str(home))
    from turnbook import install
    install.opencode(home)
    if ARMS[arm] is None:
        (config / "plugins" / "graph-mind.js").unlink()
    else:
        os.environ["TURNBOOK_INJECT"] = ARMS[arm]
    date = row["question_date"].split(" ")[0].replace("/", "-")
    try:
        tools, answer, code, error = session(model, f"(Today is {date}.) {row['question']}",
                                             home, config.parent, work / "dir")
    except subprocess.TimeoutExpired:           # one stuck session must not end the run
        tools, answer, code, error = [], "", 1, "timed out"
    os.environ.pop("TURNBOOK_INJECT", None)
    return {"tools": tools, "answer": answer, "error": error[-300:] if code else None,
            "calls": getattr(session, "calls", []) if not code else []}


def main():
    parser = ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    key = os.environ["OPENAI_API_KEY"]
    out = OUT / args.model.replace("/", "_")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "answers.jsonl"
    done = {(r["question_id"], r["arm"]) for r in map(json.loads, path.open(encoding="utf-8"))} \
        if path.exists() else set()
    cache = Path(tempfile.gettempdir()) / "graph-mind-lme-homes"
    for row in questions()[:args.limit]:
        base = cache / row["question_id"]
        home = memory_for(row, base)
        for arm in ARMS:
            if (row["question_id"], arm) in done:
                continue
            work = Path(tempfile.mkdtemp(prefix="graph-mind-lme-"))
            copy = work / "home"
            shutil.copytree(home, copy)
            (work / "dir").mkdir()
            result = ask(args.model, row, arm, copy, work)
            prompt = judge.get_anscheck_prompt(row["question_type"], row["question"],
                                               row["answer"], result["answer"])
            label = "yes" in judge.judge(prompt, key).lower() if result["answer"] else False
            record = {"question_id": row["question_id"], "type": row["question_type"],
                      "arm": arm, "label": label, **result}
            with path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(row["question_id"], arm, label, flush=True)
            shutil.rmtree(work, ignore_errors=True)
    records = [json.loads(line) for line in path.open(encoding="utf-8")]
    summary = {arm: f"{sum(r['label'] for r in records if r['arm'] == arm)}/"
                    f"{sum(1 for r in records if r['arm'] == arm)}" for arm in ARMS}
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(args.model, summary)


if __name__ == "__main__":
    main()
