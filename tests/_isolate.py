"""Imported first by every test module: Turnbook's per-user home (config, stores, Postgres hub)
becomes an empty temporary folder, so a test run never reads or writes a developer's own memory.

Once a developer's PC joined a shared hub, the tests' saves went into it and its 4,600 real lines
came back into the tests' answers: config.json under the real home was read by every test.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))   # this checkout, first

os.environ["TURNBOOK_HOME"] = tempfile.mkdtemp(prefix="graph-mind-test-home-")
for _name in ("TURNBOOK_CONFIG", "TURNBOOK_FOLDER", "TURNBOOK_DB"):
    os.environ.pop(_name, None)
