"""Explicit private text never reaches Turnbook's stored or staged conversation copy."""
import _isolate  # noqa: F401
from pathlib import Path
from unittest import mock
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from mcp import Client

from turnbook import hook
from turnbook.conversation_memory import CodexConversationCapture, ConversationMemoryStore, _redact
from turnbook.privacy import strip_private
from turnbook.mcp_server import build_server, wait_for_index


class PrivateBlockTests(unittest.TestCase):
    def test_nested_unclosed_and_separate_blocks(self):
        cases = {
            "public": "public",
            "before<private>secret</private>after": "before\nafter",
            "<PRIVATE>secret</PRIVATE> public": " public",
            "<private>one<private>two</private>three</private>safe": "safe",
            "safe<private>one<private>two</private>still private": "safe\n",
            "<private>never closed": "",
            "<private>one</private>A<private>two</private>B": "A\nB",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(strip_private(value), expected)

    def test_private_exclusion_is_independent_of_secret_masking(self):
        with mock.patch("turnbook.conversation_memory.masking_on", return_value=False):
            for preserve in (False, True):
                public, count = _redact("public<private>do not store</private>",
                                        preserve_source=preserve)
                self.assertEqual(public.strip(), "public")
                self.assertEqual(count, 1)

    def test_codex_capture_publishes_only_public_text_and_skips_private_only_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            source = sessions / "rollout.jsonl"
            source.write_text(json.dumps({"type": "session_meta", "payload": {
                "session_id": "s", "cwd": str(root)}}) + "\n", encoding="utf-8")
            sent = []
            with ConversationMemoryStore(root / "brain.sqlite") as store:
                capture = CodexConversationCapture(sessions, store,
                    state_path=root / "cursor.json", publish=sent.append,
                    now=lambda: "2026-11-02T10:00:00")
                capture.initialize_baseline()
                with source.open("a", encoding="utf-8") as stream:
                    for n, value in enumerate(("launch Friday <private>secretphrase</private>",
                                                "<private>anothersecret</private>")):
                        stream.write(json.dumps({"type": "response_item", "ordinal": n,
                            "timestamp": "2026-11-02T10:00:00", "payload": {"type": "message",
                            "role": "user", "content": [{"type": "input_text", "text": value}]}})
                                     + "\n")
                result = capture.scan()
                self.assertEqual(result["recorded_turns"], 1)
                self.assertEqual(store.search("secretphrase")["status"], "UNKNOWN")
                self.assertEqual(store.search("launch")["turns"][0]["content"], "launch Friday")
            self.assertEqual(len(sent), 1)
            self.assertNotIn("secretphrase", json.dumps(sent))
            self.assertNotIn("anothersecret", json.dumps(sent))
            self.assertNotIn(b"secretphrase", (root / "brain.sqlite").read_bytes())

    def test_gemini_staging_omits_private_text(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}):
            event = {"session_id": "s", "prompt": "public <private>hidden</private> after"}
            hook.record("gemini-cli", "BeforeAgent", event)
            path = hook.hooks_root() / "gemini-cli" / "s.jsonl"
            self.assertNotIn("hidden", path.read_text(encoding="utf-8"))
            before = path.read_bytes()
            hook.record("gemini-cli", "BeforeAgent", {**event, "prompt": "<private>only</private>"})
            self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_opencode_staging_omits_private_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (Path(__file__).resolve().parents[1] / "src/turnbook/opencode_plugin.js")
            code = source.read_text(encoding="utf-8").replace("__PYTHON__", '"unused"')
            code = code.replace("__TRANSCRIPTS__", json.dumps(str(root / "transcripts")))
            module = root / "plugin.mjs"
            module.write_text(code + '\nexport { write };\n', encoding="utf-8")
            runner = root / "check.mjs"
            runner.write_text('import { write } from "./plugin.mjs";\n'
                              'write("s", {message:{content:'
                              '"public <private>hidden</private>"}});\n'
                              'write("s", {message:{content:"<private>only</private>"}});\n',
                              encoding="utf-8")
            subprocess.run([shutil.which("node"), str(runner)], check=True,
                           capture_output=True, timeout=10)
            lines = (root / "transcripts/s.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["message"]["content"], "public")


class PrivateMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_only_remember_is_skipped_and_mixed_text_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.sqlite"
            try:
                async with Client(build_server(path)) as client:
                    result = await client.call_tool("brain_remember", {
                        "content": "<private>do not save this</private>"})
                    self.assertEqual(result.structured_content["status"], "SKIPPED")
                    await client.call_tool("brain_remember", {
                        "content": "launch Friday <private>hiddenphrase</private>",
                        "source_ref": "privacy test"})
                    found = await client.call_tool("brain_recall", {"query": "launch Friday"})
                    self.assertIn("launch Friday", str(found.structured_content))
                    self.assertNotIn("hiddenphrase", str(found.structured_content))
            finally:
                wait_for_index()
            self.assertNotIn(b"hiddenphrase", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
