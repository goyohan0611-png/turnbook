import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from turnbook.associative_memory import AssociativeMemoryIndex
from turnbook.coding_memory import CodingMemoryStore
from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.mcp_server import build_server
from turnbook.local_brain import LocalBrainStore


T1 = "2026-01-01T10:00:00"
T2 = "2026-01-01T11:00:00"


class GraphMindMcpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_share_one_persistent_project_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "memory.sqlite"
            with LocalBrainStore(db) as brain:
                brain.remember({"memory_id": "mind-overview", "scope": "global",
                    "memory_type": "PROJECT", "title": "Turnbook 프로젝트",
                    "content": "모델과 세션을 바꿔도 이어지는 사용자 소유 로컬 뇌",
                    "effective_at": T1, "known_at": T1, "actor": "user",
                    "tags": ["마인드", "MIND"], "entities": ["Turnbook"],
                    "provenance": {"source_type": "fixture", "source_ref": "test"},
                    "supersedes_memory_id": None})
            with CodingMemoryStore(db) as coding:
                coding.record({"change_id": "code-change-1", "project_id": "project:test",
                    "session_id": "session:seed", "workspace": str(Path(directory)),
                    "happened_at": T1, "known_at": T1, "change_kind": "MODIFIED",
                    "file_path": "memory.py", "language": "py",
                    "before_sha256": "a", "after_sha256": "b",
                    "reason": "Remember code work", "related_tests": ["memory-test"],
                    "symbols": [{"name": "recall", "kind": "FUNCTION", "side": "AFTER",
                                 "start_line": 1, "end_line": 2}],
                    "diff_ref": str(Path(directory) / "change.patch"),
                    "before_ref": None, "after_ref": None,
                    "provenance": {"source_type": "fixture", "source_ref": "memory.py"}})
            with ConversationMemoryStore(db) as conversations:
                conversations.record({"turn_id": "turn-1", "client": "codex",
                    "client_session_id": "session:seed", "scope": "project:test",
                    "cwd": str(Path(directory)), "happened_at": T1, "known_at": T1,
                    "role": "user", "content_redacted": "어제 결제 함수를 고쳤어",
                    "raw_sha256": "abc", "redaction_count": 0,
                    "source_path": "fixture.jsonl", "source_ordinal": 1})
            with AssociativeMemoryIndex(db) as associative:
                associative.sync_sources()
            server = build_server(db)
            async with Client(server) as client:
                listed = await client.list_tools()
                by_name = {tool.name: tool for tool in listed.tools}
                names = set(by_name)
                self.assertEqual(names, {"brain_remember", "brain_recall", "brain_context",
                                         "brain_folder", "brain_forget", "code_activity",
                                         "brain_checkpoint", "brain_work", "brain_work_status",
                                         "brain_understand"})
                self.assertFalse(by_name["brain_remember"].annotations.read_only_hint)
                for name in ("brain_recall", "brain_context", "code_activity", "brain_work_status"):
                    self.assertTrue(by_name[name].annotations.read_only_hint)
                annotations = by_name["brain_work_status"].annotations
                self.assertFalse(annotations.destructive_hint)
                self.assertTrue(annotations.idempotent_hint)
                self.assertFalse(annotations.open_world_hint)
                self.assertTrue(by_name["brain_work"].annotations.destructive_hint)
                self.assertFalse(by_name["brain_work"].annotations.read_only_hint)
                recalled = await client.call_tool("brain_recall", {
                    "query": "마인드 프로젝트가 뭐야?"})
                self.assertFalse(recalled.is_error)
                self.assertEqual(recalled.structured_content["status"], "KNOWN")
                self.assertEqual(recalled.structured_content["matches"][0]["memory_id"],
                                 "mind-overview")
                federated = await client.call_tool("brain_recall", {
                    "query": "결제 함수", "scopes": ["project:test"]})
                self.assertEqual(federated.structured_content["status"], "KNOWN")
                self.assertEqual(len(federated.structured_content["conversation_turns"]), 1)
                context = await client.call_tool("brain_context", {
                    "query": "previous memory project", "policy": "always",
                    "max_chars": 1000})
                self.assertFalse(context.is_error)
                self.assertEqual(context.structured_content["status"], "KNOWN")
                self.assertLessEqual(context.structured_content["used_chars"], 1000)
                skipped = await client.call_tool("brain_context", {
                    "query": "Explain photosynthesis", "policy": "auto"})
                self.assertEqual(skipped.structured_content["status"], "SKIPPED")
                code = await client.call_tool("code_activity", {
                    "project_id": "project:test", "symbol": "recall",
                    "happened_from": "2026-01-01T00:00:00+09:00",
                    "happened_to": "2026-01-01T23:59:59+09:00",
                    "max_diff_chars": 100000})
                self.assertFalse(code.is_error)
                self.assertEqual(code.structured_content["status"], "KNOWN")
                self.assertEqual(code.structured_content["changes"][0]["file_path"],
                                 "memory.py")
                indexed = await client.call_tool("brain_folder", {"reindex": True})
                self.assertFalse(indexed.is_error)
                self.assertIn("vectors_warmed", indexed.structured_content)


class OtherAgentsTests(unittest.TestCase):
    def test_names_only_the_installed_agent_clis(self):
        from unittest import mock

        from turnbook import mcp_server
        with mock.patch("shutil.which", lambda name: name if name == "codex" else None):
            line = mcp_server.other_agents()
            self.assertIn('codex exec "<task>"', line)
            self.assertNotIn("gemini", line)
            self.assertIn(line, mcp_server.instructions(True))
        with mock.patch("shutil.which", lambda name: None):
            self.assertEqual(mcp_server.other_agents(), "")


class RulesTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_agent_starts_with_the_current_rules(self):
        """A rule saved in Claude reaches Codex: one brain, not one settings file per app."""
        from turnbook import mcp_server
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            database = Path(folder) / "brain.sqlite"
            async with Client(build_server(database)) as claude:
                for rule in ("영어로 답해", "항상 한국어로 답해"):
                    await claude.call_tool("brain_remember", {
                        "content": rule, "memory_type": "rule", "about": "me",
                        "attribute": "reply_language"})
                await claude.call_tool("brain_remember", {"content": "공이는 골든리트리버"})
            text = mcp_server.instructions(True, database)
            self.assertTrue(text.startswith(mcp_server.RULES + "항상 한국어로 답해"))
            self.assertNotIn("영어로 답해", text)
            self.assertNotIn("공이", text)                   # a fact is not a rule
            self.assertNotIn(mcp_server.RULES, mcp_server.instructions(True))
            async with Client(build_server(database)) as codex:   # withdrawn by a plain memory
                rule = await codex.call_tool("brain_remember", {
                    "content": "커밋 전에 테스트", "memory_type": "rule"})
                self.assertIn("커밋 전에 테스트", mcp_server.user_rules(database))
                await codex.call_tool("brain_remember", {
                    "content": "테스트 규칙은 이제 없음",
                    "supersedes_memory_id": rule.structured_content["memory_id"]})
            self.assertNotIn("커밋 전에 테스트", mcp_server.user_rules(database))
            self.assertIn("항상 한국어로 답해", mcp_server.user_rules(database))
            async with Client(build_server(database)) as codex:
                await codex.call_tool("brain_remember", {         # an odd fact: others stay
                    "content": "짧게 답해", "memory_type": "rule", "attributes": {"fact": [1]}})
                self.assertIn("짧게 답해", mcp_server.user_rules(database))
                from turnbook.local_brain import LocalBrainStore
                with LocalBrainStore(database) as store:
                    korean = next(r["memory_id"] for r in store.recall(
                        "한국어", as_of="2999-01-01T00:00:00", limit=50)["matches"]
                        if r["content"] == "항상 한국어로 답해")
                await codex.call_tool("brain_remember", {         # withdrawn: English stays out
                    "content": "언어 규칙 없음", "supersedes_memory_id": korean})
            rules = mcp_server.user_rules(database)
            self.assertNotIn("한국어", rules)
            self.assertNotIn("영어로 답해", rules)


if __name__ == "__main__":
    unittest.main()
