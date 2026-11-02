"""Transcripts shaped like the real ones (record kinds and flags seen in Claude Code and Codex
files, content made up): only what a person typed and what the assistant answered is stored.
Each record carries its own marker word; the test says which markers must and must not be found.
The fixtures that earlier tests missed were exactly these shapes: a compaction summary sent as a
"user" record, a Codex heartbeat, an app's page state. What the agent did (files edited,
commands run) is kept as one "Did:" line per stretch of work; reads and tool output are not."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import json
import os
import tempfile
import unittest

from turnbook.conversation_memory import (
    ClaudeCodeConversationCapture,
    CodexConversationCapture,
    ConversationMemoryStore,
)

T = "2026-11-02T10:00:00.000Z"


def claude_records():
    def rec(kind, n, **extra):
        return {"type": kind, "uuid": f"u{n}", "sessionId": "cc", "timestamp": T,
                "cwd": "C:/work", **extra}

    def msg(role, content):
        return {"message": {"role": role, "content": content}}
    return [
        rec("user", 1, **msg("user", "KEEPtyped 배포는 금요일에 하자")),
        rec("user", 2, **msg("user", [{"type": "text", "text": "KEEPblocks 이미지 붙여서 보냄"}])),
        rec("assistant", 3, **msg("assistant", [
            {"type": "thinking", "thinking": "DROPthinking"},
            {"type": "text", "text": "KEEPanswer 금요일로 잡을게요"},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "KEEPtooluse --x\nDROPline2"}}])),
        rec("assistant", 31, **msg("assistant", [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "DROPread"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/KEEPedited.py",
                                                         "old_string": "DROPold"}}])),
        rec("user", 4, **msg("user", [{"type": "tool_result", "content": "DROPtoolresult"}])),
        rec("user", 5, isMeta=True, **msg("user", "DROPmeta caveat")),
        rec("user", 6, isSidechain=True, **msg("user", "DROPsidechain subagent")),
        rec("user", 7, isCompactSummary=True, isVisibleInTranscriptOnly=True,
            **msg("user", "DROPsummary This session is being continued")),
        rec("user", 8, **msg("user", "<system-reminder>DROPreminder</system-reminder>"
                                     "KEEPafterreminder 이거 확인해 줘")),
        rec("user", 9, **msg("user", "<command-name>/clear</command-name>"
                                     "<command-message>DROPcommand</command-message>")),
        rec("attachment", 10, attachment={"type": "hook_additional_context",
                                          "content": "DROPhookcontext"}),
        rec("attachment", 11, attachment={"type": "hook_success", "content": "DROPhook"}),
        rec("attachment", 12, attachment={"type": "queued_command", "commandMode": "prompt",
                                          "prompt": "KEEPqueued 작업 중에 보낸 말"}),
        rec("attachment", 13, attachment={"type": "queued_command",
                                          "commandMode": "task-notification",
                                          "prompt": "DROPtasknotification"}),
        rec("queue-operation", 14, content="DROPqueueop"),
        rec("api-request-blob", 15, **msg("user", "DROPapiblob")),
        rec("system", 16, subtype="turn_duration", content="DROPsystem"),
        rec("ai-title", 17, title="DROPtitle"),
        rec("last-prompt", 18, prompt="DROPlastprompt"),
        rec("file-history-snapshot", 19, snapshot={"note": "DROPsnapshot"}),
    ]


def codex_records():
    def user(n, *texts):
        return {"type": "response_item", "ordinal": n, "timestamp": T, "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": t} for t in texts]}}
    return [
        {"type": "session_meta", "payload": {"session_id": "cx", "id": "cx"}},
        user(1, "# AGENTS.md instructions for C:/work\n\n<INSTRUCTIONS>DROPagents"
                "</INSTRUCTIONS>"),
        user(2, "<environment_context>\n  <cwd>C:/work</cwd> DROPenv\n</environment_context>"),
        user(3, "<heartbeat>\n  <automation_id>DROPheartbeat</automation_id>\n</heartbeat>"),
        user(4, '<external_codex_apps_open_page>{"page_id":"DROPpage"}'
                "</external_codex_apps_open_page>"),
        user(5, "# Files mentioned by the user:\n\n## a.png: C:/a.png\n\n## My request:\n"
                "KEEPrequest 이 화면 왜 깨지지"),
        user(6, "KEEPcodexplain 서버 재시작했어"),
        {"type": "response_item", "ordinal": 7, "timestamp": T, "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "KEEPcodexanswer 재시작 확인했어요"}]}},
        {"type": "response_item", "ordinal": 8, "timestamp": T, "payload": {
            "type": "function_call", "name": "shell", "arguments": "DROPfunctioncall"}},
        {"type": "response_item", "ordinal": 81, "timestamp": T, "payload": {
            "type": "custom_tool_call", "name": "exec", "input":
            'text(await tools.exec_command({cmd:"KEEPexec -q",max_output_tokens:100}));'
            'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: KEEPpatched.py'
            '\\n@@\\n-DROPpatchline\\n*** End Patch"));'}},
        {"type": "response_item", "ordinal": 82, "timestamp": T, "payload": {
            "type": "function_call", "name": "shell", "arguments": json.dumps(
                {"command": ["bash", "-lc", "KEEPshellarray"]})}},
        {"type": "response_item", "ordinal": 9, "timestamp": T, "payload": {
            "type": "reasoning", "summary": [{"type": "summary_text", "text": "DROPreasoning"}]}},
        {"type": "compacted", "timestamp": T, "payload": {"message": "DROPcompacted"}},
        {"type": "event_msg", "timestamp": T, "payload": {"type": "user_message",
                                                          "message": "DROPeventcopy"}},
        {"type": "turn_context", "timestamp": T, "payload": {"cwd": "DROPturncontext"}},
    ]


def stored(capture_class, records, folder_name):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sessions = root / folder_name
        sessions.mkdir()
        transcript = sessions / "session.jsonl"
        transcript.write_text("", encoding="utf-8")
        with ConversationMemoryStore(root / "brain.sqlite") as store:
            capture = capture_class(sessions, store, state_path=root / "cursor.json",
                                    now=lambda: "2026-11-02T10:01:00")
            capture.initialize_baseline()
            with transcript.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.utime(transcript, (1, 1))   # quiet long ago: its last tool calls are kept
            capture.scan()
            return " ".join(row[0] for row in store.db.execute(
                "SELECT content_redacted FROM conversation_turns"))


class RealShapes(unittest.TestCase):
    def check(self, text, records):
        markers = {word for record in records for word in json.dumps(record, ensure_ascii=False)
                   .replace('"', " ").replace("<", " ").replace(">", " ").replace("\\", " ").split()
                   if word.startswith(("KEEP", "DROP"))}
        kept = sorted(m for m in markers if m.startswith("KEEP") and m not in text)
        leaked = sorted(m for m in markers if m.startswith("DROP") and m in text)
        self.assertEqual(kept, [], "said, but not stored")
        self.assertEqual(leaked, [], "stored, but nobody said it")

    def test_claude_code(self):
        records = claude_records()
        self.check(stored(ClaudeCodeConversationCapture, records, "projects"), records)

    def test_codex(self):
        records = codex_records()
        self.check(stored(CodexConversationCapture, records, "sessions"), records)

    def test_a_stretch_of_work_is_one_line_once_the_agent_stops(self):
        """Tool calls after the last reply wait while the transcript is still being written,
        then are stored once, as one turn."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "projects").mkdir()
            transcript = root / "projects" / "s.jsonl"
            transcript.write_text("", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = ClaudeCodeConversationCapture(root / "projects", store,
                                                        state_path=root / "cursor.json")
                capture.initialize_baseline()

                def call(n, command):
                    return json.dumps({"type": "assistant", "uuid": f"a{n}", "timestamp": T,
                                       "message": {"role": "assistant", "content": [
                                           {"type": "tool_use", "name": "Bash",
                                            "input": {"command": command}}]}}) + "\n"
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(call(1, "pytest"))
                self.assertEqual(capture.scan()["recorded_turns"], 0)   # still working
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(call(2, "git status"))
                os.utime(transcript, (1, 1))
                self.assertEqual(capture.scan()["recorded_turns"], 1)
                self.assertEqual(capture.scan()["recorded_turns"], 0)
                rows = [r[0] for r in store.db.execute(
                    "SELECT content_redacted FROM conversation_turns")]
        self.assertEqual(rows, ["Did:\nran: pytest\nran: git status"])

    def test_a_block_nobody_knows_yet_is_kept_and_counted(self):
        """An app's next new block is not dropped by a guess; the capture status counts it,
        so it shows up the first day instead of weeks later."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sessions").mkdir()
            transcript = root / "sessions" / "s.jsonl"
            transcript.write_text("", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(root / "sessions", store,
                                                   state_path=root / "cursor.json")
                capture.initialize_baseline()
                with transcript.open("a", encoding="utf-8") as handle:
                    for n, text in enumerate(["<new_app_state>x</new_app_state>",
                                              "<new_app_state>y</new_app_state>", "평범한 말"]):
                        handle.write(json.dumps({
                            "type": "response_item", "ordinal": n, "timestamp": T,
                            "payload": {"type": "message", "role": "user", "content": [
                                {"type": "input_text", "text": text}]}}) + "\n")
                result = capture.scan()
        self.assertEqual(result["recorded_turns"], 3)
        self.assertEqual(result["unknown_blocks"], {"new_app_state": 2})


if __name__ == "__main__":
    unittest.main()
