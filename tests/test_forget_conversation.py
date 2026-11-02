"""Forgetting by asking the assistant: it lists masked candidates, deletes only what the user
picks, and the conversation about deleting is not remembered either."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import json
import tempfile
import unittest

from mcp import Client

from turnbook.conversation_memory import ClaudeCodeConversationCapture, ConversationMemoryStore
from turnbook.forget import masked_preview
from turnbook.mcp_server import build_server, wait_for_index

SECRET = "qwer1234!"


def line(kind, minute, text=None, tool=None, uuid=None):
    content = ([{"type": "tool_use", "id": "x", "name": tool, "input": {}}] if tool
               else text)
    return json.dumps({"type": kind, "sessionId": "s1", "uuid": uuid or f"{kind}-{minute}",
                       "cwd": "C:/work", "timestamp": f"2026-10-08T10:{minute:02d}:00",
                       "message": {"role": kind, "content": content}}, ensure_ascii=False) + "\n"


class ForgetConversationTests(unittest.TestCase):
    def test_the_exchange_about_deleting_is_not_remembered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "projects" / "p" / "s1.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({"type": "summary", "sessionId": "s1"}) + "\n",
                                  encoding="utf-8")
            store = ConversationMemoryStore(root / "brain.sqlite")
            capture = ClaudeCodeConversationCapture(root / "projects", store,
                                                    state_path=root / "cursor.json")
            capture.initialize_baseline()

            def say(*lines):
                with transcript.open("a", encoding="utf-8") as stream:
                    stream.writelines(lines)
                capture.scan()

            say(line("user", 0, "the build is green again"),
                line("user", 1, f"wifi password is {SECRET}"))
            say(line("user", 5, "아까 비번 쳤던 거 지워줘"))          # captured: nothing knows yet
            say(line("assistant", 6, tool="mcp__graph-mind-memory__brain_forget", uuid="a6"),
                line("assistant", 6, "1. wifi password **** ****  어떤 걸 지울까요?", uuid="a6t"),
                line("user", 7, "1번 지워"),
                line("assistant", 8, tool="mcp__graph-mind-memory__brain_forget", uuid="a8"),
                line("assistant", 8, "지웠어요.", uuid="a8t"),
                line("user", 9, "점심 뭐 먹지"))
            kept = [row[0] for row in store.db.execute(
                "SELECT content_redacted FROM conversation_turns ORDER BY happened_at")]
            store.close()
            # the deleting itself is the tool's job (another test); the exchange must be gone
            # (the password itself is masked on capture since 0.9.9: "password is x")
            self.assertEqual(kept, ["the build is green again", "wifi password is [REDACTED]",
                                    "점심 뭐 먹지"])

    def test_list_masks_then_delete_only_what_was_picked(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "brain.sqlite"
            with ConversationMemoryStore(db) as store:
                for n, text in enumerate([f"wifi password is {SECRET}", "wifi is slow today"]):
                    store.record({"turn_id": f"t{n}", "client": "claude-code",
                                  "client_session_id": "s", "scope": "global", "role": "user",
                                  "content_redacted": text, "raw_sha256": f"h{n}",
                                  "source_path": "p", "happened_at": "2026-10-08T10:00:00",
                                  "known_at": "2026-10-08T10:00:00", "source_ordinal": n})
            import asyncio

            async def run():
                async with Client(build_server(db)) as client:
                    tools = {t.name for t in (await client.list_tools()).tools}
                    listed = (await client.call_tool("brain_forget", {"query": "wifi password"})
                              ).structured_content
                    picked = [c["id"] for c in listed["candidates"] if c["id"] == "t0"]
                    done = (await client.call_tool("brain_forget", {"ids": picked})
                            ).structured_content
                    return tools, listed, done
            tools, listed, done = asyncio.run(run())
            wait_for_index()
            self.assertIn("brain_forget", tools)
            self.assertTrue(listed["candidates"])
            self.assertFalse(any(SECRET in c["preview"] for c in listed["candidates"]))
            self.assertEqual(done["turns"], 1)
            with ConversationMemoryStore(db) as store:
                left = [r[0] for r in store.db.execute("SELECT turn_id FROM conversation_turns")]
            self.assertEqual(left, ["t1"])

    def test_previews_hide_what_could_be_a_secret(self):
        self.assertEqual(masked_preview("my wifi password is qwer1234! ok"),
                         "my wifi password is **** ok")
        self.assertEqual(masked_preview("비번은 hunter2"), "비번은 ****")
        self.assertEqual(masked_preview("pw: hunter2 and more"), "pw:**** **** and more")
        self.assertEqual(masked_preview("lunch was kimchi stew"), "lunch was kimchi stew")


if __name__ == "__main__":
    unittest.main()
