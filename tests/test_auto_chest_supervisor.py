from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import auto_chest_supervisor as supervisor


class FakePipe:
    def __init__(self, *, broken: bool = False) -> None:
        self.writes: list[str] = []
        self.flushed = False
        self.closed = False
        self.broken = broken

    def write(self, value: str) -> int:
        if self.broken:
            raise BrokenPipeError()
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True


class FakeWorker:
    def __init__(self, result: int | None = None, *, on_poll=None, broken=False) -> None:
        self.pid = 54321
        self.result = result
        self.stdin = FakePipe(broken=broken)
        self.on_poll = on_poll
        self.wait_count = 0
        self.wait_timeouts = 0
        self.wait_interrupts = 0

    def poll(self) -> int | None:
        callback, self.on_poll = self.on_poll, None
        if callback is not None:
            callback()
        return self.result

    def wait(self, timeout: float | None = None) -> int:
        self.wait_count += 1
        if self.wait_interrupts:
            self.wait_interrupts -= 1
            raise KeyboardInterrupt()
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        if self.result is None:
            self.result = 0
        return self.result


class RestartPolicyTests(unittest.TestCase):
    def test_bounded_backoff_and_expiring_window(self) -> None:
        policy = supervisor.RestartPolicy(max_restarts=3, window_seconds=600)
        self.assertEqual([policy.next_delay(i) for i in (0, 1, 2)], [1, 2, 4])
        self.assertIsNone(policy.next_delay(3))
        self.assertEqual(policy.next_delay(600), 4)
        self.assertEqual(policy.next_delay(1200), 1)

    def test_invalid_configuration_is_rejected(self) -> None:
        for options in ({"max_restarts": -1}, {"window_seconds": 0}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    supervisor.RestartPolicy(**options)


class ShutdownTests(unittest.TestCase):
    def test_stop_sends_zero_and_waits_for_worker_cleanup(self) -> None:
        worker = FakeWorker()
        messages: list[str] = []
        supervisor.stop_worker(worker, messages.append)
        self.assertEqual(worker.stdin.writes, ["0\n"])
        self.assertTrue(worker.stdin.flushed)
        self.assertTrue(worker.stdin.closed)
        self.assertEqual(worker.wait_count, 1)
        self.assertTrue(any("等待子进程恢复配置" in message for message in messages))

    def test_stop_waits_through_timeouts_and_broken_pipe(self) -> None:
        worker = FakeWorker(broken=True)
        worker.wait_timeouts = 2
        messages: list[str] = []
        supervisor.stop_worker(worker, messages.append)
        self.assertEqual(worker.wait_count, 3)
        self.assertTrue(worker.stdin.closed)
        self.assertTrue(any("停止信号未能写入" in message for message in messages))

    def test_already_finished_worker_needs_no_stop_message(self) -> None:
        worker = FakeWorker(1)
        supervisor.stop_worker(worker, lambda _: self.fail("unexpected message"))
        self.assertEqual(worker.stdin.writes, [])
        self.assertTrue(worker.stdin.closed)
        self.assertEqual(worker.wait_count, 0)

    def test_stream_zero_and_eof_request_stop(self) -> None:
        for source in ("other\n0\n", ""):
            with self.subTest(source=source):
                event = threading.Event()
                supervisor.stop_reader(event, io.StringIO(source))
                self.assertTrue(event.is_set())


class SupervisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stop = threading.Event()
        self.messages: list[str] = []
        self.passed_arguments: list[list[str]] = []

    def run_fast(self, workers: list[FakeWorker], policy: supervisor.RestartPolicy | None = None) -> int:
        iterator = iter(workers)
        def spawn(arguments):
            self.passed_arguments.append(list(arguments))
            return next(iterator)
        with patch.object(self.stop, "wait", side_effect=lambda _: self.stop.is_set()):
            return supervisor.supervise(
                ["--dry-run"], self.stop, spawn=spawn, clock=lambda: 0,
                emit=self.messages.append, policy=policy,
            )

    def test_crash_restarts_worker_then_zero_cleans_up(self) -> None:
        crashed = FakeWorker(1)
        running = FakeWorker(on_poll=self.stop.set)
        result = self.run_fast([crashed, running])
        self.assertEqual(result, 0)
        self.assertEqual(self.passed_arguments, [["--dry-run"], ["--dry-run"]])
        self.assertTrue(crashed.stdin.closed)
        self.assertEqual(running.stdin.writes, ["0\n"])
        self.assertEqual(running.wait_count, 1)
        self.assertTrue(any("自动重启" in message for message in self.messages))

    def test_repeated_crashes_stop_at_limit(self) -> None:
        workers = [FakeWorker(1) for _ in range(3)]
        result = self.run_fast(workers, supervisor.RestartPolicy(max_restarts=2))
        self.assertEqual(result, 1)
        self.assertEqual(len(self.passed_arguments), 3)
        self.assertTrue(all(worker.stdin.closed for worker in workers))
        self.assertTrue(any("重启已达上限" in message for message in self.messages))

    def test_normal_exit_and_interrupt_do_not_restart(self) -> None:
        for code, expected in ((0, 0), (130, 130), (0xC000013A, 130)):
            with self.subTest(code=code):
                self.setUp()
                worker = FakeWorker(code)
                self.assertEqual(self.run_fast([worker]), expected)
                self.assertEqual(len(self.passed_arguments), 1)
                self.assertTrue(worker.stdin.closed)

    def test_ctrl_c_stops_worker_without_restart(self) -> None:
        def interrupt() -> None:
            raise KeyboardInterrupt()
        worker = FakeWorker(on_poll=interrupt)
        result = self.run_fast([worker])
        self.assertEqual(result, 130)
        self.assertEqual(worker.stdin.writes, ["0\n"])
        self.assertEqual(worker.wait_count, 1)
        self.assertEqual(len(self.passed_arguments), 1)

    def test_stop_reports_worker_cleanup_failure(self) -> None:
        worker = FakeWorker(result=2, on_poll=self.stop.set)
        self.assertEqual(self.run_fast([worker]), 0)
        self.assertTrue(any("停止过程中领取子进程异常退出" in message for message in self.messages))

    def test_zero_during_backoff_cancels_restart(self) -> None:
        worker = FakeWorker(1)
        def spawn(arguments):
            self.passed_arguments.append(list(arguments))
            return worker
        def stop_during_wait(_seconds: float) -> bool:
            self.stop.set()
            return True
        with patch.object(self.stop, "wait", side_effect=stop_during_wait):
            result = supervisor.supervise([], self.stop, spawn=spawn, emit=self.messages.append)
        self.assertEqual(result, 0)
        self.assertEqual(len(self.passed_arguments), 1)

    def test_configuration_error_does_not_restart(self) -> None:
        worker = FakeWorker(2)
        self.assertEqual(self.run_fast([worker]), 2)
        self.assertEqual(len(self.passed_arguments), 1)
        self.assertTrue(any("配置或实例冲突" in message for message in self.messages))

    def test_one_shot_commands_do_not_start_supervisor_or_restart(self) -> None:
        for option in ("--check", "--restore-debug-policy", "--help", "-h"):
            with self.subTest(option=option):
                worker = FakeWorker(0)
                with patch.object(supervisor, "start_worker", return_value=worker) as launch, \
                     patch.object(supervisor, "configure_file_logging") as configure:
                    self.assertEqual(supervisor.main([option]), 0)
                launch.assert_called_once_with([option], one_shot=True)
                configure.assert_not_called()
                self.assertEqual(worker.wait_count, 1)

    def test_worker_uses_pipe_and_separate_windows_process_group(self) -> None:
        with patch.dict(supervisor.os.environ, {"WUWA_HELPER_NO_CONSOLE": "0"}), \
             patch.object(supervisor.subprocess, "Popen") as launch:
            supervisor.start_worker(["--dry-run"])
        call = launch.call_args
        self.assertEqual(call.args[0], [sys.executable, str(supervisor.WORKER), "--dry-run"])
        self.assertEqual(call.kwargs["cwd"], supervisor.PROJECT_ROOT)
        self.assertEqual(call.kwargs["stdin"], subprocess.PIPE)
        self.assertIsNone(call.kwargs["stdout"])
        self.assertIsNone(call.kwargs["stderr"])
        expected = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.assertEqual(call.kwargs["creationflags"], expected)

    def test_frozen_worker_inherits_output_pipes(self) -> None:
        for one_shot in (False, True):
            with self.subTest(one_shot=one_shot), \
                 patch.object(supervisor, "is_frozen", return_value=True), \
                 patch.object(supervisor.subprocess, "Popen") as launch:
                supervisor.start_worker(["--check"], one_shot=one_shot)
                self.assertIs(launch.call_args.kwargs["stdout"], sys.stdout)
                self.assertIs(launch.call_args.kwargs["stderr"], sys.stderr)
                self.assertEqual(launch.call_args.kwargs["stdin"], sys.stdin if one_shot else subprocess.PIPE)

    @unittest.skipUnless(os.name == "nt", "Windows process creation flags")
    def test_gui_worker_does_not_open_a_console_window(self) -> None:
        with patch.dict(supervisor.os.environ, {"WUWA_HELPER_NO_CONSOLE": "1"}), \
             patch.object(supervisor.subprocess, "Popen") as launch:
            supervisor.start_worker([])
        flags = launch.call_args.kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)

    def test_one_shot_interrupt_waits_for_child_cleanup(self) -> None:
        worker = FakeWorker()
        worker.wait_interrupts = 1
        with patch.object(supervisor, "start_worker", return_value=worker):
            self.assertEqual(supervisor.main(["--restore-debug-policy"]), 130)
        self.assertEqual(worker.wait_count, 2)


if __name__ == "__main__":
    unittest.main()
