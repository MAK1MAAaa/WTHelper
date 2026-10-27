"""Small Tk widgets and colors for the WTHelper desktop interface."""

from __future__ import annotations

from collections.abc import Callable
import math
import tkinter as tk
from tkinter import ttk
from typing import Any


PAPER = "#f4f3ef"
PANEL = "#ffffff"
FIELD = "#f4f7f4"
SIDEBAR = "#31443e"
SIDEBAR_HOVER = "#486359"
INK = "#253a34"
MUTED = "#5f7168"
LINE = "#dfe5de"
GREEN = "#426b59"
GREEN_HOVER = "#315846"
GREEN_PALE = "#e7f0e9"
GOLD = "#e4d19d"


def rounded_polygon(canvas: tk.Canvas, shape_width: int, shape_height: int, radius: int, **options: Any) -> int:
    right, bottom = max(2, shape_width - 1), max(2, shape_height - 1)
    r = min(radius, (right - 1) / 2, (bottom - 1) / 2)
    points: list[float] = []
    for cx, cy, start in ((right - r, 1 + r, -90), (right - r, bottom - r, 0),
                          (1 + r, bottom - r, 90), (1 + r, 1 + r, 180)):
        for step in range(9):
            angle = math.radians(start + step * 90 / 8)
            points.extend((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return canvas.create_polygon(*points, **options)


class RoundedPanel(tk.Canvas):
    """A rounded surface containing normal, keyboard-accessible Tk widgets."""

    def __init__(self, parent: tk.Misc, *, height: int, padding: int = 18) -> None:
        super().__init__(parent, height=height, background=PAPER, borderwidth=0,
                         highlightthickness=0, relief="flat")
        self.content = ttk.Frame(self, style="Card.TFrame", padding=padding)
        self._shape = rounded_polygon(self, 500, height, 20, fill=PANEL, outline=LINE, width=1)
        self._window = self.create_window(10, 10, window=self.content, anchor="nw")
        self.bind("<Configure>", self._resize, add="+")

    def _resize(self, event: tk.Event[Any]) -> None:
        width, height = max(2, event.width), max(2, event.height)
        self.delete(self._shape)
        self._shape = rounded_polygon(self, width, height, 20, fill=PANEL, outline=LINE, width=1)
        self.tag_lower(self._shape)
        self.itemconfigure(self._window, width=max(1, width-20), height=max(1, height-20))


class RoundedButton(tk.Canvas):
    """A focusable rounded button with Tk-like invoke and state semantics."""

    def __init__(
        self, parent: tk.Misc, *, text: str, command: Callable[[], Any],
        width: int = 132, height: int = 40, primary: bool = False, state: str = "normal",
        navigation: bool = False, selected: bool = False,
    ) -> None:
        super().__init__(parent, width=width, height=height, background=SIDEBAR if navigation else PANEL,
                         borderwidth=0, highlightthickness=0, takefocus=True,
                         cursor="hand2" if state != "disabled" else "arrow")
        self._text = text
        self._command = command
        self._state = state
        self._primary = primary
        self._navigation = navigation
        self._selected = selected
        self._hover = False
        self._focused = False
        self._pressed = False
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self.bind("<Enter>", self._enter, add="+")
        self.bind("<Leave>", self._leave, add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<Return>", self._key, add="+")
        self.bind("<space>", self._key, add="+")
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = self.winfo_width() if self.winfo_width() > 1 else int(self.cget("width"))
        height = self.winfo_height() if self.winfo_height() > 1 else int(self.cget("height"))
        if self._navigation:
            fill = SIDEBAR_HOVER if self._selected or self._hover else SIDEBAR
            text = "#ffffff" if self._selected else "#d8e4dc"
            outline = fill
        elif self._state == "disabled":
            fill, text, outline = "#edf1ed", "#8a9a90", "#e3e9e3"
        elif self._primary:
            fill, text, outline = (GREEN_HOVER if self._hover else GREEN), "#ffffff", GREEN
        else:
            fill, text, outline = (GREEN_PALE if self._hover else PANEL), INK, "#cbd9ce"
        if self._focused and self._state != "disabled":
            outline = "#80a88d"
        rounded_polygon(self, width, height, 16, fill=fill, outline=outline, width=2 if self._focused else 1)
        self.create_text(16 if self._navigation else width // 2, height // 2, text=self._text, fill=text,
                         anchor="w" if self._navigation else "center",
                         font=("Microsoft YaHei UI", 10, "bold" if self._primary or self._selected else "normal"))

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self._draw()

    def _enter(self, _event: tk.Event[Any]) -> None:
        self._hover = True
        self._draw()

    def _leave(self, _event: tk.Event[Any]) -> None:
        self._hover = False
        self._draw()

    def _focus_in(self, _event: tk.Event[Any]) -> None:
        self._focused = True
        self._draw()

    def _focus_out(self, _event: tk.Event[Any]) -> None:
        self._focused = False
        self._draw()

    def _press(self, _event: tk.Event[Any]) -> None:
        if self._state != "disabled":
            self._pressed = True
            self.focus_set()

    def _release(self, event: tk.Event[Any]) -> None:
        pressed, self._pressed = self._pressed, False
        if pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()

    def _key(self, _event: tk.Event[Any]) -> str:
        self.invoke()
        return "break"

    def invoke(self) -> Any:
        return self._command() if self._state != "disabled" else None

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "state" in kwargs:
            state = kwargs.pop("state")
            if state not in ("normal", "disabled"):
                raise ValueError("不支持的按钮状态。")
            if self._state != state:
                self._state = state
                if state == "disabled":
                    self._pressed = False
                super().configure(cursor="arrow" if state == "disabled" else "hand2")
                self._draw()
        if cnf is not None or kwargs:
            return super().configure(cnf, **kwargs)
        return None

    config = configure

    def cget(self, key: str) -> Any:
        if key == "state":
            return self._state
        return super().cget(key)
