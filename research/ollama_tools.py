"""Can a local model use the memory? Graph-MIND's tools handed straight to an Ollama model, the
same two sessions as opencode_models.py, without OpenCode's own ~18,000-token prompt: on a PC
with no GPU that prompt alone took Ollama ~27 minutes per turn (11 tokens/s) and timed out.

This checks the model's own part: does it save a fact it was not told to save, and look it up
in a new session. A throwaway memory (TURNBOOK_HOME); nothing leaves the PC.

    python ollama_tools.py qwen3:4b llama3.2:3b
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from turnbook.mcp_server import instructions  # noqa: E402
from opencode_models import ASK, TELL  # noqa: E402

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
TOOLS = ("brain_remember", "brain_context", "brain_recall")      # what the two sessions need


def chat(model, messages, tools):
    body = json.dumps({"model": model, "messages": messages, "tools": tools, "stream": False,
                       "think": False, "options": {"num_ctx": 8192}}).encode("utf-8")
    request = urllib.request.Request(OLLAMA + "/api/chat", body,
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=1800) as response:
        return json.loads(response.read())["message"]


async def session(model, prompt, home):
    server = StdioServerParameters(command=sys.executable, args=["-m", "turnbook.mcp_server"],
                                   env={**os.environ, "TURNBOOK_HOME": str(home),
                                        "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"})
    calls = []
    async with stdio_client(server) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        tools = [{"type": "function", "function": {"name": t.name,
                                                   "description": t.description,
                                                   "parameters": t.input_schema}}
                 for t in (await client.list_tools()).tools if t.name in TOOLS]
        messages = [{"role": "system", "content": instructions(False)},
                    {"role": "user", "content": prompt}]
        for _ in range(6):
            reply = chat(model, messages, tools)
            messages.append(reply)
            if not reply.get("tool_calls"):
                return calls, reply.get("content", "")
            for call in reply["tool_calls"]:
                name, arguments = call["function"]["name"], call["function"]["arguments"]
                result = await client.call_tool(name, arguments)
                text = "".join(getattr(c, "text", "") for c in result.content)
                calls.append({"tool": name, "input": arguments, "output": text[:400]})
                messages.append({"role": "tool", "content": text[:6000], "tool_name": name})
    return calls, ""


def check(model):
    home = Path(tempfile.mkdtemp(prefix="graph-mind-ollama-"))
    told, _ = asyncio.run(session(model, TELL, home))
    asked, answer = asyncio.run(session(model, ASK, home))
    return {"model": "ollama/" + model,
            "saved_unasked": any(c["tool"] == "brain_remember" for c in told),
            "looked_up": any(c["tool"] in ("brain_context", "brain_recall") for c in asked),
            "answered": "golden" in answer.lower() and "kongi" in answer.lower(),
            "answer": answer[:300], "calls": told + asked, "client": "direct (no OpenCode)"}


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "runs/opencode-models.jsonl"
    for name in sys.argv[1:]:
        result = check(name)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        with out.open("a", encoding="utf-8") as log:
            log.write(json.dumps(result, ensure_ascii=False) + "\n")
