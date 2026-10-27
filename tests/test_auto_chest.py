from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import auto_chest as app


def snapshot(**changes: object) -> app.Snapshot:
    value: dict[str, object] = {
        "main": True, "supported": True, "loggedIn": True,
        "balance": 3000, "visible": True, "rewardCount": 0,
        "sides": {side: {"ready": True, "clickable": True, "countdown": ""}
                  for side in app.SIDES},
    }
    value.update(changes)
    return app.Snapshot.parse(value)


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages: list[str] = []
        self.clicks: list[str] = []
        self.controller = app.ClaimController(self.messages.append, ready_grace=0)

    def click(self, side: str) -> bool:
        self.clicks.append(side)
        return True

    def test_unsafe_or_busy_states_never_click(self) -> None:
        for changes in ({"supported": False}, {"loggedIn": False}, {"visible": False},
                        {"balance": 999}, {"rewardCount": 1}):
            with self.subTest(changes=changes):
                self.controller.step(snapshot(**changes), self.click, 0)
        self.assertEqual(self.clicks, [])

    def test_one_chest_at_a_time_and_confirm_before_other_side(self) -> None:
        self.controller.step(snapshot(), self.click, 0)
        self.assertEqual(self.clicks, ["left"])
        self.controller.step(snapshot(), self.click, 1)
        self.assertEqual(self.clicks, ["left"])
        success = snapshot(rewardCount=1, balance=2000)
        success.sides["left"] = app.ChestState(False, False, "29:59")
        self.controller.step(success, self.click, 2)
        self.assertEqual(self.controller.confirmed, 1)
        self.assertIsNone(self.controller.pending)
        self.controller.step(success, self.click, 3)
        self.assertEqual(self.clicks, ["left"])
        self.controller.step(replace(success, reward_count=0), self.click, 4)
        self.assertEqual(self.clicks, ["left", "right"])

    def test_last_chest_can_confirm_when_insufficient_balance_hides_countdown(self) -> None:
        self.controller.step(snapshot(balance=1000), self.click, 0)
        result = snapshot(balance=0, rewardCount=1)
        result.sides["left"] = app.ChestState(False, False, "")
        self.controller.step(result, self.click, 1)
        self.assertEqual(self.controller.confirmed, 1)

    def test_button_disappearance_alone_is_not_success(self) -> None:
        self.controller.step(snapshot(), self.click, 0)
        result = snapshot(balance=2000)
        result.sides["left"] = app.ChestState(False, False, "29:59")
        self.controller.step(result, self.click, 1)
        self.assertEqual(self.controller.confirmed, 0)
        self.assertIsNotNone(self.controller.pending)

    def test_success_does_not_require_hover_or_balance_to_drop_during_input_activity(self) -> None:
        self.controller.step(snapshot(), self.click, 0)
        result = snapshot(balance=5000, rewardCount=1)
        result.sides["left"] = app.ChestState(False, False, "")
        self.controller.step(result, self.click, 1)
        self.assertEqual(self.controller.confirmed, 1)

    def test_failed_claim_backoff_prevents_repeat_clicks(self) -> None:
        self.controller.step(snapshot(), self.click, 0)
        blocked = snapshot()
        blocked.sides["right"] = app.ChestState(False, False, "20:00")
        self.controller.step(blocked, self.click, 16)
        self.controller.step(blocked, self.click, 17)
        self.assertEqual(self.clicks, ["left"])
        self.controller.step(blocked, self.click, 77)
        self.assertEqual(self.clicks, ["left", "left"])

    def test_lost_click_reply_requires_backoff(self) -> None:
        def lose_reply(side: str) -> bool:
            self.clicks.append(side)
            raise app.CdpError("lost reply")
        with self.assertRaises(app.CdpError):
            self.controller.step(snapshot(), lose_reply, 0)
        self.controller.disconnected(1)
        self.assertEqual(self.controller.retry_after["left"], 61)
        self.assertIsNone(self.controller.pending)

    def test_dry_run_never_clicks_and_deduplicates_logs(self) -> None:
        controller = app.ClaimController(self.messages.append, dry_run=True, ready_grace=0)
        controller.step(snapshot(), self.click, 0)
        count = len(self.messages)
        controller.step(snapshot(), self.click, 1)
        self.assertEqual(self.clicks, [])
        self.assertEqual(len(self.messages), count)

    def test_ready_must_remain_observed_for_ten_seconds_before_claim(self) -> None:
        controller = app.ClaimController(self.messages.append)
        controller.step(snapshot(), self.click, 0)
        controller.step(snapshot(), self.click, 9.9)
        self.assertEqual(self.clicks, [])
        controller.step(snapshot(), self.click, 10)
        self.assertEqual(self.clicks, ["left"])

    def test_ready_grace_restarts_if_chest_disappears(self) -> None:
        controller = app.ClaimController(self.messages.append)
        controller.step(snapshot(), self.click, 0)
        unavailable = snapshot()
        unavailable.sides["left"] = app.ChestState(False, False, "29:00")
        unavailable.sides["right"] = app.ChestState(False, False, "29:00")
        controller.step(unavailable, self.click, 5)
        controller.step(snapshot(), self.click, 6)
        controller.step(snapshot(), self.click, 15)
        self.assertEqual(self.clicks, [])
        controller.step(snapshot(), self.click, 16)
        self.assertEqual(self.clicks, ["left"])

    def test_server_cooldown_error_backs_off_increasingly_and_clears_on_success(self) -> None:
        self.controller.step(snapshot(), self.click, 0)
        self.controller.cooldown_rejected(1)
        self.assertEqual(self.controller.retry_after["left"], 11)
        blocked = snapshot()
        blocked.sides["right"] = app.ChestState(False, False, "20:00")
        self.controller.step(blocked, self.click, 10)
        self.assertEqual(self.clicks, ["left"])
        self.controller.step(blocked, self.click, 11)
        self.controller.cooldown_rejected(12)
        self.assertEqual(self.controller.retry_after["left"], 32)
        self.controller.step(blocked, self.click, 32)
        result = snapshot(rewardCount=1)
        result.sides["left"] = app.ChestState(False, False, "29:59")
        self.controller.step(result, self.click, 33)
        self.assertEqual(self.controller.cooldown_failures["left"], 0)
        self.assertEqual(self.controller.confirmed, 1)
        self.assertTrue(any("70006" in message for message in self.messages))

    def test_heartbeat_is_bounded_and_reports_missing_status(self) -> None:
        self.controller.heartbeat(0, snapshot())
        self.controller.heartbeat(299, snapshot())
        self.assertEqual(len(self.messages), 1)
        self.controller.status("等待连接")
        self.controller.heartbeat(300)
        self.assertTrue(self.messages[-1].endswith("等待连接"))
        self.controller.heartbeat(800, snapshot())
        self.assertTrue(any("可能暂停过" in message for message in self.messages))

    def test_bad_snapshot_and_side_fail_closed(self) -> None:
        for raw in (None, {}, {"balance": float("nan"), "rewardCount": 0}):
            with self.assertRaises(app.AutoChestError):
                app.Snapshot.parse(raw)
        with self.assertRaises(app.AutoChestError):
            app.click_expression("__proto__")

    def test_pipe_zero_and_eof_stop_the_program(self) -> None:
        for text in ("hello\n0\n", ""):
            event = threading.Event()
            with patch.object(app.sys, "stdin", io.StringIO(text)):
                app.console_stop_reader(event)
            self.assertTrue(event.is_set())

    def test_stop_prevents_starting_target_discovery(self) -> None:
        event = threading.Event()
        event.set()
        with patch.object(app, "discover_targets") as discover:
            self.assertIsNone(app.connect_main(19227, event))
        discover.assert_not_called()

    def test_launch_only_changes_child_environment(self) -> None:
        old_environment = os.environ.copy()
        with patch.object(app.subprocess, "Popen") as start:
            app.launch_app(Path("D:/App/WuwaTappo/WuwaTappo.exe"), 19227)
        child_environment = start.call_args.kwargs["env"]
        self.assertIn("--remote-debugging-address=127.0.0.1", child_environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"])
        self.assertIn("--remote-debugging-port=19227", child_environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"])
        self.assertNotIn("--remote-allow-origins", child_environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"])
        self.assertEqual(dict(os.environ), old_environment)

    def test_admin_required_is_actionable_and_not_retried_as_a_connection_error(self) -> None:
        error = OSError("elevation required")
        error.winerror = 740
        with patch.object(app.subprocess, "Popen", side_effect=error):
            with self.assertRaisesRegex(app.AutoChestError, "以管理员身份运行"):
                app.launch_app(Path("D:/App/WuwaTappo/WuwaTappo.exe"), 19227)


class StartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stop = threading.Event()
        self.order: list[str] = []
        self.policy = Mock()
        self.policy.recover_pending.return_value = False
        self.policy.adopt_pending.return_value = False
        self.policy.install.side_effect = lambda: self.order.append("install")
        self.policy.restore.side_effect = lambda: self.order.append("restore") or True
        self.lock = Mock()
        self.client = Mock()
        self.sleep_guard = Mock()
        self.sleep_guard.acquire.return_value = True
        self.sleep_guard.release.return_value = True
        self.messages: list[str] = []

    def common(self, stack: ExitStack) -> None:
        stack.enter_context(patch.object(app, "SingleInstance", return_value=self.lock))
        stack.enter_context(patch.object(app, "TemporaryDebugPolicy", return_value=self.policy))
        stack.enter_context(patch.object(app, "is_elevated", return_value=True))
        stack.enter_context(patch.object(app, "app_running", return_value=False))
        stack.enter_context(patch.object(app, "PreventSleep", return_value=self.sleep_guard))
        stack.enter_context(patch.object(app, "log", side_effect=self.messages.append))
        stack.enter_context(patch.object(self.stop, "wait", return_value=False))

    def test_elevated_app_gets_policy_before_launch_and_restores_on_exit(self) -> None:
        def launch(*_args: object) -> SimpleNamespace:
            self.order.append("launch")
            return SimpleNamespace(pid=123)
        def evaluate(_expression: str) -> dict[str, object]:
            self.order.append("read")
            self.stop.set()
            return {}
        self.client.evaluate.side_effect = evaluate
        with ExitStack() as stack:
            self.common(stack)
            stack.enter_context(patch.object(app, "check_listeners", side_effect=[False, True]))
            stack.enter_context(patch.object(app, "app_running", return_value=False))
            stack.enter_context(patch.object(app, "launch_app", side_effect=launch))
            stack.enter_context(patch.object(app, "connect_main", return_value=self.client))
            stack.enter_context(patch.object(app.Snapshot, "parse", return_value=snapshot()))
            result = app.run(app.parse_args(["--dry-run"]), self.stop)
        self.assertEqual(result, 0)
        self.assertEqual(self.order, ["install", "launch", "read", "restore"])
        self.policy.recover_pending.assert_called_once()
        self.lock.close.assert_called_once()
        self.client.close.assert_called_once()
        self.sleep_guard.acquire.assert_called_once()
        self.sleep_guard.release.assert_called_once()

    def test_failed_launch_restores_policy_and_does_not_retry_forever(self) -> None:
        with ExitStack() as stack:
            self.common(stack)
            stack.enter_context(patch.object(app, "check_listeners", return_value=False))
            stack.enter_context(patch.object(app, "app_running", return_value=False))
            launch = stack.enter_context(patch.object(app, "launch_app", side_effect=app.AutoChestError("failed")))
            with self.assertRaisesRegex(app.AutoChestError, "failed"):
                app.run(app.parse_args([]), self.stop)
        self.assertEqual(self.order, ["install", "restore"])
        launch.assert_called_once()
        self.lock.close.assert_called_once()

    def test_restore_only_never_launches_app_or_connects(self) -> None:
        with ExitStack() as stack:
            self.common(stack)
            launch = stack.enter_context(patch.object(app, "launch_app"))
            connect = stack.enter_context(patch.object(app, "connect_main"))
            self.assertEqual(app.run(app.parse_args(["--restore-debug-policy"]), self.stop), 0)
        launch.assert_not_called()
        connect.assert_not_called()
        self.policy.install.assert_not_called()
        self.sleep_guard.acquire.assert_not_called()

    def test_check_does_not_describe_missing_port_as_ready_or_change_policy(self) -> None:
        with ExitStack() as stack:
            self.common(stack)
            stack.enter_context(patch.object(app, "check_listeners", return_value=False))
            stack.enter_context(patch.object(app, "app_running", return_value=True))
            self.assertEqual(app.run(app.parse_args(["--check"]), self.stop), 0)
        self.assertTrue(any("尚未建立领取连接" in message for message in self.messages))
        self.policy.recover_pending.assert_not_called()
        self.policy.install.assert_not_called()

    def test_allow_sleep_skips_sleep_inhibition(self) -> None:
        with ExitStack() as stack:
            self.common(stack)
            self.stop.set()
            self.assertEqual(app.run(app.parse_args(["--allow-sleep"]), self.stop), 0)
        self.sleep_guard.acquire.assert_not_called()

    def test_restart_adopts_live_desktop_policy_without_early_restore(self) -> None:
        self.policy.adopt_pending.return_value = True
        def evaluate(_expression: str) -> dict[str, object]:
            self.order.append("read")
            self.stop.set()
            return {}
        self.client.evaluate.side_effect = evaluate
        with ExitStack() as stack:
            self.common(stack)
            stack.enter_context(patch.object(app, "check_listeners", return_value=True))
            stack.enter_context(patch.object(app, "app_running", return_value=True))
            launch = stack.enter_context(patch.object(app, "launch_app"))
            stack.enter_context(patch.object(app, "connect_main", return_value=self.client))
            stack.enter_context(patch.object(app.Snapshot, "parse", return_value=snapshot()))
            self.assertEqual(app.run(app.parse_args(["--dry-run"]), self.stop), 0)
        self.assertEqual(self.order, ["read", "restore"])
        self.policy.adopt_pending.assert_called_once()
        self.policy.recover_pending.assert_not_called()
        launch.assert_not_called()

    def test_new_server_cooldown_log_is_associated_with_the_pending_click(self) -> None:
        tail = Mock()
        tail.mark.side_effect = lambda: self.order.append("mark")
        def poll() -> str:
            self.order.append("poll")
            self.stop.set()
            return "70006"
        tail.poll.side_effect = poll
        def evaluate(expression: str) -> dict[str, object]:
            if expression == app.SNAPSHOT_EXPRESSION:
                return {}
            self.order.append("click")
            return {"clicked": True}
        self.client.evaluate.side_effect = evaluate
        real_controller = app.ClaimController
        with ExitStack() as stack:
            self.common(stack)
            stack.enter_context(patch.dict(app.os.environ, {"LOCALAPPDATA": "C:/fake"}))
            stack.enter_context(patch.object(app, "ChestErrorTail", return_value=tail))
            stack.enter_context(patch.object(app, "check_listeners", return_value=True))
            stack.enter_context(patch.object(app, "connect_main", return_value=self.client))
            stack.enter_context(patch.object(app.Snapshot, "parse", return_value=snapshot()))
            stack.enter_context(patch.object(app, "ClaimController", side_effect=lambda log, dry_run: real_controller(log, dry_run=dry_run, ready_grace=0)))
            self.assertEqual(app.run(app.parse_args([]), self.stop), 0)
        self.assertEqual(self.order[:3], ["mark", "click", "poll"])
        self.assertTrue(any("70006" in message for message in self.messages))


class PowerGuardTests(unittest.TestCase):
    def test_system_only_request_is_released(self) -> None:
        function = Mock(return_value=1)
        with patch.object(app.ctypes, "WinDLL", return_value=SimpleNamespace(SetThreadExecutionState=function)):
            guard = app.PreventSleep()
            self.assertTrue(guard.acquire())
            self.assertTrue(guard.release())
        self.assertEqual(function.call_args_list[0].args, (app.PreventSleep.ES_CONTINUOUS | app.PreventSleep.ES_SYSTEM_REQUIRED,))
        self.assertEqual(function.call_args_list[1].args, (app.PreventSleep.ES_CONTINUOUS,))


JS_DRIVER = r"""
const vm = require('node:vm');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', () => {
  const {expression, options: o} = JSON.parse(raw);
  let clicks = [];
  function element(hidden = false) {
    return {
      hidden, parentElement: null,
      getClientRects() { return [1]; },
      getBoundingClientRect() { return {width: 40, height: 40}; }
    };
  }
  const parent = element(Boolean(o.hidden));
  const dock = element(); dock.parentElement = parent;
  const anchors = {};
  for (const side of ['left', 'right']) {
    const button = element(); button.parentElement = parent;
    button.disabled = Boolean(o.busy);
    button.click = () => clicks.push(side);
    anchors[side] = {button, querySelector(selector) {
      if (selector === 'button.reward-btn') return o.ready === false ? null : button;
      if (selector === '.reward-countdown') return o.ready === false ? {textContent: '29:00'} : null;
    }};
  }
  const app = {__vue_app__: {config: {globalProperties: {$pinia: {_s: new Map([
    ['GlobalState', {isLoggedIn: o.loggedIn !== false, totalClickCount: o.balance ?? 3000}]
  ])}}}}};
  const document = {
    querySelector(selector) {
      if (selector === '#app') return app;
      if (selector === '.pet-dock.pet-dock--fixed > .pet-dock-content') return dock;
      for (const side of ['left', 'right']) {
        if (selector === '.reward-anchor--' + side) return anchors[side];
        if (selector === '.reward-anchor--' + side + ' > button.reward-btn') return anchors[side].button;
      }
      return null;
    },
    querySelectorAll() { return Array(o.rewardCount || 0).fill({}); }
  };
  const context = {
    document, location: {hash: o.route ?? '#/'},
    __TAURI_INTERNALS__: {metadata: {currentWindow: {label: 'main'}, currentWebview: {label: 'main'}}},
    getComputedStyle() { return {display: 'block', visibility: 'visible', opacity: '1'}; }
  };
  const value = vm.runInNewContext(expression, context);
  process.stdout.write(JSON.stringify({value, clicks}));
});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed only for the mock DOM tests")
class PageBridgeTests(unittest.TestCase):
    def evaluate(self, expression: str, **options: object) -> dict[str, object]:
        result = subprocess.run(
            [shutil.which("node") or "node", "-e", JS_DRIVER],
            input=json.dumps({"expression": expression, "options": options}),
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=5,
        )
        return json.loads(result.stdout)

    def test_production_bridge_reads_only_needed_state(self) -> None:
        result = self.evaluate(app.SNAPSHOT_EXPRESSION, ready=False)
        state = app.Snapshot.parse(result["value"])
        self.assertTrue(state.supported)
        self.assertEqual(state.balance, 3000)
        self.assertFalse(state.sides["left"].ready)
        self.assertEqual(state.sides["left"].countdown, "29:00")
        self.assertEqual(result["clicks"], [])

    def test_click_rechecks_login_balance_visibility_busy_and_route(self) -> None:
        for options in ({"loggedIn": False}, {"balance": 999}, {"hidden": True},
                        {"busy": True}, {"rewardCount": 1}, {"ready": False}, {"route": "#/panel"}):
            with self.subTest(options=options):
                result = self.evaluate(app.click_expression("left"), **options)
                self.assertEqual(result["clicks"], [])
                self.assertFalse(result["value"]["clicked"])

    def test_click_only_targets_requested_chest(self) -> None:
        result = self.evaluate(app.click_expression("right"))
        self.assertEqual(result["clicks"], ["right"])
        self.assertTrue(result["value"]["clicked"])


if __name__ == "__main__":
    unittest.main()
