"""Small Tk widgets and colors for the WTHelper desktop interface."""

from __future__ import annotations

from collections.abc import Callable
import base64
from functools import lru_cache
import math
import struct
import tkinter as tk
from tkinter import ttk
from typing import Any
import zlib


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


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack("!I", len(data)) + kind + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF))


@lru_cache(maxsize=128)
def _button_surface(width: int, height: int, radius: float, stroke: float,
                    background: tuple[int, int, int], outline: tuple[int, int, int],
                    fill: tuple[int, int, int]) -> bytes:
    """Rasterize a rounded rectangle with antialiased edges using only stdlib."""
    width, height = max(2, width), max(2, height)
    radius = min(radius, (width - 1) / 2, (height - 1) / 2)
    half_width, half_height = (width - 1) / 2, (height - 1) / 2
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            dx = abs(x - half_width) - (half_width - radius)
            dy = abs(y - half_height) - (half_height - radius)
            distance = (math.hypot(max(dx, 0), max(dy, 0))
                        + min(max(dx, dy), 0) - radius)
            outer = max(0.0, min(1.0, 0.5 - distance))
            inner = max(0.0, min(1.0, 0.5 - distance - stroke))
            rows.extend(round(bg * (1 - outer) + edge * (outer - inner) + core * inner)
                        for bg, edge, core in zip(background, outline, fill))
    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(rows, 6)) + _png_chunk(b"IEND", b""))


def _parent_background(parent: tk.Misc, navigation: bool) -> str:
    if navigation:
        return SIDEBAR
    try:
        if isinstance(parent, ttk.Widget):
            style_name = parent.cget("style") or parent.winfo_class()
            return ttk.Style(parent).lookup(style_name, "background") or PANEL
        return str(parent.cget("background"))
    except (tk.TclError, AttributeError):
        return PANEL


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
        color = lambda value: tuple(channel // 257 for channel in self.winfo_rgb(value))
        png = _button_surface(40, 40, 20.0, 0.0, color(PAPER), color(PANEL), color(PANEL))
        source = tk.PhotoImage(master=self, data=base64.b64encode(png))
        self._corners: list[tk.PhotoImage] = []
        for left, top in ((0, 0), (20, 0), (20, 20), (0, 20)):
            corner = tk.PhotoImage(master=self, width=20, height=20)
            self.tk.call(str(corner), "copy", str(source), "-from", left, top, left + 20, top + 20)
            self._corners.append(corner)
        self._horizontal = self.create_rectangle(0, 0, 0, 0, fill=PANEL, outline="")
        self._vertical = self.create_rectangle(0, 0, 0, 0, fill=PANEL, outline="")
        self._corner_items = [self.create_image(0, 0, image=image, anchor="nw") for image in self._corners]
        self._window = self.create_window(10, 10, window=self.content, anchor="nw")
        self.bind("<Configure>", self._resize, add="+")

    def _resize(self, event: tk.Event[Any]) -> None:
        width, height = max(2, event.width), max(2, event.height)
        self.coords(self._horizontal, 20, 0, max(20, width - 20), height)
        self.coords(self._vertical, 0, 20, width, max(20, height - 20))
        for item, (x, y) in zip(self._corner_items, ((0, 0), (width - 20, 0),
                                                    (width - 20, height - 20), (0, height - 20))):
            self.coords(item, x, y)
        self.itemconfigure(self._window, width=max(1, width-20), height=max(1, height-20))


class RoundedButton(tk.Canvas):
    """A focusable rounded button with Tk-like invoke and state semantics."""

    def __init__(
        self, parent: tk.Misc, *, text: str, command: Callable[[], Any],
        width: int = 132, height: int = 40, primary: bool = False, state: str = "normal",
        navigation: bool = False, selected: bool = False,
    ) -> None:
        super().__init__(parent, width=width, height=height, background=_parent_background(parent, navigation),
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
        self._surface_key: tuple[Any, ...] | None = None
        self._surface_image: tk.PhotoImage | None = None
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
        color = lambda value: tuple(channel // 257 for channel in self.winfo_rgb(value))
        key = (width, height, 20.0, 2.0 if self._focused else 1.0,
               color(str(self.cget("background"))), color(outline), color(fill))
        if key != self._surface_key or self._surface_image is None:
            png = _button_surface(*key)
            self._surface_image = tk.PhotoImage(master=self, data=base64.b64encode(png))
            self._surface_key = key
        self.create_image(0, 0, image=self._surface_image, anchor="nw")
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
