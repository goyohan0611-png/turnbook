"""Which search model an install uses: new memories gemma, existing ones mpnet until the capture
service has read them again with gemma; an explicit choice (turnbook model, TURNBOOK_MODEL)
always wins."""
import _isolate  # noqa: F401  (first: never touch the real ~/.turnbook)
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from turnbook.local_embedder import PRESETS, chosen_model, migrating


class ModelChoiceTests(unittest.TestCase):
    def test_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {"TURNBOOK_HOME": directory, "TURNBOOK_MODEL": ""}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(chosen_model(), PRESETS["gemma"])
                self.assertFalse(migrating())
                (home / "development.semantic-embed-mpnet8.vec").mkdir()
                self.assertEqual(chosen_model(), PRESETS["multilingual"])
                self.assertTrue(migrating())
                (home / "model.txt").write_text("english\n", encoding="utf-8")   # 0.9.48's name
                self.assertEqual(chosen_model(), PRESETS["gemma"])
                self.assertFalse(migrating())
                with mock.patch.dict(os.environ, {"TURNBOOK_MODEL": "multilingual"}):
                    self.assertEqual(chosen_model(), PRESETS["multilingual"])

    def test_migration_ends_with_gemma(self):
        from turnbook.automatic_capture import AutomaticCaptureService
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "development.semantic-embed-mpnet8.vec").mkdir()
            service = AutomaticCaptureService.__new__(AutomaticCaptureService)
            service.db_path = home / "development.sqlite"
            left = iter([64, 3, 0])             # pieces read per pass, then none left
            with mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory,
                                              "TURNBOOK_MODEL": ""}), \
                    mock.patch("turnbook.local_embedder.default_embedder"), \
                    mock.patch("turnbook.warmup.warm_turns", lambda *a, **k: next(left)):
                self.assertFalse(service.migrate_some())
                self.assertFalse(service.migrate_some())
                self.assertEqual(chosen_model(), PRESETS["multilingual"])   # mpnet meanwhile
                self.assertTrue(service.migrate_some())
                self.assertEqual(chosen_model(), PRESETS["gemma"])
                self.assertFalse(migrating())

    def test_mpnet_goes_only_when_nothing_reads_it_and_a_chosen_model_never(self):
        from turnbook.local_embedder import unused_models
        mpnet = PRESETS["multilingual"].split("@")[0]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.dict(os.environ, {"TURNBOOK_HOME": directory,
                                              "TURNBOOK_MODEL": ""}):
                (home / "development.semantic-embed-mpnet8.vec").mkdir()
                self.assertNotIn(mpnet, unused_models(), "still read while migrating")
                (home / "model.txt").write_text("gemma\n", encoding="utf-8")
                self.assertIn(mpnet, unused_models())
                (home / "model.txt").write_text("multilingual\n", encoding="utf-8")
                self.assertNotIn(mpnet, unused_models(), "the user chose it")
                custom = "intfloat/multilingual-e5-base@onnx/model.onnx"
                (home / "model.txt").write_text(custom + "\n", encoding="utf-8")
                self.assertNotIn("intfloat/multilingual-e5-base", unused_models())


if __name__ == "__main__":
    unittest.main()
