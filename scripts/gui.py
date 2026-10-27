"""Windows graphical control panel for the Wuwa Tappo helper tools."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

if __package__:
    from . import auto_chest
    from .desktop_locator import AmbiguousDesktopError, find_desktop_executable, valid_desktop_executable
    from .gui_backend import BackendError, JobEvent, JobManager, update_task_error
    from .gui_config import load_desktop_path, save_desktop_path
    from .gui_layout import build_layout, configure_styles
    from .runtime import is_frozen, project_root
    from .gui_theme import PAPER
    from .window_chrome import apply_window_chrome
else:
    import auto_chest
    from desktop_locator import AmbiguousDesktopError, find_desktop_executable, valid_desktop_executable
    from gui_backend import BackendError, JobEvent, JobManager, update_task_error
    from gui_config import load_desktop_path, save_desktop_path
    from gui_layout import build_layout, configure_styles
    from runtime import is_frozen, project_root
    from gui_theme import PAPER
    from window_chrome import apply_window_chrome


PROJECT_ROOT = project_root()
MODE_LABELS = {
    "键盘（F24）": "keyboard",
    "鼠标左键": "mouse",
    "键盘与鼠标": "both",
}


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
        self.desktop_executable = load_desktop_path()
        self.chest_status = tk.StringVar(value="未启动")
        self.tap_status = tk.StringVar(value="未启动")
        self.chest_detail = tk.StringVar(value="准备就绪" if self.admin else "请先以管理员身份运行")
        self.tap_detail = tk.StringVar(value="准备就绪" if self.admin else "请先以管理员身份运行")
        self.task_errors: dict[str, str] = {}
        self.sleep_warning_shown = False
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
        root.geometry("900x450")
        root.minsize(860, 440)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_style()
        self._build()
        root.after_idle(lambda: apply_window_chrome(root, PAPER))
        for value in (self.mode, self.count, self.rate, self.delay):
            value.trace_add("write", self._update_tap_hint)
        self._update_tap_hint()
        root.after(150, self._poll)

    def _configure_style(self) -> None:
        configure_styles(self.root)

    def _build(self) -> None:
        build_layout(self, tuple(MODE_LABELS))

    def _update_tap_hint(self, *_: object) -> None:
        if self.mode.get() != "键盘（F24）":
            self.tap_hint.set("鼠标模式会真实左击当前位置，请先将光标移至安全空白区域。")
            return
        try:
            settings = parse_tap_settings(self.count.get(), self.rate.get(), self.delay.get(), self.mode.get())
            self.tap_hint.set(f"发送 F24，预计 {settings.count / settings.rate:.1f} 秒；不输入文字。")
        except ValueError:
            self.tap_hint.set("请输入有效参数；按 ESC 也可停止。")

    def _require_admin(self) -> bool:
        if self.admin:
            return True
        messagebox.showinfo("需要管理员权限", "请点击“以管理员身份运行”，再启动任务。", parent=self.root)
        return False

    def choose_desktop(self) -> Path | None:
        if self.closing or self.manager.is_active("chest"):
            messagebox.showinfo("任务正在运行", "请先停止自动领取，再选择桌宠。", parent=self.root)
            return None
        selected = filedialog.askopenfilename(
            title="选择 WuwaTappo.exe", filetypes=[("桌宠程序", "WuwaTappo.exe"), ("可执行文件", "*.exe")],
            parent=self.root,
        )
        if not selected:
            return None
        path = valid_desktop_executable(selected)
        if path is None:
            messagebox.showerror("桌宠程序无效", "请选择有效的 WuwaTappo.exe。", parent=self.root)
            return None
        self.desktop_executable = path
        try:
            save_desktop_path(path)
        except (OSError, ValueError) as error:
            messagebox.showwarning("无法保存桌宠路径", f"本次仍可使用，重启后可能需要重新选择。\n{error}", parent=self.root)
        self.chest_detail.set("已选择桌宠程序")
        return path

    def resolve_desktop(self) -> Path | None:
        try:
            path = find_desktop_executable(preferred=self.desktop_executable,
                                           fallback=auto_chest.DEFAULT_EXECUTABLE)
        except AmbiguousDesktopError:
            messagebox.showinfo("发现多个桌宠", "请手动选择要使用的 WuwaTappo.exe。", parent=self.root)
            return self.choose_desktop()
        except (OSError, RuntimeError):
            messagebox.showinfo("无法自动查找桌宠", "请手动选择要使用的 WuwaTappo.exe。", parent=self.root)
            return self.choose_desktop()
        if path is None:
            messagebox.showinfo("未找到桌宠", "请手动选择桌宠安装目录中的 WuwaTappo.exe。", parent=self.root)
            return self.choose_desktop()
        self.desktop_executable = path
        return path

    def start_chest(self) -> None:
        if self.closing or not self._require_admin():
            return
        try:
            if existing_chest_instance():
                raise BackendError("已有自动领取脚本运行中，请先停止原 CMD 或图形界面中的任务。")
            executable = self.resolve_desktop()
            if executable is None:
                return
            self.manager.start_chest(allow_sleep=self.allow_sleep.get(), dry_run=self.chest_preview.get(), exe=executable)
        except BackendError as error:
            messagebox.showerror("无法开始自动领取", str(error), parent=self.root)

    def start_tap(self) -> None:
        if self.closing or not self._require_admin():
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
        detail = self.chest_detail if event.job == "chest" else self.tap_detail
        if event.kind == "started":
            self.task_errors.pop(event.job, None)
            if event.job == "chest":
                self.sleep_warning_shown = False
            status.set("启动中")
            detail.set("正在启动任务")
        elif event.kind == "output":
            line = event.message.strip()
            old_error = self.task_errors.get(event.job, "")
            error = update_task_error(old_error, line)
            if error:
                self.task_errors[event.job] = error
            else:
                self.task_errors.pop(event.job, None)
            if error != old_error and error:
                detail.set("任务异常，请查看提示")
            if "无法阻止系统自动睡眠" in line and not self.sleep_warning_shown:
                self.sleep_warning_shown = True
                messagebox.showwarning("无法阻止自动睡眠", "请检查 Windows 电源设置；系统睡眠时自动领取会暂停。", parent=self.root)
            if event.job in self.stop_requested:
                return
            if event.job == "chest":
                status.set(chest_status_from_line(event.message, status.get()))
                for marker, message in (
                    ("已连接桌宠主页面", "已连接，等待宝箱"),
                    ("请从托盘完全退出桌宠", "请从托盘完全退出桌宠"),
                    ("等待你在桌宠中登录", "请先在桌宠中登录"),
                    ("桌宠当前隐藏", "请先显示桌宠"),
                    ("可用敲击数不足", "可用敲击数不足 1,000"),
                    ("领取成功", "宝箱领取成功"),
                    ("预览：", "预览模式，不会领取"),
                    ("已触发", "正在确认领取结果"),
                    ("服务器冷却中", "冷却中，稍后重试"),
                    ("本地连接暂不可用", "连接暂不可用，正在重试"),
                    ("领取子进程异常退出", "任务异常，正在重启"),
                    ("正在等待可领取宝箱", "正在等待可领取宝箱"),
                ):
                    if marker in line:
                        detail.set(message)
                        break
            else:
                status.set(tap_status_from_line(event.message, status.get()))
                if "检查通过" in line:
                    detail.set("预览完成，未发送输入")
                elif "已发送" in line:
                    detail.set(line[:35])
                elif "发送完成" in line:
                    detail.set("发送完成")
                elif "秒后开始" in line:
                    detail.set(line[:25])
                elif "已取消" in line or "已停止" in line:
                    detail.set("任务已停止")
        elif event.kind == "stopping":
            status.set("停止中")
            detail.set("正在停止并等待清理")
        elif event.kind == "finished":
            stopped = event.job in self.stop_requested
            self.stop_requested.discard(event.job)
            success = event.returncode == 0
            error = event.message or self.task_errors.get(event.job, "")
            self.task_errors.pop(event.job, None)
            cancelled = stopped or status.get() == "已停止"
            expected_stop = event.returncode in (0, 130, -2, 0xC000013A)
            if error:
                status.set("异常")
                detail.set("任务异常，请查看提示")
                messagebox.showerror(f"{name}异常", error, parent=self.root)
            elif not success and not (cancelled and expected_stop):
                status.set("异常")
                detail.set("任务异常退出")
                messagebox.showerror(f"{name}异常", f"任务异常退出（退出码 {event.returncode}）。", parent=self.root)
            else:
                status.set("已停止" if stopped or status.get() == "已停止" else "已完成")
                if stopped:
                    detail.set("任务已停止")

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
        if self.closing and not self.manager.is_active("chest") and not self.manager.is_active("tap"):
            remaining = self.manager.poll_events()
            if remaining:
                for event in remaining:
                    self._handle_event(event)
                self.root.after(0, self._poll)
                return
            self.root.destroy()
            return
        self.root.after(150, self._poll)

    def elevate(self) -> None:
        if self.manager.is_active("chest") or self.manager.is_active("tap"):
            messagebox.showinfo("任务正在运行", "请先停止当前任务。", parent=self.root)
            return
        if request_elevation():
            self.root.destroy()
        else:
            messagebox.showerror("无法启动管理员窗口", "请从启动器右键选择“以管理员身份运行”。", parent=self.root)

    def close(self) -> None:
        if self.closing:
            return
        errors = self.manager.stop_all()
        if errors:
            messagebox.showerror("无法停止任务", "\n".join(errors), parent=self.root)
            return
        self.stop_requested.update(job for job in ("chest", "tap") if self.manager.is_active(job))
        self.closing = True
        if self.manager.is_active("chest"):
            self.chest_detail.set("正在停止并等待清理")
        if self.manager.is_active("tap"):
            self.tap_detail.set("正在停止并等待清理")


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
