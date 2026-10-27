"""Locate a Windows Wuwa Tappo installation without relying on the helper path."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
from typing import Iterable, Iterator


DESKTOP_NAME = "WuwaTappo.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


class AmbiguousDesktopError(Exception):
    """More than one plausible installation requires an explicit user choice."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self.paths = tuple(paths)
        super().__init__("发现多个桌宠安装位置，请手动选择 WuwaTappo.exe。")


def valid_desktop_executable(value: str | Path | None) -> Path | None:
    """Return an existing, correctly named executable, or None."""
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        if path.name.casefold() != DESKTOP_NAME.casefold() or not path.is_file():
            return None
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _unique_valid(values: Iterable[str | Path]) -> tuple[Path, ...]:
    result: dict[str, Path] = {}
    for value in values:
        path = valid_desktop_executable(value)
        if path is not None:
            result.setdefault(str(path).casefold(), path)
    return tuple(result.values())


def _one_or_none(values: Iterable[str | Path]) -> Path | None:
    paths = _unique_valid(values)
    if len(paths) > 1:
        raise AmbiguousDesktopError(paths)
    return paths[0] if paths else None


def _icon_path(value: str) -> str:
    """Parse a registry DisplayIcon, which may include quotes and an icon index."""
    value = os.path.expandvars(value.strip())
    if value.startswith('"'):
        end = value.find('"', 1)
        return value[1:end] if end > 0 else ""
    return re.sub(r",\s*-?\d+\s*$", "", value).strip()


def registry_desktop_paths() -> Iterator[Path]:
    """Read current-user and machine uninstall entries in both registry views."""
    if os.name != "nt":
        return
    import winreg

    views = ((winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY))
    for hive, view in views:
        try:
            root = winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    entry_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    entry = winreg.OpenKey(root, entry_name, 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                with entry:
                    def read_value(name: str) -> str:
                        try:
                            value, _kind = winreg.QueryValueEx(entry, name)
                        except OSError:
                            return ""
                        return value if isinstance(value, str) else ""

                    display = read_value("DisplayName")
                    if (re.sub(r"[\s_-]+", "", display).casefold() != "wuwatappo"
                            and entry_name.casefold() != "wuwatappo"):
                        continue
                    icon = _icon_path(read_value("DisplayIcon"))
                    if icon:
                        yield Path(icon)
                    location = os.path.expandvars(read_value("InstallLocation").strip().strip('"'))
                    if location:
                        yield Path(location) / DESKTOP_NAME


def running_desktop_paths() -> Iterator[Path]:
    """Get the executable path of running desktop processes using Windows APIs."""
    if os.name != "nt":
        return

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        return
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.szExeFile.casefold() == DESKTOP_NAME.casefold():
                process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                            yield Path(buffer.value)
                    finally:
                        kernel.CloseHandle(process)
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)


def find_desktop_executable(*, preferred: str | Path | None = None,
                            fallback: str | Path | None = None) -> Path | None:
    """Resolve a remembered selection, running desktop, install record, or legacy path.

    Conflicting candidates within a source are never chosen automatically.
    """
    selected = valid_desktop_executable(preferred)
    if selected:
        return selected
    for paths in (running_desktop_paths(), registry_desktop_paths()):
        candidate = _one_or_none(paths)
        if candidate:
            return candidate
    return valid_desktop_executable(fallback)
