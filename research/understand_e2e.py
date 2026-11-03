"""End to end: a real agent keeps the brain's understanding with brain_understand.

One LongMemEval_S history goes into a throwaway store as captured turns. Codex (the user's own
CLI, --ephemeral --ignore-user-config, so no real memory or hooks reach it) is connected only to
this checkout's memory server on that store and told to fold everything in: next, work out
the lines, save, until UP_TO_DATE. Then: time, batches, lines, and whether the line that answers
the history's question is there (the question itself is never shown to the agent).

    python understand_e2e.py 07741c45 [--model gpt-6.1-sol]
Output: runs/graph-v9.25-state-layer/e2e/<id>/{report.json, state.jsonl, codex.log}
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))
OUT = HERE / "runs/graph-v9.25-state-layer/e2e"

PROMPT = """Keep this memory's current understanding up to date with the brain_understand tool.
Call brain_understand with action "next" (scope "global"). Read the turns it returns and its
guide, work out the state lines, and call brain_understand with action "save", that batch and
your lines (an empty list when nothing is worth keeping). Repeat until "next" answers
UP_TO_DATE. Do not stop early, do not ask anything, do not use any other tool."""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question_id")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="medium")
    args = parser.parse_args()
    from state_layer_eval import rows

    from turnbook.conversation_memory import ConversationMemoryStore
    row = rows()[args.question_id]
    out = OUT / args.question_id
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="understand-e2e-"))
    db = home / "brain.sqlite"
    turns = 0
    with ConversationMemoryStore(db) as store:
        order = sorted(range(len(row["haystack_sessions"])),
                       key=lambda i: row["haystack_dates"][i])
        for n, i in enumerate(order, 1):
            when = row["haystack_dates"][i]       # "2023/05/20 (Sat) 02:21"
            iso = f"{when[:4]}-{when[5:7]}-{when[8:10]}T{when[-5:]}:00"
            for t, turn in enumerate(row["haystack_sessions"][i]):
                if not turn["content"].strip():
                    continue
                store.record({"turn_id": f"s{n:02d}-{t}", "client": "chat",
                              "client_session_id": f"s{n:02d}", "scope": "global",
                              "role": turn["role"], "content_redacted": turn["content"],
                              "raw_sha256": f"s{n:02d}-{t}", "source_path": f"s{n:02d}",
                              "source_ordinal": t, "happened_at": iso, "known_at": iso})
                turns += 1
    start = "import sys; sys.path.insert(0, %r); from turnbook.mcp_server import main; main()" \
        % str(SRC)
    env = "{" + ",".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in (
        ("TURNBOOK_DB", str(db)), ("TURNBOOK_HOME", str(home)), ("PYTHONUTF8", "1"),
        ("TURNBOOK_UNDERSTAND", "1"))) + "}"
    argv = [shutil.which("codex"), "exec", "--ephemeral", "--ignore-user-config",
            "--skip-git-repo-check", "-s", "read-only", "-C", str(home), "-m", args.model,
            "-c", f'model_reasoning_effort="{args.effort}"',
            "-c", 'approval_policy="never"',
            "-c", "mcp_servers.turnbook.command=" + json.dumps(sys.executable, ensure_ascii=False),
            "-c", "mcp_servers.turnbook.args=" + json.dumps(["-c", start], ensure_ascii=False),
            "-c", "mcp_servers.turnbook.env=" + env,
            "-c", "mcp_servers.turnbook.startup_timeout_sec=120",
            "-c", "mcp_servers.turnbook.tool_timeout_sec=120",
            "-c", 'mcp_servers.turnbook.tools.brain_understand.approval_mode="approve"', "-"]
    began = time.time()
    with (out / "codex.log").open("w", encoding="utf-8") as log:
        done = subprocess.run(argv, input=PROMPT.encode("utf-8"), stdout=log,
                              stderr=subprocess.STDOUT, timeout=3600)
    seconds = round(time.time() - began)
    from turnbook.understanding import Understanding
    with Understanding(db) as u:
        state = u.state("global")
        progress = u._progress("global")
        batches = u.db.execute("SELECT status, COUNT(*) FROM understand_batches "
                               "GROUP BY status").fetchall()
        left = u.next("global")["status"]
    (out / "state.jsonl").write_text("".join(json.dumps(
        {"content": m["content"], "attributes": m["attributes"]}, ensure_ascii=False) + "\n"
        for m in state), encoding="utf-8")
    answer_words = {w for w in str(row["answer"]).lower().split() if len(w) > 3}
    hits = [m["content"] for m in state if answer_words & set(m["content"].lower().split())]
    report = {"question_id": args.question_id, "question": row["question"],
              "answer": row["answer"], "model": args.model, "effort": args.effort,
              "exit": done.returncode, "seconds": seconds, "turns": turns,
              "batches": dict(batches), "progress": progress, "after": left,
              "state_lines": len(state),
              "line_chars_median": sorted(len(m["content"]) for m in state)[len(state) // 2]
              if state else 0,
              "lines_sharing_answer_words": hits[:10]}
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False),
                                     encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    main()
