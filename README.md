# WTHelper

Windows 版 Wuwa Tappo 的第三方桌宠助手，提供自动领取宝箱、敲击计数和本地计数管理功能。项目不包含 Wuwa Tappo 本体。

> 当前版本：**0.1.0**。桌宠页面和服务器接口可能随版本更新而变化；本项目基于国服客户端 1.0.1 测试，不保证兼容其他版本或服务器。

## 功能

- **自动领取宝箱**：监测登录状态、敲击余额和宝箱状态，满足条件后调用桌宠原有按钮；两个宝箱依次处理。
- **敲击计数**：支持键盘、鼠标或组合模式，可设置轮数、速率和启动延迟。
- **本地数据管理**：查看、预览和设置本地计数，支持写入前备份及从备份恢复。
- **图形界面与命令行**：图形界面集中显示任务状态和日志，也保留独立的命令行工具。
- **预览模式**：自动领取和敲击计数均可先检查或监测，而不执行领取或发送输入。

自动领取不会移动鼠标。鼠标敲击模式会在当前光标位置产生**真实左键点击**，启动前应将光标移至安全的空白区域；键盘模式默认发送 F24，可能触发其他软件绑定的快捷键。

## 运行环境

- Windows，已安装 Wuwa Tappo 国服桌宠。
- 从源码运行需要 Python 3.10 或更高版本，并包含 Tkinter。运行本工具不需要安装额外的 Python 包。
- 自动领取和敲击计数应以管理员权限运行，以匹配桌宠的权限。启动时会出现 Windows 权限确认。

## 下载与安装

从 [Releases](https://github.com/MAK1MAAaa/WTHelper/releases) 下载 Windows x64 发布包，解压到桌宠安装目录下的 `WTHelper` 文件夹，使路径呈现如下结构：

```text
WuwaTappo/
├── WuwaTappo.exe
└── WTHelper/
    └── WTHelper.exe
```

从系统托盘完全退出桌宠，运行 `WTHelper.exe`，再从界面启动需要的功能。发布包内包含独立 EXE，不需要安装 Python；`WTHelper` 目录需要可写，以保存日志、备份及策略恢复记录。运行自动领取和敲击任务需要管理员权限，界面提供提权入口。

发布文件未经过代码签名，Windows 可能显示未知发布者提示。请仅从本项目的 Releases 页面下载，并核对发布页提供的 SHA-256 值。

## 从源码运行

将本仓库放在桌宠安装目录内，确保 `WTHelper` 与 `WuwaTappo.exe` 位于以下位置：

```text
WuwaTappo/
├── WuwaTappo.exe
└── WTHelper/
    ├── WTHelper.cmd
    ├── auto_chest.cmd
    ├── tap_boost.cmd
    ├── scripts/
    └── tests/
```

例如，在桌宠安装目录中克隆仓库：

```powershell
git clone https://github.com/MAK1MAAaa/WTHelper.git WTHelper
```

1. 从系统托盘完全退出桌宠，以便自动领取功能重新启动并连接桌宠。
2. 双击 `WTHelper.cmd`，按提示授予管理员权限。
3. 在「运行控制」中选择预览模式或启动需要的功能。连接正常时，运行记录会显示「已连接桌宠主页面」。
4. 关闭助手窗口时，工具会停止由该窗口启动的任务，并等待清理完成。

也可以在 `WTHelper` 目录运行 `py .\scripts\gui.py`。如果普通权限启动，界面左侧提供管理员启动入口。单独通过 CMD 启动的任务需要在对应窗口中停止。

## 自动领取说明

默认每 2 秒检查一次；宝箱可领取且可用敲击数不少于 1,000 时，等待 10 秒后尝试领取。脚本处理服务器冷却错误时会逐步退避，连接异常时记录状态，监督进程也会在限定次数内重启异常退出的领取进程。

自动领取默认请求 Windows 阻止系统自动睡眠，屏幕仍可按系统设置关闭。停止任务后释放该请求；手动睡眠、关机或断电仍会中断任务。界面中的「允许设备自动睡眠」可恢复系统原有行为。

宝箱资格和冷却受服务器约束；本工具不会可靠地跳过或加速服务器的 30 分钟条件。桌宠隐藏时暂停领取，可使用桌宠的 F9 快捷键恢复显示。

### 命令行

在 `WTHelper` 文件夹内打开 PowerShell：

```powershell
# 检查环境，不启动桌宠或领取宝箱
py .\scripts\auto_chest_supervisor.py --check

# 监测宝箱，但不领取
py .\scripts\auto_chest_supervisor.py --dry-run

# 保持系统原有的自动睡眠行为
py .\scripts\auto_chest_supervisor.py --allow-sleep
```

也可右键 `auto_chest.cmd`，选择「以管理员身份运行」。保持窗口打开，按 **0** 或 **Ctrl+C** 停止；更新代码后需重启旧任务才能加载新版。

### 连接和权限

自动领取通过 WebView2 的本地调试连接调用桌宠页面按钮，默认地址为 `127.0.0.1:19227`，无需复制账号凭据。管理员权限下，工具会为桌宠临时设置 Windows 的 WebView2 策略项，并在正常停止或启动失败时恢复原值。恢复记录保存在 `backups/`；恢复前会检查策略是否被其他程序修改，避免覆盖外部变更。

若任务曾被强制结束、需要单独恢复策略，可在管理员终端中运行：

```powershell
py .\scripts\auto_chest.py --restore-debug-policy
```

相关文档：[Microsoft WebView2 权限说明](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security#recommended-privilege-level-for-webview2-host-applications)、[WebView2 调试文档](https://learn.microsoft.com/en-us/microsoft-edge/webview2/how-to/debug-visual-studio-code)。

## 敲击计数

先启动桌宠，再从图形界面配置任务；也可双击 `tap_boost.cmd`，默认倒计时 5 秒后发送 10,000 轮 F24 按下和释放。按住 **Esc** 或在命令行按 **Ctrl+C** 可停止。

```powershell
# 只检查参数，不发送输入
py .\scripts\tap_boost.py --count 10000 --rate 200 --dry-run

# 每轮发送一次键盘敲击和一次鼠标左键点击
py .\scripts\tap_boost.py --mode both --count 10000 --rate 100
```

命令行中 `--count` 为轮数，`--rate` 为目标轮数/秒，`--delay` 为启动延迟。工具显示的是 Windows 接受的输入事件，桌宠记录和服务器接收情况以实际结果为准。

## 本地计数与备份

图形界面的「本地数据」支持查看、预览设置、备份写入、预览恢复和备份恢复。设置的是**目标值**，而不是在原值上增加。待上报增量与累计快照含义不同，同时设置可能使合并后的总数超出预期。

实际写入前，请先停止助手内的自动领取和敲击任务，并从托盘完全退出桌宠。工具会先备份原文件；恢复时会校验清单和文件哈希。修改本地文件不代表服务器一定接受修改后的计数，也不会改变宝箱冷却。

```powershell
# 查看本地状态
py .\scripts\save_tool.py status

# 预览设置；确认桌宠已退出后使用 --apply 实际写入
py .\scripts\save_tool.py set --pending 10000
py .\scripts\save_tool.py set --pending 10000 --apply

# 将路径替换为实际备份目录；不加 --apply 时仅预览
py .\scripts\save_tool.py restore ".\backups\备份目录"
py .\scripts\save_tool.py restore ".\backups\备份目录" --apply
```

## 常见问题

### 显示运行中，但没有领取

请确认运行记录显示「已连接桌宠主页面」，桌宠已登录且没有隐藏，并检查余额和宝箱状态。可以运行 `--check` 查看状态，或者查看 `logs/auto_chest.log` 与 `logs/auto_chest_supervisor.log`。如果已有桌宠进程未启用本地连接，请从托盘完全退出桌宠，再启动自动领取。

### 桌宠无法点击或拖动

检查桌宠的游戏模式和「禁用拖动」设置：F4 可切换游戏模式，「禁用拖动」可在桌宠右键菜单或设置中关闭。F9 只控制桌宠显示或隐藏。不要通过删除整个 WebView 配置目录排查，以免丢失登录状态和其他设置。

## 开发与测试

```powershell
py -m unittest discover -s tests -v
py -m compileall -q scripts
```

在 Windows x64 上构建发布版：

```powershell
py -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.\build_release.ps1
$testProcess = Start-Process -FilePath .\dist\WTHelper.exe -ArgumentList '--self-test' -WindowStyle Hidden -Wait -PassThru
if ($testProcess.ExitCode -ne 0) { throw 'EXE 自检失败' }
```

输出位于 `dist/WTHelper.exe`。构建环境和产物不会纳入 Git；发布包还包含 [第三方许可文本](licenses/)。

测试覆盖任务停止、备份恢复、WebView2 策略恢复、宝箱状态处理和图形界面的输入校验等。部分页面脚本模拟测试需要 Node.js；正常运行工具不需要 Node.js。日志、备份、Python 缓存和历史压缩包不会纳入 Git 提交。

## 免责声明

本项目是独立开发的第三方工具，与 Wuwa Tappo 及其开发者、发行方没有官方关联，也未经其授权或背书。Wuwa Tappo 及相关名称、商标的权利归各自权利人所有。

工具会模拟系统输入、调用桌宠页面功能并在用户操作下修改本地文件；使用前请自行了解相关服务条款和规则，并妥善保存备份。客户端更新、服务器校验、自动输入或本地数据修改可能导致功能失效、数据异常或账号风险。软件按现状提供，使用者自行承担使用风险。

## 许可证

本项目代码采用 [MIT License](LICENSE)；许可证不授予 Wuwa Tappo 本体及第三方素材的使用或再分发权利。
