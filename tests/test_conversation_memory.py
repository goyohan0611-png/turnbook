import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import hashlib
import json
import tempfile
import unittest

from turnbook.conversation_memory import (ClaudeCodeConversationCapture,
                                            ClaudeCoworkConversationCapture,
                                 CodexConversationCapture,
                                 ConversationMemoryStore, search_terms)


def row(ordinal, role, text, timestamp="2026-09-16T10:00:00"):
    return {"timestamp": timestamp, "ordinal": ordinal, "type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text" if role == "user"
                                     else "output_text", "text": text}]}}


class ConversationMemoryTests(unittest.TestCase):
    def test_document_private_block_spanning_a_heading_is_never_stored(self):
        from turnbook.documents import DocumentCapture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "notes"
            folder.mkdir()
            (folder / "spec.md").write_text(
                "# Public\nbefore\n<private>hiddenbefore\n# hiddenheading\n"
                "hiddenafter\n</private>\nafter\n# Next\nvisible", encoding="utf-8")
            published = []
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = DocumentCapture([folder], store, state_path=root / "cursor.json",
                    now=lambda: "2026-11-03T10:00:00", publish=published.append)
                self.assertEqual(capture.scan()["recorded_turns"], 2)
                texts = [t["content"] for t in store.latest(limit=10)["turns"]]
                for private in ("hiddenbefore", "hiddenheading", "hiddenafter"):
                    self.assertNotIn(private, "\n".join(texts))
                    self.assertNotIn(private, json.dumps(published))
                for public in ("before", "after", "visible"):
                    self.assertIn(public, "\n".join(texts))
                self.assertEqual(sorted(texts),
                                 sorted(t["content_redacted"] for t in published))

    def test_korean_particles_come_off_the_question_words(self):
        self.assertEqual(search_terms("Caroline은 언제 지지 모임에 갔어?"),
                         ["caroline", "언제", "지지", "모임", "갔어"])
        self.assertEqual(search_terms("콩이가 콩이를"), ["콩이"])     # "콩이" itself stays
        self.assertEqual(search_terms("What did Mel paint in 2022?"),
                         ["what", "did", "mel", "paint", "in", "2022"])
        with tempfile.TemporaryDirectory() as directory:
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                for n, text in enumerate(["Caroline이 모임이 좋았대", "날씨 얘기"]):
                    store.record({"turn_id": f"t{n}", "client": "test",
                                  "client_session_id": "s", "scope": "global", "role": "user",
                                  "content_redacted": text, "raw_sha256": f"t{n}",
                                  "source_path": "s", "happened_at": "2026-10-01T10:00:00",
                                  "known_at": "2026-10-01T10:00:00", "source_ordinal": n})
                found = store.search("Caroline은 모임에 갔어?")["turns"]
                self.assertEqual([t["turn_id"] for t in found], ["t0"])

    def test_incremental_capture_redacts_secrets_and_searches_fts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions, workspace = root / "sessions", root / "project"
            sessions.mkdir()
            workspace.mkdir()
            source = sessions / "rollout-test.jsonl"
            meta = {"type": "session_meta", "payload": {
                "session_id": "session:test", "cwd": str(workspace)}}
            source.write_text(json.dumps(meta) + "\n", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json",
                    workspace_scopes={str(workspace): "project:demo"},
                    now=lambda: "2026-09-16T10:01:00")
                capture.initialize_baseline()
                with source.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row(1, "user",
                        "어제 결제 모듈을 수정했어 sk-proj-abcdefghijklmnopqrstuvwxyz"),
                        ensure_ascii=False) + "\n")
                    handle.write(json.dumps(row(2, "assistant",
                        "payment.py의 charge 함수를 수정했습니다."),
                        ensure_ascii=False) + "\n")
                first = capture.scan()
                second = capture.scan()
                found = store.search("결제 모듈", scopes=["project:demo"])
            self.assertEqual(first["recorded_turns"], 2)
            self.assertEqual(first["redactions"], 1)
            self.assertEqual(second["status"], "NO_NEW_TURNS")
            self.assertEqual(found["status"], "KNOWN")
            self.assertIn("[REDACTED_OPENAI_KEY]", found["turns"][0]["content"])
            self.assertNotIn("sk-proj-", found["turns"][0]["content"])

    def test_utc_codex_timestamp_is_normalized_to_local_store_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            source = sessions / "utc.jsonl"
            source.write_text(json.dumps({"type": "session_meta", "payload": {
                "session_id": "session:utc"}}) + "\n", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json",
                    now=lambda: "2026-09-16T19:01:00")
                capture.initialize_baseline()
                with source.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row(1, "user", "UTC timestamp memory",
                        "2026-09-16T10:00:00Z")) + "\n")
                result = capture.scan()
                found = store.search("timestamp memory")
            self.assertEqual(result["recorded_turns"], 1)
            self.assertEqual(found["status"], "KNOWN")

    def test_what_codex_puts_in_a_user_message_is_not_the_users_words(self):
        """An automation's heartbeat and an app's page state: Codex sends them as "user"
        messages. Kept out, and the ones an older version stored are erased once."""
        import re
        from unittest import mock

        from turnbook import conversation_memory
        injected = ["<heartbeat>\n  <automation_id>nightly brief</automation_id>\n</heartbeat>",
                    '<external_codex_apps_open_page>{"page_id":"pagestate"}'
                    "</external_codex_apps_open_page>"]
        before = re.compile(r"<(?:environment_context|recommended_plugins)\b[^>]*>.*?"
                            r"</(?:environment_context|recommended_plugins)>", flags=re.DOTALL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            source = sessions / "s.jsonl"
            source.write_text(json.dumps({"type": "session_meta", "payload": {
                "session_id": "session:s"}}) + "\n", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json", now=lambda: "2026-09-16T19:01:00")
                capture.initialize_baseline()
                with source.open("a", encoding="utf-8") as handle:
                    for n, text in enumerate(injected + ["<environment_context>x"
                                                         "</environment_context>배포는 금요일"]):
                        handle.write(json.dumps(row(n, "user", text), ensure_ascii=False) + "\n")
                with mock.patch.object(conversation_memory, "AUTOMATIC_BLOCKS", before):
                    capture.scan()                                   # 0.10.4 and before
                self.assertEqual(len(store.search("nightly brief")["turns"]), 1)
                self.assertEqual(len(store.search("pagestate")["turns"]), 1)
                state = json.loads((root / "cursor.json").read_text(encoding="utf-8"))
                state["cleanup"] = 0
                (root / "cursor.json").write_text(json.dumps(state), encoding="utf-8")
                capture.scan()
                self.assertEqual(store.search("nightly brief")["turns"], [])
                self.assertEqual(store.search("pagestate")["turns"], [])
                self.assertEqual(len(store.search("배포는 금요일")["turns"]), 1)
                with source.open("a", encoding="utf-8") as handle:
                    for n, text in enumerate(injected + ["그냥 한 말"], start=10):
                        handle.write(json.dumps(row(n, "user", text), ensure_ascii=False) + "\n")
                self.assertEqual(capture.scan()["recorded_turns"], 1)

    def test_existing_sessions_start_at_end_and_new_session_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            old = sessions / "old.jsonl"
            old.write_text(json.dumps(row(1, "user", "old history")) + "\n",
                           encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json")
                capture.initialize_baseline()
                new = sessions / "new.jsonl"
                new.write_text(json.dumps(row(1, "user", "new session memory")) + "\n",
                               encoding="utf-8")
                result = capture.scan()
                old_result = store.search("old history")
                new_result = store.search("new session memory")
            self.assertEqual(result["recorded_turns"], 1)
            self.assertEqual(old_result["status"], "UNKNOWN")
            self.assertEqual(new_result["status"], "KNOWN")

    def test_a_compaction_summary_is_not_the_users_words(self):
        """Claude Code writes the summary of a compacted conversation as a "user" record. Older
        versions stored it as something the user said; the next scan erases those, by id."""
        from unittest import mock

        def line(uuid, content, **extra):
            return {"type": "user", "sessionId": "cc:1", "timestamp": "2026-09-16T10:00:00",
                    "uuid": uuid, "message": {"role": "user", "content": content}, **extra}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            (projects / "p").mkdir(parents=True)
            source = projects / "p" / "cc.jsonl"
            source.write_text("", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = ClaudeCodeConversationCapture(projects, store,
                    state_path=root / "cursor.json", now=lambda: "2026-09-16T10:01:00")
                capture.initialize_baseline()
                with source.open("a", encoding="utf-8") as handle:
                    for item in (line("u1", "배포는 금요일로 하자"),
                                 line("s1", "This session is being continued. Summary: ...",
                                      isCompactSummary=True, isVisibleInTranscriptOnly=True)):
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                with mock.patch.object(ClaudeCodeConversationCapture, "_message",
                                       ClaudeCodeConversationCapture._said):    # 0.10.4 and before
                    capture.scan()
                self.assertEqual(len(store.search("Summary")["turns"]), 1)
                summary = "This session is being continued. Summary: ..."
                store.record({"turn_id": "claude-code-turn-before-0.9.2", "client": "claude-code",
                              "client_session_id": "cc:1", "scope": "global", "role": "user",
                              "content_redacted": summary, "source_path": "p",
                              "raw_sha256": hashlib.sha256(summary.encode()).hexdigest(),
                              "happened_at": "2026-09-16T10:00:00",
                              "known_at": "2026-09-16T10:00:00", "source_ordinal": 0})
                self.assertEqual(len(store.search("Summary")["turns"]), 2)
                state = json.loads((root / "cursor.json").read_text(encoding="utf-8"))
                state["cleanup"] = 0                    # a cursor from then
                (root / "cursor.json").write_text(json.dumps(state), encoding="utf-8")
                capture.scan()
                self.assertEqual(store.search("Summary")["turns"], [], "both id schemes erased")
                self.assertEqual(len(store.search("배포는 금요일로")["turns"]), 1)
                with source.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line("s2", "Another Summary text",
                                                 isCompactSummary=True)) + "\n")
                capture.scan()
                self.assertEqual(store.search("Another Summary")["turns"], [])

    def test_copies_stored_under_old_ids_are_dropped_once(self):
        """Before 0.9.2 a re-read stored every turn again under a new id."""
        def turn(turn_id, ordinal, at="2026-10-01T17:55:01.897000", text="그럴싸 하네"):
            return {"turn_id": turn_id, "client": "claude-code", "client_session_id": "s",
                    "scope": "global", "role": "user", "content_redacted": text,
                    "raw_sha256": hashlib.sha256(text.encode()).hexdigest(), "source_path": "p",
                    "happened_at": at, "known_at": at, "source_ordinal": ordinal}
        with tempfile.TemporaryDirectory() as directory:
            with ConversationMemoryStore(Path(directory) / "brain.sqlite") as store:
                store.record(turn("first", 1))
                store.record(turn("again", 15448))                       # the same turn
                store.record(turn("later", 2, at="2026-10-02T09:00:00"))  # said again, later
                self.assertEqual(store.drop_duplicates(), 1)
                self.assertEqual(sorted(t["turn_id"] for t in store.search("그럴싸")["turns"]),
                                 ["first", "later"])
                self.assertEqual(store.drop_duplicates(), 0, "once per store")
                store.record(turn("again", 15448))        # erased: not brought back by a sync
                self.assertEqual(len(store.search("그럴싸")["turns"]), 2)
                # a copy a memory was promoted from stays: erasing it would erase the memory
                store.record(turn("kept-a", 3, at="2026-10-03T09:00:00"))
                store.record(turn("kept-b", 4, at="2026-10-03T09:00:00"))
                store.db.execute("CREATE TABLE semantic_ingestion_audit(turn_id TEXT PRIMARY KEY, "
                                 "gate_version TEXT, disposition TEXT, reason TEXT, "
                                 "memory_id TEXT, processed_at TEXT)")
                store.db.execute("INSERT INTO semantic_ingestion_audit VALUES"
                                 "('kept-b','g','PROMOTED','r','m1','t')")
                store.db.execute("DELETE FROM store_flags WHERE name='deduplicated'")
                store.db.commit()
                self.assertEqual(store.drop_duplicates(), 0)

    def test_claude_code_keeps_only_what_was_said(self):
        """Of everything a Claude Code transcript holds, only typed user text and assistant text
        are conversation. Tool calls, tool results, thinking, sub-agent side chains, meta records
        and injected harness notices must not be stored; secrets must be masked."""
        def line(kind, content, **extra):
            return {"type": kind, "sessionId": "cc:1", "cwd": extra.pop("cwd", None),
                    "timestamp": "2026-09-16T10:00:00", "uuid": extra.pop("uuid", kind),
                    "message": {"role": kind, "content": content}, **extra}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects, workspace = root / "projects", root / "project"
            (projects / "p").mkdir(parents=True)
            workspace.mkdir()
            source = projects / "p" / "cc.jsonl"
            source.write_text(json.dumps({"type": "summary", "sessionId": "cc:1",
                                          "cwd": str(workspace)}) + "\n", encoding="utf-8")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = ClaudeCodeConversationCapture(projects, store,
                    state_path=root / "cursor.json",
                    workspace_scopes={str(workspace): "project:demo"},
                    now=lambda: "2026-09-16T10:01:00")
                capture.initialize_baseline()
                rows = [
                    line("user", "콩이를 제주도에 데려갔어 sk-proj-abcdefghijklmnopqrstuvwxyz"
                                 "<system-reminder>harness notice</system-reminder>"),
                    line("assistant", [{"type": "thinking", "thinking": "private"},
                                       {"type": "text", "text": "협재 해변이었군요."},
                                       {"type": "tool_use", "name": "Bash", "input": {}}]),
                    line("assistant", [{"type": "tool_use", "name": "Read", "input": {}}]),
                    line("user", [{"type": "tool_result", "content": "file dump"}]),
                    line("user", "side chain chatter", isSidechain=True),
                    line("user", "meta record", isMeta=True),
                    {"type": "attachment", "sessionId": "cc:1"},
                    {"type": "attachment", "sessionId": "cc:1", "timestamp": "2026-09-16T10:00:05",
                     "uuid": "q1", "attachment": {"type": "queued_command", "humanTurn": True,
                                                  "prompt": "작업 중에 보낸 말: 김치볶음밥 먹었어",
                                                  "commandMode": "prompt"}},
                    {"type": "queue-operation", "sessionId": "cc:1",
                     "content": "작업 중에 보낸 말: 김치볶음밥 먹었어"},
                    {"type": "attachment", "sessionId": "cc:1", "uuid": "n1",
                     "attachment": {"type": "queued_command", "commandMode": "task-notification",
                                    "prompt": "background task finished"}},
                ]
                with source.open("a", encoding="utf-8") as handle:
                    for item in rows:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                result = capture.scan()
                found = store.search("콩이 제주도", scopes=["project:demo"])
                answer = store.search("협재 해변", scopes=["project:demo"])
                noise = store.search("file dump")
                queued = store.search("김치볶음밥")
                notice = store.search("background task")
                # re-reading the whole transcript from the start must not store anything twice
                state = json.loads((root / "cursor.json").read_text(encoding="utf-8"))
                state["offsets"] = {key: 0 for key in state["offsets"]}
                (root / "cursor.json").write_text(json.dumps(state), encoding="utf-8")
                again = capture.scan()
            self.assertEqual(result["recorded_turns"], 3)
            self.assertEqual(len(queued["turns"]), 1, "a mid-task message is stored once")
            self.assertEqual(notice["status"], "UNKNOWN", "task notifications are not speech")
            self.assertTrue(all(r["disposition"] == "ALREADY_RECORDED"
                                for r in again["record_results"]), again)
            self.assertEqual(result["redactions"], 1)
            said = found["turns"][0]["content"]
            self.assertIn("[REDACTED_OPENAI_KEY]", said)
            self.assertNotIn("harness notice", said)
            self.assertEqual(answer["status"], "KNOWN")
            self.assertNotIn("private", answer["turns"][0]["content"])
            self.assertEqual(noise["status"], "UNKNOWN")

    def test_cowork_reads_only_transcripts(self):
        """Cowork sessions sit next to credentials, audit logs and sub-agent chains
        and uploads. Only the session transcripts are conversation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "sessions" / "a" / "b" / "local_x"
            chat = session / ".claude" / "projects" / "p"
            (chat / "s1" / "subagents").mkdir(parents=True)
            (session / ".claude" / ".credentials.json").write_text('{"token": "secret"}')
            (session / "audit.jsonl").write_text('{"type": "user", "message": "audit"}\n')
            (chat / "s1" / "subagents" / "agent.jsonl").write_text("")
            transcript = chat / "s1.jsonl"
            transcript.write_text("")
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = ClaudeCoworkConversationCapture(root / "sessions", store,
                    state_path=root / "cursor.json", now=lambda: "2026-10-07T10:01:00")
                capture.initialize_baseline()
                transcript.write_text(json.dumps({
                    "type": "user", "sessionId": "cw:1", "uuid": "u1",
                    "timestamp": "2026-10-07T10:00:00",
                    "message": {"role": "user", "content": "코워크에서 보고서 정리했어"}},
                    ensure_ascii=False) + "\n", encoding="utf-8")
                files = capture._files()
                result = capture.scan()
                found = store.search("보고서 정리")
            self.assertEqual([f.name for f in files], ["s1.jsonl"])
            self.assertEqual(result["recorded_turns"], 1)
            self.assertEqual(found["turns"][0]["client"], "claude-cowork")

    def test_passwords_in_sentences_cards_and_id_numbers_are_masked(self):
        from turnbook.conversation_memory import _redact
        masked = {"my password is Hunter2!xy": "my password is [REDACTED]",
                  "내 비밀번호는 qwer1234! 이야": "내 비밀번호는 [REDACTED] 이야",
                  "와이파이 비번 abcd5678": "와이파이 비번 [REDACTED]",
                  "비밀번호가 1234야": "비밀번호가 [REDACTED]야",
                  "rk_live_" + "a1B2c3D4" * 3: "[REDACTED_STRIPE_KEY]",
                  "card 4111 1111 1111 1111": "card [REDACTED_CARD]",
                  "주민번호 900101-1234567": "주민번호 [REDACTED_ID_NUMBER]"}
        for text, expected in masked.items():
            self.assertEqual(_redact(text), (expected, 1), text)
        for kept in ("the password is stored in the vault", "비밀번호는 1Password에 있어",
                     "비번 바꿨어", "order 1234 5678 9012 3456 shipped", "The PIN was set"):
            self.assertEqual(_redact(kept), (kept, 0), kept)


if __name__ == "__main__":
    unittest.main()
