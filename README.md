# Wuwa Tappo 计数与自动宝箱工具

适用于 `WTHelper` 上一级目录中的 Windows 版 Wuwa Tappo。当前本机版本为 1.0.1；计数工具最初基于 1.0.0 分析。需要 Python 3.10 或更高版本，只使用标准库，无需安装依赖。

目录结构：

```text
WuwaTappo/
├── WuwaTappo.exe
├── uninstall.exe
└── WTHelper/
    ├── auto_chest.cmd
    ├── tap_boost.cmd
    ├── scripts/
    ├── tests/
    ├── backups/
    ├── logs/
    └── scripts.zip
```

自动领取脚本默认从 `WTHelper` 上一级目录查找 `WuwaTappo.exe`，可通过 `--exe` 指定其他路径。以下 PowerShell 命令均在 `WTHelper` 文件夹内运行。`scripts.zip` 是整理前的历史快照，其中的旧路径不会自动更新；请使用当前文件夹中的脚本。备份、日志、Python 缓存及历史压缩包不纳入 Git 提交。

## 自动领取宝箱：CMD 常驻，输入 0 退出

1. 先从系统托盘**完全退出桌宠**。
2. 右键 `auto_chest.cmd`，选择**以管理员身份运行**。当前安装包要求管理员权限启动。
3. 脚本自动启动桌宠，保持 CMD 窗口打开；如果桌宠未登录，先在桌宠中完成登录。
4. 脚本默认每 2 秒检查一次。宝箱显示可领取且可用敲击数不少于 1,000 时，先等待 10 秒，以避开客户端与服务器的冷却时间偏差，再调用桌宠原有的开箱按钮。左右宝箱串行处理，并根据奖励动画和按钮状态确认结果。
5. 在 CMD 窗口按 **0** 即停止脚本，无需回车；也可按 **Ctrl+C**。桌宠继续运行。

如果已有桌宠进程未启用本地连接，脚本会提示从托盘退出，等待退出后自动启动。它会等待登录、余额和宝箱就绪；桌宠隐藏时暂停领取，可按 F9 恢复显示。脚本只增量检查触发开箱后的桌宠原生日志，识别 `70006` 时按 10、20、40、60 秒逐步退避；未能确认的结果仍使用 60 秒退避。若同时手动开箱，两次请求的错误码可能无法区分。

脚本运行期间会请求 Windows **阻止系统自动睡眠**，屏幕仍按原设置关闭；按 0 停止后释放该请求。手动睡眠、关机或断电仍会中断运行。可使用 `--allow-sleep` 保持系统原有的自动睡眠行为。

启动器包含监督进程：领取子进程意外退出后按 1、2、4、8、16 秒退避重启，10 分钟内最多重启 5 次。监督进程只在 CMD 窗口存在期间工作，直接关闭 CMD 会终止监督；请保持窗口打开，正常结束时按 0。

```powershell
# 只检查环境，不启动桌宠、不领取
py .\scripts\auto_chest_supervisor.py --check

# 预览模式：正常启动/连接桌宠并监测，但不点击宝箱
py .\scripts\auto_chest_supervisor.py --dry-run

# 将空闲检查间隔改为 5 秒
py .\scripts\auto_chest_supervisor.py --interval 5

# 默认端口冲突时改用其他端口；需先完全退出原桌宠
py .\scripts\auto_chest_supervisor.py --port 19228

# 保持系统原有的自动睡眠行为
py .\scripts\auto_chest_supervisor.py --allow-sleep
```

本地连接采用 WebView2 官方支持的调试方式，使用 `127.0.0.1:19227` 并检查监听地址。脚本通过主页面的原有按钮领取，不需要复制账号凭据。

管理员运行的 WebView2 会忽略 `WEBVIEW2_*` 环境变量及 HKCU 覆盖项，因此旧版脚本可能只启动桌宠而无法开启连接。普通权限进程使用环境变量；管理员进程在启动桌宠前，临时设置以下 HKLM 路径中的 **`WuwaTappo.exe` 与 `com.kuro.wuwatappo.cn` 两个命名值**：

```text
SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments
```

临时配置保留到脚本停止，供同一桌宠进程后续创建辅助窗口使用；按 0、Ctrl+C 或启动失败时恢复原值。已有非调试参数会保留，已有其他调试配置则拒绝覆盖。原值记录在 `backups\auto_chest_webview_policy.pending.json`；异常重启时，如桌宠仍在运行且临时配置完整，新的领取子进程会接管该配置，否则先尝试恢复。恢复时会检查当前值是否仍为本脚本写入的值，外部改动不会被覆盖。

相关依据：[Microsoft WebView2 权限说明](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security#recommended-privilege-level-for-webview2-host-applications)、[调试文档](https://learn.microsoft.com/en-us/microsoft-edge/webview2/how-to/debug-visual-studio-code)。

输入 0 会关闭脚本的连接；本次桌宠进程的调试端口会在桌宠完全退出后关闭。领取仍受服务器 30 分钟活跃时间及消耗条件约束。

### 显示“运行中”但没有自动领取

仅有 CMD、Python 或桌宠 PID，不代表领取连接已经建立。正常启动必须看到 **“已连接桌宠主页面”**；`--check` 会显示端口、登录、余额及左右按钮状态。领取进程每 5 分钟记录一次登录、桌宠可见性、余额及宝箱状态，并在连接异常时记录心跳。领取进程和监督进程分别记录到 `logs\auto_chest.log` 与 `logs\auto_chest_supervisor.log`；各文件最多 1 MiB，保留两份轮转备份。

更新脚本后，先在旧 CMD 中按 0，再从托盘完全退出桌宠，重新以管理员身份运行 `auto_chest.cmd`。旧进程不会自动加载修改后的 Python 文件。

若脚本曾被强制结束，需要单独恢复临时配置，可在管理员终端中运行：

```powershell
py .\scripts\auto_chest.py --restore-debug-policy
```

## 快速刷敲击数

先启动桌宠，然后双击 `tap_boost.cmd`，默认倒计时 5 秒后发送 10,000 次 F24 按下和释放，目标速率为每秒 200 次，约需 50 秒。F24 通常不输入字符；若其他软件绑定了 F24，它仍可能触发快捷键。按住 **ESC** 或在终端按 **Ctrl+C** 停止。

也可以在本目录打开 PowerShell，运行：

```powershell
# 只检查参数、Windows 输入结构和进程状态，不发送输入
py .\scripts\tap_boost.py --count 10000 --rate 200 --dry-run

# 发送 10,000 次键盘敲击
py .\scripts\tap_boost.py --count 10000 --rate 200

# 每轮发送一次键盘敲击和一次鼠标左键点击，共 20,000 次按下
py .\scripts\tap_boost.py --mode both --count 10000 --rate 100

# 仅发送鼠标左键点击
py .\scripts\tap_boost.py --mode mouse --count 1000 --rate 100
```

鼠标模式会点击**当前光标位置**，运行前把光标移到可安全点击的空白区域。键盘模式为默认模式。`--count` 是轮数，范围为 1–1,000,000；`--rate` 为目标轮数/秒，范围为 1–1,000；`--delay` 为启动延迟，范围为 0–30 秒。

脚本只在 `WuwaTappo.exe` 运行时发送事件，每秒检查一次进程，退出桌宠后停止。若 Windows 权限隔离阻止输入，让脚本与桌宠使用相同权限运行。脚本显示的是 Windows 接受的事件数，实际计数和服务器接收量以桌宠为准。每次按下都配对释放，避免仅重复 keydown 被桌宠去重。

## 无法互动或拖动

当前应用有两个独立的交互开关，不能只检查其中一个：

- `gameMode`：游戏模式，角色鼠标穿透。按 **F4** 切换；关闭后角色才可正常接收鼠标互动。
- `windowPassthrough`：实际控制“禁用拖动”。需要在应用设置或桌宠右键菜单中关闭“禁用拖动”。

如果角色还在显示，但无法点击也无法拖动，先按一次 **F4** 关闭当前开启的游戏模式，再在桌宠右键菜单或设置中关闭“禁用拖动”，无需重启。游戏模式的内置说明明确表示：没有箱子需要开启时禁用与桌宠的交互。**F9** 控制显示/隐藏桌宠，与互动开关不同。主窗口采用全屏透明窗口配合命中区域，`WS_EX_TRANSPARENT` 本身是正常实现，不要直接清除此标志，否则可能拦截整个桌面的鼠标输入。

这些开关保存在 WebView 的 `wuwatappo-settings` 配置中。计数工具不直接修改该配置；排查时也不应删除整个 WebView 配置目录，以免丢失登录状态及其他设置。

## 查看、修改及恢复本地计数

`save_tool.py` 支持当前国服生产环境的两个计数文件，默认目录为 `%APPDATA%\com.kuro.wuwatappo.cn`：

| 文件 | 字段 | 含义 |
| --- | --- | --- |
| `key_press_count_cn-cn-prod.dat` | `t` | 尚未上报的本地敲击增量，包含键盘及鼠标计数 |
| `cumulative_click_cn-cn-prod.dat` | `count` | 当前账号累计总数的本地快照，未扣除开箱等消耗 |

文件内容是重复 XOR 编码的 JSON。工具保留账号关联字段及其他原始字段，状态输出不显示用户标识。

```powershell
# 只读查看
py .\scripts\save_tool.py status

# 预览：将待上报增量设置为 10,000；不是增加 10,000
py .\scripts\save_tool.py set --pending 10000

# 从托盘完全退出桌宠后，备份并实际写入
py .\scripts\save_tool.py set --pending 10000 --apply

# 如需单独调整累计快照，先预览
py .\scripts\save_tool.py set --total 10000

# 将下方路径替换成写入时输出的实际备份目录
py .\scripts\save_tool.py restore ".\backups\实际备份目录"
py .\scripts\save_tool.py restore ".\backups\实际备份目录" --apply
```

`set` 和 `restore` 默认仅预览。使用 `--apply` 时会检查桌宠已退出，将原始文件备份到本目录 `backups` 下的独立目录，再以原子替换方式写入；写入失败会尝试回滚。恢复时校验备份清单和 SHA-256，并先备份当前文件。不要在桌宠运行期间改存档，应用每 5 秒保存、每 30 秒尝试上报计数，可能覆盖修改。

启动时，客户端会结合服务器累计值、本地累计快照和待上报增量取较大值恢复，因此**不要将两个字段同时设置成同一个数字并将其当作最终精确总数**。修改存档仅改变本地状态；开箱接口校验累计点击数，并定义了 `70003`“客户端点击数异常”。本地值被接受、保存或显示，不代表服务器认可对应奖励。优先使用模拟输入方式；其服务器接收结果同样需要在应用中确认。

## 宝箱 30 分钟限制的分析结果

当前版本的宝箱资格存在服务端校验，未发现可通过本地存档可靠跳过冷却的方法。

- 客户端用服务器用户资料中的 `left_treasure_chest`、`right_treasure_chest` 时间戳减去 `Date.now()`，计算显示的剩余时间。
- 开箱请求发送到 `api/frontend/chests/open`，只提交 `place` 和 `click_num`；奖励及 `left_next_time`、`right_next_time` 由响应返回。
- 服务端错误码包含 `70004`“暂无可开启的宝箱”、`70005`“剩余点击数不足”和 `70006`“宝箱冷却中”。
- 内置说明为累计 30 分钟活跃时间生成一箱，开箱消耗 1,000 字符，最多储存两箱；满槽后暂停计时。

因此，快速刷数可以补足计数条件，不能据此压缩服务器的 30 分钟条件。改系统时间只会影响客户端倒计时，不能保证提前开箱；修改本地计数文件也不会修改服务器时间戳。

以上结论来自对本机可执行文件及其嵌入 JavaScript 的只读检查，未向服务器发送探测请求。程序使用 Rust/Tauri 2.10.3 与 rdev 全局键鼠钩子；已检查的原生事件转换路径未发现过滤模拟输入标志的逻辑。

最初冷却分析对应的 1.0.0 版 `WuwaTappo.exe` SHA-256：

```text
9d9c0c73dd32ad1a80756f277582aa7868177033b8a7a880e083299ed9e477dd
```

自动领取脚本的页面结构核对基于本机 1.0.1 版本，SHA-256：

```text
dd340c92a7320d1ecf3697ba65b4ad23d8f196acb547d2baf3f75ba09d696d97
```

## 验证

```powershell
py -m unittest discover -s tests -v
py -m py_compile .\scripts\tap_boost.py .\scripts\save_tool.py .\scripts\auto_chest.py .\scripts\auto_chest_supervisor.py .\scripts\cdp_client.py .\scripts\chest_log.py .\scripts\webview_policy.py
```

离线测试覆盖计数备份恢复、输入停止与释放、本地 WebSocket 协议、领取串行处理、登录/余额/隐藏状态保护、失败退避和 DOM 状态复核。页面脚本的模拟 DOM 测试使用 Node.js，仅开发验证需要，实际运行工具不需要 Node.js。

已通过 97 项离线测试、Python 编译检查、自动领取的只读诊断，并在真实终端验证旧版预览常驻模式按 0 正常退出。测试覆盖服务器冷却码读取、就绪缓冲、心跳、防睡眠请求、监督重启、管理员启动策略恢复与外部变更保护。注册表测试全部使用模拟后端；防自动睡眠请求已在本机验证能够设置并释放。

2026-10-27 修复后，实际运行日志确认：03:25:04 左侧领取成功，03:25:09 右侧领取成功，共两枚，随后进入等待下一轮状态。只读复核确认本地连接已建立、已登录，领取后的左右按钮均不可点击。

同日夜间诊断：03:25 后到 09:22 的旧脚本日志没有心跳，无法确定其何时停止或具体停止原因；桌宠在 08:01 曾网络断开后重连，系统日志没有确认的睡眠或关机记录。上午五次领取失败与桌宠原生日志的 `70006` 一一对应，均出现在该侧上次成功约 29 分 59 秒后。新版增加就绪缓冲、错误码记录与渐进重试。11:54:11 宝箱就绪后，脚本等待 10 秒，11:54:22 首次尝试即成功领取右侧宝箱；夜间连续运行效果仍需观察。
