"""Shared executable and path handling for source and frozen builds."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import sys
from typing import Iterator, Sequence


WORKER_NAMES = {
    "auto_chest.py": "chest",
    "auto_chest_supervisor.py": "supervisor",
    "tap_boost.py": "tap",
    "save_tool.py": "save",
    "selftest_stop.py": "selftest-stop",
}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


@contextmanager
def external_dll_search() -> Iterator[None]:
    """Keep a frozen runtime's DLL directory out of unrelated executables."""
    if not is_frozen() or os.name != "nt":
        yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    set_directory = kernel.SetDllDirectoryW
    set_directory.argtypes = [wintypes.LPCWSTR]
    set_directory.restype = wintypes.BOOL
    if not set_directory(None):
        raise OSError(ctypes.get_last_error(), "无法恢复系统 DLL 搜索路径")
    try:
        yield
    finally:
        set_directory(getattr(sys, "_MEIPASS", None))


def worker_command(script: Path, arguments: Sequence[str], *, unbuffered: bool = False,
                   executable: str | None = None) -> list[str]:
    if is_frozen():
        role = WORKER_NAMES.get(script.name)
        if role is None:
            raise ValueError(f"未知辅助任务：{script.name}")
        return [sys.executable, "--worker", role, *arguments]
    return [executable or sys.executable, *(["-u"] if unbuffered else []), str(script), *arguments]
