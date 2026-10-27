from __future__ import annotations

from pathlib import Path
import io
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.gui_backend import BackendError, JobManager


SCRIPT_WAIT = """\
import sys
print('已启动', flush=True)
for line in sys.stdin:
    if line.strip() == '0':
        print('已优雅停止', flush=True)
        break
"""


class JobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scripts = Path(self.temporary.name) / "scripts"
        self.scripts.mkdir()
        (self.scripts / "auto_chest_supervisor.py").write_text(SCRIPT_WAIT, encoding="utf-8")
        (self.scripts / "tap_boost.py").write_text(SCRIPT_WAIT, encoding="utf-8")
        self.manager = JobManager(scripts_dir=self.scripts)
        self.addCleanup(self.finish_jobs)

    def finish_jobs(self) -> None:
        self.manager.stop_all()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            self.manager.is_running(job) for job in ("chest", "tap")
        ):
            time.sleep(0.01)

    def collect_until(self, predicate, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        collected = []
        while time.monotonic() < deadline:
            collected.extend(self.manager.poll_events())
            if predicate(collected):
                return collected
            time.sleep(0.01)
        self.fail(f"等待子进程事件超时：{collected!r}")

    def test_two_jobs_run_independently_and_stop_with_piped_zero(self) -> None:
        chest_pid = self.manager.start_chest()
        tap_pid = self.manager.start_tap(100, 20)
        self.assertNotEqual(chest_pid, tap_pid)
        events = self.collect_until(
            lambda rows: sum(e.kind == "output" and e.message == "已启动" for e in rows) == 2
        )
        self.assertEqual({e.job for e in events if e.kind == "started"}, {"chest", "tap"})
        self.assertTrue(self.manager.stop("chest"))
        self.assertTrue(self.manager.is_running("tap"))
        first = self.collect_until(lambda rows: any(e.job == "chest" and e.kind == "finished" for e in rows))
        self.assertTrue(any(e.job == "chest" and e.message == "已优雅停止" for e in first))
        self.assertEqual([e.returncode for e in first if e.kind == "finished"], [0])
        self.assertEqual(self.manager.stop_all(), [])
        second = self.collect_until(lambda rows: any(e.job == "tap" and e.kind == "finished" for e in rows))
        self.assertTrue(any(e.job == "tap" and e.message == "已优雅停止" for e in second))
        self.assertEqual([e.returncode for e in second if e.kind == "finished"], [0])

    def test_duplicate_job_is_rejected_until_previous_process_exits(self) -> None:
        self.manager.start_chest()
        with self.assertRaisesRegex(BackendError, "正在运行"):
            self.manager.start_chest()
        self.assertTrue(self.manager.stop("chest"))
        self.assertTrue(self.manager.stop("chest"))
        self.collect_until(lambda rows: any(e.kind == "finished" for e in rows))
        self.assertFalse(self.manager.stop("chest"))
        self.manager.start_chest()
        self.assertTrue(self.manager.is_running("chest"))

    def test_argument_validation_happens_before_launch(self) -> None:
        cases = (
            lambda: self.manager.start_tap(True, 10),
            lambda: self.manager.start_tap(0, 10),
            lambda: self.manager.start_tap(10, float("nan")),
            lambda: self.manager.start_tap(10, 1001),
            lambda: self.manager.start_tap(10, 10, mode="arbitrary"),
            lambda: self.manager.start_tap(10, 10, delay=-1),
            lambda: self.manager.start_chest(port=80),
            lambda: self.manager.start_chest(interval=float("inf")),
            lambda: self.manager.start_chest(allow_sleep="yes"),
        )
        with patch("scripts.gui_backend.subprocess.Popen") as launch:
            for operation in cases:
                with self.subTest(operation=operation):
                    with self.assertRaises(BackendError):
                        operation()
        launch.assert_not_called()

    def test_missing_script_and_spawn_failure_are_actionable(self) -> None:
        (self.scripts / "tap_boost.py").unlink()
        with self.assertRaisesRegex(BackendError, "未找到辅助脚本"):
            self.manager.start_tap(100, 10)
        with patch("scripts.gui_backend.subprocess.Popen", side_effect=OSError("failed")):
            with self.assertRaisesRegex(BackendError, "启动任务失败"):
                self.manager.start_chest()
        self.assertFalse(self.manager.is_running("chest"))

    def test_output_flood_cannot_block_child_exit_and_is_reported(self) -> None:
        script = self.scripts / "auto_chest_supervisor.py"
        script.write_text("for i in range(5000): print(i, flush=True)\n", encoding="utf-8")
        self.manager = JobManager(scripts_dir=self.scripts, max_events=4)
        self.manager.start_chest()
        deadline = time.monotonic() + 5
        while self.manager.is_running("chest") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.manager.is_running("chest"))
        events = self.collect_until(lambda rows: any(e.kind == "finished" for e in rows))
        self.assertEqual([event.returncode for event in events if event.kind == "finished"], [0])
        self.assertTrue(any("已省略" in event.message for event in events))

    def test_stop_unknown_job_is_rejected(self) -> None:
        with self.assertRaisesRegex(BackendError, "未知任务"):
            self.manager.stop("unknown")

    def test_active_until_output_drain_and_final_event_even_after_exit(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        reading = threading.Event()
        class DelayedOutput:
            def readline(self, _size: int) -> str:
                reading.set()
                release.wait(3)
                return ""
            def close(self) -> None:
                pass
        process = Mock()
        process.pid = 12345
        process.stdout = DelayedOutput()
        process.stdin = io.StringIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        with patch("scripts.gui_backend.subprocess.Popen", return_value=process):
            self.manager.start_chest()
            self.assertTrue(reading.wait(1))
            self.assertFalse(self.manager.is_running("chest"))
            self.assertTrue(self.manager.is_active("chest"))
            release.set()
            events = self.collect_until(lambda rows: any(e.kind == "finished" for e in rows))
        self.assertFalse(self.manager.is_active("chest"))
        self.assertEqual([e.returncode for e in events if e.kind == "finished"], [0])

    def test_chest_child_inherits_no_console_flag_and_utf8(self) -> None:
        process = Mock()
        process.pid = 12345
        process.stdout = io.StringIO("")
        process.stdin = io.StringIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        with patch("scripts.gui_backend.subprocess.Popen", return_value=process) as launch:
            self.manager.start_chest()
            self.collect_until(lambda rows: any(e.kind == "finished" for e in rows))
        environment = launch.call_args.kwargs["env"]
        self.assertEqual(environment["WUWA_HELPER_NO_CONSOLE"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")


if __name__ == "__main__":
    unittest.main()
