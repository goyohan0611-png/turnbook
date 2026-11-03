"""Two independent MCP server processes share work without sharing client memory."""
import _isolate  # noqa: F401
from pathlib import Path
import asyncio
import os
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters


class WorkStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_assignment_and_handover_across_real_stdio_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = {key: value for key, value in os.environ.items() if key in (
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA",
                "USERPROFILE", "HOME")}
            env.update(TURNBOOK_HOME=str(root / "home"), TURNBOOK_DB=str(root / "brain.sqlite"),
                       TURNBOOK_CAPTURED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       HF_HUB_OFFLINE="1")
            script = ("import sys;sys.path.insert(0,sys.argv[1]);"
                      "from turnbook.mcp_server import build_server;"
                      "build_server(sys.argv[2]).run()")
            arguments = (["-P"] if sys.version_info >= (3, 11) else []) + ["-c", script,
                         str(Path(__file__).resolve().parents[1] / "src"),
                         str(root / "brain.sqlite")]
            parameters = StdioServerParameters(command=sys.executable, args=arguments, env=env,
                                                cwd=str(root))

            async def call(client, action, **values):
                response = await client.call_tool("brain_work", {
                    "action": action, "workspace": str(root), **values})
                self.assertFalse(response.is_error, str(response))
                return response.structured_content

            async with Client(parameters, read_timeout_seconds=30) as codex, \
                    Client(parameters, read_timeout_seconds=30) as claude:
                await call(codex, "assign", task="implement", agent="coordinator",
                           expected_revision=0, objective="로그인 구현", assigned_to="codex")
                await call(codex, "assign", task="verify", agent="coordinator",
                           expected_revision=0, objective="변경 검토", assigned_to="claude-code",
                           depends_on=["implement"])
                blocked = await call(claude, "claim", task="verify", agent="claude-code:one",
                                     expected_revision=1)
                self.assertEqual(blocked["reason"], "DEPENDENCIES_NOT_COMPLETE")
                await call(codex, "claim", task="implement", agent="codex:one", expected_revision=1)
                await call(codex, "checkpoint", task="implement", agent="codex:one",
                           expected_revision=2, summary="구현 완료 <private>hiddenphrase</private>",
                           references=["src/login.py"], checks=["Fixture check recorded"])
                await call(codex, "complete", task="implement", agent="codex:one",
                           expected_revision=3)
                review = await call(claude, "claim", task="verify", agent="claude-code:one",
                                    expected_revision=1)
                self.assertEqual(review["inputs"][0]["summary"], "구현 완료")
                self.assertEqual(review["inputs"][0]["references"], ["src/login.py"])
                await call(claude, "complete", task="verify", agent="claude-code:one",
                           expected_revision=2)
                await call(codex, "claim", task="continue", agent="codex:one",
                           expected_revision=0, objective="남은 작업")
                await call(codex, "pause", task="continue", agent="codex:one",
                           expected_revision=1, summary="파일 수정 완료", next_action="회귀 검사")
                inherited = await call(claude, "claim", task="continue", agent="claude-code:two",
                                       expected_revision=2)
                self.assertEqual(inherited["work"]["next_action"], "회귀 검사")
                race = await asyncio.gather(
                    call(codex, "claim", task="race", agent="codex:one", expected_revision=0,
                         objective="One shared task"),
                    call(claude, "claim", task="race", agent="claude-code:one", expected_revision=0,
                         objective="One shared task"))
                self.assertEqual(sorted(r["status"] for r in race), ["CONFLICT", "OK"])

            # A third, newly started server sees the same state and cannot silently steal it.
            async with Client(parameters, read_timeout_seconds=30) as fresh:
                response = await fresh.call_tool("brain_work_status", {
                    "workspace": str(root), "task": "continue"})
                self.assertFalse(response.is_error, str(response))
                known = response.structured_content
                self.assertEqual(known["work"]["owner"], "claude-code:two")
                blocked = await call(fresh, "claim", task="continue", agent="opencode:three",
                                     expected_revision=known["revision"])
                self.assertEqual(blocked["reason"], "OWNER_STILL_ACTIVE")
            self.assertNotIn(b"hiddenphrase", (root / "brain.sqlite").read_bytes())


if __name__ == "__main__":
    unittest.main()
