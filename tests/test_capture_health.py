import _isolate  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from turnbook.automatic_capture import AutomaticCaptureService

stall = AutomaticCaptureService._stall_warnings


class StallWarningTests(unittest.TestCase):
    def setUp(self):
        self.service = SimpleNamespace(state_dir=Path(tempfile.mkdtemp()),
                                       now=lambda: "2026-10-12T00:00:00Z",
                                       STALL_ROWS=AutomaticCaptureService.STALL_ROWS)

    def run_pass(self, rows, turns=0):
        return stall(self.service, {"claude_code": {"scanned_rows": rows, "recorded_turns": turns},
                                    "codex": {"status": "SKIPPED"}})

    def test_rows_without_turns_add_up_across_passes_then_warn(self):
        self.assertEqual(self.run_pass(1500), [])
        warnings = self.run_pass(600)
        self.assertEqual(len(warnings), 1)
        self.assertIn("claude_code: 2100", warnings[0])

    def test_a_turn_found_clears_the_warning(self):
        self.run_pass(2500)
        self.assertEqual(self.run_pass(10, turns=1), [])
        self.assertEqual(self.run_pass(1999), [])


if __name__ == "__main__":
    unittest.main()
