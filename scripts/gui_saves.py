"""Tkinter page for previewing and safely managing local Wuwa Tappo counts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Literal

if __package__:
    from .gui_backend import console_python
    from .runtime import project_root, worker_command
    from .gui_theme import FIELD, GREEN, GREEN_PALE, INK, LINE, MUTED, PANEL, PAPER, RoundedButton, RoundedPanel
else:
    from gui_backend import console_python
    from runtime import project_root, worker_command
    from gui_theme import FIELD, GREEN, GREEN_PALE, INK, LINE, MUTED, PANEL, PAPER, RoundedButton, RoundedPanel


PROJECT_ROOT = project_root()
SAVE_TOOL = PROJECT_ROOT / "scripts" / "save_tool.py"
MAX_COUNT = (1 << 53) - 1
MAX_OUTPUT_CHARS = 12_000
MAX_LOG_CHARS = 30_000
COMMAND_TIMEOUT = 30.0


class InputError(ValueError):
    """The requested count or backup directory is invalid."""


@dataclass(frozen=True)
class SaveOperation:
    kind: Literal["status", "set", "restore"]
    pending: int | None = None
    total: int | None = None
    backup: Path | None = None


def parse_optional_count(value: str, label: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    if not re.fullmatch(r"[0-9]+", text):
        raise InputError(f"{label}必须是非负整数。")
    try:
        number = int(text)
    except ValueError as error:
        raise InputError(f"{label}数字过长。") from error
    if number > MAX_COUNT:
        raise InputError(f"{label}不能超过 {MAX_COUNT}。")
    return number


def make_set_operation(pending: str, total: str) -> SaveOperation:
    pending_value = parse_optional_count(pending, "待上报增量")
    total_value = parse_optional_count(total, "累计快照")
    if pending_value is None and total_value is None:
        raise InputError("请至少填写一项本地计数。")
    return SaveOperation("set", pending=pending_value, total=total_value)


def make_restore_operation(directory: str) -> SaveOperation:
    text = directory.strip()
    if not text or "\x00" in text:
        raise InputError("请选择有效的备份目录。")
    try:
        path = Path(text).expanduser().absolute()
        if not path.is_dir() or not (path / "manifest.json").is_file():
            raise InputError("备份目录不存在，或没有 manifest.json。")
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError("备份目录路径无效。") from error
    return SaveOperation("restore", backup=path)


def build_command(operation: SaveOperation, *, apply: bool = False) -> list[str]:
    command = worker_command(SAVE_TOOL, [operation.kind], unbuffered=True, executable=console_python())
    if operation.kind == "status":
        if apply or operation.pending is not None or operation.total is not None or operation.backup is not None:
            raise InputError("状态查询不支持写入参数。")
    elif operation.kind == "set":
        if operation.backup is not None or (operation.pending is None and operation.total is None):
            raise InputError("设置操作参数无效。")
        for label, value, flag in (("待上报增量", operation.pending, "--pending"),
                                   ("累计快照", operation.total, "--total")):
            if value is not None:
                if type(value) is not int or not 0 <= value <= MAX_COUNT:
                    raise InputError(f"{label}超出允许范围。")
                command.extend((flag, str(value)))
    elif operation.kind == "restore":
        if operation.backup is None or operation.pending is not None or operation.total is not None:
            raise InputError("恢复操作参数无效。")
        command.append(str(operation.backup))
    else:
        raise InputError("未知操作。")
    if apply:
        command.append("--apply")
    return command


class PreviewGate:
    """Allow a write only for a successful preview at the current input revision."""

    def __init__(self) -> None:
        self.revision = 0
        self._approved: tuple[SaveOperation, int] | None = None

    def invalidate(self) -> None:
        self.revision += 1
        self._approved = None

    def accept(self, operation: SaveOperation, revision: int) -> bool:
        if revision != self.revision or operation.kind == "status":
            return False
        self._approved = (operation, revision)
        return True

    def allows(self, operation: SaveOperation) -> bool:
        return self._approved == (operation, self.revision)

    def allows_kind(self, kind: Literal["set", "restore"]) -> bool:
        return bool(self._approved and self._approved[1] == self.revision
                    and self._approved[0].kind == kind)


def write_denial(can_write: Callable[[], bool] | None) -> str | None:
    """Fail closed when another task is active or its state cannot be checked."""
    if can_write is None:
        return None
    try:
        if can_write() is True:
            return None
    except Exception as error:
        detail = str(error).strip() or type(error).__name__
        return f"无法确认任务状态：{detail[:500]}"
    return "请先停止自动领取和刷数任务。"


@dataclass(frozen=True)
class CommandResult:
    operation: SaveOperation
    apply: bool
    revision: int
    returncode: int | None
    output: str
    timed_out: bool = False


@dataclass(frozen=True)
class CommandNotice:
    text: str


def _bounded_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = value or ""
    if len(text) > MAX_OUTPUT_CHARS:
        return "输出过长，显示末尾内容：\n" + text[-MAX_OUTPUT_CHARS:]
    return text


def execute_command(
    operation: SaveOperation, apply: bool, revision: int,
    notify: Callable[[CommandNotice], None], *, timeout: float = COMMAND_TIMEOUT,
) -> CommandResult:
    command = build_command(operation, apply=apply)
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        process = subprocess.Popen(
            command, cwd=PROJECT_ROOT, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        try:
            output, _ = process.communicate(timeout=timeout)
            return CommandResult(operation, apply, revision, process.returncode, _bounded_output(output))
        except subprocess.TimeoutExpired:
            if apply:
                notify(CommandNotice("写入操作超过 30 秒，仍在执行；请等待完成，避免中断备份或恢复。"))
                output, _ = process.communicate()
                return CommandResult(operation, apply, revision, process.returncode, _bounded_output(output))
            process.kill()
            output, _ = process.communicate()
            return CommandResult(operation, apply, revision, None, _bounded_output(output), timed_out=True)
    except OSError as error:
        return CommandResult(operation, apply, revision, None, f"无法启动存档工具：{error}")


class CommandRunner:
    """Run one command off the UI thread and return events through a queue."""

    def __init__(
        self,
        execute: Callable[[SaveOperation, bool, int, Callable[[CommandNotice], None]], CommandResult] = execute_command,
    ) -> None:
        self.events: queue.Queue[CommandResult | CommandNotice] = queue.Queue()
        self._execute = execute
        self._lock = threading.Lock()
        self._busy = False

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, operation: SaveOperation, *, apply: bool, revision: int) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def worker() -> None:
            try:
                result = self._execute(operation, apply, revision, self.events.put)
            except Exception as error:
                result = CommandResult(operation, apply, revision, None, f"存档工具运行失败：{error}")
            with self._lock:
                self._busy = False
            self.events.put(result)

        try:
            threading.Thread(target=worker, name="wth-save-operation", daemon=True).start()
        except RuntimeError:
            with self._lock:
                self._busy = False
            raise
        return True


class SavesPage(ttk.Frame):
    """Local data controls; actual I/O is delegated to the existing save tool."""

    def __init__(
        self, parent: tk.Misc, on_message: Callable[[str], None] | None = None,
        can_write: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent, padding=2)
        self._on_message = on_message
        self._can_write = can_write
        self._runner = CommandRunner()
        self._gate = PreviewGate()
        self._working = False
        self._alive = True
        self._after_id: str | None = None
        self.pending_var = tk.StringVar(master=self)
        self.total_var = tk.StringVar(master=self)
        self.backup_var = tk.StringVar(master=self)
        self.state_var = tk.StringVar(master=self, value="修改前请先预览。")
        self._build_ui()
        for variable in (self.pending_var, self.total_var, self.backup_var):
            variable.trace_add("write", self._on_input_changed)
        self._refresh_buttons()
        self._after_id = self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Data.TFrame", background=PAPER)
        style.configure("Data.Card.TFrame", background=PANEL)
        style.configure("Data.Card.TLabel", background=PANEL, foreground=INK)
        style.configure("Data.CardMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Data.Muted.TLabel", background=PAPER, foreground=MUTED)
        style.configure("Data.TEntry", fieldbackground=FIELD, foreground=INK,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=(8, 5))
        style.map("Data.TEntry", bordercolor=[("focus", GREEN)])
        self.configure(style="Data.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        status_panel = RoundedPanel(self, height=60, padding=5)
        status_panel.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        status = status_panel.content
        status.configure(style="Data.Card.TFrame")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.state_var, style="Data.Card.TLabel",
                  font=("Microsoft YaHei UI", 9), wraplength=350).grid(row=0, column=0, sticky="w")
        self.status_button = RoundedButton(status, text="查看计数", width=88, height=30,
                                           command=self._show_status)
        self.status_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

        cards = ttk.Frame(self, style="Data.TFrame")
        cards.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        cards.columnconfigure((0, 1), weight=1, uniform="data-cards")

        edit_panel = RoundedPanel(cards, height=195, padding=8)
        edit_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        edit = edit_panel.content
        edit.configure(style="Data.Card.TFrame")
        edit.columnconfigure(0, weight=1)
        ttk.Label(edit, text="设置计数", style="Data.Card.TLabel",
                  font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        fields = ttk.Frame(edit, style="Data.Card.TFrame")
        fields.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        fields.columnconfigure((0, 1), weight=1, uniform="data-fields")
        ttk.Label(fields, text="待上报增量", style="Data.CardMuted.TLabel",
                  font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Label(fields, text="累计快照", style="Data.CardMuted.TLabel",
                  font=("Microsoft YaHei UI", 9)).grid(row=0, column=1, sticky="w", padx=(4, 0))
        ttk.Entry(fields, textvariable=self.pending_var, style="Data.TEntry", width=6).grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=(2, 0))
        ttk.Entry(fields, textvariable=self.total_var, style="Data.TEntry", width=6).grid(
            row=1, column=1, sticky="ew", padx=(4, 0), pady=(2, 0))
        ttk.Label(edit, text="留空保持原值；两项同填可能使总数叠加。", style="Data.CardMuted.TLabel",
                  wraplength=270, font=("Microsoft YaHei UI", 9)).grid(
                      row=2, column=0, sticky="w", pady=(5, 6))
        edit_buttons = ttk.Frame(edit, style="Data.Card.TFrame")
        edit_buttons.grid(row=3, column=0, sticky="sw")
        self.preview_set_button = RoundedButton(edit_buttons, text="预览设置", width=82, height=30,
                                                command=self._preview_set)
        self.preview_set_button.pack(side="left")
        self.apply_set_button = RoundedButton(edit_buttons, text="备份并写入", width=100, height=30,
                                              primary=True, command=self._apply_set)
        self.apply_set_button.pack(side="left", padx=(6, 0))

        restore_panel = RoundedPanel(cards, height=195, padding=8)
        restore_panel.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        restore = restore_panel.content
        restore.configure(style="Data.Card.TFrame")
        restore.columnconfigure(0, weight=1)
        ttk.Label(restore, text="从备份恢复", style="Data.Card.TLabel",
                  font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        path_row = ttk.Frame(restore, style="Data.Card.TFrame")
        path_row.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        path_row.columnconfigure(0, weight=1)
        ttk.Entry(path_row, textvariable=self.backup_var, style="Data.TEntry", width=6).grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        self.browse_button = RoundedButton(path_row, text="选择目录", width=82, height=30,
                                           command=self._browse_backup)
        self.browse_button.grid(row=0, column=1, sticky="e")
        ttk.Label(restore, text="恢复前会备份当前文件。", style="Data.CardMuted.TLabel",
                  wraplength=220, font=("Microsoft YaHei UI", 9)).grid(
                      row=2, column=0, sticky="w", pady=(7, 8))
        restore_buttons = ttk.Frame(restore, style="Data.Card.TFrame")
        restore_buttons.grid(row=3, column=0, sticky="sw")
        self.preview_restore_button = RoundedButton(restore_buttons, text="预览恢复", width=82, height=30,
                                                    command=self._preview_restore)
        self.preview_restore_button.pack(side="left")
        self.apply_restore_button = RoundedButton(restore_buttons, text="备份并恢复", width=100, height=30,
                                                  primary=True, command=self._apply_restore)
        self.apply_restore_button.pack(side="left", padx=(6, 0))

        results_panel = RoundedPanel(self, height=100, padding=8)
        results_panel.grid(row=2, column=0, sticky="nsew")
        results = results_panel.content
        results.configure(style="Data.Card.TFrame")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(1, weight=1)
        ttk.Label(results, text="操作结果", style="Data.Card.TLabel",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.output = tk.Text(results, height=2, wrap="word", state="disabled", undo=False,
                              background=FIELD, foreground=INK, insertbackground=INK,
                              selectbackground=GREEN_PALE, relief="flat", borderwidth=0,
                              highlightthickness=0, padx=10, pady=7, font=("Microsoft YaHei UI", 9))
        self.output.grid(row=1, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(results, orient="vertical", command=self.output.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scrollbar.set)
        ttk.Label(self, text="写入前请退出桌宠；服务器可能拒绝修改，宝箱冷却不会改变。",
                  style="Data.Muted.TLabel", wraplength=550, font=("Microsoft YaHei UI", 9)).grid(
                      row=3, column=0, sticky="w", pady=(7, 0))

    def is_busy(self) -> bool:
        return self._working or self._runner.is_busy()

    def _emit(self, message: str) -> None:
        if not self._alive:
            return
        text = _bounded_output(message).rstrip()
        if not text:
            return
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        length = int(self.output.count("1.0", "end-1c", "chars")[0])
        if length > MAX_LOG_CHARS:
            self.output.delete("1.0", f"1.0+{length - MAX_LOG_CHARS}c")
        self.output.configure(state="disabled")
        self.output.see("end")
        if self._on_message is not None:
            try:
                self._on_message(text)
            except Exception:
                pass

    def _on_input_changed(self, *_args: str) -> None:
        self._gate.invalidate()
        if self._alive:
            self.state_var.set("内容已变化，请重新预览后再写入。")
            self._refresh_buttons()

    def _current_operation(self, kind: Literal["set", "restore"]) -> SaveOperation:
        if kind == "set":
            return make_set_operation(self.pending_var.get(), self.total_var.get())
        return make_restore_operation(self.backup_var.get())

    def _refresh_buttons(self) -> None:
        enabled = "disabled" if self.is_busy() else "normal"
        for button in (self.status_button, self.preview_set_button, self.preview_restore_button, self.browse_button):
            button.configure(state=enabled)
        for kind, button in (("set", self.apply_set_button), ("restore", self.apply_restore_button)):
            allowed = not self.is_busy() and self._gate.allows_kind(kind)
            button.configure(state="normal" if allowed else "disabled")

    def _submit(self, operation: SaveOperation, *, apply: bool = False) -> None:
        if self.is_busy():
            return
        if apply and not self._gate.allows(operation):
            self.state_var.set("请先预览当前内容。")
            self._refresh_buttons()
            return
        if apply:
            denial = write_denial(self._can_write)
            if denial is not None:
                self.state_var.set(denial)
                self._emit(denial)
                self._refresh_buttons()
                return
        self._gate.invalidate()
        revision = self._gate.revision
        self._working = True
        self.state_var.set("正在执行，请等待……")
        self._refresh_buttons()
        try:
            started = self._runner.submit(operation, apply=apply, revision=revision)
        except RuntimeError as error:
            self._working = False
            self._emit(f"无法启动后台操作：{error}")
            self.state_var.set("无法启动后台操作。")
            self._refresh_buttons()
            return
        if not started:
            self._working = False
            self.state_var.set("已有操作正在执行，请稍后重试。")
            self._refresh_buttons()

    def _show_status(self) -> None:
        self._submit(SaveOperation("status"))

    def _action(self, kind: Literal["set", "restore"], *, apply: bool) -> None:
        try:
            operation = self._current_operation(kind)
        except InputError as error:
            if apply:
                self._gate.invalidate()
                self._refresh_buttons()
            messagebox.showerror("输入无效", str(error), parent=self)
            return
        self._submit(operation, apply=apply)

    def _preview_set(self) -> None:
        self._action("set", apply=False)

    def _apply_set(self) -> None:
        self._action("set", apply=True)

    def _preview_restore(self) -> None:
        self._action("restore", apply=False)

    def _apply_restore(self) -> None:
        self._action("restore", apply=True)

    def _browse_backup(self) -> None:
        directory = filedialog.askdirectory(
            parent=self, title="选择包含 manifest.json 的备份目录",
            initialdir=str(PROJECT_ROOT / "backups") if (PROJECT_ROOT / "backups").is_dir() else str(PROJECT_ROOT),
            mustexist=True,
        )
        if directory:
            self.backup_var.set(directory)

    def _poll_events(self) -> None:
        if not self._alive:
            return
        while True:
            try:
                event = self._runner.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, CommandNotice):
                self.state_var.set(event.text)
                self._emit(event.text)
                continue
            self._working = False
            if event.output:
                self._emit(event.output)
            if event.timed_out:
                self.state_var.set("只读操作超时，未进行写入。")
            elif event.returncode != 0:
                self.state_var.set("操作失败，请查看下方结果。")
            elif event.apply:
                self.state_var.set("写入完成；如需再次修改请重新预览。")
            elif event.operation.kind == "status":
                self.state_var.set("已读取本地计数。")
            else:
                if self._gate.accept(event.operation, event.revision):
                    self.state_var.set("预览完成；确认结果后可备份并写入。")
                else:
                    self.state_var.set("预览期间输入已变化，请重新预览。")
            self._refresh_buttons()
        self._after_id = self.after(100, self._poll_events)

    def destroy(self) -> None:
        self._alive = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        super().destroy()


def create_saves_page(
    parent: tk.Misc, on_message: Callable[[str], None] | None = None,
    can_write: Callable[[], bool] | None = None,
) -> SavesPage:
    return SavesPage(parent, on_message=on_message, can_write=can_write)
