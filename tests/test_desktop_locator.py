"""Discovery tests use temporary fake files and never start the desktop app."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts import desktop_locator as locator


class DesktopLocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.first = root / "one" / "WuwaTappo.exe"
        self.second = root / "two" / "WuwaTappo.exe"
        for path in (self.first, self.second):
            path.parent.mkdir()
            path.write_bytes(b"fake")

    def test_preferred_path_wins_without_discovery(self) -> None:
        with patch.object(locator, "running_desktop_paths", side_effect=AssertionError), \
             patch.object(locator, "registry_desktop_paths", side_effect=AssertionError):
            self.assertEqual(locator.find_desktop_executable(preferred=self.first), self.first.resolve())

    def test_stale_preferred_falls_back_to_running_process(self) -> None:
        with patch.object(locator, "running_desktop_paths", return_value=iter([self.second])), \
             patch.object(locator, "registry_desktop_paths", return_value=iter([self.first])):
            self.assertEqual(locator.find_desktop_executable(preferred=self.first.parent / "missing.exe"),
                             self.second.resolve())

    def test_registry_and_legacy_fallback_are_checked_in_order(self) -> None:
        with patch.object(locator, "running_desktop_paths", return_value=iter([])), \
             patch.object(locator, "registry_desktop_paths", return_value=iter([self.first])):
            self.assertEqual(locator.find_desktop_executable(fallback=self.second), self.first.resolve())
        with patch.object(locator, "running_desktop_paths", return_value=iter([])), \
             patch.object(locator, "registry_desktop_paths", return_value=iter([])):
            self.assertEqual(locator.find_desktop_executable(fallback=self.second), self.second.resolve())

    def test_multiple_paths_are_not_selected_implicitly(self) -> None:
        with patch.object(locator, "running_desktop_paths", return_value=iter([self.first, self.second])), \
             patch.object(locator, "registry_desktop_paths", return_value=iter([])):
            with self.assertRaises(locator.AmbiguousDesktopError) as error:
                locator.find_desktop_executable()
        self.assertEqual(set(error.exception.paths), {self.first.resolve(), self.second.resolve()})

    def test_invalid_names_missing_files_and_duplicates(self) -> None:
        wrong = self.first.parent / "another.exe"
        wrong.write_bytes(b"fake")
        with patch.object(locator, "running_desktop_paths", return_value=iter([wrong, self.first, self.first])), \
             patch.object(locator, "registry_desktop_paths", return_value=iter([])):
            self.assertEqual(locator.find_desktop_executable(), self.first.resolve())
        self.assertIsNone(locator.valid_desktop_executable(wrong))
        self.assertIsNone(locator.valid_desktop_executable(self.first.parent / "missing" / "WuwaTappo.exe"))

    def test_display_icon_parses_quotes_and_resource_indices(self) -> None:
        self.assertEqual(locator._icon_path('"C:\\App, Files\\WuwaTappo.exe",0'),
                         'C:\\App, Files\\WuwaTappo.exe')
        self.assertEqual(locator._icon_path('C:\\App\\WuwaTappo.exe,-1'), 'C:\\App\\WuwaTappo.exe')
        self.assertEqual(locator._icon_path('"broken'), '')

    @unittest.skipUnless(os.name == "nt", "Windows registry discovery")
    def test_registry_reads_user_and_machine_in_both_views(self) -> None:
        import winreg

        class Key:
            def __init__(self, is_root: bool) -> None:
                self.is_root = is_root

            def __enter__(self) -> "Key":
                return self

            def __exit__(self, *_args: object) -> None:
                pass

        seen: list[tuple[object, int]] = []

        def open_key(key: object, name: str, _reserved: int, access: int) -> Key:
            if name == locator.UNINSTALL_KEY:
                seen.append((key, access))
                return Key(True)
            self.assertEqual(name, "WuwaTappo")
            return Key(False)

        def enum_key(_key: object, index: int) -> str:
            if index:
                raise OSError("no more entries")
            return "WuwaTappo"

        def query(_key: object, name: str) -> tuple[str, int]:
            values = {"DisplayName": "Wuwa Tappo", "DisplayIcon": f'"{self.first}",0',
                      "InstallLocation": f'"{self.first.parent}"'}
            return values[name], 1

        with patch.object(winreg, "OpenKey", side_effect=open_key), \
             patch.object(winreg, "EnumKey", side_effect=enum_key), \
             patch.object(winreg, "QueryValueEx", side_effect=query):
            self.assertEqual(locator._unique_valid(locator.registry_desktop_paths()), (self.first.resolve(),))
        self.assertEqual(set(seen), {
            (winreg.HKEY_CURRENT_USER, winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_CURRENT_USER, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
        })
