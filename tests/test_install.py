"""install.py on a fresh, fake home: every app found gets the memory server, a second run (or a
run after moving the folder) leaves exactly one entry, and the user's other settings survive."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
from unittest import mock
import base64
import json
import os
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
            self.assertEqual(toml.count("[mcp_servers.graph-mind-memory]"), 1)
            self.assertIn('args = ["-m", "turnbook.mcp_server"]', toml)
            self.assertIn("[mcp_servers.other]", toml)
            self.assertNotIn("'old'", toml)
            self.assertNotIn("required", toml)       # a failing server must not block a chat
            self.assertIn("TURNBOOK_CAPTURED", toml)
            code_cli = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
            self.assertEqual(code_cli["keep"], 1)
            self.assertEqual(code_cli["mcpServers"]["graph-mind-memory"]["command"], sys.executable)
            app = json.loads((desktop / "claude_desktop_config.json").read_text(encoding="utf-8"))
            entry = app["mcpServers"]["graph-mind-memory"]
            self.assertEqual(entry["args"], install.SERVER)
            self.assertEqual(entry["env"]["MINE"], "1")
            self.assertNotIn("type", entry)
            self.assertNotIn("TURNBOOK_CAPTURED", entry["env"])     # desktop chat: not captured
            self.assertEqual(code_cli["mcpServers"]["graph-mind-memory"]["env"]
                             ["TURNBOOK_CAPTURED"], "1")
            login_item = {
                "win32": home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu"
                         / "Programs" / "Startup" / "Graph-MIND Automatic Capture.lnk",
                "darwin": home / "Library" / "LaunchAgents" / "com.graph-mind.capture.plist",
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
                self.assertNotIn("graph-mind-memory", toml)
                self.assertIn("[mcp_servers.other]", toml)
                self.assertIn("model = 'x'", toml)
                code_cli = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
                self.assertNotIn("graph-mind-memory", code_cli["mcpServers"])
                self.assertEqual(code_cli["keep"], 1)
                app = json.loads((desktop / "claude_desktop_config.json")
                                 .read_text(encoding="utf-8"))
                self.assertNotIn("graph-mind-memory", app["mcpServers"])
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
                self.assertIn("graph-mind-memory", cursor)
                self.assertEqual(read(home / ".gemini" / "settings.json")["theme"], "Dracula")
                vs = read(vscode / "mcp.json")["servers"]
                self.assertEqual(vs["graph-mind-memory"]["type"], "stdio")
                self.assertIn("other", vs)
                install.uninstall([])
                cursor = read(home / ".cursor" / "mcp.json")["mcpServers"]
                self.assertNotIn("graph-mind-memory", cursor)
                self.assertNotIn("graph-mind-memory", read(vscode / "mcp.json")["servers"])
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
                server = settings["mcp"]["graph-mind-memory"]
                self.assertEqual(server["type"], "local")
                self.assertEqual(server["command"][1:], ["-m", "turnbook.mcp_server"])
                guide = str(folder / "graph-mind.md")
                self.assertEqual(settings["instructions"], ["mine.md", guide])
                self.assertIn("brain_remember", Path(guide).read_text(encoding="utf-8"))
                self.assertEqual(settings["model"], "opencode/big-pickle")
                plugin = (folder / "plugins" / "graph-mind.js").read_text(encoding="utf-8")
                self.assertIn(json.dumps(sys.executable), plugin)
                self.assertNotIn("__PYTHON__", plugin)
                self.assertNotIn("__TRANSCRIPTS__", plugin)
                install.opencode(home)                      # again: no second entry
                self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["instructions"],
                                 ["mine.md", guide])
                install.uninstall([])
                settings = json.loads(config.read_text(encoding="utf-8"))
                self.assertNotIn("graph-mind-memory", settings["mcp"])
                self.assertEqual(settings["instructions"], ["mine.md"])
                self.assertFalse((folder / "plugins" / "graph-mind.js").exists())

    def test_files_that_are_ours_by_name_go_with_uninstall(self):
        """Left behind empty: Copilot CLI's hooks/turnbook.json and OpenCode's graph-mind.md."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".copilot").mkdir()
            (home / ".config" / "opencode").mkdir(parents=True)
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"), "COPILOT_HOME": "",
                           "TURNBOOK_HOME": str(home / ".graph-mind"),
                           "TURNBOOK_CONFIG": str(home / ".graph-mind" / "config.json")}
            ours = [home / ".copilot" / "hooks" / "turnbook.json",
                    home / ".config" / "opencode" / "graph-mind.md"]
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
