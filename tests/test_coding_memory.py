import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import tempfile
import unittest

from turnbook.coding_memory import CodingMemoryCapture, CodingMemoryStore


T1 = "2026-09-15T10:00:00"


class CodingMemoryTests(unittest.TestCase):
    def test_snapshot_diff_symbols_and_indexed_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state = root / "workspace", root / "state"
            workspace.mkdir()
            source = workspace / "app.py"
            source.write_text("def greet(name):\n    return 'hi ' + name\n", encoding="utf-8")
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store,
                    project_id="project:demo", session_id="session:1",
                    state_dir=state, now=lambda: T1)
                baseline = capture.initialize_baseline()
                self.assertEqual(baseline["file_count"], 1)
                source.write_text("def greet(name):\n    return f'hello {name}'\n\n"
                                  "def farewell(name):\n    return f'bye {name}'\n",
                                  encoding="utf-8")
                result = capture.scan(reason="Improve greeting",
                                      related_tests=["test_greet"])
                self.assertEqual(result["status"], "CHANGES_RECORDED")
                self.assertEqual(result["change_count"], 1)
                self.assertIn("greet", result["changes"][0]["symbols"])
                self.assertIn("farewell", result["changes"][0]["symbols"])
                self.assertIn("hello", Path(result["changes"][0]["diff_ref"])
                              .read_text(encoding="utf-8"))
                queried = store.query(project_id="project:demo", symbol="greet",
                                      happened_from="2026-09-15T00:00:00+09:00",
                                      happened_to="2026-09-15T23:59:59+09:00")
                self.assertEqual(queried["status"], "KNOWN")
                self.assertEqual(queried["changes"][0]["file_path"], "app.py")
                self.assertEqual(queried["changes"][0]["reason"], "Improve greeting")
                self.assertEqual(queried["changes"][0]["related_tests"], ["test_greet"])
                self.assertIn("hello", queried["changes"][0]["diff_excerpt"])
                self.assertFalse(queried["changes"][0]["diff_truncated"])
                self.assertEqual(capture.scan(reason="No new change")["status"],
                                 "NO_CHANGES")

    def test_project_file_action_filters_and_unknown_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state = root / "workspace", root / "state"
            workspace.mkdir()
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store,
                    project_id="project:one", session_id="session:1",
                    state_dir=state, now=lambda: T1)
                capture.initialize_baseline()
                (workspace / "new.py").write_text("class Added:\n    pass\n", encoding="utf-8")
                capture.scan(reason="Add class")
                (workspace / "test_new.py").write_text("def test_added():\n    pass\n",
                                                       encoding="utf-8")
                capture.scan(reason="Add test")
                found = store.query(project_id="project:one", file_path="new.py",
                                    change_kind="ADDED")
                missing = store.query(project_id="project:two")
            self.assertEqual(found["changes"][0]["change_kind"], "ADDED")
            self.assertEqual(found["changes"][0]["file_path"], "new.py")
            self.assertEqual(found["changes"][0]["symbols"][0]["symbol_name"], "Added")
            self.assertEqual((missing["status"], missing["reason"]),
                             ("UNKNOWN", "NO_MATCHING_CODE_CHANGE"))

    def test_unchanged_files_and_excluded_folders_are_not_read(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "node_modules" / "lib").mkdir(parents=True)
            (workspace / "node_modules" / "lib" / "big.js").write_text("x = 1\n")
            (workspace / ".venv-rivals" / "Lib").mkdir(parents=True)        # any name
            (workspace / ".venv-rivals" / "pyvenv.cfg").write_text("home = x\n")
            (workspace / ".venv-rivals" / "Lib" / "site.py").write_text("y = 1\n")
            (workspace / "a.py").write_text("a = 1\n")
            (workspace / "b.py").write_text("b = 1\n")
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store, project_id="project:demo",
                                              session_id="session:1", state_dir=root / "state",
                                              now=lambda: T1)
                first = capture.snapshot()
                self.assertEqual(sorted(first), ["a.py", "b.py"])
                (workspace / "b.py").write_text("b = 22\n")
                read = []
                original = Path.read_bytes
                with mock.patch.object(Path, "read_bytes",
                                       lambda p: read.append(p.name) or original(p)):
                    second = capture.snapshot(first)
                self.assertEqual(read, ["b.py"])
                self.assertEqual(second["a.py"], first["a.py"])
                self.assertNotEqual(second["b.py"]["sha256"], first["b.py"]["sha256"])

    def test_a_newly_excluded_folder_is_not_recorded_as_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "envy").mkdir(parents=True)
            (workspace / "envy" / "lib.py").write_text("x = 1\n")
            (workspace / "app.py").write_text("a = 1\n")
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store, project_id="project:demo",
                                              session_id="session:1", state_dir=root / "state",
                                              now=lambda: T1)
                capture.initialize_baseline()
                (workspace / "envy" / "pyvenv.cfg").write_text("home = x\n")  # now excluded
                self.assertEqual(capture.scan(reason="r")["status"], "NO_CHANGES")
                (workspace / "app.py").unlink()                         # a real deletion
                self.assertEqual(capture.scan(reason="r")["changes"][0]["change_kind"],
                                 "DELETED")

    def test_a_manifest_that_could_not_be_saved_does_not_record_the_change_twice(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "app.py").write_text("a = 1\n")
            clock = iter(f"2026-09-15T10:00:0{i}" for i in range(10))
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store, project_id="project:demo",
                                              session_id="session:1", state_dir=root / "state",
                                              now=lambda: next(clock))
                capture.initialize_baseline()
                (workspace / "app.py").write_text("a = 2\n")
                with mock.patch.object(capture, "_write_manifest",
                                       side_effect=PermissionError("locked")):
                    with self.assertRaises(PermissionError):
                        capture.scan(reason="r")
                again = capture.scan(reason="r")             # the next poll, a second later
                self.assertEqual(again["record_results"][0]["disposition"], "ALREADY_RECORDED")
                (workspace / "app.py").write_text("a = 1\n")
                capture.scan(reason="r")
                (workspace / "app.py").write_text("a = 2\n")      # the same edit, made again
                self.assertEqual(capture.scan(reason="r")["record_results"][0]["disposition"],
                                 "RECORDED")

    def test_only_the_current_version_of_each_file_is_kept_as_a_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "app.py").write_text("a = 1\n")
            clock = iter(f"2026-09-15T10:00:0{i}" for i in range(10))
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store, project_id="project:demo",
                                              session_id="session:1", state_dir=root / "state",
                                              now=lambda: next(clock))
                capture.initialize_baseline()
                for value in ("a = 22\n", "a = 333\n", "a = 4444\n"):
                    (workspace / "app.py").write_text(value)
                    last = capture.scan(reason="r")
                self.assertEqual(len(list((root / "state" / "blobs").glob("*/*.blob"))), 1)
                patch = Path(last["changes"][0]["diff_ref"]).read_text(encoding="utf-8")
                self.assertIn("-a = 333", patch)                # the history is in the patches
                self.assertIn("+a = 4444", patch)

    def test_false_deletions_of_files_still_there_are_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            for name in ("junk.py", "gone.py", "back.py"):
                (workspace / name).write_text(f"# {name}\n")
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store, project_id="project:demo",
                                              session_id="session:1", state_dir=root / "state",
                                              now=lambda: T1)
                capture.initialize_baseline()
                for name in ("junk.py", "gone.py", "back.py"):
                    (workspace / name).unlink()
                capture.scan(reason="r")                              # three DELETED
                (workspace / "back.py").write_text("# again\n")
                capture.scan(reason="r")                              # back.py ADDED again
                (workspace / "junk.py").write_text("# junk.py\n")     # there, never re-seen
                self.assertEqual(capture.drop_false_deletions(), 1)
                kinds = sorted((row["file_path"], row["change_kind"]) for row in store.db.execute(
                    "SELECT file_path,change_kind FROM coding_changes"))
                self.assertEqual(kinds, [("back.py", "ADDED"), ("back.py", "DELETED"),
                                         ("gone.py", "DELETED")])
                self.assertEqual(len(list((root / "state" / "diffs").iterdir())), 3)

    def test_secrets_in_changed_files_are_not_stored(self):
        """A config file's password went into the stored copy and its diff as typed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config = workspace / "local.yaml"
            config.write_text("host: db\n", encoding="utf-8")
            with CodingMemoryStore(root / "brain.sqlite") as store:
                capture = CodingMemoryCapture(workspace, store, project_id="project:demo",
                                              session_id="session:1", state_dir=root / "state",
                                              now=lambda: T1)
                capture.initialize_baseline()
                config.write_text("host: db\npassword: sensitive456\n", encoding="utf-8")
                result = capture.scan(reason="Add password")
            stored = "".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in (root / "state").rglob("*") if p.is_file())
            self.assertEqual(result["change_count"], 1)
            self.assertNotIn("sensitive456", stored)


if __name__ == "__main__":
    unittest.main()
