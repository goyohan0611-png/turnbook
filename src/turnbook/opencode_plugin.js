// Turnbook for OpenCode, written by turnbook-install (rewritten on every install).
//
// 1. Every user message and every finished assistant reply is appended, as said, to a transcript
//    the capture service reads like Claude Code's: masked, stored, searchable by meaning, shared
//    with the user's other agents. Nothing else: no tool calls, no reasoning, except a mark
//    where brain_forget ran, so the conversation about deleting is not kept either.
// 2. Before the model answers a message, the memory that bears on it is added to the system
//    prompt (python -m turnbook.inject), so every model gets the past, not only the ones that
//    think to call brain_context.
// Any failure is silent: memory must never stand in the way of a message.
import { appendFileSync, closeSync, fchmodSync, mkdirSync, openSync } from "node:fs";
import { join } from "node:path";
import { execFile } from "node:child_process";

const PYTHON = __PYTHON__;
const TRANSCRIPTS = __TRANSCRIPTS__;
const INJECT_TIMEOUT_MS = 5000;     // the Python side stops itself after 3 s

function write(sessionID, record) {
  try {
    mkdirSync(TRANSCRIPTS, { recursive: true, mode: 0o700 });   // what was typed: this user only
    const fd = openSync(join(TRANSCRIPTS, sessionID + ".jsonl"), "a", 0o600);
    try {   // a file made before 0.9.14 kept its 0644: the mode above applies only to new ones
      if (process.platform !== "win32") fchmodSync(fd, 0o600);
      appendFileSync(fd, JSON.stringify(record) + "\n");
    } finally {
      closeSync(fd);
    }
  } catch {}
}

function recall(text) {
  return new Promise((resolve) => {
    try {
      // -c without the current folder: Python 3.10 ignores PYTHONSAFEPATH
      const child = execFile(PYTHON, ["-c",
        "import sys; sys.path[:] = [p for p in sys.path if p]; " +
        "import runpy; runpy.run_module('turnbook.inject', run_name='__main__')"],
        { timeout: INJECT_TIMEOUT_MS, windowsHide: true, maxBuffer: 1 << 20,
          env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONSAFEPATH: "1" } },
        (error, stdout) => resolve(error ? "" : String(stdout || "")));
      child.stdin.end(text);
    } catch {
      resolve("");
    }
  });
}

// OpenCode can run for weeks: keep the newest KEEP ids, oldest out first (insertion order).
// An event about a message 1,000 messages back never comes.
const KEEP = 1000;
function cap(seen) {
  while (seen.size > KEEP) seen.delete(seen.keys().next().value);
}

export const GraphMindPlugin = async ({ directory }) => {
  const pending = new Map();     // sessionID -> memory for the message being answered
  const roles = new Map();       // messageID -> "user" | "assistant"
  const texts = new Map();       // messageID -> Map(partID -> text)
  const written = new Set();     // assistant messageIDs already appended
  return {
    "chat.message": async (input, output) => {
      const text = (output.parts || []).filter((p) => p.type === "text" && !p.synthetic)
        .map((p) => p.text || "").join("\n").trim();
      if (!text) return;
      write(input.sessionID, { type: "user", uuid: output.message?.id || input.messageID,
        sessionId: input.sessionID, cwd: directory, timestamp: new Date().toISOString(),
        message: { content: text } });
      const memory = await recall(text);
      if (memory.trim()) pending.set(input.sessionID, memory);
    },
    // the model called brain_forget: the conversation about deleting is not kept
    // (docs/privacy.md); Claude Code's record of a tool call, which the capture reads as that
    "tool.execute.after": async (input) => {
      if (String(input.tool || "").endsWith("brain_forget")) {
        write(input.sessionID, { type: "assistant", uuid: input.callID, sessionId: input.sessionID,
          cwd: directory, timestamp: new Date().toISOString(),
          message: { content: [{ type: "tool_use", name: input.tool }] } });
      }
    },
    "experimental.chat.system.transform": async (input, output) => {
      const memory = input.sessionID && pending.get(input.sessionID);
      if (memory) {
        output.system.push(memory);
        pending.delete(input.sessionID);
      }
    },
    event: async ({ event }) => {
      try {
        const props = event.properties || {};
        if (event.type === "message.updated" && props.info) {
          const info = props.info;
          roles.set(info.id, info.role);
          cap(roles);
          if (info.role === "assistant" && info.time?.completed && !written.has(info.id)) {
            const text = [...(texts.get(info.id)?.values() || [])].join("\n").trim();
            if (text) {
              written.add(info.id);
              cap(written);
              write(info.sessionID, { type: "assistant", uuid: info.id, sessionId: info.sessionID,
                cwd: directory, timestamp: new Date(info.time.completed).toISOString(),
                message: { content: text } });
            }
            texts.delete(info.id);
          }
        } else if (event.type === "message.part.updated" && props.part?.type === "text") {
          const part = props.part;
          if (part.synthetic || roles.get(part.messageID) === "user") return;
          if (!texts.has(part.messageID)) {
            texts.set(part.messageID, new Map());
            cap(texts);
          }
          texts.get(part.messageID).set(part.id, part.text || "");
        }
      } catch {}
    },
  };
};

export default { id: "graph-mind", server: GraphMindPlugin };
