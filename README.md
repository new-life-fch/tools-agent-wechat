# 学习场景「截图答题 → 微信回传」中继

用户按 `Ctrl+\`` 截图（或直接在微信里给机器人发消息）→ **常驻推送脚本**在有新事件时主动唤醒
Agent（静默 8 秒后）→ Agent 识图解题 → 答案发回微信 → 继续等下一批。

**微信通道用腾讯 iLink Bot API（官方开放协议，hermes-agent 同款）**，纯 HTTPS，天生跨局域网/公网。

```
                  ┌──────────────────── 你的电脑（Agent 所在机器）────────────────────┐
 按 Ctrl+` ──────►│ screenpeer.py ──► shots/*.png ─┐                                 │
 (任意前台程序)   │   (start_capture.py 启动)      │                                 │
                  │                                ├─► wait_events.py --push（常驻）│
 WeChat ─────────►│ wechat_gateway.py serve ──► wechat_inbox.jsonl ─┘                │
 (手机)      ▲    │   (长轮询 iLink)                │  有新事件 → 静默 8s → POST 推送  │
             │    │                                ▼                                │
             └────┼── sendmessage ◄── wechat_gateway.py ◄── Agent 识图/解题/验证      │
                  │                                          ▲                      │
                  │            relay-wake 插件：Agent.followup() ── 主动开新 turn ──┘
                  └─────────────────────────────────────────────────────────────────┘
```

**为什么是「唤醒」而不是「阻塞等待」**：阻塞脚本打完事件就自己退出，靠 DSH 的 job 结算通知去叫醒
Agent，而那条通知受 `tool-jobs.maxConsecutiveWakes` 预算限制（默认连续 3 次），用完就只进收件箱不唤醒
——实测出现过 **47 分 54 秒空窗**。推送模式把这件事交给一个 host 插件直接调 `Agent.followup()`，
不受该预算约束。

## 安装（macOS / Windows）

要求 **Python 3.10+**（开发环境 3.14）。依赖都是纯 Python 包或官方 wheel，无需编译工具链。

### macOS

```bash
# 1) 装 Python（已装可跳过）：Homebrew 或 python.org 安装包都行
brew install python@3.12

# 2) 建独立虚拟环境 + 装依赖
cd one-person-agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install pillow pygments cryptography "qrcode[pil]" mss pynput pyobjc-framework-Quartz

# 3) 授权（只需一次）：系统设置 → 隐私与安全性 →
#    · 屏幕录制（截屏用）
#    · 辅助功能（Ctrl+` 全局热键拦截用）
#    勾选你运行脚本的终端（Terminal / iTerm），然后**重启终端**

# 4) 首次启动
.venv/bin/python wechat_gateway.py login --new    # 扫码登录（生成你自己的机器人）
.venv/bin/python wechat_gateway.py serve           # 常驻：微信收发 + HTTP API
.venv/bin/python start_capture.py                  # 常驻：Ctrl+` 截图落进 shots/
.venv/bin/python wait_events.py --push             # 常驻：有新事件就唤醒 Agent
```

> 本工作区里用的是共享环境 `/Users/fch/project/dp-workspace/.venv/bin/python`，把上面的 `.venv/bin/python` 换成它即可。
> 发给别人时建议让对方按上面步骤建**他自己的** `.venv`（别把 `.venv` 一起打包）。

### Windows

```bat
:: 1) 装 Python 3.10+：python.org 安装包，安装时务必勾选 "Add python.exe to PATH"
::    pyobjc-framework-Quartz 是 macOS 专用，Windows 不要装

:: 2) 建虚拟环境 + 装依赖
cd one-person-agent
py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install pillow pygments cryptography qrcode[pil] mss pynput

:: 3) 无需任何系统授权：热键走 Win32 低级键盘钩子（WH_KEYBOARD_LL），截屏不需要权限

:: 4) 首次启动
.venv\Scripts\python wechat_gateway.py login --new
.venv\Scripts\python wechat_gateway.py serve
.venv\Scripts\python start_capture.py
.venv\Scripts\python wait_events.py --push
```

Windows 备注：
- 控制台若出现中文乱码，先 `chcp 65001` 或 `set PYTHONUTF8=1`；脚本已做 `errors="replace"` 兜底，**不会因编码问题崩溃**。
- PowerShell 里路径用 `.venv\Scripts\python.exe`；`&&` 连接命令请改用 `;` 或分次执行。
- 端口占用识别走 `netstat -ano` + `tasklist`（macOS/Linux 走 `lsof`）。
- **日志看起来「卡住不打印、按一下 Enter 才刷出来」**：这是 CMD 窗口的**快速编辑模式**（鼠标点进/划选窗口就冻结输出），
  不是脚本缓冲问题（本仓库脚本每行都 `flush`）。处理：用 **Windows Terminal** 跑，或右键标题栏 →「属性/默认值」→
  选项 → 取消勾选「快速编辑模式」；**Ctrl+C 停不掉进程**也是同一个原因（选择状态下 Ctrl+C 被控制台拿去做复制），
  先按一下 Enter 取消选择再 Ctrl+C，或 `taskkill /F /PID <pid>`。
- **推送模式需要 DSH 装在 Windows 这台机器上**，并装 `relay-wake` 插件（见下一节，Windows 路径用 `C:/...` 正斜杠）。
  中继脚本侧是纯 Python 标准库，装好插件 + token 对上即可；只监听回环、不新开端口，不会触发防火墙弹窗。

## 推送通道（relay-wake 插件）

`wait_events.py --push` 会把新事件 POST 到 DSH 里的 host 插件，由插件调 `Agent.followup()` 开一个新 turn。
插件在 `dsh-plugin-relay-wake/`，两种装法（**macOS / Windows 通用，二选一**，详见该目录的 README）：

- **免安装热载**（本机自用最省事，不用重启）：在 `~/.dsh/profiles/web/cordis.patch.yml` 里 insert 一行，
  `name` 写 `dsh-plugin-relay-wake/index.js` 的绝对路径（Windows 用 `C:/...` 正斜杠）。保存即生效，回滚就删掉这一段。
- **官方 bundle**（可分发到别的机器）：在 DSH 检出目录里
  `pnpm dsh plugin --profile web add <dsh-plugin-relay-wake 的路径>`，然后重启一次 `dsh web`
  （重启会杀掉后台任务，起来后重新拉起截图端/网关/推送脚本）。卸载 `remove` 同理。

两种方式**只能二选一**（同一个行 id）；token 必须与 `wait_events.json` 的 `push.token` 一致，
生成：`python -c "import secrets;print(secrets.token_hex(16))"`。

## 启动流程（三个常驻）

```bash
P=/Users/fch/project/dp-workspace/.venv/bin/python; cd /Users/fch/project/dp-workspace/one-person-agent

# ① 一次性：微信扫码登录（只在首次 / 换号时做；需你本人用手机扫）
$P wechat_gateway.py login --new

# ② 常驻 A：微信网关 —— 长轮询收微信消息 + 本地 HTTP API（127.0.0.1:8799）
$P wechat_gateway.py serve

# ③ 常驻 B：截图端 —— 按 Ctrl+` 截图落进 shots/（端口/目录读 wait_events.json 的 capture 段）
$P start_capture.py
$P start_capture.py --print-cmd     # 先确认端口/目录，不启动

# ④ 常驻 C：推送脚本 —— 有新事件静默 8 秒后主动唤醒 Agent（在 DSH 里由 Agent 以后台任务起）
$P wait_events.py --push
```

**顺序无所谓**（三个进程互不依赖），但要「先全起来，再让用户按热键」。各步的存活验证：

| 步骤 | 验证 |
|---|---|
| ① | `$P wechat_gateway.py doctor` → 凭据 ✓ / iLink 可达 ✓ |
| ② | `curl -s http://127.0.0.1:8799/health` → `"ok": true`，收到消息时 `inbox.count` 增长 |
| ③ | 日志出现「接收保存目录 …」「[热键] Ctrl+` 已启用」；`lsof -nP -iTCP:9119 -sTCP:LISTEN` 有输出 |
| ④ | 横幅出现 `模式: 常驻推送` + `推送: http://127.0.0.1:3080/relay/wake → 会话 session-…` + `静默去抖 8.0s`；`curl -s http://127.0.0.1:3080/relay/health` → `"ok":true` |

**推送模式的语义**（别按「轮询」理解）：事件先攒着，**最后一个事件之后安静 N 秒**（默认 8s，`--debounce` 可调）
才推一次，一批一起送；期间持续有新事件就一直不推；没有新事件就什么都不做。推送成功才推进账本，
失败保留待重试（最多重复投递，**绝不丢**）；连续失败 6 次会主动退出（退出码 5），让 job 结算通知兜底叫醒 Agent。

### 谁读哪份配置 / 谁拉起谁

```
start_capture.py ──读 wait_events.json 的 capture 段──► subprocess.Popen([
                                                          python, screenpeer.py,
                                                          self→127.0.0.1,
                                                          --port 9119,
                                                          --save-dir shots,
                                                          --cooldown 1.5 ])
wait_events.py --push ──读 wait_events.json 全部──► 盯 shots/ + wechat_inbox.jsonl → POST /relay/wake
wechat_gateway.py ─────读 wechat_gateway.json（独立）──► iLink Bot API + HTTP :8799
```

| 配置文件 | 谁读 | 管什么 |
|---|---|---|
| `wait_events.json` | `wait_events.py` **和** `start_capture.py` | 截图目录、`capture` 段（端口/对端/节流/auto_port）、轮询间隔、`push` 段（url/token/session/去抖）、状态文件、微信源路径 |
| `wechat_gateway.json` | `wechat_gateway.py` | token、默认收件人、HTTP 端口、发送/接收行为（含 `inbox.dedup_ttl_sec`） |

即：**截图这一侧只配 `wait_events.json`，微信那一侧只配 `wechat_gateway.json`**。
`screenpeer.py` 是工具本体（未被修改），只由 `start_capture.py` 拉起；
也可以绕过启动器直接跑：`$P screenpeer.py 127.0.0.1 --port 9119 --save-dir shots`。

## VSCode 底框（答题板，可选）

除了发微信，Agent 还会把同一份答案**顺手铺到编辑器底部**——你在 Qoder / Cursor / Trae / VSCode 里
刷题时不用掏手机，长文本可以自由上下滚动。

```
Agent ──► vscode_notify.py ──► HTTP 127.0.0.1:<port>/push ──► 扩展 ──► 底部面板「答题板」
```

装一次就够（扩展源码在 `vscode-answer-panel/`）：

```bash
cd vscode-answer-panel
./scripts/build.sh          # 跑离线测试 + 打 .vsix
./scripts/install.sh        # 自动侧载进本机所有 VSCode 系编辑器
# 然后在编辑器里 Reload Window
```

- **它不是常驻进程**，不用你启动：编辑器激活扩展后自己监听，端口与令牌写在
  `~/.answer-panel/bridge.json`，`vscode_notify.py` 自动去读。
- **推不上不影响主流程**：编辑器没开 / 扩展没装时退出码 `2`，静默跳过，微信照发。
- **只发文本**：保留缩进、空行与代码块边界，不做语法着色；算法题的代码图仍然只服务微信。
- **适配分支编辑器**：只用稳定 API、零运行时依赖、`engines.vscode` 压到 `^1.85.0`，
  因此 Qoder / Cursor / Trae / Windsurf / VSCodium 都能装。
- 面板里可以上下滚动、跟随最新、单条复制、全部复制、清空；命令面板搜「答题板」也有同名命令。

## 文件清单

| 文件 | 作用 |
|---|---|
| `wait_events.py` | **常驻推送脚本**：每 2s 扫一次截图目录 + 微信收件箱，攒够一批且静默 8s 后 POST 给 relay-wake 插件唤醒 Agent；账本保证「投递过的不再投递」，推送失败重试不丢 |
| `wait_events.json` | 配置：监听源 / 截图目录 / 轮询间隔 / 状态文件，**`push` 段（url、token、会话、去抖秒数）**，以及**截图端 `capture` 段（端口、对端、节流、auto_port）** |
| `dsh-plugin-relay-wake/` | **唤醒插件**（host 平面、零依赖纯 JS）：`GET /relay/health`、`POST /relay/wake` → `Agent.followup()`；含双平台安装说明 |
| `push_probe.py` | 推送模式时序自测（本地桩验证去抖/失败重试/常驻，跨平台，约 30 秒） |
| `wechat_gateway.py` | **微信服务**：`login` `send` `serve` `inbox` `chats` `typing` `doctor` `logout`，serve 模式还提供本地 HTTP API |
| `wechat_gateway.json` | 微信配置（token、默认收件人、HTTP 端口、去重时间窗等；token 由 login 自动回填） |
| `ilink_client.py` | iLink Bot API 协议实现（登录/收发/媒体 AES-128-ECB 加密上传/长轮询/切片） |
| `code_image.py` | **代码转图片**：pygments 着色 + Pillow 绘制，中文注释不乱码，深/浅主题、行号、高亮、自适应宽度 |
| `start_capture.py` | 截图端启动器：端口/对端/目录全部读 `wait_events.json` 的 `capture` 段，命令行可覆盖 |
| `screenpeer.py` | 原截图工具（全局热键 `Ctrl+\``，事件在系统层被吃掉，前台程序收不到这个按键） |
| `vscode_notify.py` | **推送到编辑器底框**：`push` `clear` `status` `doctor`，只用标准库、2 秒超时、永不阻塞（没面板就退出码 2） |
| `vscode-answer-panel/` | **底框扩展源码**（纯 JS、零运行时依赖、约 18 KB vsix）+ `scripts/build.sh` / `scripts/install.sh` + 31 项离线测试 |
| `selftest.py` | 离线自测（106 项：不重复投递/双源/重放/去重时间窗/加密/切片/代码图/端口配置/独占登记/底框扩展） |
| `SKILL.md` | **给 Agent 的编排流程与注意事项**（同一份也装在项目级技能目录 `<工作区>/.agents/skills/study-wechat-relay/`，DSH 自动加载；**不装用户级**） |

## 关键行为（都是刻意的）

- **主动唤醒，不是阻塞轮询**：脚本常驻、新事件静默 8 秒后唤醒 Agent，没有事件就什么都不做；
  它不受 `tool-jobs.maxConsecutiveWakes`（默认 3 次）预算限制——那条限制是阻塞模式的死因。
- **看过的不再看**：状态账本 `.state/wait_events_state.json` 记录所有投递过的名字，
  跨进程/跨重启/跨会话都生效 → 同一张图不会二次污染 Agent 上下文。
- **被中断也不丢事件**：**推送成功**才算「确认送达」；被 kill 掉的那一轮，下次运行以 `REPLAY` 补投，
  推送失败的批次留在账本里重试（最多重复投递，绝不丢）。
- **微信消息只显示用户说的话**：`WX 帮我看看这题`（带附件时补本地路径），不带时间/ID 等噪音；调试用 `--wechat-meta`。
- **网关去重带时间窗**：只挡「同一条消息被服务端重投」（`inbox.dedup_ttl_sec`，默认 10 秒）。
  过了窗口你再发同样一句话照样收得到——旧版无时间上限，把重复发的「测试」静默吞掉了（已修）。
- **冷启动**：首次运行时目录里的历史截图静默忽略；微信历史消息回溯 5 分钟（可配），免得漏掉你刚发的问题。
- **三级降级**：算法题发「代码图 → 源码文件 → 代码片段」，前一级失败才降级。
- **底框是附送的**：答案先铺到编辑器底部（本地推送，2 秒超时），再发微信。编辑器没开就静默跳过，不重试、不阻塞、也不影响微信那条主通道。

## 常用命令

```bash
P=/Users/fch/project/dp-workspace/.venv/bin/python; cd /Users/fch/project/dp-workspace/one-person-agent

$P wait_events.py --push        # 常驻推送（唯一跑法；后台起一次，之后别管它）
$P wait_events.py --push --debounce 15   # 改静默去抖时长
$P wait_events.py --status      # 看账本 + 推送计数
$P wait_events.py --once        # 只扫一遍（调试；--push 下扫完就推、推完即退）
$P push_probe.py                # 推送模式时序自测（本地桩，约 30 秒）
$P wechat_gateway.py send --text "第 3 题（单选）｜答案：B"       # 发文本
$P wechat_gateway.py send --text "思路…" --image code.png --file solution.py
$P wechat_gateway.py inbox --since 0        # 看收到的消息
$P wechat_gateway.py chats                  # 已知会话（拿 chat_id 用 --to 指定）
$P code_image.py --in a.py --out a.png --lang python --title "T1"
$P vscode_notify.py push -t "第3题" -f answer.md   # 顺手铺到编辑器底框
$P selftest.py                              # 离线自测

curl -s http://127.0.0.1:3080/relay/health  # 推送通道：唤醒计数 / 活着的会话列表
curl -s http://127.0.0.1:8799/health        # 网关状态
curl -s -X POST http://127.0.0.1:8799/send -H 'Content-Type: application/json' \
     -d '{"text":"hello","images":["/abs/code.png"]}'                # HTTP 方式发送
```

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `send` 报 `prepare failed` / `ret=-2` | 与收件人的会话还没建立：**让接收方先在微信里给机器人发一条消息**，之后就能主动推 |
| 在微信里连发同样的话，只有第一条有回应 | 旧版去重没有时间上限（同一网关进程内重复文本被静默吞掉）——**已修**：`inbox.dedup_ttl_sec` 默认 10 秒 |
| 起了 `--push` 但一直没被唤醒 | **正常**：没有新事件它就不推。别重启、别用 `--once` 轮询试探；确认截图端和网关在跑、`/relay/health` 通 |
| 日志出现「推送失败（…）：N 条事件保留…」 | 不用管，5 秒后自动重试。连续失败才看：`404 session-not-live` = 目标会话的 agent 不在了（在 GUI 里打开该会话）；`401 bad-token` = token 与插件配置不一致 |
| 推送脚本的 job 变成 `completed` | 脚本崩了或主动退出（连续失败 6 次）。看它的输出定位原因，再起一次 |
| 端口 9119 被占用 | 本机已有另一个 screenpeer 实例（`--print-cmd` 会打印占用者 PID）。默认自动避让并提示；要固定端口就把 `wait_events.json` 的 `capture.auto_port` 设 `false`（直接报错，不偷偷换）。局域网互传模式永不自动换端口 |
| 热键按了没反应 | macOS 需给运行终端授权「屏幕录制 + 辅助功能」，授权后重启终端；Windows 无需授权 |
| 收不到微信消息 | iLink 的 bot 是**独立身份**（`xxx@im.bot`），普通微信群基本收不到事件、也进不了群；**私聊可靠** |
| token 失效 | `$P wechat_gateway.py login --force` 重新扫码（必须你本人扫）|
| 图片在微信里是灰块 | `aes_key` 必须是 base64(hex 字符串)，见 `ilink_client.py` 的注释，别改坏 |

## Windows 适配

代码层面已做的适配：

| 位置 | 处理 |
|---|---|
| 路径 | 全用 `os.path` / 绝对路径可配；配置文件支持相对路径；插件侧用正斜杠 `C:/...` |
| 热键 | `screenpeer.py` 走 `WH_KEYBOARD_LL` 钩子，无需授权（该脚本未改动） |
| 端口探测 | 改成「先 connect 再 bind」——Windows 的 `SO_REUSEADDR` 允许重复绑定，纯 bind 探测会误判空闲 |
| 端口占用者 | `netstat -ano` + `tasklist`（macOS/Linux 用 `lsof`），拿不到就静默降级 |
| 控制台编码 | 脚本启动时把 stdout/stderr 的 `errors` 放宽为 `replace`：GBK 控制台打印 ✓/⚠ 不会崩（中文照常，个别符号变 `?`） |
| 控制台假死 | CMD 快速编辑模式会冻结输出、吃掉 Ctrl+C——见上面 Windows 备注 |
| 状态文件 | `os.replace` 原子替换（插件侧用唯一临时名 + `rename`），被 kill 不会写坏账本 |
| 字体 | `code_image.py` 找 Consolas / 微软雅黑（CJK），找不到才回退 |
| 信号 | `SIGTERM/SIGINT` 注册容错，Windows 下同样能干净退出 |
| 进程 | `Popen(stdin=PIPE)` 保活、`terminate()` 停止，均为跨平台 API |
| 推送通道 | 插件只用 `node:crypto/fs/path/url`，行为与 macOS 一致；中继脚本用 urllib，纯标准库 |

## 依赖

| 包 | 用途 | 谁需要 |
|---|---|---|
| `pillow` | 代码图渲染 | 全部平台 |
| `pygments` | 代码着色 | 全部平台 |
| `cryptography` | 微信媒体 AES-128-ECB 加解密 | 全部平台 |
| `qrcode[pil]` | 终端/PNG 二维码 | 全部平台 |
| `mss` | 截屏 | 全部平台 |
| `pynput` | 非 macOS/Windows 平台的降级热键监听 | 可选 |
| `pyobjc-framework-Quartz` | macOS 全局热键拦截（CGEventTap） | 仅 macOS |

安装命令见上面「安装（macOS / Windows）」。全部为纯 Python 或官方 wheel，不需要编译器。
`dsh-plugin-relay-wake` **零依赖**（不 import 任何 `@deepseek-ai/*`）。

## 安全

- `wechat_gateway.json` 里含 token，落盘权限已自动设为 `600`，**不要提交到版本库**；
- 使用的是腾讯官方 iLink Bot API（扫码授权、可随时在微信里删除该机器人会话解绑），不是第三方逆向协议；
- HTTP API 默认只监听 `127.0.0.1`；要跨机调用请设 `http.api_token` 并把 host 改 `0.0.0.0`；
- 推送通道只挂在本机回环的 web 端口上，额外校验 `Host` 必须是回环 + 可选的 `x-relay-token` 头。
