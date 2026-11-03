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


if __name__ == "__main__":
    unittest.main()
