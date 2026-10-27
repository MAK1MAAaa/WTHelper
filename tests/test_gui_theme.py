"""Interaction tests for the custom button used by the desktop UI."""

import tkinter as tk
from types import SimpleNamespace
import unittest

from scripts.gui_theme import PAPER, PANEL, RoundedButton, RoundedPanel, _button_surface


class RoundedButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.attributes("-alpha", 0.0)
        except tk.TclError as error:
            self.skipTest(f"无法创建隔离 Tk 窗口：{error}")
        self.addCleanup(self.root.destroy)
        self.calls: list[str] = []
        self.button = RoundedButton(self.root, text="测试", command=lambda: self.calls.append("执行"))
        self.button.pack()
        self.root.update()

    def test_release_outside_cancels_and_release_inside_invokes(self) -> None:
        self.button._press(SimpleNamespace())
        self.assertEqual(self.calls, [])
        self.button._release(SimpleNamespace(x=-1, y=-1))
        self.assertEqual(self.calls, [])
        self.button._press(SimpleNamespace())
        self.button._release(SimpleNamespace(x=5, y=5))
        self.assertEqual(self.calls, ["执行"])

    def test_disabled_button_does_not_invoke(self) -> None:
        self.button.configure(state="disabled")
        self.button.invoke()
        self.button._key(SimpleNamespace())
        self.assertEqual(self.calls, [])
        self.button.configure(state="normal")
        self.button._key(SimpleNamespace())
        self.assertEqual(self.calls, ["执行"])

    def test_button_corners_have_antialiased_pixels(self) -> None:
        image = self.button._surface_image
        self.assertIsNotNone(image)
        background = image.get(0, 0)
        fill = image.get(24, 12)
        pixels = {image.get(x, y) for x in range(20) for y in range(12)}
        self.assertIn(background, pixels)
        self.assertIn(fill, pixels)
        self.assertGreater(len(pixels), 3)

    def test_unchanged_state_reuses_image_and_cached_surface(self) -> None:
        image = self.button._surface_image
        self.button._draw()
        self.assertIs(self.button._surface_image, image)
        self.assertLessEqual(_button_surface.cache_info().maxsize, 128)
        self.button.configure(state="disabled")
        self.assertIsNot(self.button._surface_image, image)

    def test_corner_background_matches_parent(self) -> None:
        parent = tk.Frame(self.root, background="#f6e9f1")
        parent.pack()
        button = RoundedButton(parent, text="测试", command=lambda: None)
        button.pack()
        self.root.update()
        self.assertEqual(button._surface_image.get(0, 0), (246, 233, 241))

    def test_panel_corners_blend_directly_into_background(self) -> None:
        panel = RoundedPanel(self.root, height=100)
        panel.pack(fill="both", expand=True)
        self.root.geometry("300x200")
        self.root.update()
        corner = panel._corners[0]
        expected_background = tuple(channel // 257 for channel in self.root.winfo_rgb(PAPER))
        expected_fill = tuple(channel // 257 for channel in self.root.winfo_rgb(PANEL))
        self.assertEqual(corner.get(0, 0), expected_background)
        self.assertEqual(corner.get(19, 19), expected_fill)
        self.assertGreater(len({corner.get(x, y) for x in range(20) for y in range(20)}), 2)
        self.assertEqual(panel.coords(panel._corner_items[2]),
                         [float(panel.winfo_width() - 20), float(panel.winfo_height() - 20)])
