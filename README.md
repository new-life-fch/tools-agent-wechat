# 学习场景「截图答题 → 微信回传」中继

用户在前台按 `Ctrl+\`` 截图 → 脚本阻塞等待 → Agent 识图解题 → 答案发回微信 → 再阻塞等待。
**微信通道用腾讯 iLink Bot API（官方开放协议，hermes-agent 同款）**，纯 HTTPS，天生跨局域网/公网。

```
                    ┌─────────────────────── 你的电脑（Agent 所在机器）───────────────────────┐
   按 Ctrl+` ──────►│ screenpeer.py ──► shots/*.png ─┐                                    │
   (任意前台程序)    │   (start_capture.py 启动)      │                                    │
                    │                                ├─► wait_events.py ◄── 阻塞等待      │
   WeChat ─────────►│ wechat_gateway.py serve ──► wechat_inbox.jsonl ─┘                  │
   (手机)      ▲    │   (长轮询 iLink)                │              │                     │
               │    │                                ▼              ▼                     │
               └────┼── sendmessage ◄── wechat_gateway.py ◄── Agent 识图/解题/验证         │
                    └────────────────────────────────────────────────────────────────────┘
```

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
.venv/bin/python wait_events.py                    # 阻塞等待（Agent 用）
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
- **推送模式（`--push`）需要 DSH 装在 Windows 这台机器上**，并装 `relay-wake` 插件（两种装法见
  `dsh-plugin-relay-wake/README.md`，Windows 路径用 `C:/...` 正斜杠）。中继脚本侧是纯 Python 标准库，
  装好插件 + token 对上即可；只监听回环、不新开端口，不会触发防火墙弹窗。

## 把工具发给别人（对方要扫自己的码）

**关键**：iLink 的机器人身份是**跟微信号绑定**的，别人必须扫出一张属于他自己的二维码。
而 `wechat_gateway.json` 里存着 **token**——整包直接发出去，等于把自己的机器人身份一起送人了。

```bash
# A. 打包前：清掉你自己的凭据、收到的消息、媒体和状态
python wechat_gateway.py logout --purge     # 清 token / 收件箱 / 媒体 / 上下文状态
rm -rf shots/* work/* .state/               # 清截图与解题产物（Windows 手动删目录内容）
rm -rf .venv __pycache__                    # 别打包虚拟环境

# B. 打包（只带代码与配置模板）
cd .. && zip -r screenpeer-kit.zip one-person-agent \
    -x "*/__pycache__/*" "*/work/*" "*/.state/*" "*/.venv/*" "*/shots/*"

# C. 对方拿到后：装依赖（见上），然后扫码——**务必加 --new**
python wechat_gateway.py login --new        # → 生成全新二维码，扫出他自己的机器人
python wechat_gateway.py doctor             # 确认 account_id 是他自己的
```

- `login`（不加参数）= **沿用**同目录里已有的机器人身份；`login --new` = **申请一张全新二维码**。
- 如果忘了 `--purge` 就把包发了出去：对方只要用 `--new` 扫码，就会覆盖掉你的凭据，不会再连到你的机器人；
  但更稳妥的做法是**打包前先 purge**，否则你的 token 已经在对方硬盘上了（建议在微信里删除该机器人会话解绑）。

## 整个链路的启动流程（三个常驻 + 一个主循环）

```bash
P=/Users/fch/project/dp-workspace/.venv/bin/python
cd /Users/fch/project/dp-workspace/one-person-agent

# ① 一次性：微信扫码登录（只在首次 / 换号时做；需你本人用手机扫）
$P wechat_gateway.py login

# ② 常驻 A：微信网关 —— 长轮询收微信消息 + 本地 HTTP API（127.0.0.1:8799）
$P wechat_gateway.py serve

# ③ 常驻 B：截图端 —— 按 Ctrl+` 截图落进 shots/（端口/目录读 wait_events.json 的 capture 段）
$P start_capture.py --print-cmd     # 先确认端口/目录，不启动
$P start_capture.py

# ④ 主循环（Agent 侧）：常驻推送 → 有新事件时主动唤醒 Agent → 读图解题 → 发微信
$P wait_events.py --push
#   备用（没有 DSH / 没装 relay-wake 插件时）：$P wait_events.py   ← 打印事件 + 空闲退出
```

**`--push` 是什么**：脚本常驻不退出；收到截图/微信后**静默 N 秒**（默认 8s，
`--debounce` 可调）没有新事件，就把这一批 POST 给 DSH 的 `relay-wake` 插件
（`http://127.0.0.1:3080/relay/wake`，见 `dsh-plugin-relay-wake/`），由插件调
`Agent.followup()` **直接开一个新 turn 把 Agent 叫醒**。持续有新事件就一直不推（攒成一批）；
没有新事件就什么都不做。推送成功才推进账本，失败保留重试（最多重复投递，绝不丢）。

为什么不再靠「脚本退出 → job 结算通知」：那条通知受 `tool-jobs.maxConsecutiveWakes`
预算限制（默认连续 3 次），用完就只进收件箱不唤醒——实测出现过 **47 分 54 秒空窗**。

**推送通道怎么装（macOS / Windows 通用，详见 `dsh-plugin-relay-wake/README.md`）**：

- **免安装热载**（本机自用最省事，不用重启）：在 `~/.dsh/profiles/web/cordis.patch.yml`
  里 insert 一行，`name` 写 `dsh-plugin-relay-wake/index.js` 的绝对路径（Windows 用 `C:/...` 正斜杠）。
  保存即生效，回滚就删掉这一段。
- **官方 bundle**（可分发到别的机器）：在 DSH 检出目录里
  `pnpm dsh plugin --profile web add <dsh-plugin-relay-wake 的路径>`，然后重启一次 `dsh web`
  （重启会杀掉后台任务，起来后重新拉起截图端/网关/推送脚本）。卸载 `remove` 同理。
- 两种方式**只能二选一**（同一个行 id），token 必须与 `wait_events.json` 的 `push.token` 一致；
  生成：`python -c "import secrets;print(secrets.token_hex(16))"`。

**顺序无所谓**（三个进程互不依赖），但要「先全起来，再让用户按热键」。
各步的存活验证：

| 步骤 | 验证 |
|---|---|
| ① | `$P wechat_gateway.py doctor` → 凭据 ✓ / iLink 可达 ✓ |
| ② | `curl -s http://127.0.0.1:8799/health` → `"ok": true`，收到消息时 `inbox.count` 增长 |
| ③ | 日志出现「接收保存目录 …」「[热键] Ctrl+` 已启用」；`lsof -nP -iTCP:5077 -sTCP:LISTEN` 有输出 |
| ④ | `--push` 横幅出现「模式: 常驻推送」+「推送: …/relay/wake → 会话 session-…」；`curl -s http://127.0.0.1:3080/relay/health` → `"ok":true`（备用模式：打印横幅后安静等待即是正常，**零事件不退出**，静默 ≠ 卡死）|

### 谁读哪份配置 / 谁拉起谁

```
start_capture.py ──读 wait_events.json 的 capture 段──► subprocess.Popen([
                                                          python, screenpeer.py,
                                                          self→127.0.0.1,
                                                          --port 5077,
                                                          --save-dir shots,
                                                          --cooldown 2.0 ])
wait_events.py   ──读 wait_events.json 全部────────────────► 盯 shots/ + wechat_inbox.jsonl
wechat_gateway.py ─读 wechat_gateway.json（独立）─────────► iLink Bot API + HTTP :8799
```

| 配置文件 | 谁读 | 管什么 |
|---|---|---|
| `wait_events.json` | `wait_events.py` **和** `start_capture.py` | 截图目录、`capture` 段（端口/对端/节流/auto_port）、轮询间隔、空闲阈值、状态文件、微信源路径 |
| `wechat_gateway.json` | `wechat_gateway.py` | token、默认收件人、HTTP 端口、发送/接收行为 |

即：**截图这一侧只配 `wait_events.json`，微信那一侧只配 `wechat_gateway.json`**。
`screenpeer.py` 是工具本体（未被修改），只由 `start_capture.py` 拉起；
也可以绕过启动器直接跑：`$P screenpeer.py 127.0.0.1 --port 5077 --save-dir shots`。

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
| `wait_events.py` | **阻塞轮询脚本**：每 2s 扫一次截图目录 + 微信收件箱，打印新事件名；零事件时一直阻塞，打印过之后空闲 10s 安全退出；账本保证「打印过的不再打印」 |
| `wait_events.json` | 配置：间隔 / 空闲阈值 / 监听源 / 截图目录，以及**截图端 `capture` 段（端口、对端、节流、auto_port）** |
| `wechat_gateway.py` | **微信服务**：`login` `send` `serve` `inbox` `chats` `typing` `doctor` `logout`，serve 模式还提供本地 HTTP API |
| `wechat_gateway.json` | 微信配置（token、默认收件人、HTTP 端口等；token 由 login 自动回填） |
| `ilink_client.py` | iLink Bot API 协议实现（登录/收发/媒体 AES-128-ECB 加密上传/长轮询/切片） |
| `code_image.py` | **代码转图片**：pygments 着色 + Pillow 绘制，中文注释不乱码，深/浅主题、行号、高亮、自适应宽度 |
| `start_capture.py` | 截图端启动器：端口/对端/目录全部读 `wait_events.json` 的 `capture` 段，命令行可覆盖 |
| `screenpeer.py` | 原截图工具（全局热键 `Ctrl+\``，事件在系统层被吃掉，前台程序收不到这个按键） |
| `vscode_notify.py` | **推送到编辑器底框**：`push` `clear` `status` `doctor`，只用标准库、2 秒超时、永不阻塞（没面板就退出码 2） |
| `vscode-answer-panel/` | **底框扩展源码**（纯 JS、零运行时依赖、约 18 KB vsix）+ `scripts/build.sh` / `scripts/install.sh` + 31 项离线测试 |
| `selftest.py` | 离线自测（88 项：不重复打印/双源/重放/加密/切片/代码图/零事件阻塞/端口配置/独占登记/底框扩展） |
| `SKILL.md` | **给 Agent 的编排流程与注意事项**（同一份也装在项目级技能目录 `<工作区>/.agents/skills/study-wechat-relay/`，DSH 自动加载；**不装用户级**） |

## 关键行为（都是刻意的）

- **看过的不再看**：状态账本 `.state/wait_events_state.json` 记录所有打印过的名字，
  跨进程/跨重启/跨会话都生效 → 同一张图不会二次污染 Agent 上下文。
- **微信消息只打印用户说的话**：`WX 帮我看看这题`（带附件时补本地路径），不带时间/ID 等噪音；调试用 `--wechat-meta`。
- **双源一起等**：截图目录 + 微信收件箱合成一条监听链路，谁先来算谁的事件——Agent 不必「同时监听两个脚本」。
- **推送模式（`--push`）不占用你的注意力**：脚本常驻、新事件静默 8 秒后主动唤醒 Agent，没有事件就什么都不做。
  旧的阻塞模式（打印 → 空闲退出 → 靠 job 结算通知唤醒）仍然可用（`wait_events.py` 不加 `--push`），
  但那条通知受 `maxConsecutiveWakes` 预算限制，长时间中继会断（实测空窗 47 分钟）。
- **被中断也不丢事件**：**推送成功**才算「确认送达」；被 kill 掉的那一轮，下次运行以 `REPLAY` 补投，
  推送失败的批次留在账本里重试（最多重复投递，绝不丢）。
- **网关去重带时间窗**：只挡「同一条消息被服务端重投」（`inbox.dedup_ttl_sec`，默认 10 秒）。
  过了窗口你再发同样一句话照样收得到——旧版无时间上限，把重复发的「测试」静默吞掉了（已修）。
- **冷启动**：首次运行时目录里的历史截图静默忽略；微信历史消息回溯 5 分钟（可配），免得漏掉你刚发的问题。
- **三级降级**：算法题发「代码图 → 源码文件 → 代码片段」，前一级失败才降级。
- **底框是附送的**：答案先铺到编辑器底部（本地推送，2 秒超时），再发微信。编辑器没开就静默跳过，不重试、不阻塞、也不影响微信那条主通道。

## 常用命令

```bash
$P wechat_gateway.py send --text "第 3 题（单选）｜答案：B"       # 发文本
$P wechat_gateway.py send --text "思路…" --image code.png --file solution.py
$P wechat_gateway.py inbox --since 0        # 看收到的消息
$P wechat_gateway.py chats                  # 已知会话（拿 chat_id 用 --to 指定）
$P wait_events.py --once                    # 只扫一遍（调试）
$P wait_events.py --status                  # 看账本
$P code_image.py --in a.py --out a.png --lang python --title "T1"
$P vscode_notify.py push -t "第3题" -f answer.md   # 顺手铺到编辑器底框
$P vscode_notify.py status                 # 看有没有活着的底框（退出码 2 = 没有）
$P selftest.py                              # 离线自测
```

HTTP API（`serve` 模式，默认只听 `127.0.0.1:8799`）：

```bash
curl -s http://127.0.0.1:8799/health
curl -s http://127.0.0.1:8799/inbox?since=0
curl -s -X POST http://127.0.0.1:8799/send -H 'Content-Type: application/json' \
     -d '{"text":"hello","images":["/abs/code.png"]}'
```

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `send` 报 `prepare failed` / `ret=-2` | 与收件人的会话还没建立：**让接收方先在微信里给机器人发一条消息**，之后就能主动推 |
| 端口 5077 被占用 | 本机已有另一个 screenpeer 实例（`--print-cmd` 会打印占用者 PID）。默认自动避让并提示；要固定端口就把 `wait_events.json` 的 `capture.auto_port` 设 `false`（直接报错，不偷偷换）。局域网互传模式永不自动换端口 |
| 热键按了没反应 | macOS 需给运行终端授权「屏幕录制 + 辅助功能」，授权后重启终端；Windows 无需授权 |
| 收不到微信消息 | iLink 的 bot 是**独立身份**（`xxx@im.bot`），普通微信群基本收不到事件、也进不了群；**私聊可靠** |
| token 失效 | `$P wechat_gateway.py login --force` 重新扫码（必须你本人扫）|
| 图片在微信里是灰块 | `aes_key` 必须是 base64(hex 字符串)，见 `ilink_client.py` 的注释，别改坏 |

## Windows 适配

代码层面已做的适配（**未在真机实测**，逻辑上均跨平台）：

| 位置 | 处理 |
|---|---|
| 路径 | 全用 `os.path` / 绝对路径可配；配置文件支持相对路径 |
| 热键 | `screenpeer.py` 走 `WH_KEYBOARD_LL` 钩子，无需授权（该脚本未改动） |
| 端口探测 | 改成「先 connect 再 bind」——Windows 的 `SO_REUSEADDR` 允许重复绑定，纯 bind 探测会误判空闲 |
| 端口占用者 | `netstat -ano` + `tasklist`（macOS/Linux 用 `lsof`），拿不到就静默降级 |
| 控制台编码 | 脚本启动时把 stdout/stderr 的 `errors` 放宽为 `replace`：GBK 控制台打印 ✓/⚠ 不会崩（中文照常，个别符号变 `?`） |
| 状态文件 | `os.replace` 原子替换，Windows 同盘可用；被 kill 不会写坏账本 |
| 字体 | `code_image.py` 找 Consolas / 微软雅黑（CJK），找不到才回退 |
| 信号 | `SIGTERM/SIGINT` 注册容错，Windows 下同样能干净退出 |
| 进程 | `Popen(stdin=PIPE)` 保活、`terminate()` 停止，均为跨平台 API |

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

## 安全

- `wechat_gateway.json` 里含 token，落盘权限已自动设为 `600`，**不要提交到版本库**；
- 使用的是腾讯官方 iLink Bot API（扫码授权、可随时在微信里删除该机器人会话解绑），不是第三方逆向协议；
- HTTP API 默认只监听 `127.0.0.1`；要跨机调用请设 `http.api_token` 并把 host 改 `0.0.0.0`。
