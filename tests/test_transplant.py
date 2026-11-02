import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import os
import tempfile
import unittest

from mcp import Client

from turnbook.conversation_memory import ConversationMemoryStore
from turnbook.local_brain import LocalBrainStore
from turnbook.mcp_server import build_server, wait_for_index


class TransplantTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_whole_memory_moves_to_another_pc_as_one_file_facts_included(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "old.sqlite", Path(directory) / "new.sqlite"
            brain = Path(directory) / "brain.jsonl"
            with ConversationMemoryStore(old) as turns:
                turns.record({"turn_id": "t1", "client": "claude-code", "client_session_id": "s",
                              "scope": "global", "role": "user",
                              "content_redacted": "콩이를 제주도에 데려갔어", "raw_sha256": "x",
                              "source_path": "p", "happened_at": "2026-03-01T09:00:00",
                              "known_at": "2026-03-01T09:00:00", "source_ordinal": 1})
            async with Client(build_server(old, terminal=True)) as client:
                for when, text in [("2024-03-01", "I live in Busan"),
                                   ("2026-09-20", "We moved to Seoul, Mapo")]:
                    await client.call_tool("brain_remember", {
                        "content": text, "about": "me", "attribute": "home_city",
                        "effective_at": when + "T09:00:00"})
                out = (await client.call_tool("brain_folder", {"export_to": str(brain)})
                       ).structured_content["exported"]
            async with Client(build_server(new, terminal=True)) as client:
                first = (await client.call_tool("brain_folder", {"import_from": str(brain)})
                         ).structured_content["imported"]
                again = (await client.call_tool("brain_folder", {"import_from": str(brain)})
                         ).structured_content["imported"]
            wait_for_index()
            with LocalBrainStore(new) as store:
                homes = [m for m in store.recall("me", as_of="2099-01-01T00:00:00")["matches"]
                         if m.get("earlier") is not None]
            with ConversationMemoryStore(new) as turns:
                found = turns.search("콩이 제주도")["turns"]
        self.assertEqual((out["memories"], out["turns"]), (2, 1))
        self.assertEqual(first["imported"], 3)
        self.assertEqual(again["imported"], 0)                     # nothing twice
        self.assertEqual([m["content"] for m in homes], ["We moved to Seoul, Mapo"])  # still a fact
        self.assertEqual(len(found), 1)

    async def test_a_chat_cannot_move_or_export_the_memory(self):
        # a web page the model reads could ask for any of these; only the user's terminal does them
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            target = Path(directory) / "out.jsonl"
            async with Client(build_server(Path(directory) / "m.sqlite")) as client:
                answers = [(await client.call_tool("brain_folder", call)).structured_content
                           for call in ({"path": "share"}, {"path": "gm1.abc"},
                                        {"path": str(directory)}, {"share_existing": True},
                                        {"export_to": str(target)})]
        self.assertEqual({a["status"] for a in answers}, {"RUN_IN_TERMINAL"})
        self.assertEqual(answers[1]["command"], 'turnbook folder "gm1.abc"')
        self.assertEqual(answers[4]["command"], f'turnbook export "{target}"')
        self.assertFalse(target.exists())

    async def test_one_projects_conversations_move_without_the_rest(self):
        """"이 프로젝트 대화만 넘겨줘": the folder and its subfolders, nothing else."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            alpha, beta = root / "alpha", root / "beta"
            (alpha / "web").mkdir(parents=True)
            beta.mkdir()
            old, new, out = root / "old.sqlite", root / "new.sqlite", root / "alpha.jsonl"
            with ConversationMemoryStore(old) as turns:
                for i, (cwd, text) in enumerate([(alpha, "alpha: launch is Nov 14"),
                                                 (alpha / "web", "alpha web: new login page"),
                                                 (beta, "beta: my salary talk"),
                                                 ("alpha", "relative: from another PC's path"),
                                                 (None, "no folder: dentist on Friday")]):
                    turns.record({"turn_id": f"t{i}", "client": "claude-code",
                                  "client_session_id": "s", "scope": "global", "role": "user",
                                  "cwd": str(cwd) if cwd else None, "content_redacted": text,
                                  "raw_sha256": f"t{i}", "source_path": "p",
                                  "happened_at": "2026-03-01T09:00:00",
                                  "known_at": "2026-03-01T09:00:00", "source_ordinal": i})
            async with Client(build_server(old)) as client:
                asked = (await client.call_tool("brain_folder", {
                    "export_to": str(out), "project": str(alpha)})).structured_content
            self.assertEqual(asked["command"],
                             f'turnbook export "{out}" --project "{alpha}"')
            here = os.getcwd()
            os.chdir(root)      # a path that is not absolute here must not resolve into alpha
            try:
                async with Client(build_server(old, terminal=True)) as client:
                    done = (await client.call_tool("brain_folder", {
                        "export_to": str(out), "project": str(alpha)})).structured_content
                    missing = (await client.call_tool("brain_folder", {
                        "export_to": str(out), "project": str(root / "nope")})
                        ).structured_content
            finally:
                os.chdir(here)
            self.assertEqual(done["exported"]["turns"], 2)
            self.assertEqual(missing["status"], "REFUSED")
            async with Client(build_server(new, terminal=True)) as client:
                await client.call_tool("brain_folder", {"import_from": str(out)})
            with ConversationMemoryStore(new) as turns:
                moved = sorted(t["content"] for t in turns.latest(limit=10)["turns"])
        self.assertEqual(moved, ["alpha web: new login page", "alpha: launch is Nov 14"])


class MeaningTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_memory_is_found_by_meaning_not_only_by_its_words(self):
        # OpenCode has no capture: what the model saved is all there is, and "what breed is my
        # dog?" shares no word with it (two of three OpenCode models answered "not in memory")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            async with Client(build_server(Path(directory) / "m.sqlite")) as client:
                await client.call_tool("brain_remember", {
                    "content": "Our golden retriever Kongi turned three last week, about 30 kg."})
                await client.call_tool("brain_remember", {"content": "We use Postgres."})
                found = {}
                for query in ("What breed is my dog?", "우리 강아지 품종이 뭐였지?",
                              "Paris weather forecast?"):
                    rows = (await client.call_tool("brain_recall", {"query": query})
                            ).structured_content.get("matches", [])
                    found[query] = [m["content"][:10] for m in rows]
        self.assertEqual(found["What breed is my dog?"][:1], ["Our golden"])
        from turnbook.local_embedder import LOCAL_MODEL, PRESETS
        if LOCAL_MODEL == PRESETS["multilingual"]:   # the English model reads no Korean
            self.assertEqual(found["우리 강아지 품종이 뭐였지?"][:1], ["Our golden"])
        self.assertEqual(found["Paris weather forecast?"], [])


class TerminalCommandTests(unittest.TestCase):
    def test_a_path_that_would_run_a_command_is_refused(self):
        """The user pastes this command: a path like  x"; evil; #  must not run evil."""
        from turnbook.mcp_server import folder_commands
        self.assertEqual(folder_commands(r"C:\Users\me\Brain", False, None, None),
                         r'turnbook folder "C:\Users\me\Brain"')
        for bad in [r'C:\Memory"; Write-Output X; #', "a$(id)", "x & calc", "a`b", "x\nrm"]:
            with self.assertRaises(ValueError, msg=bad):
                folder_commands(None, False, bad, None)


if __name__ == "__main__":
    unittest.main()
