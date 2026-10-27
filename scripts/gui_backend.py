"""Manage GUI-owned helper processes without blocking the Tk event loop."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from typing import Literal, Sequence

if __package__:
    from .runtime import is_frozen, project_root, worker_command
else:
    from runtime import is_frozen, project_root, worker_command


JobName = Literal["chest", "tap"]
EventKind = Literal["started", "output", "stopping", "finished"]
SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = project_root()
MAX_EVENTS = 2048
MAX_LINE_LENGTH = 8192


def update_task_error(current: str, line: str) -> str:
    """Keep only the current actionable failure, including a recovery command."""
    line = line.strip()
    if "已连接桌宠主页面" in line:
        return ""
    markers = ("自动领取已停止：", "错误: ", "错误：", "监督进程发生错误", "启动失败（", "停止过程中领取子进程异常退出")
    marker = next((item for item in markers if item in line), None)
    if marker:
        if marker == "停止过程中领取子进程异常退出" and current:
            return (current + "\n" + line[line.index(marker):])[:4000]
        return line[line.index(marker):][:2000]
    if "重启已达上限" in line:
        return (current + "\n任务重试已达上限。")[:4000] if current else line[:2000]
    if "无法解除本进程的防睡眠请求" in line:
        return (current + "\n" + line).strip()[:4000]
    if current and line.startswith("& "):
        return (current + "\n" + line)[:4000]
    return current


class BackendError(Exception):
    """A helper process could not be started or asked to stop."""


@dataclass(frozen=True, slots=True)
class JobEvent:
    job: JobName
    kind: EventKind
    message: str = ""
    pid: int | None = None
    returncode: int | None = None


@dataclass(slots=True)
class _Job:
    process: subprocess.Popen[str]
    stop_requested: bool = False


def console_python() -> str:
    """Use the same Python installation with reliable redirected streams."""
    executable = Path(sys.executable)
    if executable.name.casefold() == "pythonw.exe":
        console_executable = executable.with_name("python.exe")
        if console_executable.is_file():
            return str(console_executable)
    return sys.executable


def _validate_number(value: object, low: float, high: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendError(f"{label}必须是数字。")
    try:
        number = float(value)
    except OverflowError as error:
        raise BackendError(f"{label}超出允许范围。") from error
    if not math.isfinite(number) or not low <= number <= high:
        raise BackendError(f"{label}必须介于 {low:g} 和 {high:g} 之间。")
    return number


class JobManager:
    """Start independent jobs, collect output, and request graceful stops.

    Process output is drained in daemon threads into a bounded queue; callers
    should consume events from their GUI thread with ``poll_events``. A stop
    request writes to the child's private stdin pipe and never terminates a
    process or touches an independently started instance.
    """

    def __init__(
        self, *, scripts_dir: Path = SCRIPTS_DIR, max_events: int = MAX_EVENTS,
    ) -> None:
        if max_events < 2:
            raise ValueError("事件队列至少需要两个位置。")
        self._scripts_dir = Path(scripts_dir)
        self._events: queue.Queue[JobEvent] = queue.Queue(maxsize=max_events)
        self._jobs: dict[JobName, _Job] = {}
        self._dropped: dict[JobName, int] = {"chest": 0, "tap": 0}
        self._lock = threading.RLock()

    def _emit(self, event: JobEvent) -> None:
        with self._lock:
            try:
                self._events.put_nowait(event)
            except queue.Full:
                if event.kind == "output":
                    self._dropped[event.job] += 1
                    return
                try:
                    discarded = self._events.get_nowait()
                    if discarded.kind == "output":
                        self._dropped[discarded.job] += 1
                except queue.Empty:
                    pass
                self._events.put_nowait(event)

    def start_chest(
        self, *, allow_sleep: bool = False, dry_run: bool = False,
        interval: float = 2.0, port: int = 19227, exe: Path | None = None,
    ) -> int:
        if type(allow_sleep) is not bool or type(dry_run) is not bool:
            raise BackendError("自动领取选项必须为布尔值。")
        interval_number = _validate_number(interval, 0.5, 60, "检查间隔")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise BackendError("端口必须是 1024 到 65535 之间的整数。")
        args = ["--interval", f"{interval_number:g}", "--port", str(port)]
        if exe is not None:
            executable = Path(exe)
            if executable.name.casefold() != "wuwatappo.exe" or not executable.is_file():
                raise BackendError("所选桌宠程序不存在，请重新选择 WuwaTappo.exe。")
            args.extend(("--exe", str(executable.resolve())))
        if allow_sleep:
            args.append("--allow-sleep")
        if dry_run:
            args.append("--dry-run")
        return self._start("chest", "auto_chest_supervisor.py", args)

    def start_tap(
        self, count: int, rate: float, mode: str = "keyboard", *,
        delay: float = 5.0, dry_run: bool = False,
    ) -> int:
        if type(count) is not int or not 1 <= count <= 1_000_000:
            raise BackendError("敲击轮数必须是 1 到 1,000,000 之间的整数。")
        rate_number = _validate_number(rate, 1, 1000, "敲击速率")
        delay_number = _validate_number(delay, 0, 30, "启动延迟")
        if mode not in ("keyboard", "mouse", "both"):
            raise BackendError("不支持此输入模式。")
        if type(dry_run) is not bool:
            raise BackendError("预览选项必须为布尔值。")
        args = [
            "--count", str(count), "--rate", f"{rate_number:g}",
            "--mode", mode, "--delay", f"{delay_number:g}", "--stdin-stop",
        ]
        if dry_run:
            args.append("--dry-run")
        return self._start("tap", "tap_boost.py", args)

    def _start(self, job: JobName, filename: str, arguments: Sequence[str]) -> int:
        script = self._scripts_dir / filename
        if not is_frozen() and not script.is_file():
            raise BackendError(f"未找到辅助脚本：{filename}")
        with self._lock:
            if job in self._jobs:
                raise BackendError("该任务正在运行或正在结束，请稍候。")
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            if job == "chest":
                environment["WUWA_HELPER_NO_CONSOLE"] = "1"
            try:
                process = subprocess.Popen(
                    worker_command(script, arguments, unbuffered=True, executable=console_python()),
                    cwd=str(PROJECT_ROOT if is_frozen() else self._scripts_dir.parent),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError as error:
                raise BackendError(f"启动任务失败：{error}") from error
            record = _Job(process)
            self._jobs[job] = record
            self._emit(JobEvent(job, "started", pid=process.pid))
            threading.Thread(
                target=self._collect, args=(job, record),
                name=f"wuwa-helper-{job}", daemon=True,
            ).start()
            return process.pid

    def _collect(self, job: JobName, record: _Job) -> None:
        process = record.process
        message = ""
        task_error = ""
        exit_code: int | None = None
        try:
            if process.stdout is not None:
                while True:
                    line = process.stdout.readline(MAX_LINE_LENGTH)
                    if not line:
                        break
                    line = line.rstrip("\r\n")
                    task_error = update_task_error(task_error, line)
                    self._emit(JobEvent(job, "output", line, pid=process.pid))
            exit_code = process.wait()
        except (OSError, ValueError) as error:
            message = f"读取子进程输出失败：{error}"
            try:
                exit_code = process.wait()
            except OSError:
                pass
        finally:
            for stream in (process.stdout, process.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            with self._lock:
                if self._jobs.get(job) is record:
                    del self._jobs[job]
                self._emit(JobEvent(job, "finished", task_error or message, pid=process.pid, returncode=exit_code))

    def stop(self, job: JobName) -> bool:
        if job not in ("chest", "tap"):
            raise BackendError("未知任务。")
        with self._lock:
            record = self._jobs.get(job)
            if record is None or record.process.poll() is not None:
                return False
            if record.stop_requested:
                return True
            stream = record.process.stdin
            if stream is None:
                raise BackendError("任务的停止通道不可用，请确认任务是否仍在运行。")
            try:
                stream.write("0\n")
                stream.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                if record.process.poll() is not None:
                    return False
                raise BackendError("无法发送停止请求，请确认任务是否仍在运行。") from error
            record.stop_requested = True
            self._emit(JobEvent(job, "stopping", pid=record.process.pid))
            return True

    def stop_all(self) -> list[str]:
        with self._lock:
            jobs = tuple(self._jobs)
        errors: list[str] = []
        for job in jobs:
            try:
                self.stop(job)
            except BackendError as error:
                errors.append(f"{job}: {error}")
        return errors

    def is_running(self, job: JobName) -> bool:
        with self._lock:
            record = self._jobs.get(job)
            return record is not None and record.process.poll() is None

    def is_active(self, job: JobName) -> bool:
        """Remain active until process output and handles are fully cleaned up."""
        with self._lock:
            return job in self._jobs

    def poll_events(self, max_items: int = 256) -> list[JobEvent]:
        if max_items < 1:
            raise ValueError("max_items 必须大于零。")
        events: list[JobEvent] = []
        with self._lock:
            for _ in range(max_items):
                try:
                    events.append(self._events.get_nowait())
                except queue.Empty:
                    break
            for job in ("chest", "tap"):
                if len(events) >= max_items:
                    break
                dropped = self._dropped[job]
                if dropped:
                    events.append(JobEvent(job, "output", f"状态消息过多，已省略 {dropped} 条。"))
                    self._dropped[job] = 0
        return events
