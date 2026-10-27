"""Keep Wuwa Tappo's normal chest claim flow running in a Windows console."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import csv
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import http.client
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

if __package__:
    from .cdp_client import CdpClient, CdpError
    from .chest_log import ChestErrorTail
    from .webview_policy import PolicyError, TemporaryDebugPolicy
else:
    from cdp_client import CdpClient, CdpError
    from chest_log import ChestErrorTail
    from webview_policy import PolicyError, TemporaryDebugPolicy


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXECUTABLE = PROJECT_ROOT.parent / "WuwaTappo.exe"
DEFAULT_PORT = 19227
MIN_BALANCE = 1_000
SIDES = ("left", "right")
SIDE_NAMES = {"left": "左侧", "right": "右侧"}
MAX_HTTP_BYTES = 1_048_576
READY_GRACE_SECONDS = 10.0
HEARTBEAT_SECONDS = 300.0
POLICY_BACKUP = PROJECT_ROOT / "backups" / "auto_chest_webview_policy.pending.json"
LOG_PATH = PROJECT_ROOT / "logs" / "auto_chest.log"
LOGGER = logging.getLogger("wuwatappo.auto_chest")
LOGGER.propagate = False

SNAPSHOT_FUNCTION = r"""function () {
  const metadata = globalThis.__TAURI_INTERNALS__?.metadata;
  const route = location.hash.replace(/^#/, '').split('?')[0];
  const dock = document.querySelector('.pet-dock.pet-dock--fixed > .pet-dock-content');
  const main = metadata?.currentWindow?.label === 'main'
    && metadata?.currentWebview?.label === 'main'
    && (route === '' || route === '/') && !!dock;
  const store = document.querySelector('#app')?.__vue_app__
    ?.config.globalProperties.$pinia?._s?.get('GlobalState');
  const balance = Number(store?.totalClickCount);
  const visible = (element) => {
    if (!element || !element.getClientRects().length) return false;
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    for (let current = element; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (current.hidden || style.display === 'none'
          || style.visibility === 'hidden' || style.visibility === 'collapse'
          || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const sides = {};
  for (const side of ['left', 'right']) {
    const anchor = document.querySelector('.reward-anchor--' + side);
    const button = anchor?.querySelector('button.reward-btn');
    const countdown = anchor?.querySelector('.reward-countdown');
    sides[side] = {
      ready: !!button,
      clickable: !!button && !button.disabled && visible(button),
      countdown: (countdown?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 48)
    };
  }
  return {
    main,
    supported: main && !!store && Number.isFinite(balance) && balance >= 0,
    loggedIn: !!store?.isLoggedIn,
    balance: Number.isFinite(balance) ? balance : 0,
    visible: visible(dock),
    rewardCount: document.querySelectorAll(
      '.chest-reward-popup-layer .chest-drop-motion-root').length,
    sides
  };
}"""
SNAPSHOT_EXPRESSION = f"({SNAPSHOT_FUNCTION})()"


class AutoChestError(Exception):
    """An application or local control connection cannot be used safely."""


def click_expression(side: str) -> str:
    if side not in SIDES:
        raise AutoChestError("未知宝箱位置。")
    return """(() => {
      const state = (%s)();
      const side = %s;
      if (!state.supported || !state.loggedIn || !state.visible
          || state.balance < 1000 || state.rewardCount > 0
          || !state.sides[side].clickable) return {clicked: false};
      const button = document.querySelector('.reward-anchor--' + side + ' > button.reward-btn');
      if (!button || button.disabled) return {clicked: false};
      button.click();
      return {clicked: true, balance: state.balance};
    })()""" % (SNAPSHOT_FUNCTION, json.dumps(side))


@dataclass(frozen=True)
class ChestState:
    ready: bool
    clickable: bool
    countdown: str


@dataclass(frozen=True)
class Snapshot:
    main: bool
    supported: bool
    logged_in: bool
    balance: float
    visible: bool
    reward_count: int
    sides: dict[str, ChestState]

    @classmethod
    def parse(cls, value: Any) -> Snapshot:
        if not isinstance(value, dict):
            raise AutoChestError("桌宠页面返回了未知状态。")
        try:
            balance = value["balance"]
            reward_count = value["rewardCount"]
            if (
                type(balance) not in (int, float)
                or not math.isfinite(balance)
                or balance < 0
                or type(reward_count) is not int
                or reward_count < 0
            ):
                raise ValueError
            for name in ("main", "supported", "loggedIn", "visible"):
                if type(value[name]) is not bool:
                    raise ValueError
            sides: dict[str, ChestState] = {}
            for side in SIDES:
                raw = value["sides"][side]
                if (
                    type(raw["ready"]) is not bool
                    or type(raw["clickable"]) is not bool
                    or not isinstance(raw["countdown"], str)
                ):
                    raise ValueError
                sides[side] = ChestState(raw["ready"], raw["clickable"], raw["countdown"][:48])
            return cls(
                value["main"], value["supported"], value["loggedIn"], balance,
                value["visible"], reward_count, sides,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise AutoChestError("桌宠页面状态格式不受支持，已暂停领取。") from error


@dataclass
class PendingClaim:
    side: str
    deadline: float
    saw_reward: bool = False


class ClaimController:
    """Only claim one chest at a time, and confirm its UI result before continuing."""

    def __init__(
        self, log: Callable[[str], None], *, dry_run: bool = False,
        ready_grace: float = READY_GRACE_SECONDS,
    ) -> None:
        self.log = log
        self.dry_run = dry_run
        self.ready_grace = ready_grace
        self.pending: PendingClaim | None = None
        self.retry_after = dict.fromkeys(SIDES, 0.0)
        self.ready_since: dict[str, float | None] = dict.fromkeys(SIDES, None)
        self.cooldown_failures: dict[str, int] = dict.fromkeys(SIDES, 0)
        self.confirmed = 0
        self.last_status = ""
        self.last_heartbeat: float | None = None
        self.dry_reported: set[str] = set()

    def status(self, text: str) -> None:
        if text != self.last_status:
            self.log(text)
            self.last_status = text

    def disconnected(self, now: float) -> None:
        self.ready_since = dict.fromkeys(SIDES, None)
        if self.pending is not None:
            self.retry_after[self.pending.side] = now + 60
            self.pending = None
            self.log("连接中断，上一笔结果未知；该侧至少等待 60 秒后重试。")

    def heartbeat(self, now: float, state: Snapshot | None = None) -> None:
        if self.last_heartbeat is not None and now - self.last_heartbeat < HEARTBEAT_SECONDS:
            return
        if self.last_heartbeat is not None and now - self.last_heartbeat > HEARTBEAT_SECONDS + 120:
            self.log(f"上次状态检查距今 {now - self.last_heartbeat:.0f} 秒，期间系统或脚本可能暂停过。")
        self.last_heartbeat = now
        if state is None:
            self.log(f"状态心跳：{self.last_status or '等待桌宠连接'}")
            return
        def side_status(side: str) -> str:
            chest = state.sides[side]
            if chest.clickable:
                return "可领取"
            return chest.countdown or ("忙碌" if chest.ready else "等待")
        self.log(
            f"状态心跳：{'已登录' if state.logged_in else '未登录'}；"
            f"桌宠{'可见' if state.visible else '隐藏'}；可用敲击数 {state.balance:,.0f}；"
            f"左侧 {side_status('left')}，右侧 {side_status('right')}。"
        )

    def cooldown_rejected(self, now: float) -> None:
        if self.pending is None:
            return
        side = self.pending.side
        self.pending = None
        self.cooldown_failures[side] += 1
        delay = min(60, 10 * (2 ** min(self.cooldown_failures[side] - 1, 3)))
        self.retry_after[side] = now + delay
        self.log(f"触发{SIDE_NAMES[side]}宝箱后，桌宠日志显示服务器冷却中（70006）；{delay} 秒后重试。")

    def step(self, state: Snapshot, click: Callable[[str], bool], now: float) -> None:
        if self.pending is not None:
            pending = self.pending
            pending.saw_reward |= state.reward_count > 0
            chest = state.sides[pending.side]
            if (state.supported and state.logged_in and pending.saw_reward
                    and not chest.ready):
                self.confirmed += 1
                self.cooldown_failures[pending.side] = 0
                self.ready_since[pending.side] = None
                self.log(f"{SIDE_NAMES[pending.side]}宝箱领取成功，本次累计 {self.confirmed} 个。")
                self.retry_after[pending.side] = now + 5
                self.pending = None
            elif now >= pending.deadline:
                self.log(f"{SIDE_NAMES[pending.side]}宝箱未确认领取结果，60 秒后再检查。")
                self.retry_after[pending.side] = now + 60
                self.pending = None
            return
        if not state.supported:
            self.ready_since = dict.fromkeys(SIDES, None)
            self.status("等待桌宠主页面初始化；页面结构不匹配时暂停领取。")
            return
        if not state.logged_in:
            self.ready_since = dict.fromkeys(SIDES, None)
            self.status("等待你在桌宠中登录。")
            return
        if not state.visible:
            self.ready_since = dict.fromkeys(SIDES, None)
            self.status("桌宠当前隐藏，等待恢复显示。")
            return
        if state.balance < MIN_BALANCE:
            self.ready_since = dict.fromkeys(SIDES, None)
            self.status(f"可用敲击数不足 1,000，暂停领取（当前 {state.balance:,.0f}）。")
            return
        for side in SIDES:
            chest = state.sides[side]
            if not chest.ready:
                self.dry_reported.discard(side)
                self.ready_since[side] = None
            elif chest.clickable and self.ready_since[side] is None:
                self.ready_since[side] = now
        if state.reward_count:
            self.status("等待奖励动画结束。")
            return
        waiting_for_grace = False
        for side in SIDES:
            chest = state.sides[side]
            if not chest.clickable or now < self.retry_after[side]:
                continue
            ready_at = self.ready_since[side]
            if ready_at is None or now - ready_at < self.ready_grace:
                waiting_for_grace = True
                continue
            if self.dry_run:
                if side not in self.dry_reported:
                    self.log(f"预览：{SIDE_NAMES[side]}宝箱可领取，未点击。")
                    self.dry_reported.add(side)
                continue
            # Mark the operation before sending: a lost reply must not cause an immediate retry.
            self.pending = PendingClaim(side, now + 15)
            self.retry_after[side] = now + 60
            if click(side):
                self.log(f"已触发{SIDE_NAMES[side]}宝箱领取，等待桌宠确认。")
            else:
                self.pending = None
                self.retry_after[side] = now + 2
            return
        if waiting_for_grace:
            self.status(f"宝箱已就绪，等待 {self.ready_grace:g} 秒冷却缓冲。")
        else:
            self.status("正在等待可领取宝箱；输入 0 退出。")


def log(message: str) -> None:
    print(f"[{datetime.now().astimezone():%H:%M:%S}] {message}", flush=True)
    LOGGER.info(message)


def configure_file_logging() -> None:
    if LOGGER.handlers:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_PATH, maxBytes=1_048_576, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
    except OSError:
        print("日志文件无法写入，将仅在 CMD 中显示状态。", flush=True)


def is_elevated() -> bool:
    if os.name != "nt":
        return False
    library = ctypes.WinDLL("shell32", use_last_error=True)
    library.IsUserAnAdmin.argtypes = []
    library.IsUserAnAdmin.restype = wintypes.BOOL
    return bool(library.IsUserAnAdmin())


class PreventSleep:
    """Keep Windows from idling into sleep while this thread is active."""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self) -> None:
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        self._set_state = library.SetThreadExecutionState
        self._set_state.argtypes = [wintypes.DWORD]
        self._set_state.restype = wintypes.DWORD
        self.active = False

    def acquire(self) -> bool:
        self.active = bool(self._set_state(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED))
        return self.active

    def release(self) -> bool:
        if not self.active:
            return True
        released = bool(self._set_state(self.ES_CONTINUOUS))
        if released:
            self.active = False
        return released


def app_running() -> bool:
    if os.name != "nt":
        raise AutoChestError("此脚本仅支持 Windows。")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    result = subprocess.run(
        [str(system_root / "System32" / "tasklist.exe"), "/FI",
         "IMAGENAME eq WuwaTappo.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace", timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or not result.stdout.strip():
        raise AutoChestError("无法可靠检查桌宠进程。")
    return any(row and row[0].casefold() == "wuwatappo.exe"
               for row in csv.reader(io.StringIO(result.stdout)))


def check_listeners(port: int) -> bool:
    """Reject a debugging port that listens on an address other than loopback."""
    if os.name != "nt":
        raise AutoChestError("调试端口检查仅支持 Windows。")
    library = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_table = library.GetExtendedTcpTable
    get_table.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                          wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    get_table.restype = wintypes.DWORD
    found = False
    for family, row_size in ((socket.AF_INET, 24), (socket.AF_INET6, 56)):
        size = wintypes.DWORD()
        result = get_table(None, ctypes.byref(size), False, family, 5, 0)
        if result not in (0, 122):
            raise AutoChestError("无法检查调试端口的监听地址。")
        for attempt in range(3):
            buffer = ctypes.create_string_buffer(size.value)
            result = get_table(buffer, ctypes.byref(size), False, family, 5, 0)
            if result != 122:
                break
        if result:
            raise AutoChestError("读取调试端口监听地址失败。")
        raw = buffer.raw
        count = struct.unpack_from("<I", raw)[0]
        if 4 + count * row_size > len(raw):
            raise AutoChestError("调试端口表格式异常。")
        for index in range(count):
            offset = 4 + index * row_size
            if family == socket.AF_INET:
                state, address, raw_port = struct.unpack_from("<III", raw, offset)
                host = socket.inet_ntop(family, struct.pack("<I", address))
            else:
                host = socket.inet_ntop(family, raw[offset:offset + 16])
                raw_port = struct.unpack_from("<I", raw, offset + 20)[0]
                state = struct.unpack_from("<I", raw, offset + 48)[0]
            if state == 2 and socket.ntohs(raw_port & 0xFFFF) == port:
                if host not in ("127.0.0.1", "::1"):
                    raise AutoChestError("该端口未限定本机回环地址，请改用其他端口。")
                found = True
    return found


def discover_targets(port: int) -> list[str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/json/list")
        response = connection.getresponse()
        raw = response.read(MAX_HTTP_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_HTTP_BYTES:
            raise AutoChestError("本地调试端口没有返回受支持的页面列表。")
        targets = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise AutoChestError("本地调试页面列表格式异常。") from error
    finally:
        connection.close()
    if not isinstance(targets, list):
        raise AutoChestError("本地调试页面列表格式异常。")
    urls: list[str] = []
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        page = urlsplit(str(target.get("url", "")))
        if (page.scheme not in ("tauri", "http", "https")
                or page.hostname not in ("localhost", "tauri.localhost")):
            continue
        endpoint = urlsplit(str(target.get("webSocketDebuggerUrl", "")))
        if (endpoint.scheme != "ws" or endpoint.hostname not in ("127.0.0.1", "localhost")
                or endpoint.port != port or endpoint.username is not None
                or endpoint.password is not None or endpoint.query or endpoint.fragment
                or not endpoint.path.startswith("/devtools/page/")):
            continue
        urls.append(f"ws://127.0.0.1:{port}{endpoint.path}")
    return urls


def connect_main(port: int, stop: threading.Event | None = None) -> CdpClient | None:
    if stop is not None and stop.is_set():
        return None
    matches: list[CdpClient] = []
    try:
        for url in discover_targets(port):
            if stop is not None and stop.is_set():
                break
            client = CdpClient(url, timeout=3)
            try:
                state = Snapshot.parse(client.evaluate(SNAPSHOT_EXPRESSION))
                if state.main:
                    matches.append(client)
                else:
                    client.close()
            except (CdpError, AutoChestError, OSError):
                client.close()
        if len(matches) > 1:
            raise AutoChestError("发现多个桌宠主页面，为避免重复领取已停止连接。")
        return matches.pop() if matches else None
    finally:
        for client in matches:
            client.close()


def launch_app(executable: Path, port: int) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    arguments = environment.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if "remote-debugging" in arguments:
        raise AutoChestError("现有 WebView2 环境变量含其他调试参数，请先清除冲突后重试。")
    environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        arguments + f" --remote-debugging-address=127.0.0.1 --remote-debugging-port={port}"
    ).strip()
    try:
        return subprocess.Popen(
            [str(executable)], cwd=executable.parent, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        if getattr(error, "winerror", None) == 740:
            raise AutoChestError(
                "桌宠要求管理员权限启动。请右键 auto_chest.cmd，选择“以管理员身份运行”。"
            ) from error
        raise AutoChestError(
            f"启动桌宠失败（错误码 {getattr(error, 'winerror', None) or error.errno}）。"
        ) from error


def console_stop_reader(stop: threading.Event) -> None:
    if os.name == "nt" and sys.stdin.isatty():
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
        for line in sys.stdin:
            if line.strip() == "0":
                stop.set()
                return
        stop.set()


class SingleInstance:
    def __init__(self) -> None:
        self.library = ctypes.WinDLL("kernel32", use_last_error=True)
        self.library.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.library.CreateMutexW.restype = wintypes.HANDLE
        self.library.CloseHandle.argtypes = [wintypes.HANDLE]
        self.library.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        self.handle = self.library.CreateMutexW(None, False, "Local\\WuwaTappoAutoChest")
        if not self.handle:
            raise AutoChestError("无法创建脚本单实例锁。")
        if ctypes.get_last_error() == 183:
            self.close()
            raise AutoChestError("自动领取脚本已经运行，请在原 CMD 窗口操作。")

    def close(self) -> None:
        if self.handle:
            self.library.CloseHandle(self.handle)
            self.handle = None


def bounded_port(value: str) -> int:
    number = int(value)
    if not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError("端口必须介于 1024 和 65535 之间。")
    return number


def bounded_interval(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.5 <= number <= 60:
        raise argparse.ArgumentTypeError("检查间隔必须介于 0.5 和 60 秒之间。")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=bounded_port, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=bounded_interval, default=2.0, help="空闲检查间隔，默认 2 秒")
    parser.add_argument("--allow-sleep", action="store_true", help="不阻止 Windows 自动睡眠")
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXECUTABLE)
    parser.add_argument("--dry-run", action="store_true", help="只监测，不领取")
    parser.add_argument("--attach-only", action="store_true", help="不启动桌宠，只等待已有本地连接")
    parser.add_argument("--check", action="store_true", help="检查本机环境后退出，不启动应用、不领取")
    parser.add_argument("--restore-debug-policy", action="store_true", help="恢复上次异常退出遗留的桌宠调试配置后退出")
    return parser.parse_args(argv)


def run(args: argparse.Namespace, stop: threading.Event) -> int:
    if os.name != "nt":
        raise AutoChestError("此脚本仅支持 Windows。")
    executable = args.exe.resolve()
    if executable.name.casefold() != "wuwatappo.exe" or not executable.is_file():
        raise AutoChestError("未找到 WuwaTappo.exe，请确认其位于 WTHelper 的上一级目录，或使用 --exe 指定路径。")
    if args.check:
        listening = check_listeners(args.port)
        log(f"桌宠{'运行中' if app_running() else '未运行'}；"
            f"当前脚本{'管理员' if is_elevated() else '普通'}权限；"
            f"本地端口 {args.port} {'有监听' if listening else '未启动，尚未建立领取连接'}。")
        if listening:
            client = connect_main(args.port, stop)
            if client is None:
                log("端口可用，但没有找到受支持的桌宠主页面。")
            else:
                try:
                    state = Snapshot.parse(client.evaluate(SNAPSHOT_EXPRESSION))
                    log(f"主页面{'受支持' if state.supported else '未初始化'}；"
                        f"{'已登录' if state.logged_in else '未登录'}；"
                        f"可用敲击数 {state.balance:,.0f}；"
                        f"左侧{'可点击' if state.sides['left'].clickable else '不可点击'}，"
                        f"右侧{'可点击' if state.sides['right'].clickable else '不可点击'}。")
                finally:
                    client.close()
        return 0
    lock = SingleInstance()
    policy = TemporaryDebugPolicy(args.port, POLICY_BACKUP)
    client: CdpClient | None = None
    controller = ClaimController(log, dry_run=args.dry_run)
    local_app_data = os.environ.get("LOCALAPPDATA")
    error_tail = ChestErrorTail(
        Path(local_app_data) / "com.kuro.wuwatappo.cn" / "logs" / "Wuwa Tappo.log"
    ) if local_app_data else None
    sleep_guard: PreventSleep | None = None
    launched: subprocess.Popen[bytes] | None = None
    launched_at = 0.0
    adopted_running_app = False
    announced_wait = False
    try:
        if args.restore_debug_policy:
            if policy.recover_pending():
                log("已恢复上次异常退出遗留的桌宠调试配置。")
            log("调试配置恢复检查完成。")
            return 0
        if app_running() and policy.adopt_pending():
            adopted_running_app = True
            launched_at = time.monotonic()
            log("已接管仍在运行的桌宠及其临时调试配置；停止时恢复原值。")
        elif policy.recover_pending():
            log("已恢复上次异常退出遗留的桌宠调试配置。")
        if not args.allow_sleep:
            try:
                sleep_guard = PreventSleep()
                if sleep_guard.acquire():
                    log("已阻止系统自动睡眠；屏幕关闭设置不受影响。")
                else:
                    log("无法阻止系统自动睡眠，请检查 Windows 电源设置。")
            except OSError:
                log("无法阻止系统自动睡眠，请检查 Windows 电源设置。")
        log(f"自动领取已启动（脚本 PID {os.getpid()}）。CMD 保持打开，输入 0 即退出，也可按 Ctrl+C。")
        if args.dry_run:
            log("当前为预览模式，不会点击宝箱。")
        while not stop.is_set():
            try:
                if client is None:
                    if check_listeners(args.port):
                        client = connect_main(args.port, stop)
                        if client is None:
                            controller.status("本地端口已开启，等待唯一的桌宠主页面。")
                    elif args.attach_only:
                        controller.status("等待已有的桌宠本地控制连接；输入 0 退出。")
                    elif app_running():
                        if launched is None:
                            if adopted_running_app:
                                if time.monotonic() - launched_at > 30:
                                    raise AutoChestError("已接管的桌宠未开放本地连接，请退出桌宠后重新运行脚本。")
                                controller.status("等待已接管的桌宠开放本地连接。")
                            elif not announced_wait:
                                log("桌宠已运行但未启用本地连接。请从托盘完全退出桌宠；脚本会自动重新启动。")
                                announced_wait = True
                        elif time.monotonic() - launched_at > 30:
                            raise AutoChestError("桌宠启动后仍未开放本地连接；请检查 WebView2 或使用相同权限运行。")
                    else:
                        if launched is not None:
                            controller.status("桌宠已退出，自动领取暂停。重新启动本脚本可继续。")
                        else:
                            if is_elevated():
                                log("正在为 Wuwa Tappo 临时设置应用专用 WebView2 本地调试配置；停止脚本时恢复。")
                                policy.install()
                            launched = launch_app(executable, args.port)
                            launched_at = time.monotonic()
                            log(f"已启动桌宠（PID {launched.pid}），正在等待主页面和登录状态。")
                    if client is not None:
                        log("已连接桌宠主页面，开始检查宝箱。")
                        controller.last_status = ""
                    else:
                        controller.heartbeat(time.monotonic())
                        stop.wait(1)
                        continue
                if stop.is_set():
                    break
                state = Snapshot.parse(client.evaluate(SNAPSHOT_EXPRESSION))
                controller.heartbeat(time.monotonic(), state)
                if controller.pending is not None and error_tail is not None and error_tail.poll() == "70006":
                    controller.cooldown_rejected(time.monotonic())
                    stop.wait(min(args.interval, 1))
                    continue
                def click(side: str) -> bool:
                    if stop.is_set():
                        return False
                    if error_tail is not None:
                        error_tail.mark()
                    result = client.evaluate(click_expression(side))
                    return isinstance(result, dict) and result.get("clicked") is True
                controller.step(state, click, time.monotonic())
                stop.wait(0.25 if controller.pending else args.interval)
            except (CdpError, ConnectionError, TimeoutError, http.client.HTTPException, OSError) as error:
                if client is not None:
                    client.close()
                    client = None
                controller.disconnected(time.monotonic())
                controller.status(f"本地连接暂不可用（{type(error).__name__}），稍后重连；输入 0 退出。")
                controller.heartbeat(time.monotonic())
                stop.wait(3)
        log(f"已停止自动领取，本次确认领取 {controller.confirmed} 个宝箱。桌宠继续运行。")
        return 0
    finally:
        if client is not None:
            client.close()
        try:
            if policy.restore():
                log("应用专用临时调试配置已恢复。")
        finally:
            try:
                if sleep_guard is not None and not sleep_guard.release():
                    log("无法解除本进程的防睡眠请求；脚本退出后 Windows 会自动清理。")
            finally:
                lock.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.check:
        configure_file_logging()
    stop = threading.Event()
    reader: threading.Thread | None = None
    if not args.check:
        reader = threading.Thread(target=console_stop_reader, args=(stop,), daemon=True)
        reader.start()
    try:
        return run(args, stop)
    except KeyboardInterrupt:
        stop.set()
        log("已由 Ctrl+C 停止自动领取。桌宠继续运行。")
        return 130
    except (AutoChestError, PolicyError, ValueError) as error:
        log(f"自动领取已停止：{error}")
        return 2
    except (CdpError, OSError, subprocess.SubprocessError) as error:
        log(f"自动领取已停止：{error}")
        return 1
    finally:
        stop.set()
        if reader is not None:
            reader.join(timeout=0.3)


if __name__ == "__main__":
    raise SystemExit(main())
