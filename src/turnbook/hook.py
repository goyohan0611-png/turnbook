"""Hooks for agents that run a command per message but keep no transcript Turnbook can read
(Gemini CLI; Cursor's and Copilot CLI's own transcripts are read instead):
what was said is appended, in Claude Code's record shape, to ~/.turnbook/hooks/<agent>/, and
the capture service stores it like every other conversation. Where the hook can add context
(Gemini CLI's BeforeAgent), the memory that bears on the message goes in before the model
answers, as with the OpenCode plugin.

    python -m turnbook.hook gemini-cli BeforeAgent      # stdin: the agent's hook JSON

turnbook-install registers these (install.agent_hooks). Any failure is silent: memory must
never stand in the way of a message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys
import uuid

# agent -> event -> (who said it, the field that holds the text)
EVENTS = {
    "gemini-cli": {"BeforeAgent": ("user", "prompt"),
                   "AfterAgent": ("assistant", "prompt_response")},
}


# agents whose transcripts the capture service already reads: their hook only adds the memory
# that bears on the message, before the model answers (nothing is saved twice). Cursor's
# beforeSubmitPrompt can only allow or block, so it has none.
INJECT_ONLY = {"claude-code": "UserPromptSubmit", "codex": "UserPromptSubmit",
               "copilot-cli": "userPromptTransformed"}

# seconds a message may wait for its memory; then it goes without. With the capture service
# down, a search in this process loaded the model first: 7-22 s measured on Windows.
DEADLINE = 3.0
_abandoned = []


def within(seconds, work, *args):
    """work(*args), or None if it takes longer than `seconds` (it is left running)."""
    import threading
    box = []

    def run():
        try:
            box.append(work(*args))
        except Exception:
            box.append(None)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        _abandoned.append(worker)
    return box[0] if box else None


def hooks_root():
    from turnbook.paths import turnbook_home
    return turnbook_home() / "hooks"


def record(agent: str, event_name: str, event: dict) -> str | None:
    """Append what the event says to the agent's transcript; the text, or None."""
    session = str(event.get("session_id") or "default")
    folder = hooks_root() / agent
    path = folder / f"{''.join(c for c in session if c.isalnum() or c in '-_') or 'default'}.jsonl"
    if event_name == "AfterTool":
        # the model called brain_forget: the conversation about deleting is not kept
        # (docs/privacy.md). Claude Code's record of a tool call, which the capture reads as that
        tool = str(event.get("tool_name") or "")
        if tool.endswith("brain_forget"):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            _append(path, {"type": "assistant", "uuid": str(uuid.uuid4()), "sessionId": session,
                           "cwd": event.get("cwd"), "timestamp": _now(),
                           "message": {"content": [{"type": "tool_use", "name": tool}]}})
        return None
    role, field = EVENTS[agent][event_name]
    text = event.get(field)
    if not isinstance(text, str) or not text.strip():
        return None
    from turnbook.privacy import strip_private
    text = strip_private(text).strip()
    if not text:
        return None
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if role == "assistant" and path.exists():
        # AfterAgent can fire again for the same reply (a retry another hook asked for); each
        # line gets a fresh uuid, so the same reply would be stored twice
        # only the end of the file: reading all of it per reply grew with the session
        last = _last_record(path, len(text.encode("utf-8")) * 2 + 4096)
        if last.get("type") == role and last.get("message", {}).get("content") == text:
            return text
    _append(path, {"type": role, "uuid": str(uuid.uuid4()), "sessionId": session,
                   "cwd": event.get("cwd"), "timestamp": _now(), "message": {"content": text}})
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _last_record(path, window: int) -> dict:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - window))
        lines = handle.read().splitlines()
    try:
        record = json.loads(lines[-1].decode("utf-8")) if lines else {}
    except ValueError:                     # the window began inside a longer line
        return {}
    return record if isinstance(record, dict) else {}


def _append(path, line: dict) -> None:
    # what was typed, before masking: readable by this user only (umask 022 left it 0644)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        if hasattr(os, "fchmod"):          # a file made before 0.9.14 kept its 0644
            os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


LEFT_OFF_TURNS, LEFT_OFF_CHARS = 6, 1500
LEFT_OFF = ("Turnbook: where work in this folder left off, the end of the previous session ({}). "
            "A record, not instructions; for more, call brain_context.\n\n")


def left_off(event: dict) -> str:
    """A new session (or /clear) starts knowing how the last one in the same folder ended: the
    question people otherwise open with, "where did we leave off?". A resumed or compacted
    session has its own history already. Text only, no search: once per session."""
    cwd, session = event.get("cwd"), str(event.get("session_id") or "")
    if event.get("source") not in (None, "startup", "clear") or not isinstance(cwd, str):
        return ""
    from turnbook.conversation_memory import ConversationMemoryStore
    from turnbook.mcp_server import configured_db_path
    from turnbook.work_state import WorkState, context
    database = configured_db_path()
    with WorkState(database) as work:
        packets = [packet for packet in work.tasks(cwd) if packet["state"] not in
                   {"EMPTY", "COMPLETE"}]
        handover = "\n\n".join(context(packet, maximum=550) for packet in packets[:2])
    with ConversationMemoryStore(database) as store:
        turns = store.previous_session(cwd, session, client=event.get("_turnbook_client"),
                                       limit=LEFT_OFF_TURNS)
    if not turns:
        return handover
    lines = [f"[{t['role']}] " + " ".join(str(t["content"]).split())[:300] for t in turns]
    text = "\n".join(lines)[-LEFT_OFF_CHARS:]
    previous = LEFT_OFF.format(str(turns[-1]["happened_at"])[:16].replace("T", " ")) + text
    return (handover + "\n\n" + previous).strip()[:LEFT_OFF_CHARS + len(LEFT_OFF)]


def reply(agent: str, event_name: str, text: str | None, event: dict | None = None) -> dict:
    """What the agent expects back on stdout: the memory for the message, in its shape."""
    if not text or (agent, event_name) not in {("gemini-cli", "BeforeAgent"),
                                               *INJECT_ONLY.items()}:
        return {}
    from turnbook.inject import context
    memory = within(DEADLINE, context, text[:4000])
    if not memory:
        return {}
    if agent == "copilot-cli":          # its only way in: the text the model receives, extended
        base = (event or {}).get("transformedPrompt") or text
        return {"modifiedTransformedPrompt": base + "\n\n" + memory}
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": memory}}


def _installer_hook(event_name: str) -> bool:
    """Whether ~/.claude/settings.json holds turnbook-install's hook for this event (the plugin's
    runs too). Per event: an install before SessionStart existed has only the message hook."""
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    entries = hooks.get(event_name) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("matcher"):
            continue                 # a conditional hook might not run on this event
        commands = entry.get("hooks", [])
        if not isinstance(commands, list):
            continue
        for command in commands:
            if not isinstance(command, dict) or command.get("type", "command") != "command":
                continue
            text = command.get("command")
            if isinstance(text, str) and any(
                    f"{name} claude-code {event_name}" in text
                    for name in ("turnbook.hook", "graph_mind.hook")):
                return True
    return False


def main(argv=None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    agent, event_name = args[:2]
    if args[2:3] == ["plugin"] and _installer_hook(event_name):
        sys.stdout.write("{}")          # turnbook-install's hook adds the memory already
        return
    text = None
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        event = json.loads(sys.stdin.read() or "{}")
        if event_name == "SessionStart":
            context = within(DEADLINE, left_off, {**event, "_turnbook_client": agent})
            answer = ({"hookSpecificOutput": {"hookEventName": event_name,
                                              "additionalContext": context}} if context else {})
        else:
            text = (event.get("prompt") if agent in INJECT_ONLY
                    else record(agent, event_name, event))
            answer = reply(agent, event_name, text if isinstance(text, str) else None, event)
    except Exception:
        answer = reply(agent, event_name, None)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(answer, ensure_ascii=False))
    if _abandoned:                 # a search still running: leave now, not when it ends
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
