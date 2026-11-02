"""install.py on a fresh, fake home: every app found gets the memory server, a second run (or a
run after moving the folder) leaves exactly one entry, and the user's other settings survive."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from contextlib import closing
from pathlib import Path
from unittest import mock
import base64
import json
import os
import sqlite3
import sys
import tempfile
import unittest

from turnbook import install


class InstallTests(unittest.TestCase):
    def test_at_a_terminal_it_asks_before_changing_anything(self):
        with mock.patch.object(install.sys, "stdin") as stdin, \
                mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(install, "agent_hooks") as hooks, \
                mock.patch("builtins.print"):
            stdin.isatty.return_value = True
            self.assertEqual(install.main(["--skip-packages"]), 0)
        hooks.assert_not_called()

    def test_registers_every_app_once_and_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                "model = 'x'\n\n[mcp_servers.graph-mind-memory]\ncommand = 'old'\n"
                "args = ['old.py']\n\n[mcp_servers.graph-mind-memory.env]\nA = '1'\n\n"
                "[mcp_servers.other]\nurl = 'u'\n", encoding="utf-8")
            (home / ".claude.json").write_text(json.dumps({"keep": 1}), encoding="utf-8")
            desktop = home / "AppData" / "Roaming" / "Claude"
            desktop.mkdir(parents=True)
            (desktop / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {
                "graph-mind-memory": {"command": "old", "env": {"MINE": "1"}}}}), encoding="utf-8")
            code = "gm1." + base64.urlsafe_b64encode(json.dumps(
                {"h": ["192.0.2.1"], "p": 54329, "k": "pw"}).encode()).decode()
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"),
                           "TURNBOOK_HOME": str(home / ".graph-mind"),
                           "TURNBOOK_CONFIG": str(home / ".graph-mind" / "config.json")}
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("turnbook.install.shutil.which", return_value=None):
                for _ in range(2):
                    install.main(["--skip-packages", "--skip-model", "--no-start", "--yes",
                                  "--join", code])
            toml = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertEqual(toml.count("[mcp_servers.turnbook]"), 1)
            self.assertNotIn("graph-mind-memory", toml, "the name before 0.11 is replaced")
            self.assertIn('args = ["-m", "turnbook.mcp_server"]', toml)
            self.assertIn("[mcp_servers.other]", toml)
            self.assertNotIn("'old'", toml)
            self.assertNotIn("required", toml)       # a failing server must not block a chat
            self.assertIn("TURNBOOK_CAPTURED", toml)
            code_cli = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
            self.assertEqual(code_cli["keep"], 1)
            self.assertEqual(code_cli["mcpServers"]["turnbook"]["command"], sys.executable)
            app = json.loads((desktop / "claude_desktop_config.json").read_text(encoding="utf-8"))
            self.assertNotIn("graph-mind-memory", app["mcpServers"])
            entry = app["mcpServers"]["turnbook"]
            self.assertEqual(entry["args"], install.SERVER)
            self.assertEqual(entry["env"]["MINE"], "1")
            self.assertNotIn("type", entry)
            self.assertNotIn("TURNBOOK_CAPTURED", entry["env"])     # desktop chat: not captured
            self.assertEqual(code_cli["mcpServers"]["turnbook"]["env"]
                             ["TURNBOOK_CAPTURED"], "1")
            login_item = {
                "win32": home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu"
                         / "Programs" / "Startup" / "Turnbook.lnk",
                "darwin": home / "Library" / "LaunchAgents" / "com.turnbook.capture.plist",
            }.get(sys.platform, home / ".config" / "autostart" / "turnbook-capture.desktop")
            self.assertTrue(login_item.exists(), login_item)
            capture = json.loads((home / ".graph-mind" / "automatic-capture.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(capture["code_workspaces"], [])    # never site-packages
            chosen = json.loads((home / ".graph-mind" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(chosen["memory_folder"], code)

            # and back out: every app unregistered, the user's other settings untouched,
            # the memory kept unless asked to purge it
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("turnbook.install.shutil.which", return_value=None):
                install.uninstall([])
                toml = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
                self.assertNotIn("[mcp_servers.turnbook", toml)
                self.assertIn("[mcp_servers.other]", toml)
                self.assertIn("model = 'x'", toml)
                code_cli = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
                self.assertNotIn("turnbook", code_cli["mcpServers"])
                self.assertEqual(code_cli["keep"], 1)
                app = json.loads((desktop / "claude_desktop_config.json")
                                 .read_text(encoding="utf-8"))
                self.assertNotIn("turnbook", app["mcpServers"])
                self.assertFalse(login_item.exists())
                self.assertTrue((home / ".graph-mind").exists(), "memory kept by default")
                install.uninstall(["--purge", "--yes"])
                self.assertFalse((home / ".graph-mind").exists())

    def test_only_our_codex_tables_are_dropped_however_they_are_written(self):
        text = ('x = 1\n[mcp_servers."graph\\u002dmind-memory"]\nc = 1\n'
                "[ mcp_servers . 'graph-mind-memory' . env ] # ours\nA = 1\n"
                '[mcp_servers."graph-mind-memory.env"]\nB = 1\n'
                '[mcp_servers."graph- mind-memory"]\nC = 1\n')
        self.assertEqual(install._without_codex_block(text),
                         'x = 1\n[mcp_servers."graph-mind-memory.env"]\nB = 1\n'
                         '[mcp_servers."graph- mind-memory"]\nC = 1\n')

    def test_every_installed_mcp_client_is_connected_and_disconnected(self):
        """Cursor, Windsurf, Gemini CLI, LM Studio, VS Code and Cline: each gets the server in
        its own format when its folder exists, keeps its other settings, and loses only our
        entry on uninstall. A VS Code file with comments (not JSON) is left alone."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"),
                           "TURNBOOK_HOME": str(home / ".graph-mind"),
                           "TURNBOOK_CONFIG": str(home / ".graph-mind" / "config.json")}
            with mock.patch.dict(os.environ, environment):
                vscode = install._vscode_user_dir(home)
            (home / ".cursor").mkdir()
            (home / ".cursor" / "mcp.json").write_text(json.dumps(
                {"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
            (home / ".codeium" / "windsurf").mkdir(parents=True)
            (home / ".gemini").mkdir()
            (home / ".gemini" / "settings.json").write_text(json.dumps({"theme": "Dracula"}),
                                                            encoding="utf-8")
            (home / ".lmstudio").mkdir()
            (vscode / "globalStorage" / "saoudrizwan.claude-dev").mkdir(parents=True)
            (vscode / "mcp.json").write_text(json.dumps({"servers": {"other": {"command": "y"}}}),
                                             encoding="utf-8")
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("turnbook.install.shutil.which", return_value=None):
                connected = install.other_apps(home)
                self.assertEqual(sorted(connected), sorted(
                    ["Cursor", "Windsurf", "Gemini CLI", "LM Studio", "VS Code", "Cline"]))

                def read(path):
                    return json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("other", read(home / ".cursor" / "mcp.json")["mcpServers"])
                cursor = read(home / ".cursor" / "mcp.json")["mcpServers"]
                self.assertIn("turnbook", cursor)
                self.assertEqual(read(home / ".gemini" / "settings.json")["theme"], "Dracula")
                vs = read(vscode / "mcp.json")["servers"]
                self.assertEqual(vs["turnbook"]["type"], "stdio")
                self.assertIn("other", vs)
                install.uninstall([])
                cursor = read(home / ".cursor" / "mcp.json")["mcpServers"]
                self.assertNotIn("turnbook", cursor)
                self.assertNotIn("turnbook", read(vscode / "mcp.json")["servers"])
                self.assertEqual(read(home / ".gemini" / "settings.json")["theme"], "Dracula")
                (vscode / "mcp.json").write_text('{\n  // my servers\n  "servers": {}\n}',
                                                 encoding="utf-8")
                self.assertNotIn("VS Code", install.other_apps(home))
                self.assertIn("// my servers", (vscode / "mcp.json").read_text(encoding="utf-8"))

    def test_opencode_gets_the_server_and_the_instructions_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"),
                           "TURNBOOK_HOME": str(home / ".graph-mind"),
                           "TURNBOOK_CONFIG": str(home / ".graph-mind" / "config.json")}
            folder = home / ".config" / "opencode"
            folder.mkdir(parents=True)
            config = folder / "opencode.jsonc"
            config.write_text(json.dumps({"model": "opencode/big-pickle",
                                          "instructions": ["mine.md"]}), encoding="utf-8")
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("turnbook.install.shutil.which", return_value=None):
                self.assertEqual(install.opencode(home), "OpenCode")
                settings = json.loads(config.read_text(encoding="utf-8"))
                server = settings["mcp"]["turnbook"]
                self.assertEqual(server["type"], "local")
                self.assertEqual(server["command"][1:], ["-m", "turnbook.mcp_server"])
                guide = str(folder / "turnbook.md")
                self.assertEqual(settings["instructions"], ["mine.md", guide])
                self.assertIn("brain_remember", Path(guide).read_text(encoding="utf-8"))
                self.assertEqual(settings["model"], "opencode/big-pickle")
                plugin = (folder / "plugins" / "turnbook.js").read_text(encoding="utf-8")
                self.assertIn(json.dumps(sys.executable), plugin)
                self.assertNotIn("__PYTHON__", plugin)
                self.assertNotIn("__TRANSCRIPTS__", plugin)
                install.opencode(home)                      # again: no second entry
                self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["instructions"],
                                 ["mine.md", guide])
                install.uninstall([])
                settings = json.loads(config.read_text(encoding="utf-8"))
                self.assertNotIn("turnbook", settings["mcp"])
                self.assertEqual(settings["instructions"], ["mine.md"])
                self.assertFalse((folder / "plugins" / "turnbook.js").exists())

    def test_an_install_from_before_0_11_is_taken_over(self):
        """graph_mind.hook in Claude Code's hooks, the Graph-MIND Startup shortcut, OpenCode's
        graph-mind files: each replaced by the turnbook one, never left beside it."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            old_hook = {"hooks": [{"type": "command",
                                   "command": "python -m graph_mind.hook claude-code x"}]}
            (home / ".claude" / "settings.json").write_text(json.dumps(
                {"hooks": {"UserPromptSubmit": [old_hook]}}), encoding="utf-8")
            opencode = home / ".config" / "opencode"
            (opencode / "plugins").mkdir(parents=True)
            (opencode / "plugins" / "graph-mind.js").write_text("old", encoding="utf-8")
            (opencode / "graph-mind.md").write_text("old", encoding="utf-8")
            (opencode / "opencode.json").write_text(json.dumps(
                {"mcp": {"graph-mind-memory": {}},
                 "instructions": [str(opencode / "graph-mind.md")]}), encoding="utf-8")
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"),
                           "TURNBOOK_HOME": str(home / ".turnbook"),
                           "TURNBOOK_CONFIG": str(home / ".turnbook" / "config.json")}
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("turnbook.install.shutil.which", return_value=None), \
                    mock.patch("builtins.print"):
                old_items = install._old_login_items()
                for item in old_items:
                    item.parent.mkdir(parents=True, exist_ok=True)
                    item.write_text("old", encoding="utf-8")
                install.main(["--skip-packages", "--skip-model", "--no-start", "--yes"])
            hooks = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            commands = json.dumps(hooks)
            self.assertEqual(commands.count("turnbook.hook"), 1)
            self.assertNotIn("graph_mind.hook", commands)
            self.assertEqual([item for item in old_items if item.exists()], [])
            settings = json.loads((opencode / "opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(list(settings["mcp"]), ["turnbook"])
            self.assertEqual(settings["instructions"], [str(opencode / "turnbook.md")])
            self.assertFalse((opencode / "graph-mind.md").exists())
            self.assertFalse((opencode / "plugins" / "graph-mind.js").exists())

    def test_the_memory_folder_moves_with_the_paths_written_in_it(self):
        from turnbook import paths
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"USERPROFILE": directory, "HOME": directory,
                                             "TURNBOOK_HOME": ""}):
            os.environ.pop("TURNBOOK_HOME")
            home = Path(directory)
            old, new = home / ".graph-mind", home / ".turnbook"
            (old / "coding" / "abc").mkdir(parents=True)
            self.assertEqual(paths.turnbook_home(), old, "used where it is until it moves")
            (old / "automatic-capture.json").write_text(json.dumps(
                {"db_path": str(old / "development.sqlite"),
                 "code_workspaces": [{"state_dir": str(old / "coding" / "abc")}]}),
                encoding="utf-8")
            (old / "coding" / "abc" / "coding-manifest.json").write_text(json.dumps(
                {"files": {"a.py": {"blob_ref": str(old / "coding" / "abc" / "x.blob")}}}),
                encoding="utf-8")
            with closing(sqlite3.connect(old / "development.sqlite")) as db:
                db.execute("CREATE TABLE coding_changes(diff_ref TEXT, before_ref TEXT, "
                           "after_ref TEXT)")
                db.execute("INSERT INTO coding_changes VALUES(?,?,NULL)",
                           (str(old / "coding" / "abc" / "d.patch"), "elsewhere"))
                db.commit()
            self.assertTrue(paths.move_old_home())
            self.assertTrue(old.is_symlink() or paths._is_junction(old)
                            or old.resolve() == new.resolve(), "the old name leads to the new")
            self.assertEqual(paths.turnbook_home(), new)
            settings = json.loads((new / "automatic-capture.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["db_path"], str(new / "development.sqlite"))
            self.assertEqual(settings["code_workspaces"][0]["state_dir"],
                             str(new / "coding" / "abc"))
            manifest = (new / "coding" / "abc" / "coding-manifest.json").read_text(
                encoding="utf-8")
            self.assertNotIn(".graph-mind", manifest)
            with closing(sqlite3.connect(new / "development.sqlite")) as db:
                self.assertEqual(db.execute("SELECT diff_ref, before_ref FROM coding_changes"
                                            ).fetchone(),
                                 (str(new / "coding" / "abc" / "d.patch"), "elsewhere"))
            self.assertFalse(paths.move_old_home(), "once")
            # a server still on old code writes through the old name into the new folder
            (old / "automatic-capture").mkdir(exist_ok=True)
            (old / "automatic-capture" / "service.pid").write_text("1", encoding="utf-8")
            self.assertTrue((new / "automatic-capture" / "service.pid").exists())
            self.assertEqual(paths.turnbook_home(), new)

    def test_a_leftover_old_folder_is_linked_only_when_it_holds_nothing(self):
        from turnbook import paths
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"USERPROFILE": directory, "HOME": directory}):
            os.environ.pop("TURNBOOK_HOME", None)
            old, new = Path(directory) / ".graph-mind", Path(directory) / ".turnbook"
            new.mkdir()
            (old / "automatic-capture").mkdir(parents=True)
            (old / "automatic-capture" / "service.pid").write_text("1", encoding="utf-8")
            (old / "notes.sqlite").write_text("real", encoding="utf-8")
            paths.link_old_home()
            self.assertTrue((old / "notes.sqlite").exists(), "never thrown away")
            (old / "notes.sqlite").unlink()
            paths.link_old_home()                          # only bookkeeping left: replaced
            (new / "marker").write_text("x", encoding="utf-8")
            self.assertTrue((old / "marker").exists())

    def test_files_that_are_ours_by_name_go_with_uninstall(self):
        """Left behind empty: Copilot CLI's hooks/turnbook.json and OpenCode's turnbook.md."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".copilot").mkdir()
            (home / ".config" / "opencode").mkdir(parents=True)
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"), "COPILOT_HOME": "",
                           "TURNBOOK_HOME": str(home / ".graph-mind"),
                           "TURNBOOK_CONFIG": str(home / ".graph-mind" / "config.json")}
            ours = [home / ".copilot" / "hooks" / "turnbook.json",
                    home / ".config" / "opencode" / "turnbook.md"]
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("turnbook.install.shutil.which", return_value=None), \
                    mock.patch("builtins.print"):
                install.main(["--skip-packages", "--skip-model", "--no-start", "--yes"])
                self.assertTrue(all(path.exists() for path in ours))
                install.uninstall([])
            self.assertEqual([path for path in ours if path.exists()], [])

    def test_mangled_code_stops_before_anything_changes(self):
        with self.assertRaises(ValueError):
            install.main(["--skip-packages", "--join", "gm1.%%%"])


    def test_model_goes_to_own_folder_when_the_usual_cache_is_locked(self):
        """A fresh Windows profile refused ~/.cache (access denied): Hugging Face's whole home
        moves to ~/.turnbook/huggingface, and every later process is pointed there."""
        from turnbook import local_embedder
        def locked(*_):
            raise PermissionError(5, "Access is denied: ~/.cache/huggingface")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory}), \
                mock.patch.object(local_embedder, "model_paths", locked), \
                mock.patch("turnbook.install.remove_old_models"), \
                mock.patch("turnbook.install.subprocess.run") as retry:
            os.environ.pop("HF_HOME", None)
            install.embedding_model()
            own = str(Path(directory) / "huggingface")
            self.assertEqual(retry.call_args.kwargs["env"]["HF_HOME"], own)
            from turnbook import local_embedder
            local_embedder.use_own_hf_home()          # what the server does when it starts
            self.assertEqual(os.environ["HF_HOME"], own)


if __name__ == "__main__":
    unittest.main()
