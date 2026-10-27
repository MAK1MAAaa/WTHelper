from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.chest_log import ChestErrorTail, MAX_LINE_BYTES, MAX_READ_BYTES


ERROR = b"2026 INFO POST api/frontend/chests/open code=70006\n"


class ChestErrorTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.log"
        self.path.write_bytes(b"")
        self.tail = ChestErrorTail(self.path)

    def append(self, content: bytes) -> None:
        with self.path.open("ab") as file:
            file.write(content)

    def test_mark_ignores_history_and_reports_new_complete_error_once(self) -> None:
        self.path.write_bytes(ERROR)
        self.tail.mark()
        self.assertIsNone(self.tail.poll())
        self.append(b"other request\n" + ERROR)
        self.assertEqual(self.tail.poll(), "70006")
        self.assertIsNone(self.tail.poll())

    def test_requires_chest_endpoint_and_exact_integer_code(self) -> None:
        self.tail.mark()
        self.append(
            b"POST api/frontend/chests/open code=700060\n"
            b"POST api/frontend/chests/other code=70006\n"
            b"POST api/frontend/chests/open errorcode=70006\n"
        )
        self.assertIsNone(self.tail.poll())
        self.append(b"POST api/frontend/chests/open code=70006, retry\n")
        self.assertEqual(self.tail.poll(), "70006")

    def test_existing_unfinished_line_is_not_attributed_to_new_claim(self) -> None:
        self.path.write_bytes(b"POST api/frontend/chests/open")
        self.tail.mark()
        self.append(b" code=70006\n")
        self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertEqual(self.tail.poll(), "70006")

    def test_split_utf8_and_split_line_are_assembled(self) -> None:
        self.tail.mark()
        line = "提示 POST api/frontend/chests/open code=70006\n".encode("utf-8")
        self.append(line[:1])
        self.assertIsNone(self.tail.poll())
        self.append(line[1:20])
        self.assertIsNone(self.tail.poll())
        self.append(line[20:])
        self.assertEqual(self.tail.poll(), "70006")

    def test_truncation_resets_to_new_eof_without_reading_previous_records(self) -> None:
        self.path.write_bytes(b"previous log content longer than truncated file\n")
        self.tail.mark()
        self.path.write_bytes(b"x\n")
        self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertEqual(self.tail.poll(), "70006")

    def test_rotation_resets_to_new_eof_without_attributing_old_error(self) -> None:
        self.tail.mark()
        rotated = self.path.with_name("old.log")
        self.path.replace(rotated)
        self.path.write_bytes(ERROR)
        self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertEqual(self.tail.poll(), "70006")

    def test_missing_file_recovery_discards_initial_contents(self) -> None:
        self.path.unlink()
        self.tail.mark()
        self.assertIsNone(self.tail.poll())
        self.path.write_bytes(ERROR)
        self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertEqual(self.tail.poll(), "70006")

    def test_access_failure_fails_closed_and_reanchors_on_recovery(self) -> None:
        self.tail.mark()
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertEqual(self.tail.poll(), "70006")

    def test_oversized_unfinished_line_is_discarded_until_its_newline(self) -> None:
        self.tail.mark()
        self.append(b"x" * (MAX_LINE_BYTES + 1))
        self.assertIsNone(self.tail.poll())
        self.append(b" POST api/frontend/chests/open code=70006\n")
        self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertEqual(self.tail.poll(), "70006")

    def test_oversized_backlog_is_skipped_and_following_lines_are_processed(self) -> None:
        self.tail.mark()
        self.append(b"x\n" * ((MAX_READ_BYTES // 2) + 1) + ERROR)
        self.assertIsNone(self.tail.poll())
        self.append(ERROR)
        self.assertEqual(self.tail.poll(), "70006")

    def test_poll_without_mark_and_invalid_utf8_do_not_report(self) -> None:
        self.append(ERROR)
        self.assertIsNone(self.tail.poll())
        self.tail.mark()
        self.append(b"\xff POST api/frontend/chests/open code=70006\n")
        self.assertIsNone(self.tail.poll())


if __name__ == "__main__":
    unittest.main()
