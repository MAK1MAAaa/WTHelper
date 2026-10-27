# WTHelper

Windows 版 Wuwa Tappo 的第三方桌宠助手，提供自动领取宝箱和敲击计数功能。项目不包含 Wuwa Tappo 本体。

> 当前源码基于 **0.1.0** 开发。以下说明适用于当前源码和本地构建；已发布版本以相应 Release 为准。桌宠页面和服务器接口可能随版本更新而变化；本项目基于国服客户端 1.0.1 测试，不保证兼容其他版本或服务器。

## 功能

- **自动领取宝箱**：监测登录状态、敲击余额和宝箱状态，满足条件后调用桌宠原有按钮；两个宝箱依次处理。
- **敲击计数**：支持键盘、鼠标或组合模式，可设置轮数、速率和启动延迟。
- **图形界面与命令行**：图形界面集中显示任务运行状态，也保留独立的命令行工具。
- **窗口外观**：卡片使用平滑圆角；支持自定义系统标题栏配色的 Windows 版本会将标题栏融入界面，其他版本保留系统默认样式。
- **预览模式**：自动领取和敲击计数均可先检查或监测，而不执行领取或发送输入。

自动领取不会移动鼠标。鼠标敲击模式会在当前光标位置产生**真实左键点击**，启动前应将光标移至安全的空白区域；键盘模式默认发送 F24，可能触发其他软件绑定的快捷键。

## 运行环境

- Windows，已安装 Wuwa Tappo 国服桌宠。
- 从源码运行需要 Python 3.10 或更高版本，并包含 Tkinter。运行本工具不需要安装额外的 Python 包。
- 自动领取和敲击计数应以管理员权限运行，以匹配桌宠的权限。启动时会出现 Windows 权限确认。

## 下载与安装

本地构建的 `WTHelper.exe` 可以放在任意目录，无需与桌宠安装目录相邻。当前源码在开始自动领取时依次检查已保存的选择、运行中的桌宠、Windows 安装记录和旧版目录布局；找不到桌宠或有多个候选时，会提示手动选择 `WuwaTappo.exe`。也可以点击界面中的「选择桌宠」更换路径，手动选择的路径会被记住。

历史发布包可从 [Releases](https://github.com/MAK1MAAaa/WTHelper/releases) 获取；已发布版本可能不包含当前源码的改动。

运行 `WTHelper.exe`，再从界面启动需要的功能。独立 EXE 不需要安装 Python。运行自动领取和敲击任务需要管理员权限，界面提供提权入口；如果已有桌宠进程未启用本地连接，请从系统托盘完全退出桌宠，等待助手重新启动并连接。

软件不在 EXE 同级目录创建日志或备份，也不生成助手日志文件。界面只显示当前任务状态，发生异常时显示错误提示，不保留日志记录。为确保异常退出后仍能恢复临时 WebView2 策略，必要的恢复记录保存在 `%LOCALAPPDATA%\WTHelper`；手动选择的桌宠路径也保存在该用户目录中。

发布文件未经过代码签名，Windows 可能显示未知发布者提示。请仅从本项目的 Releases 页面下载，并核对发布页提供的 SHA-256 值。

## 从源码运行

克隆仓库到任意目录：

```powershell
git clone https://github.com/MAK1MAAaa/WTHelper.git WTHelper
```

1. 双击 `WTHelper.cmd`，按提示授予管理员权限。
2. 选择预览模式或启动需要的功能；必要时点击「选择桌宠」指定 `WuwaTappo.exe`。连接正常时，自动领取卡片显示「已连接」。
3. 关闭助手窗口时，工具会停止由该窗口启动的任务，并等待清理完成。

也可以在 `WTHelper` 目录运行 `py .\scripts\gui.py`。如果普通权限启动，界面提供管理员启动入口。单独通过 CMD 启动的任务需要在对应窗口中停止。

## 自动领取说明

默认每 2 秒检查一次；宝箱可领取且可用敲击数不少于 1,000 时，等待 10 秒后尝试领取。脚本处理服务器冷却错误时会逐步退避，连接异常时记录状态，监督进程也会在限定次数内重启异常退出的领取进程。

自动领取默认请求 Windows 阻止系统自动睡眠，屏幕仍可按系统设置关闭。停止任务后释放该请求；手动睡眠、关机或断电仍会中断任务。界面中的「允许设备自动睡眠」可恢复系统原有行为。

宝箱资格和冷却受服务器约束；本工具不会可靠地跳过或加速服务器的 30 分钟条件。桌宠隐藏时暂停领取，可使用桌宠的 F9 快捷键恢复显示。

### 命令行

在 `WTHelper` 文件夹内打开 PowerShell：

```powershell
# 检查指定桌宠，不启动桌宠或领取宝箱
py .\scripts\auto_chest_supervisor.py --exe 'C:\桌宠安装目录\WuwaTappo.exe' --check

# 监测指定桌宠的宝箱，但不领取
py .\scripts\auto_chest_supervisor.py --exe 'C:\桌宠安装目录\WuwaTappo.exe' --dry-run

# 保持系统原有的自动睡眠行为
py .\scripts\auto_chest_supervisor.py --exe 'C:\桌宠安装目录\WuwaTappo.exe' --allow-sleep
```

也可右键 `auto_chest.cmd`，选择「以管理员身份运行」。保持窗口打开，按 **0** 或 **Ctrl+C** 停止；更新代码后需重启旧任务才能加载新版。

### 连接和权限

自动领取通过 WebView2 的本地调试连接调用桌宠页面按钮，默认地址为 `127.0.0.1:19227`，无需复制账号凭据。管理员权限下，工具会为桌宠临时设置 Windows 的 WebView2 策略项，并在正常停止或启动失败时恢复原值。恢复记录保存在 `%LOCALAPPDATA%\WTHelper\auto_chest_webview_policy.pending.json`；恢复前会检查策略是否被其他程序修改，避免覆盖外部变更。工具只读取桌宠本身的日志来识别冷却错误，不会修改或删除该日志。

若任务曾被强制结束、需要单独恢复策略，可在管理员终端中运行：

```powershell
py .\scripts\auto_chest.py --restore-debug-policy
```

如果检测到旧版目录中存在尚未恢复的策略记录，工具会停止并提示路径，避免覆盖原配置。可从管理员 PowerShell 运行以下命令，将路径替换为提示的实际备份文件：

```powershell
py .\scripts\auto_chest.py --restore-legacy-policy 'C:\旧助手目录\backups\auto_chest_webview_policy.pending.json'
```

使用独立 EXE 时，可在管理员 PowerShell 中运行（将两个路径替换为实际路径）：

```powershell
$helper = 'C:\助手目录\WTHelper.exe'
$legacyBackup = 'C:\旧助手目录\backups\auto_chest_webview_policy.pending.json'
$restore = Start-Process -FilePath $helper -ArgumentList @('--worker', 'chest', '--restore-legacy-policy', ('"' + $legacyBackup + '"')) -WindowStyle Hidden -Wait -PassThru
if ($restore.ExitCode -ne 0) { throw '旧策略恢复失败，请保留原备份并检查权限和文件路径。' }
```

旧版日志或备份不会被自动删除；请确认不再需要后自行处理。

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

## 常见问题

### 显示运行中，但没有领取

请确认自动领取卡片显示「已连接」，桌宠已登录且没有隐藏，并检查余额和宝箱状态。源码运行时可以带 `--exe` 运行 `--check` 查看状态。如果已有桌宠进程未启用本地连接，请从托盘完全退出桌宠，再启动自动领取。

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

测试覆盖任务停止、WebView2 策略恢复、桌宠路径发现、宝箱状态处理和图形界面的输入校验等。部分页面脚本模拟测试需要 Node.js；正常运行工具不需要 Node.js。旧日志、备份、Python 缓存和历史压缩包不会纳入 Git 提交。

## 免责声明

本项目是独立开发的第三方工具，与 Wuwa Tappo 及其开发者、发行方没有官方关联，也未经其授权或背书。Wuwa Tappo 及相关名称、商标的权利归各自权利人所有。

工具会模拟系统输入、调用桌宠页面功能并临时设置 WebView2 策略；使用前请自行了解相关服务条款和规则。客户端更新、服务器校验或自动输入可能导致功能失效、数据异常或账号风险。软件按现状提供，使用者自行承担使用风险。

## 许可证

本项目代码采用 [MIT License](LICENSE)；许可证不授予 Wuwa Tappo 本体及第三方素材的使用或再分发权利。
