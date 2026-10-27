from __future__ import annotations

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "save_tool.py"
SPEC = importlib.util.spec_from_file_location("save_tool", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
save_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = save_tool
SPEC.loader.exec_module(save_tool)


class SaveToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "profile"
        self.data.mkdir()
        self.backups = self.root / "backups"
        self.keyboard = {"t": 123, "environment": "cn-cn-prod", "future": [1, 2]}
        self.total = {
            "count": 456,
            "environment": "cn-cn-prod",
            "cuid": "test-user",
            "future": {"enabled": True},
        }
        self.write_record(save_tool.PENDING, self.keyboard)
        self.write_record(save_tool.TOTAL, self.total)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_record(self, spec: object, record: dict[str, object]) -> None:
        (self.data / spec.filename).write_bytes(save_tool.encode_save(record, spec))

    def read_record(self, spec: object) -> dict[str, object]:
        return save_tool.decode_save((self.data / spec.filename).read_bytes(), spec)

    def test_xor_round_trip_and_unknown_fields_preserved(self) -> None:
        for spec, record in (
            (save_tool.PENDING, self.keyboard),
            (save_tool.TOTAL, self.total),
        ):
            raw = save_tool.encode_save(record, spec)
            self.assertEqual(record, save_tool.decode_save(raw, spec))
        replacements = save_tool.prepare_set(self.data, 9000, 10000)
        changed = save_tool.decode_save(
            replacements[save_tool.TOTAL.filename], save_tool.TOTAL
        )
        self.assertEqual(changed, {**self.total, "count": 10000})

    def test_invalid_count_types_and_bounds(self) -> None:
        for value in (-1, True, 1.5, "1", None, 1 << 53):
            with self.subTest(value=value), self.assertRaises(save_tool.SaveError):
                save_tool.checked_count(value)

    def test_unknown_format_environment_and_duplicate_fields(self) -> None:
        for plain in (
            b'garbage',
            b'{"t":1,"environment":"other"}',
            b'{"t":1,"t":2,"environment":"cn-cn-prod"}',
            b'{"t":true,"environment":"cn-cn-prod"}',
            b'[]',
        ):
            with self.subTest(plain=plain), self.assertRaises(save_tool.SaveError):
                save_tool.decode_save(
                    save_tool.xor_bytes(plain, save_tool.PENDING.xor_key),
                    save_tool.PENDING,
                )

    def test_apply_backup_and_restore(self) -> None:
        original = {
            spec.filename: (self.data / spec.filename).read_bytes()
            for spec in (save_tool.PENDING, save_tool.TOTAL)
        }
        replacements = save_tool.prepare_set(self.data, 9000, 10000)
        with patch.object(save_tool, "app_running", return_value=False):
            backup = save_tool.apply_files(self.data, replacements, self.backups)
            self.assertEqual(self.read_record(save_tool.PENDING)["t"], 9000)
            restored = save_tool.load_backup(backup)
            self.assertEqual(restored, original)
            undo_backup = save_tool.apply_files(self.data, restored, self.backups)
        self.assertEqual(self.read_record(save_tool.PENDING), self.keyboard)
        self.assertEqual(self.read_record(save_tool.TOTAL), self.total)
        self.assertEqual(save_tool.load_backup(undo_backup), replacements)

    def test_running_app_prevents_write(self) -> None:
        replacements = save_tool.prepare_set(self.data, 9000, None)
        with patch.object(save_tool, "app_running", return_value=True):
            with self.assertRaises(save_tool.SaveError):
                save_tool.apply_files(self.data, replacements, self.backups)
        self.assertEqual(self.read_record(save_tool.PENDING), self.keyboard)
        self.assertFalse(self.backups.exists())

    def test_cli_preview_does_not_write_or_print_user_identifier(self) -> None:
        output = io.StringIO()
        arguments = [
            "--data-dir", str(self.data),
            "--backup-root", str(self.backups),
            "set", "--pending", "9000",
        ]
        with contextlib.redirect_stdout(output):
            with patch.object(save_tool, "app_running", side_effect=AssertionError):
                self.assertEqual(save_tool.main(arguments), 0)
        self.assertNotIn("test-user", output.getvalue())
        self.assertIn("仅预览", output.getvalue())
        self.assertEqual(self.read_record(save_tool.PENDING), self.keyboard)
        self.assertFalse(self.backups.exists())

    def test_partial_failure_rolls_back(self) -> None:
        replacements = save_tool.prepare_set(self.data, 9000, 10000)
        original_write = save_tool.atomic_write

        def write_with_failure(path: Path, raw: bytes) -> None:
            if path == self.data / save_tool.TOTAL.filename:
                raise OSError("simulated failure")
            original_write(path, raw)

        with patch.object(save_tool, "app_running", return_value=False):
            with patch.object(save_tool, "atomic_write", side_effect=write_with_failure):
                with self.assertRaisesRegex(save_tool.SaveError, "已回滚"):
                    save_tool.apply_files(self.data, replacements, self.backups)
        self.assertEqual(self.read_record(save_tool.PENDING), self.keyboard)
        self.assertEqual(self.read_record(save_tool.TOTAL), self.total)

    def test_backup_rejects_path_traversal_and_unknown_filename(self) -> None:
        self.backups.mkdir()
        for filename in ("../escape.dat", "unknown.dat", "C:\\escape.dat"):
            manifest = {
                "version": 1,
                "environment": "cn-cn-prod",
                "files": {filename: {"sha256": "0" * 64, "size": 1}},
            }
            (self.backups / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.subTest(filename=filename), self.assertRaises(save_tool.SaveError):
                save_tool.load_backup(self.backups)

    def test_backup_rejects_tampered_content(self) -> None:
        replacements = save_tool.prepare_set(self.data, 9000, None)
        backup = save_tool.create_backup(replacements, self.backups)
        target = backup / save_tool.PENDING.filename
        target.write_bytes(target.read_bytes() + b"x")
        with self.assertRaisesRegex(save_tool.SaveError, "校验失败"):
            save_tool.load_backup(backup)


if __name__ == "__main__":
    unittest.main()
