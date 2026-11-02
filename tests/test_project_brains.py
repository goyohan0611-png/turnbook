"""Project brains on one PC: checkpoints published on request, links to where the work is (a git
commit at its real stage, a Slack or drive link) and never the files, secrets refused, nothing
personal let in."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from unittest import mock
import base64
import os
import sqlite3
import subprocess
import tempfile
import unittest

from mcp import Client

from turnbook.project_brains import Ledger

KEY = "sk-proj-" + "a" * 40


def ledger(path=None, key=None):
    return Ledger(sqlite3.connect(path or ":memory:"), key=key)


class CheckpointTests(unittest.TestCase):
    def test_publish_needs_an_existing_project(self):
        book = ledger()
        with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
            book.publish("graph-mind", "decision", "x", "yohan")

    def test_timeline_supersede_and_retract(self):
        book = ledger()
        book.create_project("graph-mind", "yohan")
        first = book.publish("graph-mind", "decision", "허브는 PC 한 대", "yohan")
        second = book.publish("graph-mind", "decision", "허브는 PC 두 대로 이중화", "minji",
                              supersedes=first["id"])
        progress = book.publish("graph-mind", "progress", "3D 보기 배포 완료", "yohan",
                                details="memory_view.py; 테스트 3개 통과; 남은 일: GIF",
                                source_refs=["codex-turn-abc", "commit:4211ae2"])
        timeline = book.timeline("graph-mind")
        self.assertEqual([c["id"] for c in timeline], [progress["id"], second["id"], first["id"]])
        self.assertEqual(timeline[2]["status"], "superseded")
        self.assertEqual(timeline[0]["source_refs"], ["codex-turn-abc", "commit:4211ae2"])
        self.assertIn("남은 일", timeline[0]["details"])
        book.retract(progress["id"], "yohan")
        self.assertNotIn(progress["id"], [c["id"] for c in book.timeline("graph-mind")])

    def test_text_is_refused_where_ids_belong_and_secrets_anywhere(self):
        book = ledger()
        book.create_project("p", "a")
        with self.assertRaisesRegex(ValueError, "SOURCE_REFS_ARE_IDS"):
            book.publish("p", "progress", "ok", "a", source_refs=["x" * 300])
        with self.assertRaisesRegex(ValueError, "SECRET_IN_CHECKPOINT"):
            book.publish("p", "progress", "ok", "a", details=f"키는 {KEY}")


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.book = ledger()
        self.book.create_project("proposal", "yohan")

    def test_a_link_is_kept_and_a_file_is_not(self):
        shared = self.book.attach("proposal", "https://lab241.slack.com/archives/C1/p17",
                                  "제안서 v2 슬랙에 올림", "yohan",
                                  context={"what": "가격표 수정", "review_focus": "5p 가격"})
        self.assertEqual(shared["artifacts"], [{"kind": "link",
                                                "url": "https://lab241.slack.com/archives/C1/p17"}])
        self.assertEqual(self.book.timeline("proposal")[0]["context"]["what"], "가격표 수정")
        deck = self.dir / "제안서.pptx"
        deck.write_bytes(b"PK")
        with self.assertRaisesRegex(ValueError, "FILES_STAY_WHERE_THEY_ARE"):
            self.book.attach("proposal", deck, "제안서", "yohan")

    def test_a_git_folder_is_linked_with_the_stage_it_reached(self):
        repo = self.dir / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                        capture_output=True)
        run("init", "-q")
        (repo / "app.py").write_text("print(1)\n")
        run("add", ".")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
        checkpoint = self.book.attach("proposal", repo, None, "yohan")
        link = checkpoint["artifacts"][0]
        self.assertEqual((link["kind"], len(link["commit"]), link["stage"]),
                         ("git", 40, "committed"))              # not pushed: never "done"


class ScanTests(unittest.TestCase):
    def test_card_numbers_are_found_only_when_they_pass_the_check(self):
        from turnbook.project_brains import scan_text
        self.assertEqual(scan_text("card 4111 1111 1111 1111"), ["REDACTED_CARD"])
        self.assertEqual(scan_text("build 4111 1111 1111 1112"), [])


class SealedTests(unittest.TestCase):
    def test_with_a_key_summaries_are_not_stored_in_clear(self):
        from cryptography.fernet import Fernet
        path = Path(tempfile.mkdtemp()) / "hub.sqlite"
        book = ledger(path, key=Fernet(base64.urlsafe_b64encode(b"k" * 32)))
        book.create_project("p", "a")
        book.publish("p", "decision", "사업계획 확정", "a", details="매출 목표 30억")
        book.db.close()
        raw = path.read_bytes()
        self.assertNotIn("사업계획".encode(), raw)
        self.assertNotIn("매출".encode(), raw)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_through_the_assistant_and_personal_memory_stays_out(self):
        from turnbook.mcp_server import build_server
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"TURNBOOK_CONFIG": str(root / "config.json")}):
            async with Client(build_server(root / "brain.sqlite")) as client:
                await client.call_tool("brain_remember", {
                    "content": "개인 메모: 연봉 협상 전략", "title": "private",
                    "memory_type": "fact", "source_ref": "t"})
                call = lambda **a: client.call_tool("brain_checkpoint", a)  # noqa: E731
                refused = (await call(action="publish", project="graph-mind", type="progress",
                                      summary="x")).structured_content
                self.assertEqual(refused["status"], "REFUSED")
                unnamed = (await call(action="create_project", project="graph-mind")
                           ).structured_content
                self.assertEqual(unnamed["status"], "NAME_REQUIRED")       # not this PC's name
                named = (await call(action="set_name", name="고요한")).structured_content
                self.assertEqual(named["name"], "고요한")
                await call(action="create_project", project="graph-mind", path=str(root))
                # no project named: the bound working folder picks it
                await call(action="publish", workspace=str(root), type="progress",
                           summary="제안서 v1 완료", details="남은 일: 5p 가격표")
                linked = (await call(action="attach", workspace=str(root),
                                     path="https://drive.google.com/file/d/abc",
                                     summary="제안서 v1")).structured_content
                self.assertEqual(linked["artifacts"][0]["kind"], "link")
                timeline = (await call(action="timeline",
                                       project="graph-mind")).structured_content
        home = Path(os.environ["TURNBOOK_HOME"])
        dump = "\n".join(sqlite3.connect(home / "ledger.sqlite").iterdump())
        self.assertNotIn("연봉", dump)                 # personal memory never reaches it
        self.assertEqual(len(timeline["checkpoints"]), 2)


class CodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_code_goes_where_it_belongs_whichever_tool_gets_it(self):
        """The user pastes 'a code'; a team invite given to the brain-folder tool joins the team,
        and the user's own-PC code given to the team tool becomes a terminal command."""
        from turnbook.mcp_server import build_server
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"TURNBOOK_CONFIG": str(root / "config.json")}), \
                mock.patch("turnbook.project_brains.join_team",
                           return_value={"joined_as": "민지", "projects": ["p"]}) as team, \
                mock.patch("turnbook.sharing.set_folder",
                           side_effect=ValueError("INVALID_CONNECTION_CODE")) as own:
            async with Client(build_server(root / "brain.sqlite")) as client:
                await client.call_tool("brain_checkpoint", {"action": "set_name", "name": "민지"})
                joined = (await client.call_tool("brain_folder", {"path": "gmt1.e30"})
                          ).structured_content
                self.assertEqual(joined["joined_as"], "민지")
                team.assert_called_once()
                mine = (await client.call_tool("brain_checkpoint", {
                    "action": "join_team", "code": "gm1.e30"})).structured_content
                # where own-PC codes go: the user's terminal, never a chat
                self.assertEqual(mine["status"], "RUN_IN_TERMINAL")
                self.assertEqual(mine["command"], 'turnbook folder "gm1.e30"')
                own.assert_not_called()


if __name__ == "__main__":
    unittest.main()
