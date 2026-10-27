"""Windows graphical control panel for the Wuwa Tappo helper tools."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

if __package__:
    from . import auto_chest
    from .gui_backend import BackendError, JobEvent, JobManager
    from .gui_layout import build_layout, configure_styles
    from .gui_saves import create_saves_page
    from .runtime import is_frozen, project_root
else:
    import auto_chest
    from gui_backend import BackendError, JobEvent, JobManager
    from gui_layout import build_layout, configure_styles
    from gui_saves import create_saves_page
    from runtime import is_frozen, project_root


PROJECT_ROOT = project_root()
MODE_LABELS = {
    "键盘（F24）": "keyboard",
    "鼠标左键": "mouse",
    "键盘与鼠标": "both",
}
MAX_LOG_LINES = 800


@dataclass(frozen=True, slots=True)
class TapSettings:
    count: int
    rate: float
    delay: float
    mode: str


def parse_tap_settings(count_text: str, rate_text: str, delay_text: str, mode_label: str) -> TapSettings:
    try:
        count = int(count_text.strip())
        rate = float(rate_text.strip())
        delay = float(delay_text.strip())
    except ValueError as error:
        raise ValueError("请输入有效的轮数、速率和启动延迟。") from error
    if not 1 <= count <= 1_000_000:
        raise ValueError("轮数必须在 1 到 1,000,000 之间。")
    if not math.isfinite(rate) or not 1 <= rate <= 1_000:
        raise ValueError("速率必须在 1 到 1,000 轮/秒之间。")
    if not math.isfinite(delay) or not 0 <= delay <= 30:
        raise ValueError("启动延迟必须在 0 到 30 秒之间。")
    if mode_label not in MODE_LABELS:
        raise ValueError("请选择输入模式。")
    return TapSettings(count, rate, delay, MODE_LABELS[mode_label])


def chest_status_from_line(line: str, current: str) -> str:
    if "领取子进程已启动" in line or "自动领取已启动" in line:
        return "启动中"
    if "已连接桌宠主页面" in line:
        return "已连接"
    if "请从托盘完全退出桌宠" in line:
        return "等待退出桌宠"
    if "等待你在桌宠中登录" in line:
        return "等待登录"
    if "本地连接暂不可用" in line or "领取子进程异常退出" in line:
        return "连接重试中"
    if "自动领取已停止" in line or "停止自动领取" in line:
        return "已停止"
    return current


def tap_status_from_line(line: str, current: str) -> str:
    if line.startswith("模式:"):
        return "启动中"
    if "秒后开始" in line:
        return "倒计时"
    if "已发送" in line:
        return "正在发送"
    if "发送完成" in line:
        return "发送完成"
    if "已取消" in line or "已停止" in line:
        return "已停止"
    return current


def gui_output_text(message: str) -> str:
    return (message.replace("CMD 保持打开，输入 0 即退出，也可按 Ctrl+C。", "可在此窗口停止。")
            .replace("；输入 0 可停止", "")
            .replace("；输入 0 退出", "")
            .replace("按住 ESC 或在终端按 Ctrl+C 可停止。", "按住 ESC 或使用停止按钮可结束。"))


def existing_chest_instance() -> bool:
    """Check the existing helper mutex without taking ownership."""
    if os.name != "nt":
        raise BackendError("图形界面仅支持 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel.OpenMutexW(0x00100000, False, "Local\\WuwaTappoAutoChest")
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == 2:
        return False
    if error == 5:
        return True
    raise BackendError(f"无法检查已有自动领取进程（错误码 {error}）。")


def request_elevation() -> bool:
    """Ask Windows to start this GUI with administrator privileges."""
    if os.name != "nt":
        return False
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    shell.ShellExecuteW.restype = ctypes.c_void_p
    frozen = is_frozen()
    parameters = None if frozen else subprocess.list2cmdline([str(Path(__file__).resolve())])
    previous_reset = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
    if frozen:
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        result = shell.ShellExecuteW(None, "runas", sys.executable, parameters, str(PROJECT_ROOT), 1)
    finally:
        if frozen:
            if previous_reset is None:
                os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
            else:
                os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = previous_reset
    return bool(result and result > 32)


class HelperWindow:
    def __init__(self, root: tk.Tk, manager: JobManager | None = None) -> None:
        self.root = root
        self.manager = manager if manager is not None else JobManager()
        self.admin = auto_chest.is_elevated()
        self.closing = False
        self.stop_requested: set[str] = set()
        self.desktop_results: queue.SimpleQueue[bool | None] = queue.SimpleQueue()
        self.desktop_probe_active = False
        self.next_desktop_probe = 0.0
        self.chest_status = tk.StringVar(value="未启动")
        self.tap_status = tk.StringVar(value="未启动")
        self.desktop_status = tk.StringVar(value="桌宠状态：检测中")
        self.mode = tk.StringVar(value="键盘（F24）")
        self.count = tk.StringVar(value="10000")
        self.rate = tk.StringVar(value="200")
        self.delay = tk.StringVar(value="5")
        self.allow_sleep = tk.BooleanVar(value=False)
        self.chest_preview = tk.BooleanVar(value=False)
        self.tap_preview = tk.BooleanVar(value=False)
        self.tap_hint = tk.StringVar()

        root.title("WTHelper · 桌宠助手")
        root.geometry("900x620")
        root.minsize(860, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_style()
        self._build()
        for value in (self.mode, self.count, self.rate, self.delay):
            value.trace_add("write", self._update_tap_hint)
        self._update_tap_hint()
        self._append_log("系统", "界面已就绪。选择需要的功能后点击开始。")
        if not self.admin:
            self._append_log("系统", "当前不是管理员权限；自动领取和刷数前请以管理员身份运行。")
        root.after(150, self._poll)

    def _configure_style(self) -> None:
        configure_styles(self.root)

    def _build(self) -> None:
        build_layout(self, create_saves_page, tuple(MODE_LABELS))

    def _update_tap_hint(self, *_: object) -> None:
        if self.mode.get() != "键盘（F24）":
            self.tap_hint.set("鼠标模式会在光标当前位置真实左击；启动后请移至安全空白区域。")
            return
        try:
            settings = parse_tap_settings(self.count.get(), self.rate.get(), self.delay.get(), self.mode.get())
            self.tap_hint.set(f"发送 F24，预计 {settings.count / settings.rate:.1f} 秒；不输入文字。")
        except ValueError:
            self.tap_hint.set("请输入有效参数；按 ESC 也可停止。")

    def _append_log(self, source: str, message: str) -> None:
        if not message:
            return
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{datetime.now():%H:%M:%S}] {source}  {message}\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_box.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _require_admin(self) -> bool:
        if self.admin:
            return True
        messagebox.showinfo("需要管理员权限", "请点击左侧“以管理员身份运行”，再启动任务。", parent=self.root)
        return False

    def can_write_saves(self) -> bool:
        if self.closing or self.manager.is_active("chest") or self.manager.is_active("tap"):
            return False
        return not existing_chest_instance()

    def _data_operation_idle(self) -> bool:
        if not self.saves_page.is_busy():
            return True
        messagebox.showinfo("本地数据操作进行中", "请等待本地数据操作完成，再启动其他任务。", parent=self.root)
        return False

    def start_chest(self) -> None:
        if self.closing or not self._require_admin() or not self._data_operation_idle():
            return
        try:
            if existing_chest_instance():
                raise BackendError("已有自动领取脚本运行中，请先停止原 CMD 或图形界面中的任务。")
            self.manager.start_chest(allow_sleep=self.allow_sleep.get(), dry_run=self.chest_preview.get())
        except BackendError as error:
            messagebox.showerror("无法开始自动领取", str(error), parent=self.root)

    def start_tap(self) -> None:
        if self.closing or not self._require_admin() or not self._data_operation_idle():
            return
        try:
            settings = parse_tap_settings(self.count.get(), self.rate.get(), self.delay.get(), self.mode.get())
            if settings.mode != "keyboard" and settings.delay < 3 and not self.tap_preview.get():
                raise ValueError("鼠标点击模式需至少 3 秒启动延迟，以便移开光标。")
            self.manager.start_tap(settings.count, settings.rate, settings.mode,
                                   delay=settings.delay, dry_run=self.tap_preview.get())
        except (ValueError, BackendError) as error:
            messagebox.showerror("无法开始刷数", str(error), parent=self.root)

    def stop_job(self, job: str) -> None:
        try:
            if self.manager.stop(job):
                self.stop_requested.add(job)
        except BackendError as error:
            messagebox.showerror("无法停止任务", str(error), parent=self.root)

    def _handle_event(self, event: JobEvent) -> None:
        name = "自动领取" if event.job == "chest" else "敲击计数"
        status = self.chest_status if event.job == "chest" else self.tap_status
        if event.kind == "started":
            status.set("启动中")
            self._append_log(name, f"进程已启动（PID {event.pid}）。")
        elif event.kind == "output":
            self._append_log(name, gui_output_text(event.message))
            if event.job in self.stop_requested:
                return
            if event.job == "chest":
                status.set(chest_status_from_line(event.message, status.get()))
            else:
                status.set(tap_status_from_line(event.message, status.get()))
        elif event.kind == "stopping":
            status.set("停止中")
            self._append_log(name, "正在停止并等待清理。")
        elif event.kind == "finished":
            stopped = event.job in self.stop_requested
            self.stop_requested.discard(event.job)
            success = event.returncode == 0
            status.set("已停止" if stopped else ("已完成" if success else "已退出"))
            suffix = f"（退出码 {event.returncode}）" if event.returncode is not None else ""
            self._append_log(name, (event.message or "进程已结束") + suffix + "。")

    def _probe_desktop(self) -> None:
        try:
            value: bool | None = auto_chest.app_running()
        except (auto_chest.AutoChestError, OSError, subprocess.SubprocessError):
            value = None
        self.desktop_results.put(value)

    def _poll(self) -> None:
        for event in self.manager.poll_events():
            self._handle_event(event)
        while not self.desktop_results.empty():
            value = self.desktop_results.get_nowait()
            self.desktop_probe_active = False
            self.desktop_status.set("桌宠状态：" + ("运行中" if value is True else "未运行" if value is False else "无法检测"))
        now = time.monotonic()
        if not self.closing and not self.desktop_probe_active and now >= self.next_desktop_probe:
            self.desktop_probe_active = True
            self.next_desktop_probe = now + 10
            threading.Thread(target=self._probe_desktop, name="wuwa-desktop-status", daemon=True).start()
        for job, start, stop in (("chest", self.chest_start, self.chest_stop), ("tap", self.tap_start, self.tap_stop)):
            active = self.manager.is_active(job)
            start.configure(state="disabled" if active or self.closing else "normal")
            stop.configure(state="normal" if active and job not in self.stop_requested and not self.closing else "disabled")
        if self.closing and not self.manager.is_active("chest") and not self.manager.is_active("tap") and not self.saves_page.is_busy():
            self.root.destroy()
            return
        self.root.after(150, self._poll)

    def open_directory(self, name: str) -> None:
        if name not in ("backups", "logs"):
            return
        try:
            path = PROJECT_ROOT / name
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except OSError as error:
            messagebox.showerror("无法打开目录", str(error), parent=self.root)

    def elevate(self) -> None:
        if self.manager.is_active("chest") or self.manager.is_active("tap") or self.saves_page.is_busy():
            messagebox.showinfo("任务正在运行", "请先停止当前任务，或等待本地数据操作完成。", parent=self.root)
            return
        if request_elevation():
            self.root.destroy()
        else:
            self._append_log("系统", "未能启动管理员窗口，请从启动器右键选择“以管理员身份运行”。")

    def close(self) -> None:
        if self.closing:
            return
        errors = self.manager.stop_all()
        if errors:
            messagebox.showerror("无法停止任务", "\n".join(errors), parent=self.root)
            return
        self.stop_requested.update(job for job in ("chest", "tap") if self.manager.is_active(job))
        self.closing = True
        self.notebook.tab(self.saves_page, state="disabled")
        self._append_log("系统", "正在等待任务清理后关闭窗口。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elevate", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != "nt":
        print("图形界面仅支持 Windows。", file=sys.stderr)
        return 1
    if args.elevate and not auto_chest.is_elevated() and request_elevation():
        return 0
    root = tk.Tk()
    HelperWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
