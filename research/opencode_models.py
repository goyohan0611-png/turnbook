"""Which models work with Graph-MIND through OpenCode? Each one, in two fresh sessions:

    1. the user mentions a fact in passing (no "remember this"): does the model save it?
    2. a new session asks about it: does the model look it up, and answer right?

OpenCode connects the memory server; the model only sees the tools and the instructions file the
installer writes. Every run uses a throwaway memory (TURNBOOK_HOME) and a throwaway OpenCode
config (XDG_CONFIG_HOME), so neither the user's memory nor their OpenCode settings are touched.
Signed-in providers and their models are the user's own (OpenCode's free models cost nothing).

    python opencode_models.py opencode/big-pickle opencode/deepseek-v4.1-flash-free
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


TELL = ("Quick question about dog food portions. By the way, our golden retriever Kongi turned "
        "three last week and he's about 30 kg now. How much dry food should he get per day?")
ASK = "What breed is my dog, and what's his name?"
OPENCODE = shutil.which("opencode.cmd") or shutil.which("opencode")   # npm on Windows: a .cmd


def session(model, prompt, home, config, work):
    env = {**os.environ, "TURNBOOK_HOME": str(home), "XDG_CONFIG_HOME": str(config),
           "PYTHONUTF8": "1"}
    env.pop("TURNBOOK_DB", None)
    run = subprocess.run([OPENCODE, "run", "-m", model, "--format", "json", "--dir", str(work),
                          prompt], env=env, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=600)
    tools, text, calls = [], [], []
    for line in run.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") or {}
        if part.get("type") == "tool":
            state = part.get("state") or {}
            tools.append(part.get("tool"))
            calls.append({"tool": part.get("tool"), "input": state.get("input"),
                          "output": str(state.get("output", ""))[:600]})
        elif part.get("type") == "text":
            text.append(part.get("text", ""))
    session.calls = calls
    return tools, " ".join(text), run.returncode, run.stderr[-500:]


def capture(home):
    """What the capture service does on its next pass: the plugin's transcript into the store."""
    from turnbook.conversation_memory import ConversationMemoryStore, OpenCodeConversationCapture
    root = home / "opencode"
    if not root.is_dir():
        return 0
    with ConversationMemoryStore(home / "development.sqlite") as store:
        reader = OpenCodeConversationCapture(root, store, state_path=home / "cursor.json")
        reader.initialize_baseline()
        reader.scan()
        return store.db.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0]


def check(model):
    base = Path(tempfile.mkdtemp(prefix="graph-mind-opencode-"))
    home, config, work = base / "home", base / "config" / "opencode", base / "work"
    for path in (home, config, work):
        path.mkdir(parents=True)
    # what graph-mind-install writes (server, instructions, plugin), into the throwaway config
    os.environ.update(XDG_CONFIG_HOME=str(config.parent), TURNBOOK_HOME=str(home))
    from turnbook import install
    install.opencode(home)
    if not PLUGIN:
        (config / "plugins" / "graph-mind.js").unlink()
    settings = json.loads((config / "opencode.json").read_text(encoding="utf-8"))
    if model.startswith("ollama/"):
        # local models through Ollama, served at OLLAMA_URL (its default context is too short
        # for OpenCode's prompt and the tools: run it with OLLAMA_CONTEXT_LENGTH=16384 or more)
        settings["provider"] = {"ollama": {
            "npm": "@ai-sdk/openai-compatible", "name": "Ollama",
            "options": {"baseURL": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/v1"},
            "models": {model.split("/", 1)[1]: {"name": model.split("/", 1)[1], "tools": True}}}}
    (config / "opencode.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    told, _, code1, err1 = session(model, TELL, home, config.parent, work)
    calls = list(session.calls)
    captured = capture(home)                     # the capture service's pass, between sessions
    asked, answer, code2, err2 = session(model, ASK, home, config.parent, work)
    calls += session.calls
    saved = [t for t in told if t and "brain_remember" in t]
    looked = [t for t in asked if t and ("brain_context" in t or "brain_recall" in t)]
    right = "golden" in answer.lower() and "kongi" in answer.lower()
    result = {"model": model, "saved_unasked": bool(saved), "looked_up": bool(looked),
              "answered": right, "tools_1": told, "tools_2": asked, "answer": answer[:300],
              "calls": calls, "plugin": PLUGIN, "captured_turns": captured}
    if code1 or code2:
        result["error"] = (err1 or err2).strip()[-300:]
    shutil.rmtree(base, ignore_errors=True)
    return result


PLUGIN = True


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "runs/opencode-models.jsonl"
    out.parent.mkdir(exist_ok=True)
    if "--no-plugin" in sys.argv:            # as before the plugin: MCP tools and instructions
        PLUGIN = False
        sys.argv.remove("--no-plugin")
    for model in sys.argv[1:]:
        result = check(model)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        with out.open("a", encoding="utf-8") as log:
            log.write(json.dumps(result, ensure_ascii=False) + "\n")
