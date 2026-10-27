"""Generate bounded Windows input events for a running WuwaTappo instance."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import math
import os
import sys
import threading
import time
from collections.abc import Sequence
from typing import TextIO


MAX_COUNT = 1_000_000
MAX_RATE = 1_000
MAX_BATCH = 16
VK_ESCAPE = 0x1B
VK_F24 = 0x87
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
ULONG_PTR = ctypes.c_size_t


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class InputUnion(ctypes.Union):
    _fields_ = [
        ("mi", MouseInput),
        ("ki", KeyboardInput),
        ("hi", HardwareInput),
    ]


class Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]


class ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ULONG_PTR),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class WindowsInput:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("此脚本仅支持 Windows。")
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(Input) != expected_size:
            raise RuntimeError("Windows INPUT 结构大小不正确，已停止。")

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = wintypes.SHORT
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
        self.kernel32.Process32FirstW.restype = wintypes.BOOL
        self.kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
        self.kernel32.Process32NextW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def app_running(self) -> bool:
        handle = self.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ProcessEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = self.kernel32.Process32FirstW(handle, ctypes.byref(entry))
            while has_entry:
                if entry.szExeFile.casefold() == "wuwatappo.exe":
                    return True
                has_entry = self.kernel32.Process32NextW(handle, ctypes.byref(entry))
            error = ctypes.get_last_error()
            if error != ERROR_NO_MORE_FILES:
                raise ctypes.WinError(error)
            return False
        finally:
            self.kernel32.CloseHandle(handle)

    def escape_pressed(self) -> bool:
        return bool(self.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)

    def send(self, events: Sequence[Input]) -> None:
        array = (Input * len(events))(*events)
        ctypes.set_last_error(0)
        sent = self.user32.SendInput(len(array), array, ctypes.sizeof(Input))
        if sent != len(array):
            error = ctypes.get_last_error()
            raise RuntimeError(
                f"SendInput 仅接受 {sent}/{len(array)} 个事件，错误码 {error}。"
                "请确认脚本与桌宠权限一致；Windows 权限隔离可能阻止输入。"
            )


def keyboard_event(flags: int = 0) -> Input:
    event = Input(type=INPUT_KEYBOARD)
    event.ki = KeyboardInput(wVk=VK_F24, dwFlags=flags)
    return event


def mouse_event(flags: int) -> Input:
    event = Input(type=INPUT_MOUSE)
    event.mi = MouseInput(dwFlags=flags)
    return event


def cycle_events(mode: str) -> list[Input]:
    events: list[Input] = []
    if mode in {"keyboard", "both"}:
        events.extend([keyboard_event(), keyboard_event(KEYEVENTF_KEYUP)])
    if mode in {"mouse", "both"}:
        events.extend([mouse_event(MOUSEEVENTF_LEFTDOWN), mouse_event(MOUSEEVENTF_LEFTUP)])
    return events


def release_events(mode: str) -> list[Input]:
    events: list[Input] = []
    if mode in {"keyboard", "both"}:
        events.append(keyboard_event(KEYEVENTF_KEYUP))
    if mode in {"mouse", "both"}:
        events.append(mouse_event(MOUSEEVENTF_LEFTUP))
    return events


def bounded_int(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_COUNT:
        raise argparse.ArgumentTypeError(f"次数必须介于 1 和 {MAX_COUNT} 之间。")
    return number


def bounded_rate(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= MAX_RATE:
        raise argparse.ArgumentTypeError(f"速率必须介于 1 和 {MAX_RATE} 轮/秒之间。")
    return number


def bounded_delay(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 30:
        raise argparse.ArgumentTypeError("启动延迟必须介于 0 和 30 秒之间。")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=bounded_int, default=10_000, help="发送轮数，默认 10000")
    parser.add_argument("--rate", type=bounded_rate, default=200.0, help="每秒轮数，默认 200，上限 1000")
    parser.add_argument("--mode", choices=("keyboard", "mouse", "both"), default="keyboard")
    parser.add_argument("--delay", type=bounded_delay, default=5.0, help="启动倒计时秒数，默认 5")
    parser.add_argument("--dry-run", action="store_true", help="只验证参数、结构和进程，不发送输入")
    parser.add_argument("--stdin-stop", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def stdin_stop_reader(stop: threading.Event, stream: TextIO) -> None:
    """Read stop commands without leaving a blocked pipe reader at shutdown."""
    if os.name == "nt":
        import msvcrt
        try:
            descriptor = stream.fileno()
            handle = msvcrt.get_osfhandle(descriptor)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetFileType.argtypes = [wintypes.HANDLE]
            kernel.GetFileType.restype = wintypes.DWORD
            if kernel.GetFileType(handle) == 3:
                peek = kernel.PeekNamedPipe
                peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
                peek.restype = wintypes.BOOL
                pending = bytearray()
                while not stop.wait(0.05):
                    available = wintypes.DWORD()
                    if not peek(handle, None, 0, None, ctypes.byref(available), None):
                        stop.set()
                        return
                    if not available.value:
                        continue
                    chunk = os.read(descriptor, min(available.value, 4096))
                    if not chunk:
                        stop.set()
                        return
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        if line.strip() == b"0":
                            stop.set()
                            return
                    if len(pending) > 4096:
                        pending.clear()
                return
        except (AttributeError, OSError, ValueError):
            pass
    try:
        for line in stream:
            if line.strip() == "0":
                stop.set()
                return
    except (OSError, ValueError):
        pass
    stop.set()


def wait_until(api: WindowsInput, deadline: float, stop: threading.Event | None = None) -> bool:
    while True:
        if (stop is not None and stop.is_set()) or api.escape_pressed():
            return False
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 0.02))


def run(args: argparse.Namespace, api: WindowsInput, stop: threading.Event | None = None) -> int:
    running = api.app_running()
    events = cycle_events(args.mode)
    per_cycle = 2 if args.mode == "both" else 1
    print(
        f"模式: {args.mode}；轮数: {args.count:,}；速率: {args.rate:g} 轮/秒；"
        f"预计按下次数: {args.count * per_cycle:,}；INPUT: {ctypes.sizeof(Input)} 字节。",
        flush=True,
    )
    if args.dry_run:
        print(f"检查通过；桌宠运行状态: {'运行中' if running else '未运行'}；未发送输入。")
        return 0
    if not running:
        raise RuntimeError("未发现 WuwaTappo.exe，请先启动桌宠。")
    if args.mode in {"mouse", "both"}:
        print("鼠标模式会在当前光标位置执行左键点击，请先将光标移到可安全点击的空白区域。", flush=True)
    print("键盘模式使用 F24。按住 ESC 或在终端按 Ctrl+C 可停止。", flush=True)

    countdown_end = time.perf_counter() + args.delay
    previous_second = -1
    while time.perf_counter() < countdown_end:
        remaining = math.ceil(countdown_end - time.perf_counter())
        if remaining != previous_second:
            print(f"{remaining} 秒后开始。", flush=True)
            previous_second = remaining
        if not wait_until(api, min(countdown_end, time.perf_counter() + 0.1), stop):
            print("已取消，未发送输入。")
            return 130

    sent_cycles = 0
    started = time.perf_counter()
    next_report = started + 1.0
    next_process_check = started
    next_batch_at = started
    batch_limit = max(1, min(MAX_BATCH, int(args.rate * 0.02)))
    try:
        while sent_cycles < args.count:
            now = time.perf_counter()
            if now >= next_process_check:
                if not api.app_running():
                    raise RuntimeError("桌宠已退出，停止发送。")
                next_process_check = now + 1.0
            if not wait_until(api, next_batch_at, stop):
                print("收到停止请求，已停止。", flush=True)
                return 130
            batch = min(batch_limit, args.count - sent_cycles)
            send_started = time.perf_counter()
            api.send(events * batch)
            sent_cycles += batch
            next_batch_at = max(next_batch_at, send_started) + batch / args.rate
            now = time.perf_counter()
            if now >= next_report:
                print(f"已发送 {sent_cycles:,}/{args.count:,} 轮。", flush=True)
                next_report = now + 1.0
    except (KeyboardInterrupt, RuntimeError):
        try:
            api.send(release_events(args.mode))
        except RuntimeError:
            pass
        raise
    finally:
        elapsed = time.perf_counter() - started
        print(f"累计发送 {sent_cycles:,} 轮，用时 {elapsed:.2f} 秒。", flush=True)
    print("发送完成。以上为系统接受的事件数，桌宠实际增加量请以界面为准。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    stop = threading.Event()
    reader: threading.Thread | None = None
    if args.stdin_stop and not args.dry_run:
        reader = threading.Thread(target=stdin_stop_reader, args=(stop, sys.stdin), daemon=True)
        reader.start()
    try:
        return run(args, WindowsInput(), stop)
    except KeyboardInterrupt:
        print("已由 Ctrl+C 停止。", file=sys.stderr)
        return 130
    except (OSError, RuntimeError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    finally:
        stop.set()
        if reader is not None:
            reader.join(timeout=0.3)


if __name__ == "__main__":
    raise SystemExit(main())
