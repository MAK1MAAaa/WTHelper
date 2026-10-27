from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import gui
from scripts.gui import chest_status_from_line, parse_tap_settings, tap_status_from_line


class TapSettingsTests(unittest.TestCase):
    def test_valid_inputs_are_converted_and_modes_are_mapped(self) -> None:
        for label, expected in (
            ("键盘（F24）", "keyboard"),
            ("鼠标左键", "mouse"),
            ("键盘与鼠标", "both"),
        ):
            with self.subTest(mode=label):
                settings = parse_tap_settings(" 10000 ", " 200.5 ", " 5 ", label)
                self.assertEqual(settings.count, 10000)
                self.assertEqual(settings.rate, 200.5)
                self.assertEqual(settings.delay, 5)
                self.assertEqual(settings.mode, expected)

    def test_inclusive_lower_and_upper_limits(self) -> None:
        low = parse_tap_settings("1", "1", "0", "键盘（F24）")
        high = parse_tap_settings("1000000", "1000", "30", "鼠标左键")
        self.assertEqual((low.count, low.rate, low.delay), (1, 1, 0))
        self.assertEqual((high.count, high.rate, high.delay), (1000000, 1000, 30))

    def test_count_must_be_an_integer_within_bounds(self) -> None:
        for count in ("", "0", "-1", "1000001", "1.0", "1e3", "abc"):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    parse_tap_settings(count, "200", "5", "键盘（F24）")

    def test_rate_must_be_finite_and_within_bounds(self) -> None:
        for rate in ("", "0", "0.99", "1000.01", "nan", "inf", "-inf", "abc"):
            with self.subTest(rate=rate):
                with self.assertRaises(ValueError):
                    parse_tap_settings("100", rate, "5", "键盘（F24）")

    def test_delay_must_be_finite_and_within_bounds(self) -> None:
        for delay in ("", "-0.1", "30.01", "nan", "inf", "-inf", "abc"):
            with self.subTest(delay=delay):
                with self.assertRaises(ValueError):
                    parse_tap_settings("100", "200", delay, "键盘（F24）")

    def test_unrecognized_mode_cannot_default_to_mouse(self) -> None:
        for mode in ("", "鼠标", "keyboard", "未知模式"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    parse_tap_settings("100", "200", "5", mode)


class ChestStatusTests(unittest.TestCase):
    def test_status_follows_confirmed_runtime_messages(self) -> None:
        messages = (
            ("监督进程：领取子进程已启动（PID 123）；输入 0 可停止。", "启动中"),
            ("桌宠已运行但未启用本地连接。请从托盘完全退出桌宠；脚本会自动重新启动。", "等待退出桌宠"),
            ("等待你在桌宠中登录。", "等待登录"),
            ("已连接桌宠主页面，开始检查宝箱。", "已连接"),
            ("本地连接暂不可用（CdpError），稍后重连；输入 0 退出。", "连接重试中"),
            ("已停止自动领取，本次确认领取 1 个宝箱。桌宠继续运行。", "已停止"),
        )
        current = "启动中"
        for line, expected in messages:
            with self.subTest(line=line):
                current = chest_status_from_line(line, current)
                self.assertEqual(current, expected)

    def test_unknown_log_preserves_connection_status(self) -> None:
        for current in ("启动中", "等待登录", "已连接", "连接重试中", "已停止"):
            with self.subTest(current=current):
                self.assertEqual(chest_status_from_line("与连接状态无关的日志", current), current)

    def test_five_minute_heartbeat_is_not_itself_a_new_connection(self) -> None:
        self.assertEqual(chest_status_from_line("状态心跳：等待桌宠连接", "启动中"), "启动中")
        self.assertEqual(chest_status_from_line("状态心跳：已登录；桌宠可见", "已连接"), "已连接")

    def test_supervisor_restart_clears_stale_connected_status(self) -> None:
        self.assertEqual(chest_status_from_line("领取子进程异常退出，退出码 1；1 秒后自动重启。", "已连接"), "连接重试中")
        self.assertEqual(chest_status_from_line("监督进程：领取子进程已启动（PID 123）；输入 0 可停止。", "已连接"), "启动中")


class TapStatusTests(unittest.TestCase):
    def test_status_follows_actual_tap_output(self) -> None:
        messages = (
            ("模式: keyboard；轮数: 10,000；速率: 200 轮/秒", "启动中"),
            ("5 秒后开始。", "倒计时"),
            ("已发送 200/10,000 轮。", "正在发送"),
            ("收到停止请求，已停止。", "已停止"),
            ("发送完成。以上为系统接受的事件数，桌宠实际增加量请以界面为准。", "发送完成"),
        )
        current = "启动中"
        for line, expected in messages:
            with self.subTest(line=line):
                current = tap_status_from_line(line, current)
                self.assertEqual(current, expected)

    def test_count_summary_does_not_hide_failure_or_stop_state(self) -> None:
        self.assertEqual(tap_status_from_line("累计发送 16 轮，用时 0.10 秒。", "已停止"), "已停止")
        self.assertEqual(tap_status_from_line("累计发送 16 轮，用时 0.10 秒。", "正在发送"), "正在发送")

    def test_unrelated_text_does_not_change_status(self) -> None:
        self.assertEqual(tap_status_from_line("其他日志信息", "正在发送"), "正在发送")


class FakeSavesPage(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, on_message=None, can_write=None) -> None:
        super().__init__(parent)
        self.busy = False
        self.can_write = can_write

    def is_busy(self) -> bool:
        return self.busy


class FakeJobManager:
    def __init__(self) -> None:
        self.active = {"chest": False, "tap": False}
        self.chest_calls: list[dict[str, object]] = []
        self.tap_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.stop_all_count = 0

    def start_chest(self, **kwargs: object) -> int:
        self.chest_calls.append(kwargs)
        self.active["chest"] = True
        return 123

    def start_tap(self, *args: object, **kwargs: object) -> int:
        self.tap_calls.append((args, kwargs))
        self.active["tap"] = True
        return 456

    def is_active(self, job: str) -> bool:
        return self.active[job]

    def poll_events(self) -> list[gui.JobEvent]:
        return []

    def stop_all(self) -> list[str]:
        self.stop_all_count += 1
        return []


class TkControlSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as error:
            self.skipTest(f"无法创建隔离 Tk 窗口：{error}")
        self.addCleanup(self._destroy_root)
        self.manager = FakeJobManager()
        for target, replacement in (
            ("scripts.gui.auto_chest.is_elevated", True),
            ("scripts.gui.existing_chest_instance", False),
        ):
            patcher = patch(target, return_value=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("scripts.gui.create_saves_page", side_effect=FakeSavesPage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = gui.HelperWindow(self.root, manager=self.manager)
        self.window.next_desktop_probe = float("inf")
        self.root.update_idletasks()

    def _destroy_root(self) -> None:
        try:
            for event_id in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(event_id)
            self.root.destroy()
        except tk.TclError:
            pass

    def test_buttons_pass_the_current_ui_options_to_the_backend(self) -> None:
        self.window.allow_sleep.set(True)
        self.window.chest_preview.set(True)
        self.window.chest_start.invoke()
        self.assertEqual(self.manager.chest_calls, [{"allow_sleep": True, "dry_run": True}])

        self.window.count.set("25")
        self.window.rate.set("10.5")
        self.window.delay.set("4")
        self.window.mode.set("键盘与鼠标")
        self.window.tap_preview.set(True)
        self.window.tap_start.invoke()
        self.assertEqual(self.manager.tap_calls, [((25, 10.5, "both"), {"delay": 4.0, "dry_run": True})])

    def test_navigation_switches_page_and_heading(self) -> None:
        self.window.nav_data.invoke()
        self.assertEqual(self.window.notebook.index("current"), 1)
        self.assertEqual(self.window.page_heading.get(), "本地数据")
        self.window.nav_home.invoke()
        self.assertEqual(self.window.notebook.index("current"), 0)
        self.assertEqual(self.window.page_heading.get(), "运行控制")

    def test_process_start_is_not_misreported_as_connected(self) -> None:
        self.window.chest_start.invoke()
        self.window._handle_event(gui.JobEvent("chest", "started", pid=123))
        self.assertEqual(self.window.chest_status.get(), "启动中")
        self.window._handle_event(gui.JobEvent("chest", "output", "状态心跳：等待桌宠连接"))
        self.assertEqual(self.window.chest_status.get(), "启动中")
        self.window._handle_event(gui.JobEvent("chest", "output", "已连接桌宠主页面，开始检查宝箱。"))
        self.assertEqual(self.window.chest_status.get(), "已连接")

    def test_close_requests_stop_then_waits_until_job_is_inactive(self) -> None:
        self.manager.active["chest"] = True
        with patch.object(self.root, "destroy") as destroy:
            self.window.close()
            self.assertTrue(self.window.closing)
            self.assertEqual(self.manager.stop_all_count, 1)
            self.window._poll()
            destroy.assert_not_called()
            self.manager.active["chest"] = False
            self.window._poll()
            destroy.assert_called_once()

    def test_existing_instance_prevents_duplicate_chest_start(self) -> None:
        with patch("scripts.gui.existing_chest_instance", return_value=True), \
             patch("scripts.gui.messagebox.showerror") as error:
            self.window.chest_start.invoke()
        self.assertEqual(self.manager.chest_calls, [])
        error.assert_called_once()

    def test_local_write_blocks_both_job_start_buttons(self) -> None:
        self.window.saves_page.busy = True
        with patch("scripts.gui.messagebox.showinfo") as info:
            self.window.chest_start.invoke()
            self.window.tap_start.invoke()
        self.assertEqual(self.manager.chest_calls, [])
        self.assertEqual(self.manager.tap_calls, [])
        self.assertEqual(info.call_count, 2)

    def test_write_gate_blocks_background_jobs_and_external_chest_instance(self) -> None:
        self.assertTrue(self.window.saves_page.can_write())
        self.manager.active["tap"] = True
        self.assertFalse(self.window.saves_page.can_write())
        self.manager.active["tap"] = False
        with patch("scripts.gui.existing_chest_instance", return_value=True):
            self.assertFalse(self.window.saves_page.can_write())

    def test_mouse_mode_requires_three_seconds_except_in_preview(self) -> None:
        self.window.mode.set("鼠标左键")
        self.window.delay.set("0")
        with patch("scripts.gui.messagebox.showerror") as error:
            self.window.tap_start.invoke()
        self.assertEqual(self.manager.tap_calls, [])
        error.assert_called_once()

        self.window.delay.set("3")
        self.window.tap_start.invoke()
        self.assertEqual(self.manager.tap_calls[0][1]["delay"], 3.0)
        self.manager.active["tap"] = False

        self.window.delay.set("0")
        self.window.tap_preview.set(True)
        self.window.tap_start.invoke()
        self.assertEqual(self.manager.tap_calls[1][1]["delay"], 0.0)
        self.assertTrue(self.manager.tap_calls[1][1]["dry_run"])

    def test_keyboard_mode_accepts_zero_start_delay(self) -> None:
        self.window.mode.set("键盘（F24）")
        self.window.delay.set("0")
        with patch("scripts.gui.messagebox.showerror") as error:
            self.window.tap_start.invoke()
        error.assert_not_called()
        self.assertEqual(self.manager.tap_calls[0][0][2], "keyboard")
        self.assertEqual(self.manager.tap_calls[0][1]["delay"], 0.0)

    def test_minimum_window_keeps_mouse_mode_controls_visible(self) -> None:
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("860x600")
        self.root.deiconify()
        self.window.mode.set("鼠标左键")
        self.root.update()
        for widget in (self.window.chest_start, self.window.chest_stop,
                       self.window.tap_start, self.window.tap_stop):
            with self.subTest(widget=widget):
                panel = widget.master.master
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                     panel.winfo_rooty() + panel.winfo_height())
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                     panel.winfo_rootx() + panel.winfo_width())
        tap_panel = self.window.tap_start.master.master
        hint = next(child for child in tap_panel.winfo_children()
                    if isinstance(child, ttk.Label)
                    and str(child.cget("textvariable")) == str(self.window.tap_hint))
        self.assertGreaterEqual(hint.winfo_height(), hint.winfo_reqheight())
        self.assertLessEqual(hint.winfo_rootx() + hint.winfo_width(),
                             tap_panel.winfo_rootx() + tap_panel.winfo_width())


if __name__ == "__main__":
    unittest.main()
