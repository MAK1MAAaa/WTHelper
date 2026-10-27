"""Match the Windows title bar to the application's background when supported."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import tkinter as tk


def colorref(color: str) -> int:
    """Convert a six-digit RGB color to the Windows COLORREF representation."""
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError("Expected a six-digit RGB color")
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return red | green << 8 | blue << 16


def apply_window_chrome(root: tk.Tk, background: str) -> None:
    """Color the native frame on supported Windows versions, keeping its controls."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        # Tk's widget handle is a child of its native top-level window.
        handle = user32.GetParent(root.winfo_id())
        if not handle:
            return
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                             ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        value = wintypes.DWORD(colorref(background))
        # Unsupported Windows versions return an HRESULT and retain system styling.
        for attribute in (34, 35):  # border and caption colors
            dwm.DwmSetWindowAttribute(handle, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except (OSError, AttributeError, tk.TclError, ValueError):
        pass
