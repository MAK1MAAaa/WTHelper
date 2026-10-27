from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import gui_saves as saves
from scripts.gui_layout import configure_styles


class CompactLayoutTests(unittest.TestCase):
    def test_actions_and_results_fit_in_minimum_window(self) -> None:
        try:
            root = tk.Tk()
            root.attributes("-alpha", 0.0)
        except tk.TclError as error:
            self.skipTest(f"无法创建隔离 Tk 窗口：{error}")
        self.addCleanup(root.destroy)
        configure_styles(root)
        root.geometry("660x510")
        page = saves.SavesPage(root)
        page.pack(fill="both", expand=True)
        root.update()
        self.assertGreaterEqual(page.output.winfo_height(), 50)
        for button in (page.status_button, page.preview_set_button, page.apply_set_button,
                       page.browse_button, page.preview_restore_button, page.apply_restore_button):
            with self.subTest(button=button):
                panel = button.master
                while panel.master is not page and not isinstance(panel.master, tk.Canvas):
                    panel = panel.master
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                     panel.winfo_rootx() + panel.winfo_width())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                     panel.winfo_rooty() + panel.winfo_height())
        page.destroy()


class OperationTests(unittest.TestCase):
    def test_set_accepts_zero_and_rejects_empty_or_invalid_counts(self) -> None:
        operation = saves.make_set_operation("0", " 42 ")
        self.assertEqual((operation.pending, operation.total), (0, 42))
        self.assertIsNone(saves.make_set_operation("", "1").pending)
        for pending, total in (("", ""), ("-1", ""), ("1.5", ""), ("1e3", ""),
                               (str(saves.MAX_COUNT + 1), ""), ("9" * 5000, "")):
            with self.subTest(pending=pending[:20], total=total):
                with self.assertRaises(saves.InputError):
                    saves.make_set_operation(pending, total)

    def test_command_uses_individual_arguments_and_apply_only_when_requested(self) -> None:
        operation = saves.make_set_operation("100", "")
        preview = saves.build_command(operation)
        self.assertEqual(preview[:4], [sys.executable, "-u", str(saves.SAVE_TOOL), "set"])
        self.assertEqual(preview[4:], ["--pending", "100"])
        self.assertEqual(saves.build_command(operation, apply=True), preview + ["--apply"])
        self.assertNotIn("--apply", saves.build_command(saves.SaveOperation("status")))

    def test_restore_validates_directory_and_preserves_special_characters_as_one_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "备份 & $(literal)"
            backup.mkdir()
            with self.assertRaises(saves.InputError):
                saves.make_restore_operation(str(backup))
            (backup / "manifest.json").write_text("{}", encoding="utf-8")
            operation = saves.make_restore_operation(str(backup))
            command = saves.build_command(operation, apply=True)
            self.assertEqual(command[-3:], ["restore", str(backup.absolute()), "--apply"])
            for value in ("", "\x00", str(root / "missing")):
                with self.subTest(value=value):
                    with self.assertRaises(saves.InputError):
                        saves.make_restore_operation(value)

    def test_invalid_constructed_operations_cannot_create_write_commands(self) -> None:
        for operation in (saves.SaveOperation("set"), saves.SaveOperation("set", pending=True),
                          saves.SaveOperation("set", total=saves.MAX_COUNT + 1),
                          saves.SaveOperation("restore")):
            with self.subTest(operation=operation):
                with self.assertRaises(saves.InputError):
                    saves.build_command(operation, apply=True)
        with self.assertRaises(saves.InputError):
            saves.build_command(saves.SaveOperation("status"), apply=True)


class PreviewGateTests(unittest.TestCase):
    def test_write_requires_successful_preview_for_exact_operation(self) -> None:
        gate = saves.PreviewGate()
        first = saves.make_set_operation("100", "")
        second = saves.make_set_operation("101", "")
        self.assertFalse(gate.allows(first))
        self.assertTrue(gate.accept(first, gate.revision))
        self.assertTrue(gate.allows(first))
        self.assertTrue(gate.allows_kind("set"))
        self.assertFalse(gate.allows_kind("restore"))
        self.assertFalse(gate.allows(second))
        gate.invalidate()
        self.assertFalse(gate.allows(first))

    def test_change_then_change_back_still_invalidates_inflight_preview(self) -> None:
        gate = saves.PreviewGate()
        operation = saves.make_set_operation("100", "")
        old_revision = gate.revision
        gate.invalidate()
        gate.invalidate()
        self.assertFalse(gate.accept(operation, old_revision))
        self.assertFalse(gate.allows(operation))
        self.assertTrue(gate.accept(operation, gate.revision))
        self.assertTrue(gate.allows(operation))

    def test_status_query_cannot_approve_a_write(self) -> None:
        gate = saves.PreviewGate()
        self.assertFalse(gate.accept(saves.SaveOperation("status"), gate.revision))


class WriteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.operation = saves.make_set_operation("100", "")
        self.page = object.__new__(saves.SavesPage)
        self.page._working = False
        self.page._runner = Mock()
        self.page._runner.is_busy.return_value = False
        self.page._runner.submit.return_value = True
        self.page._gate = saves.PreviewGate()
        self.page._gate.accept(self.operation, self.page._gate.revision)
        self.page.state_var = Mock()

    def test_optional_guard_allows_by_default_and_rejects_false(self) -> None:
        self.assertIsNone(saves.write_denial(None))
        self.assertIsNone(saves.write_denial(lambda: True))
        self.assertIn("请先停止", saves.write_denial(lambda: False) or "")

    def test_denied_write_keeps_preview_and_never_starts_subprocess(self) -> None:
        self.page._can_write = lambda: False
        with patch.object(saves.SavesPage, "_emit") as emit, \
             patch.object(saves.SavesPage, "_refresh_buttons"):
            self.page._submit(self.operation, apply=True)
        self.page._runner.submit.assert_not_called()
        self.assertTrue(self.page._gate.allows(self.operation))
        self.page.state_var.set.assert_called_once_with("请先停止自动领取和刷数任务。")
        emit.assert_called_once_with("请先停止自动领取和刷数任务。")

    def test_guard_error_is_shown_and_does_not_erase_preview(self) -> None:
        def unavailable() -> bool:
            raise RuntimeError("无法读取互斥量")
        self.page._can_write = unavailable
        with patch.object(saves.SavesPage, "_emit") as emit, \
             patch.object(saves.SavesPage, "_refresh_buttons"):
            self.page._submit(self.operation, apply=True)
        self.page._runner.submit.assert_not_called()
        self.assertTrue(self.page._gate.allows(self.operation))
        emit.assert_called_once_with("无法确认任务状态：无法读取互斥量")

    def test_guard_runs_before_allowed_write_is_submitted(self) -> None:
        events: list[str] = []
        def allow() -> bool:
            events.append("guard")
            return True
        self.page._can_write = allow
        self.page._runner.submit.side_effect = lambda *_args, **_kwargs: events.append("submit") or True
        with patch.object(saves.SavesPage, "_emit"), \
             patch.object(saves.SavesPage, "_refresh_buttons"):
            self.page._submit(self.operation, apply=True)
        self.assertEqual(events, ["guard", "submit"])
        self.assertFalse(self.page._gate.allows(self.operation))

    def test_preview_does_not_require_write_guard(self) -> None:
        self.page._can_write = Mock(return_value=False)
        with patch.object(saves.SavesPage, "_emit"), \
             patch.object(saves.SavesPage, "_refresh_buttons"):
            self.page._submit(self.operation, apply=False)
        self.page._can_write.assert_not_called()
        self.page._runner.submit.assert_called_once()


class ExecutionTests(unittest.TestCase):
    def test_preview_runs_argument_list_without_shell_and_with_utf8(self) -> None:
        process = Mock(returncode=0)
        process.communicate.return_value = ("仅预览，未修改。", None)
        operation = saves.make_set_operation("10", "")
        with patch.object(saves.subprocess, "Popen", return_value=process) as popen:
            result = saves.execute_command(operation, False, 7, lambda _: self.fail("unexpected notice"))
        self.assertEqual(result.returncode, 0)
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("--apply", args[0])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(kwargs["cwd"], saves.PROJECT_ROOT)

    def test_read_only_timeout_is_stopped(self) -> None:
        process = Mock(returncode=1)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 30), ("", None)]
        with patch.object(saves.subprocess, "Popen", return_value=process):
            result = saves.execute_command(saves.SaveOperation("status"), False, 0, lambda _: None)
        process.kill.assert_called_once()
        self.assertTrue(result.timed_out)

    def test_write_timeout_waits_without_killing_and_reports_completion(self) -> None:
        process = Mock(returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 30), ("写入完成", None)]
        notices: list[saves.CommandNotice] = []
        with patch.object(saves.subprocess, "Popen", return_value=process):
            result = saves.execute_command(saves.make_set_operation("10", ""), True, 2, notices.append)
        process.kill.assert_not_called()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(any("仍在执行" in notice.text for notice in notices))

    def test_output_is_bounded(self) -> None:
        text = saves._bounded_output("x" * (saves.MAX_OUTPUT_CHARS + 500))
        self.assertLessEqual(len(text), saves.MAX_OUTPUT_CHARS + 30)
        self.assertTrue(text.endswith("x" * 100))

    def test_runner_rejects_concurrent_requests_without_touching_real_files(self) -> None:
        started = threading.Event()
        release = threading.Event()
        def fake_execute(operation, apply, revision, _notify):
            started.set()
            release.wait(2)
            return saves.CommandResult(operation, apply, revision, 0, "完成")
        runner = saves.CommandRunner(execute=fake_execute)
        operation = saves.SaveOperation("status")
        with patch.object(saves.subprocess, "Popen", side_effect=AssertionError("real process forbidden")):
            self.assertTrue(runner.submit(operation, apply=False, revision=0))
            self.assertTrue(started.wait(1))
            self.assertFalse(runner.submit(operation, apply=False, revision=0))
            release.set()
            event = runner.events.get(timeout=2)
        self.assertIsInstance(event, saves.CommandResult)
        self.assertFalse(runner.is_busy())


if __name__ == "__main__":
    unittest.main()
