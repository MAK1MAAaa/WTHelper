"""The optional desktop path is stored in the user state directory."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import gui_config


class GuiConfigTests(unittest.TestCase):
    def test_selection_round_trip_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state" / "settings.json"
            selected = Path(directory) / "a folder" / "WuwaTappo.exe"
            with patch.object(gui_config, "settings_path", return_value=target):
                self.assertIsNone(gui_config.load_desktop_path())
                self.assertFalse(target.parent.exists())
                gui_config.save_desktop_path(selected)
                self.assertEqual(gui_config.load_desktop_path(), selected)
                self.assertEqual([path.name for path in target.parent.iterdir()], ["settings.json"])

    def test_invalid_settings_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            with patch.object(gui_config, "settings_path", return_value=target):
                for value in ("{", "[]", '{"desktop_executable": null}', "x" * (gui_config.MAX_SETTINGS_BYTES + 1)):
                    target.write_text(value, encoding="utf-8")
                    self.assertIsNone(gui_config.load_desktop_path())
