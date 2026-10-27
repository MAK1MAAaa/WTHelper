from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import window_chrome


class WindowChromeTests(unittest.TestCase):
    def test_rgb_is_encoded_as_colorref(self) -> None:
        self.assertEqual(window_chrome.colorref("#f4f3ef"), 0xEFF3F4)

    def test_invalid_color_is_rejected(self) -> None:
        for color in ("f4f3ef", "#fff", "#xyzxyz"):
            with self.subTest(color=color), self.assertRaises(ValueError):
                window_chrome.colorref(color)

    @patch("scripts.window_chrome.os.name", "nt")
    @patch("scripts.window_chrome.ctypes.WinDLL", create=True)
    def test_native_title_and_border_use_parent_window(self, load: MagicMock) -> None:
        user32, dwm = MagicMock(), MagicMock()
        user32.GetParent.return_value = 4321
        load.side_effect = [user32, dwm]
        root = MagicMock()
        root.winfo_id.return_value = 1234
        received = []
        def capture(handle: int, attribute: int, value: object, size: int) -> int:
            received.append((handle, attribute, ctypes.cast(value, ctypes.POINTER(wintypes.DWORD)).contents.value, size))
            return 0
        dwm.DwmSetWindowAttribute.side_effect = capture
        window_chrome.apply_window_chrome(root, "#f4f3ef")
        user32.GetParent.assert_called_once_with(1234)
        self.assertEqual(received, [(4321, 34, 0xEFF3F4, ctypes.sizeof(wintypes.DWORD)),
                                    (4321, 35, 0xEFF3F4, ctypes.sizeof(wintypes.DWORD))])

    @patch("scripts.window_chrome.os.name", "nt")
    @patch("scripts.window_chrome.ctypes.WinDLL", create=True, side_effect=OSError("unavailable"))
    def test_unavailable_dwm_keeps_native_window_usable(self, _load: MagicMock) -> None:
        window_chrome.apply_window_chrome(MagicMock(), "#f4f3ef")

    @patch("scripts.window_chrome.os.name", "posix")
    @patch("scripts.window_chrome.ctypes.WinDLL", create=True)
    def test_non_windows_does_not_load_native_libraries(self, load: MagicMock) -> None:
        window_chrome.apply_window_chrome(MagicMock(), "#f4f3ef")
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
