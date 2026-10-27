"""Offline tests: all input and process operations use a fake Windows API."""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "tap_boost.py"
SPEC = importlib.util.spec_from_file_location("tap_boost", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
tap_boost = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tap_boost)


class FakeWindowsInput:
    def __init__(
        self,
        *,
        running: bool = True,
        escape: bool = False,
        fail_on_send: int | None = None,
    ) -> None:
        self.running = running
        self.escape = escape
        self.fail_on_send = fail_on_send
        self.attempts: list[list[tap_boost.Input]] = []

    def app_running(self) -> bool:
        return self.running

    def escape_pressed(self) -> bool:
        return self.escape

    def send(self, events: list[tap_boost.Input]) -> None:
        self.attempts.append(list(events))
        if len(self.attempts) == self.fail_on_send:
            raise RuntimeError("模拟 SendInput 失败")


class TapBoostTests(unittest.TestCase):
    def run_fake(self, api: FakeWindowsInput, *arguments: str) -> int:
        args = tap_boost.parse_args(["--delay", "0", *arguments])
        with redirect_stdout(io.StringIO()):
            return tap_boost.run(args, cast(tap_boost.WindowsInput, api))

    def test_missing_app_does_not_send(self) -> None:
        api = FakeWindowsInput(running=False)
        with self.assertRaisesRegex(RuntimeError, "未发现 WuwaTappo"):
            self.run_fake(api, "--count", "1")
        self.assertEqual(api.attempts, [])

    def test_escape_before_first_batch_does_not_send(self) -> None:
        api = FakeWindowsInput(escape=True)
        self.assertEqual(self.run_fake(api, "--count", "1"), 130)
        self.assertEqual(api.attempts, [])

    def test_escape_during_countdown_does_not_send(self) -> None:
        api = FakeWindowsInput(escape=True)
        self.assertEqual(self.run_fake(api, "--delay", "5", "--count", "1"), 130)
        self.assertEqual(api.attempts, [])

    def test_send_failure_attempts_release_and_preserves_error(self) -> None:
        api = FakeWindowsInput(fail_on_send=1)
        with self.assertRaisesRegex(RuntimeError, "模拟 SendInput 失败"):
            self.run_fake(api, "--count", "1", "--mode", "both")
        self.assertEqual(len(api.attempts), 2)
        release = api.attempts[1]
        self.assertEqual(len(release), 2)
        self.assertEqual(release[0].type, tap_boost.INPUT_KEYBOARD)
        self.assertEqual(release[0].ki.wVk, tap_boost.VK_F24)
        self.assertEqual(release[0].ki.dwFlags, tap_boost.KEYEVENTF_KEYUP)
        self.assertEqual(release[1].type, tap_boost.INPUT_MOUSE)
        self.assertEqual(release[1].mi.dwFlags, tap_boost.MOUSEEVENTF_LEFTUP)

    def test_partial_last_batch_does_not_exceed_requested_count(self) -> None:
        api = FakeWindowsInput()
        self.assertEqual(self.run_fake(api, "--count", "19", "--rate", "1000"), 0)
        self.assertEqual([len(batch) for batch in api.attempts], [32, 6])
        events = [event for batch in api.attempts for event in batch]
        self.assertEqual(len(events), 38)
        self.assertEqual(sum(event.ki.dwFlags == 0 for event in events), 19)
        self.assertTrue(all(event.type == tap_boost.INPUT_KEYBOARD for event in events))
        self.assertTrue(all(event.ki.wVk == tap_boost.VK_F24 for event in events))

    def test_both_mode_emits_one_keypress_and_one_mouse_click_per_cycle(self) -> None:
        api = FakeWindowsInput()
        self.assertEqual(
            self.run_fake(api, "--count", "3", "--rate", "1000", "--mode", "both"),
            0,
        )
        events = [event for batch in api.attempts for event in batch]
        self.assertEqual(len(events), 12)
        for index in range(0, len(events), 4):
            down, up, mouse_down, mouse_up = events[index : index + 4]
            self.assertEqual((down.type, up.type), (tap_boost.INPUT_KEYBOARD,) * 2)
            self.assertEqual((down.ki.wVk, up.ki.wVk), (tap_boost.VK_F24,) * 2)
            self.assertEqual(down.ki.dwFlags, 0)
            self.assertEqual(up.ki.dwFlags, tap_boost.KEYEVENTF_KEYUP)
            self.assertEqual((mouse_down.type, mouse_up.type), (tap_boost.INPUT_MOUSE,) * 2)
            self.assertEqual(mouse_down.mi.dwFlags, tap_boost.MOUSEEVENTF_LEFTDOWN)
            self.assertEqual(mouse_up.mi.dwFlags, tap_boost.MOUSEEVENTF_LEFTUP)

    def test_dry_run_does_not_send_even_without_app(self) -> None:
        api = FakeWindowsInput(running=False)
        self.assertEqual(self.run_fake(api, "--dry-run", "--mode", "both"), 0)
        self.assertEqual(api.attempts, [])

    def test_stdin_stop_and_eof_set_stop_event(self) -> None:
        for text in ("anything\n0\n", ""):
            stop = threading.Event()
            tap_boost.stdin_stop_reader(stop, io.StringIO(text))
            self.assertTrue(stop.is_set())

    def test_stop_during_countdown_sends_no_events(self) -> None:
        api = FakeWindowsInput()
        stop = threading.Event()
        stop.set()
        args = tap_boost.parse_args(["--count", "10", "--delay", "5", "--stdin-stop"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tap_boost.run(args, cast(tap_boost.WindowsInput, api), stop), 130)
        self.assertEqual(api.attempts, [])

    def test_stop_after_a_batch_does_not_send_additional_events(self) -> None:
        stop = threading.Event()
        class StopAfterFirstBatch(FakeWindowsInput):
            def send(self, events: list[tap_boost.Input]) -> None:
                super().send(events)
                stop.set()
        api = StopAfterFirstBatch()
        args = tap_boost.parse_args(["--count", "100", "--rate", "1000", "--delay", "0", "--mode", "both", "--stdin-stop"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tap_boost.run(args, cast(tap_boost.WindowsInput, api), stop), 130)
        self.assertEqual(len(api.attempts), 1)
        self.assertEqual(len(api.attempts[0]) % 4, 0)

    @unittest.skipUnless(os.name == "nt", "Windows pipe handling")
    def test_pipe_reader_exits_when_task_finishes_with_parent_stdin_still_open(self) -> None:
        code = (
            "import sys,threading,time; "
            f"sys.path.insert(0,{str(MODULE_PATH.parent)!r}); "
            "from tap_boost import stdin_stop_reader; "
            "stop=threading.Event(); "
            "reader=threading.Thread(target=stdin_stop_reader,args=(stop,sys.stdin),daemon=True); "
            "reader.start(); time.sleep(0.1); stop.set(); reader.join(1); "
            "sys.exit(1 if reader.is_alive() else 0)"
        )
        process = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertEqual(process.wait(timeout=4), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=4)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
