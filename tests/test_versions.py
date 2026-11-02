"""One version everywhere a release names it: the package, the MCP Registry entry and the Claude
Code plugin, which pins the package it runs (the plugin directory requires an exact version)."""
from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent


class Versions(unittest.TestCase):
    def test_one_version(self):
        package = re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(
            encoding="utf-8"), re.M)[1]
        server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        plugin = (ROOT / "plugins/turnbook/.claude-plugin/plugin.json").read_text(
            encoding="utf-8")
        self.assertEqual(server["version"], package)
        self.assertEqual({p["version"] for p in server["packages"]}, {package})
        self.assertEqual(json.loads(plugin)["version"], package)
        pinned = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "plugins").rglob("*.json"))
        self.assertEqual(set(re.findall(r"turnbook==([\w.]+)", pinned)), {package})
        codex = json.loads((ROOT / "plugins/turnbook/.codex-plugin/plugin.json").read_text(
            encoding="utf-8"))
        self.assertEqual(codex["version"], package)


if __name__ == "__main__":
    unittest.main()
