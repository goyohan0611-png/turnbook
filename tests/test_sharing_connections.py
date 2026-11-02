"""Read connections are reused while healthy, and closed on failure and shutdown."""
import _isolate  # noqa: F401
from unittest import mock
import unittest

import psycopg

from turnbook import sharing


class SharingConnectionTests(unittest.TestCase):
    def test_initialization_failure_closes_the_new_connection(self):
        connection = mock.MagicMock()
        connection.execute.side_effect = RuntimeError("schema unavailable")
        with mock.patch("psycopg.connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "schema unavailable"):
                sharing._pg("fixture")
        connection.close.assert_called_once()

    def test_reconnect_closes_the_dropped_reader(self):
        old, new = mock.MagicMock(closed=False), mock.MagicMock(closed=False)
        old.execute.side_effect = psycopg.OperationalError("connection lost")
        new.execute.return_value.fetchall.return_value = [(1, "line")]
        with mock.patch.dict(sharing._READERS, {}, clear=True), \
                mock.patch.object(sharing, "_pg", side_effect=[old, new]):
            self.assertEqual(sharing._read_rows("fixture", "SELECT", ()), [(1, "line")])
            old.close.assert_called_once()
            self.assertIs(sharing._READERS["fixture"], new)
            sharing.close_readers()
            new.close.assert_called_once()

    def test_healthy_reader_is_reused_until_explicit_cleanup(self):
        reader = mock.MagicMock(closed=False)
        with mock.patch.dict(sharing._READERS, {}, clear=True), \
                mock.patch.object(sharing, "_pg", return_value=reader) as connect:
            sharing._read_rows("fixture", "SELECT", ())
            sharing._read_rows("fixture", "SELECT", ())
            connect.assert_called_once()
            reader.close.assert_not_called()
            sharing.close_readers()
            sharing.close_readers()
            reader.close.assert_called_once()

    def test_one_close_failure_does_not_leave_other_readers_open(self):
        first, second = mock.MagicMock(), mock.MagicMock()
        first.close.side_effect = RuntimeError("already broken")
        with mock.patch.dict(sharing._READERS, {"a": first, "b": second}, clear=True):
            sharing.close_readers()
            self.assertEqual(sharing._READERS, {})
        second.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
