"""Work continues across clients, without a stale or competing agent overwriting the baton."""
import _isolate  # noqa: F401
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest import mock
import tempfile
import unittest

from mcp import Client

from turnbook import hook
from turnbook.mcp_server import build_server
from turnbook.work_state import WorkState, workspace_key


class WorkStateTests(unittest.TestCase):
    def test_handover_survives_connections_and_preserves_the_packet(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "brain.sqlite"
            with WorkState(database) as work:
                self.assertIsNone(work.read("C:/project"))
                first = work.change("claim", "C:/project", agent="claude:one",
                                    expected_revision=0, objective="Fix sign in")
                self.assertEqual(first["work"]["state"], "ACTIVE")
                blocked = work.change("claim", "C:/project", agent="codex:two",
                                      expected_revision=1, objective="Something else")
                self.assertEqual(blocked["reason"], "OWNER_STILL_ACTIVE")
                paused = work.change("pause", "C:/project", agent="claude:one",
                                     expected_revision=1, summary="Fixed login.py",
                                     next_action="Run regression tests",
                                     references=["login.py", "commit abc1234"],
                                     checks=["unit: passed"])
                self.assertEqual(paused["revision"], 2)
            with WorkState(database) as work:
                claimed = work.change("claim", "c:\\PROJECT\\", agent="codex:two",
                                      expected_revision=2)
                self.assertEqual(claimed["work"]["summary"], "Fixed login.py")
                self.assertEqual(claimed["work"]["checks"], ["unit: passed"])
                stale = work.change("checkpoint", "C:/project", agent="claude:one",
                                    expected_revision=2, summary="Overwrite")
                self.assertEqual(stale["reason"], "REVISION_CHANGED")
                rejected = work.change("checkpoint", "C:/project", agent="claude:one",
                                       expected_revision=3, summary="Overwrite")
                self.assertEqual(rejected["reason"], "NOT_ACTIVE_OWNER")
                self.assertIsNone(work.read("C:/other-project"))

    def test_two_clients_cannot_claim_the_same_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "brain.sqlite"
            gate = Barrier(2)
            def claim(agent):
                with WorkState(database) as work:
                    gate.wait(timeout=5)
                    return work.change("claim", "/project", agent=agent,
                                       expected_revision=0, objective="One job")
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(claim, ["claude:one", "codex:two"]))
            self.assertEqual(sorted(r["status"] for r in results), ["CONFLICT", "OK"])
            self.assertEqual(next(r for r in results if r["status"] == "CONFLICT")["reason"],
                             "REVISION_CHANGED")

    def test_implementation_and_test_roles_with_versioned_dependencies(self):
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            work.change("assign", "/p", task="implement", agent="coordinator",
                        expected_revision=0, assigned_to="codex", objective="Fix login")
            work.change("assign", "/p", task="test", agent="coordinator",
                        expected_revision=0, assigned_to="claude", objective="Review and test",
                        depends_on=["implement"])
            self.assertEqual(work.change("claim", "/p", task="test", agent="claude:one",
                                         expected_revision=1)["reason"],
                             "DEPENDENCIES_NOT_COMPLETE")
            self.assertEqual(work.change("claim", "/p", task="implement", agent="claude:one",
                                         expected_revision=1)["reason"],
                             "ASSIGNED_TO_ANOTHER_AGENT")
            work.change("claim", "/p", task="implement", agent="codex:one", expected_revision=1)
            # An independent task may run concurrently in the same workspace.
            parallel = work.change("claim", "/p", task="test-design", agent="claude:one",
                                   expected_revision=0, objective="Prepare test cases")
            self.assertEqual(parallel["status"], "OK")
            work.change("complete", "/p", task="implement", agent="codex:one",
                        expected_revision=2, summary="Done", references=["login.py"])
            test = work.change("claim", "/p", task="test", agent="claude:one", expected_revision=1)
            self.assertEqual(test["work"]["dependency_revisions"], {"implement": 3})
            self.assertEqual(test["inputs"][0]["references"], ["login.py"])
            self.assertEqual(test["inputs"][0]["summary"], "Done")
            # The implementation changes after testing starts: the old test cannot be signed off.
            work.change("claim", "/p", task="implement", agent="codex:two",
                        expected_revision=3, objective="Change login again")
            work.change("complete", "/p", task="implement", agent="codex:two", expected_revision=4)
            self.assertEqual(work.change("complete", "/p", task="test", agent="claude:one",
                                         expected_revision=2)["reason"], "DEPENDENCY_CHANGED")
            work.change("pause", "/p", task="test", agent="claude:one", expected_revision=2,
                        next_action="Test the changed implementation", assigned_to="claude")
            work.change("claim", "/p", task="test", agent="claude:two", expected_revision=3)
            self.assertEqual(work.change("complete", "/p", task="test", agent="claude:two",
                                         expected_revision=4)["status"], "OK")
            self.assertEqual(len(work.tasks("/p")), 3)
            self.assertEqual(work.tasks("/other"), [])

    def test_dependencies_cannot_be_removed_or_made_cyclic(self):
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            work.change("claim", "/p", task="a", agent="one", expected_revision=0, objective="A")
            work.change("complete", "/p", task="a", agent="one", expected_revision=1)
            work.change("assign", "/p", task="b", agent="one", expected_revision=0,
                        objective="B", assigned_to="any", depends_on=["a"])
            self.assertEqual(work.change("claim", "/p", task="b", agent="one",
                                         expected_revision=1, depends_on=[])["reason"],
                             "DEPENDENCIES_ARE_FIXED")
            self.assertEqual(work.change("claim", "/p", task="a", agent="one",
                                         expected_revision=2, objective="A2",
                                         depends_on=["b"])["reason"], "DEPENDENCY_CYCLE")
            self.assertEqual(work.change("assign", "/p", task="c", agent="one",
                                         expected_revision=0, objective="C", assigned_to="any",
                                         depends_on=["missing"])["reason"], "MISSING_DEPENDENCY")

    def test_completion_and_clear_never_reuse_a_revision(self):
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            work.change("claim", "/project", agent="one", expected_revision=0, objective="First")
            self.assertEqual(work.change("clear", "/project", agent="two",
                                         expected_revision=1)["reason"], "OWNER_STILL_ACTIVE")
            self.assertEqual(work.change("pause", "/project", agent="one",
                                         expected_revision=1)["reason"], "NEXT_ACTION_REQUIRED")
            work.change("complete", "/project", agent="one", expected_revision=1,
                        summary="All tests pass")
            work.change("clear", "/project", agent="one", expected_revision=2)
            self.assertEqual(work.read("/project")["summary"], "")
            self.assertEqual(work.read("/project")["state"], "EMPTY")
            self.assertEqual(work.change("claim", "/project", agent="two", expected_revision=0,
                                         objective="Second")["reason"], "REVISION_CHANGED")
            self.assertEqual(work.change("claim", "/project", agent="two", expected_revision=3,
                                         objective="Second")["revision"], 4)

    def test_private_text_is_not_saved_even_when_using_the_store_directly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brain.sqlite"
            with WorkState(path) as work:
                result = work.change("claim", "/project", agent="one", expected_revision=0,
                                     objective="Fix <private>hiddenphrase</private> login",
                                     references=["file.py <private>privatepath</private>"])
                self.assertNotIn("hiddenphrase", str(result))
                self.assertNotIn("privatepath", str(result))
            self.assertNotIn(b"hiddenphrase", path.read_bytes())

    def test_paths_and_input_validation(self):
        self.assertEqual(workspace_key("C:/Work/../Project/"), workspace_key("c:\\project"))
        self.assertNotEqual(workspace_key("/Project"), workspace_key("/project"))
        with tempfile.TemporaryDirectory() as folder, \
                WorkState(Path(folder) / "brain.sqlite") as work:
            for path in ("", "relative/path", "C:relative"):
                with self.assertRaises(ValueError):
                    work.read(path)
            with self.assertRaises(ValueError):
                work.change("claim", "/project", agent="one", expected_revision=None)
            with self.assertRaises(ValueError):
                work.change("claim", "/project", agent="one", expected_revision=0,
                            objective="x" * 1001)

    def test_new_session_receives_work_state_without_needing_previous_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "brain.sqlite"
            with WorkState(database) as work:
                work.change("claim", "/project", agent="claude:one", expected_revision=0,
                            objective="Fix login " + "x" * 980,
                            next_action="Run tests " + "y" * 1900)
                work.change("pause", "/project", agent="claude:one", expected_revision=1,
                            summary="Implementation complete " + "z" * 3900)
            with mock.patch("turnbook.mcp_server.configured_db_path", return_value=database):
                text = hook.left_off({"cwd": "/project", "session_id": "new", "source": "startup"})
                self.assertIn("Implementation complete", text)
                self.assertIn("Run tests", text)
                self.assertIn("PAUSED", text)
                self.assertEqual(hook.left_off({"cwd": "/other", "session_id": "new"}), "")


class WorkToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_mcp_sessions_can_pass_work_without_repeating_the_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "brain.sqlite"
            async with Client(build_server(database)) as claude:
                empty = await claude.call_tool("brain_work",
                                               {"action": "status", "workspace": "/p"})
                self.assertEqual(empty.structured_content["revision"], 0)
                await claude.call_tool("brain_work", {"action": "claim", "workspace": "/p",
                    "agent": "claude:s1", "expected_revision": 0, "objective": "Implement sign in"})
                await claude.call_tool("brain_work", {"action": "pause", "workspace": "/p",
                    "agent": "claude:s1", "expected_revision": 1, "summary": "Code done",
                    "next_action": "Run test_login.py", "references": ["login.py"]})
            async with Client(build_server(database)) as codex:
                status = await codex.call_tool("brain_work",
                                              {"action": "status", "workspace": "/p"})
                self.assertEqual(status.structured_content["work"]["next_action"],
                                 "Run test_login.py")
                claimed = await codex.call_tool("brain_work", {"action": "claim", "workspace": "/p",
                    "agent": "codex:s2", "expected_revision": 2})
                self.assertEqual(claimed.structured_content["work"]["owner"], "codex:s2")
                invalid = await codex.call_tool("brain_work", {"action": "claim", "workspace": "p"})
                self.assertEqual(invalid.structured_content["status"], "INVALID")
                await codex.call_tool("brain_work", {"action": "assign", "workspace": "/p",
                    "task": "verify", "agent": "codex:s2", "assigned_to": "claude",
                    "expected_revision": 0, "depends_on": ["main"], "objective": "Review changes"})
                await codex.call_tool("brain_work", {"action": "complete", "workspace": "/p",
                    "agent": "codex:s2", "expected_revision": 3, "summary": "Implementation ready",
                    "references": ["login.py"], "checks": ["12 tests passed"]})
            async with Client(build_server(database)) as reviewer:
                listed = await reviewer.call_tool("brain_work",
                                                 {"action": "list", "workspace": "/p"})
                self.assertEqual(len(listed.structured_content["tasks"]), 2)
                claimed = await reviewer.call_tool("brain_work", {
                    "action": "claim", "workspace": "/p", "task": "verify",
                    "agent": "claude:s3", "expected_revision": 1})
                inputs = claimed.structured_content["inputs"]
                self.assertEqual(inputs[0]["checks"], ["12 tests passed"])
                self.assertEqual(inputs[0]["references"], ["login.py"])


if __name__ == "__main__":
    unittest.main()
