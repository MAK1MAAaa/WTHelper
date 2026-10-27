"""Keep the automatic chest worker running in one Windows console."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import TextIO

if __package__:
    from . import auto_chest as app
    from .tap_boost import stdin_stop_reader
else:
    import auto_chest as app
    from tap_boost import stdin_stop_reader


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER = PROJECT_ROOT / "scripts" / "auto_chest.py"
SUPERVISOR_LOG_PATH = PROJECT_ROOT / "logs" / "auto_chest_supervisor.log"
LOGGER = logging.getLogger("wuwatappo.auto_chest.supervisor")
LOGGER.propagate = False
ONE_SHOT_OPTIONS = frozenset(("--check", "--restore-debug-policy", "--help", "-h"))
INTERRUPTED_EXIT_CODES = frozenset((130, -2, 0xC000013A))


def configure_file_logging() -> None:
    if LOGGER.handlers:
        return
    try:
        SUPERVISOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            SUPERVISOR_LOG_PATH, maxBytes=1_048_576, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
    except OSError:
        print("监督进程日志无法写入，将仅在 CMD 中显示状态。", flush=True)


def log(message: str) -> None:
    app.log(f"监督进程：{message}")
    LOGGER.info(message)


class RestartPolicy:
    """Limit unexpected worker exits in a rolling window and back off retries."""

    def __init__(self, max_restarts: int = 5, window_seconds: float = 600.0) -> None:
        if max_restarts < 0 or window_seconds <= 0:
            raise ValueError("重启次数或时间窗口无效。")
        self.max_restarts = max_restarts
        self.window_seconds = window_seconds
        self.events: deque[float] = deque()

    def next_delay(self, now: float) -> float | None:
        while self.events and now - self.events[0] >= self.window_seconds:
            self.events.popleft()
        if len(self.events) >= self.max_restarts:
            return None
        delay = min(2.0 ** len(self.events), 30.0)
        self.events.append(now)
        return delay


def start_worker(arguments: Sequence[str], *, one_shot: bool = False) -> subprocess.Popen[str]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" and not one_shot else 0
    if os.name == "nt" and os.environ.get("WUWA_HELPER_NO_CONSOLE") == "1":
        creationflags |= subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        [sys.executable, str(WORKER), *arguments],
        cwd=PROJECT_ROOT,
        stdin=None if one_shot else subprocess.PIPE,
        stdout=None,
        stderr=None,
        text=True,
        encoding="utf-8",
        bufsize=1,
        creationflags=creationflags,
    )


def stop_reader(stop: threading.Event, stream: TextIO | None = None) -> None:
    source = stream if stream is not None else sys.stdin
    if os.name == "nt" and source.isatty():
        import msvcrt
        while not stop.wait(0.1):
            if not msvcrt.kbhit():
                continue
            character = msvcrt.getwch()
            if character in ("\x00", "\xe0"):
                msvcrt.getwch()
            elif character in ("0", "\x03"):
                stop.set()
                return
    else:
        stdin_stop_reader(stop, source)


def close_stdin(worker: subprocess.Popen[str]) -> None:
    if worker.stdin is not None:
        try:
            worker.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def stop_worker(worker: subprocess.Popen[str], emit: Callable[[str], None] = log) -> None:
    """Ask the worker to stop and wait for its own cleanup; never force-kill it."""
    if worker.poll() is not None:
        close_stdin(worker)
        return
    emit("正在停止领取，等待子进程恢复配置。")
    try:
        if worker.stdin is not None:
            worker.stdin.write("0\n")
            worker.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        emit("停止信号未能写入，将等待子进程退出。")
    next_notice = time.monotonic() + 30
    while True:
        try:
            worker.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if now >= next_notice:
                emit("子进程仍在清理，请保持窗口打开。")
                next_notice = now + 30
        except KeyboardInterrupt:
            emit("已收到停止请求，继续等待子进程清理。")
    close_stdin(worker)


def run_one_shot(arguments: Sequence[str]) -> int:
    worker: subprocess.Popen[str] | None = None
    try:
        worker = start_worker(arguments, one_shot=True)
        return worker.wait()
    except KeyboardInterrupt:
        if worker is not None:
            while worker.poll() is None:
                try:
                    worker.wait(timeout=1)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    continue
        return 130
    except OSError as error:
        log(f"启动失败（{type(error).__name__}）。")
        return 1


def supervise(
    arguments: Sequence[str],
    stop: threading.Event,
    *,
    spawn: Callable[[Sequence[str]], subprocess.Popen[str]] | None = None,
    clock: Callable[[], float] | None = None,
    emit: Callable[[str], None] | None = None,
    policy: RestartPolicy | None = None,
) -> int:
    """Restart abnormal exits while preserving an explicit, graceful stop path."""
    launch = spawn if spawn is not None else start_worker
    now = clock if clock is not None else time.monotonic
    report = emit if emit is not None else log
    restart_policy = policy if policy is not None else RestartPolicy()
    worker: subprocess.Popen[str] | None = None
    try:
        while not stop.is_set():
            try:
                worker = launch(arguments)
            except OSError as error:
                failure = f"无法启动领取子进程（{type(error).__name__}）"
            else:
                report(f"领取子进程已启动（PID {worker.pid}）；输入 0 可停止。")
                while worker.poll() is None and not stop.wait(0.2):
                    pass
                if stop.is_set():
                    return 0
                result = worker.poll()
                close_stdin(worker)
                worker = None
                if result == 0:
                    report("领取子进程已正常退出。")
                    return 0
                if result == 2:
                    report("领取子进程遇到配置或实例冲突，已停止，请查看上方日志。")
                    return 2
                if result in INTERRUPTED_EXIT_CODES:
                    report("领取子进程收到中断，停止监督。")
                    return 130
                failure = f"领取子进程异常退出，退出码 {result}"
            delay = restart_policy.next_delay(now())
            if delay is None:
                report(f"{failure}；10 分钟内重启已达上限，请查看日志并手动重试。")
                return 1
            report(f"{failure}；{delay:g} 秒后自动重启。")
            if stop.wait(delay):
                return 0
        return 0
    except KeyboardInterrupt:
        stop.set()
        report("收到 Ctrl+C，停止监督。")
        return 130
    except OSError as error:
        report(f"监督进程发生错误（{type(error).__name__}），停止监督。")
        return 1
    finally:
        stop.set()
        if worker is not None:
            stop_worker(worker, report)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(argument in ONE_SHOT_OPTIONS for argument in arguments):
        return run_one_shot(arguments)
    if os.name != "nt":
        print("自动领取监督进程仅支持 Windows。", file=sys.stderr)
        return 1
    configure_file_logging()
    stop = threading.Event()
    reader = threading.Thread(target=stop_reader, args=(stop,), daemon=True)
    reader.start()
    try:
        return supervise(arguments, stop)
    finally:
        stop.set()
        reader.join(timeout=0.3)


if __name__ == "__main__":
    raise SystemExit(main())
