"""Session handovers stay in one workspace and never combine two apps' session ids."""
import _isolate  # noqa: F401
from pathlib import Path
from unittest import mock
import json
import os
import subprocess
import sys
import tempfile
import unittest

from turnbook import hook
from turnbook.conversation_memory import ConversationMemoryStore


def record(store, n, *, client="claude-code", session="previous", cwd="C:/work",
           role="user", text=None):
    store.record({"turn_id": f"resume-{n}", "client": client,
                  "client_session_id": session, "scope": "global", "cwd": cwd, "role": role,
                  "content_redacted": text or f"message {n}", "raw_sha256": f"hash-{n}",
                  "source_path": f"{client}/{session}", "source_ordinal": n,
                  "happened_at": f"2026-11-02T10:{n:02d}:00",
                  "known_at": f"2026-11-02T10:{n:02d}:00"})


class SessionStartTests(unittest.TestCase):
    def test_windows_workspace_and_app_namespaced_session(self):
        with tempfile.TemporaryDirectory() as folder, \
                ConversationMemoryStore(Path(folder) / "brain.sqlite") as store:
            record(store, 1, client="opencode", session="same", text="other app")
            record(store, 2, session="same", cwd="c:\\WORK", text="the task")
            record(store, 3, session="same", cwd="C:/work", role="assistant", text="the result")
            record(store, 4, client="codex", session="same", text="current session")
            record(store, 5, client="document", session="spec", role="document", text="a spec")
            record(store, 6, cwd="C:/work-other", text="private other project")
            turns = store.previous_session("C:/Work", "same", client="codex")
            self.assertEqual([t["content"] for t in turns], ["the task", "the result"])

    def test_posix_paths_stay_case_sensitive_and_relative_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as folder, \
                ConversationMemoryStore(Path(folder) / "brain.sqlite") as store:
            record(store, 1, cwd="/home/me/Project", text="wanted")
            record(store, 2, cwd="/home/me/project", text="another project")
            self.assertEqual([t["content"] for t in store.previous_session(
                "/home/me/Project", "new")], ["wanted"])
            self.assertEqual(store.previous_session("Project", "new"), [])
            self.assertEqual(store.previous_session("/home/me/Project", ""), [])

    def test_documents_and_other_folders_do_not_become_the_handover(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "brain.sqlite"
            with ConversationMemoryStore(database) as store:
                record(store, 1, text="fixed login", role="assistant")
                record(store, 2, client="document", role="document", text="not a conversation")
                record(store, 3, cwd="C:/elsewhere", text="not this project")
            with mock.patch("turnbook.mcp_server.configured_db_path", return_value=database):
                event = {"cwd": "C:/work", "session_id": "new", "source": "startup"}
                self.assertIn("fixed login", hook.left_off(event))
                self.assertNotIn("not a conversation", hook.left_off(event))
                for source in ("resume", "compact", "fork"):
                    self.assertEqual(hook.left_off({**event, "source": source}), "")
                self.assertIn("fixed login", hook.left_off({**event, "source": "clear"}))

    def test_plugin_checks_only_an_unconditional_command_in_the_same_event(self):
        command = "python -m turnbook.hook claude-code SessionStart"
        with tempfile.TemporaryDirectory() as folder, mock.patch("pathlib.Path.home",
                                                               return_value=Path(folder)):
            settings = Path(folder) / ".claude" / "settings.json"
            settings.parent.mkdir()
            for value in ({"note": command}, {"hooks": {"UserPromptSubmit": [
                    {"hooks": [{"command": command}]}]}},
                    {"hooks": {"SessionStart": [{"matcher": "resume", "hooks": [
                        {"command": command}]}]}}, {"hooks": []}, []):
                settings.write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(hook._installer_hook("SessionStart"))
            settings.write_text(json.dumps({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": command}]}]}}), encoding="utf-8")
            self.assertTrue(hook._installer_hook("SessionStart"))
            self.assertFalse(hook._installer_hook("UserPromptSubmit"))
            settings.write_text("broken json", encoding="utf-8")
            self.assertFalse(hook._installer_hook("SessionStart"))

    def test_session_start_cli_exits_after_its_deadline(self):
        script = ("import sys,time; sys.path.insert(0,sys.argv[1]); "
                  "from turnbook import hook; hook.DEADLINE=0.05; "
                  "hook.left_off=lambda event: time.sleep(20); "
                  "hook.main(['claude-code','SessionStart'])")
        result = subprocess.run([sys.executable, "-c", script,
                                 str(Path(__file__).resolve().parents[1] / "src")],
                                input='{"cwd":"C:/work","session_id":"new"}',
                                capture_output=True, text=True, timeout=5, env=os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})


if __name__ == "__main__":
    unittest.main()
