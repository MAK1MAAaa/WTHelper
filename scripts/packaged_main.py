"""Single executable entry point for the GUI and its isolated worker roles."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk

from scripts import auto_chest, auto_chest_supervisor, gui, tap_boost
from scripts.gui_backend import JobManager
from scripts.runtime import is_frozen, project_root, worker_command


def prepare_standard_streams() -> None:
    """Keep redirected worker pipes and handle missing streams in windowed builds."""
    if not is_frozen():
        return
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8", errors="replace"))
        elif hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


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
        window.notebook.select(0)
        root.update_idletasks()
        if not window.chest_start.winfo_exists() or not window.tap_start.winfo_exists():
            raise RuntimeError("GUI controls could not be created")
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
    for script in ("auto_chest_supervisor.py",):
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
    prepare_standard_streams()
    if args == ["--self-test"]:
        return self_test()
    if args[:1] == ["--worker"]:
        workers = {
            "chest": auto_chest.main,
            "supervisor": auto_chest_supervisor.main,
            "tap": tap_boost.main,
            "selftest-stop": selftest_stop,
        }
        if len(args) < 2 or args[1] not in workers:
            print("未知辅助任务。", file=sys.stderr)
            return 2
        return workers[args[1]](args[2:])
    return gui.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
