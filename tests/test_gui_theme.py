"""Interaction tests for the custom button used by the desktop UI."""

import tkinter as tk
from types import SimpleNamespace
import unittest

from scripts.gui_theme import RoundedButton


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

    def test_button_outline_has_a_real_round_corner(self) -> None:
        shape = self.button.find_all()[0]
        points = self.button.coords(shape)
        width = self.button.winfo_width()
        self.assertAlmostEqual(points[0], width - 17, places=3)
        self.assertAlmostEqual(points[1], 1, places=3)
        self.assertTrue(any(abs(x - 1) < 0.001 for x in points[::2]))
