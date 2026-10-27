"""Single executable entry point for the GUI and its isolated worker roles."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import tkinter as tk

from scripts import auto_chest, auto_chest_supervisor, gui, save_tool, tap_boost
from scripts.gui_backend import JobManager
from scripts.runtime import is_frozen, project_root, worker_command


class _IdleManager:
    def is_active(self, _job: str) -> bool:
        return False

    def poll_events(self) -> list[object]:
        return []


def gui_smoke_test() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        window = gui.HelperWindow(root, manager=_IdleManager())
        window.next_desktop_probe = float("inf")
        root.update_idletasks()
        window.notebook.select(1)
        root.update_idletasks()
        if not window.saves_page.winfo_exists():
            raise RuntimeError("GUI data page could not be created")
    finally:
        root.destroy()


def selftest_stop(_args: list[str]) -> int:
    print("ready", flush=True)
    for line in sys.stdin:
        if line.strip() == "0":
            print("stopped", flush=True)
            return 0
    return 1


def self_test() -> int:
    """Exercise packaged process routing without sending input or touching saves."""
    gui_smoke_test()
    manager = JobManager()
    manager.start_tap(1, 1, delay=0, dry_run=True)
    output: list[str] = []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for event in manager.poll_events():
            output.append(event.message)
            if event.kind == "finished":
                if event.returncode != 0 or not any("检查通过" in line and "未发送输入" in line for line in output):
                    print(f"Tap worker preview failed: code={event.returncode}; output={output!r}", file=sys.stderr)
                    return 1
                break
        else:
            time.sleep(0.05)
            continue
        break
    else:
        manager.stop_all()
        print("Tap worker preview timed out.", file=sys.stderr)
        return 1

    if is_frozen():
        manager._start("tap", "selftest_stop.py", [])
        deadline = time.monotonic() + 20
        ready = stopped = False
        while time.monotonic() < deadline:
            for event in manager.poll_events():
                if event.kind == "output" and event.message == "ready":
                    ready = manager.stop("tap")
                if event.kind == "output" and event.message == "stopped":
                    stopped = True
                if event.kind == "finished":
                    if not (ready and stopped and event.returncode == 0):
                        print("Worker stop test failed.", file=sys.stderr)
                        return 1
                    break
            else:
                time.sleep(0.05)
                continue
            break
        else:
            manager.stop_all()
            print("Worker stop test timed out.", file=sys.stderr)
            return 1

    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    for script in ("save_tool.py", "auto_chest_supervisor.py"):
        command = worker_command(project_root() / "scripts" / script, ["--help"])
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", timeout=20,
                                env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode != 0 or "usage:" not in result.stdout:
            print(f"Worker help failed: {script}.", file=sys.stderr)
            return 1
    print("Self-test passed: preview and worker routing.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--self-test"]:
        return self_test()
    if args[:1] == ["--worker"]:
        if is_frozen():
            for stream in (sys.stdin, sys.stdout, sys.stderr):
                if stream is not None and hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
        workers = {
            "chest": auto_chest.main,
            "supervisor": auto_chest_supervisor.main,
            "tap": tap_boost.main,
            "save": save_tool.main,
            "selftest-stop": selftest_stop,
        }
        if len(args) < 2 or args[1] not in workers:
            print("未知辅助任务。", file=sys.stderr)
            return 2
        return workers[args[1]](args[2:])
    if os.name == "nt" and getattr(sys, "frozen", False):
        ctypes.windll.kernel32.FreeConsole()
    return gui.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
