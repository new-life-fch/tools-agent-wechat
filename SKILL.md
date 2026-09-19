---
name: study-wechat-relay
description: 学习场景「截图 → 解题 → 微信回传」中继。用户按 Ctrl+` 截图（或直接在微信里给机器人发消息）后，Agent 起的**常驻推送脚本**会主动唤醒 Agent（静默 8 秒）→ 读图识题 → 解题验证 → 答案发回微信 → 继续等下一批。Use when the user asks for 刷题助手 / 截图答题 / 答案发微信 / 学习中继 / 帮我盯着截图和微信 / study relay.
metadata: { "tags": "study, relay, wechat, screenshot, resident-push, exam" }
---

# study-wechat-relay — 截图答题 → 微信回传

工具链固定在 `/Users/fch/project/dp-workspace/one-person-agent/`。
按下面的命令用。

```
用户按 Ctrl+` ──► shots/*.png ─┐
                               ├─► wait_events.py --push（常驻）
微信消息 ──► wechat_inbox.jsonl ┘        │ 有新事件 → 静默 8s → POST 给 relay-wake 插件
                                         ▼
                              Agent.followup() → 直接开一个新 turn 唤醒你
                                         │
                                         ├─► wechat_gateway.py ──► 微信（主通道）
                                         └─► vscode_notify.py  ──► 编辑器「答题板」（附送）
```

**关键**：你会被常驻进程主动唤醒，所以起完常驻推送脚本就可以结束本轮，
什么都不用守着；下一批事件到来时脚本会唤醒你，事件清单直接出现在那条消息里。禁止杀掉常驻推送脚本、网关、截图脚本（用户要求除外），这三个脚本常驻后台。

## 0. 开工前

```bash
export PY=/Users/fch/project/dp-workspace/.venv/bin/python OA=/Users/fch/project/dp-workspace/one-person-agent
```

> ⚠️ 每次 bash 调用都是全新 shell，`$PY`/`$OA` 不会跨调用保留——下面所有 `$PY $OA/...` 的命令，
> 每条前面都要重带这一行（或直接写绝对路径）。

三个常驻进程供血，**缺一个就等不到事件**。各查一句，缺哪个起哪个：

| 常驻 | 起法（后台跑，不设超时） | 不在跑的症状 |
|---|---|---|
| 微信网关 | `$PY $OA/wechat_gateway.py serve` | 永远收不到 `WX` 事件；`curl -s http://127.0.0.1:8799/health` 不通 |
| 截图端 | `$PY $OA/start_capture.py` | 用户按 Ctrl+` 没反应（`shots/` 不出新图）；先 `--print-cmd` 看端口/目录 |
| 推送通道 | DSH 插件 `relay-wake`（已装则常驻） | `curl -s http://127.0.0.1:3080/relay/health` 不通 → **停下来告诉用户装插件**，别自己造轮询 |

配置：截图端口/目录在 `wait_events.json`（`capture` 段 + `shot_dirs`），微信在 `wechat_gateway.json`；
**改配置要重启对应进程**。推送 token 在 `wait_events.json` 的 `push.token`，必须与插件行的 `config.token` 一致（不一致会 401）。

编辑器「答题板」**不是常驻进程**：编辑器启动时自己激活，你只在步骤 4 顺手推一下，推不上就算了。

## 1. 铁律

1. **推送脚本只起一次，别守着它。** 它不会自己退（零事件时也不退）；不要 `job_output(wait)` 死等、
   不要因为「在跑但没输出」重启它。只有 `job_list` 里它变成 `completed`（=崩了）才重开。
2. **同一张图/同一条消息只处理一次。** 脚本账本保证不重复投递；你自己也别重读旧图。
3. **题号以图为准。** 图里写「第 3 题」就回「第 3 题」；一图多题就逐题分开回，顺序按题号。
4. **算法题必须先跑通样例再发**，且 Go 固定 I/O 写法（见步骤 3）。没验证过的代码不许发微信。
5. **发微信按三级降级**：代码图 → 文件 → 代码片段（文本），前一级失败才降级。
6. **一轮处理完就结束本轮。** 不要 `sleep`、不要自己找活干、不要问「还要继续吗」——下一批事件脚本会唤醒你。
7. **不要自己在代码里做 LLM 路由**：双向消息由「你这个 Agent」处理，脚本只负责搬运。
8. **图就是唯一信息源**：整图读一遍即可，**不裁剪、不放大、不搜原题**（除非用户明说「搜一下」）。
9. **VSCode 底框是附送通道**：推不上（编辑器没开/扩展没装）一律静默跳过，**绝不**因此重试、阻塞或提示用户装扩展。
10. **权限被拒不要问用户，自己绕过去**：产物、缓存、临时文件都指回 `$OA/work/`（临时文件用 `/tmp`）。
    正常解题不会遇到越界；绝不为此弹选项卡或停下来等用户——用户常常不在电脑前。

## 2. SOP

### 步骤 1 — 起常驻推送（整个会话只做一次）

```
bash({ command: "$PY $OA/wait_events.py --push", run_in_background: true })   → 拿到 job_id
```

- 起完就结束本轮。**不要**守着它、不要重复起。
- 有新事件 → 它**静默 8 秒**（最后一个事件之后没有新事件）再推，新 turn 的消息里直接带事件清单。
- 横幅出现这三行 = 通道活着：`模式: 常驻推送`、`推送: http://127.0.0.1:3080/relay/wake → 会话 session-…`、
  `静默去抖 8.0s`。
- **同一时刻只有一个等待脚本**（脚本带独占登记）：新会话起会自动接管，被接管的旧脚本**静默退出**（退出码 4）——
  看到就**不要重启**，事件归当前会话，一条都不会丢。

### 步骤 2 — 读图 / 读消息

- `IMG <绝对路径>` → 用 `read_image` 读那张图。
- `WX <正文>` → 用户从微信发来的话；带 `[图片]`/`[文件]` 时后面就是本地路径，同样 `read_image` 读。

**读图一次到位**：整图读一遍，题面/样例/语言/模式全在里面——不裁剪、不放大、不重复读、不搜原题。

**图被截断时**：题面/样例明显不完整而同批还有后续 `IMG`，就接着读下一张（多半是同一题剩下的部分）；
后面没有新图了就用手上这张直接做，不要干等。

判题：题型（单选 / 多选 / 不定项 / 判断 / 填空 / 简答 / 算法）+ **抄下题号原文**（「3.」「第 3 题」「(3)」都算）。
算法题另外记下：题目描述、输入输出格式、**全部样例**、数据范围、**要求语言**（图里模板的语言，没有就用 Go 并说明）、
**模式**（`ACM` 完整程序读 stdin 写 stdout / `核心代码` 只交类或函数）。

### 步骤 3 — 解题与**本地验证**

产物落盘，每题一个批次目录：

```bash
B=$OA/work/$(date +%Y%m%d_%H%M%S) && mkdir -p $B   # 记下绝对路径；$B 同样不跨 bash 调用保留
```

- **算法题**：写 `$B/solution.<ext>`，然后真的跑样例：
  - ACM 模式：样例输入存 `$B/t1.in` 直接跑并比对——Go `go run $B/solution.go < $B/t1.in`；
    Python `$PY $B/solution.py < $B/t1.in`；C++ `g++ -O2 -o $B/a $B/solution.cpp && $B/a < $B/t1.in`。
  - 核心代码模式：写 `$B/run_tests.py` 调用你的函数/类，断言所有样例。
  - **全部样例通过**才继续；不过就改，最多绕 3 轮，仍不过就如实告诉用户卡在哪。
- **非算法题**：直接核对选项/空；不确定的把依据写清楚。

**Go 固定 I/O 写法**（用户明确要求的判题口径；别的语言暂不要求）：

| | 必须这样 | 不要这样 |
|---|---|---|
| 输入 | `in := bufio.NewReader(os.Stdin)` + `fmt.Fscan(in, &x)` / `in.ReadString('\n')` | `fmt.Scan(&x)`：每次无缓冲读，q=2×10⁵ 时实测 1.34s，有超时风险 |
| 输出 | `fmt.Println(...)` / `fmt.Print(...)` / `fmt.Printf(...)` | `bufio.NewWriter(os.Stdout)` + `Flush()`、`os.Stdout.Write(...)`：**平台检测不到**，会判成没输出 |

```go
in := bufio.NewReader(os.Stdin)
var q int
fmt.Fscan(in, &q)
for ; q > 0; q-- {
    var x int
    fmt.Fscan(in, &x)
    fmt.Println(x)      // 需要拼行就用 strings.Builder 拼好，最后一次 fmt.Print
}
```

> 发代码图和源码文件前自查：`grep -n "bufio.NewWriter\|os.Stdout.Write\|fmt.Scan(" solution.go` 不该有命中。

### 步骤 4 — 发到微信（+ 顺手铺到 VSCode 底框）

先铺底框（本地、2 秒超时、失败闭嘴），再发微信（主通道）：

```bash
# ① 底框（best-effort，正文长或带代码就 --file）
$PY $OA/vscode_notify.py push --title "第 3 题" --file $B/answer.md

# ② 微信（主通道）
$PY $OA/wechat_gateway.py send --text "第 3 题（单选）｜答案：B
简析：……"
```

- `--title` 写图里的**题号原文**；一题一条，多题按题号依次推。
- 底框退出码 `0` 推到了 / `2` 编辑器没开或扩展没装 / `3` 连上了被拒 —— 后两种都是**正常现象**，
  直接跳过继续，不要重试、不要提醒用户装扩展（排查才用 `doctor`）。
- 算法题顺序固定：**思路文字 → 代码图 → 源码文件**：

  ```bash
  $PY $OA/code_image.py --in $B/solution.go --out $B/code.png \
      --lang go --title "T1 · 两数之和" --subtitle "Go · 核心代码模式" \
      --footer "时间 O(n) / 空间 O(1)" --highlight 5,6
  $PY $OA/wechat_gateway.py send \
      --text "题目：两数之和｜模式：核心代码｜语言：Go
  思路：哈希表边遍历边记下标……
  复杂度：时间 O(n)，空间 O(n)
  样例：2/2 通过" \
      --image $B/code.png --file $B/solution.go
  ```
- 发送成功判定：输出 `[结果] 成功 …`（退出码 0）；失败见 §5。
- **不要向用户提问、不要给选项卡**：用户没收到微信会自己在微信里发消息，那条消息会把你唤醒。

### 之后 — 收尾

把这批事件处理完就**结束本轮**，不需要任何等待动作。脚本仍在跑，下一批事件会唤醒你。
一次唤醒可能带多个事件（多张图/多条消息），**按顺序全部处理完**再收尾。

## 3. 事件格式与解析

```
IMG /abs/path/shots/20260916_120705_542.png                 # 新截图
IMG /abs/path/shots/20260916_115900_777.png REPLAY          # 上次被强杀后补投的，照常处理
WX 帮我看看这题                                             # 微信消息：只有用户说的话
WX 这题用 Java 再写一遍 [图片] /abs/.../wechat_media/a.jpg   # 带附件时给本地路径
WX 在吗 [chat=xxx@im.wechat]                                # 只有非默认会话才带 chat 提示
```

- `WX` 行默认**只给用户说的话**（无时间/msg_id/seq 噪音）；`REPLAY` 照常处理并回复。
- 只有出现 `[chat=…]` 时才需要用 `--to <chat_id>` 指定回复目标，否则直接回默认会话。
- 需要完整 JSON（调试）时加 `--wechat-meta`。

## 4. 回复模板

**非算法题**（答案先行，题号与图片一致）：

```
第 3 题（单选）｜答案：B
简析：……

第 5 题（多选）｜答案：ABD
简析：……

第 7 题（填空）｜答案：(1) 3  (2) 5
简析：……
```

- **简析直击要点就行，别长篇大论。**
- 一图多题：可以合并成一条（按题号分行）或一题一条，但顺序按题号；一图一题就别加多余前缀。

**算法题**三条顺序固定：① 文本（题目名 + 模式 + 语言 + 3~6 行思路 + 复杂度 + 样例验证结论）
② 代码图 ③ `solution.<ext>` 文件。图片失败 → 只发文件；文件也失败 → 只发代码片段（围栏包住，
网关按代码块边界切片，不会切坏围栏）：

```bash
$PY $OA/wechat_gateway.py send --text "…思路…" --image $B/code.png --file $B/solution.go
$PY $OA/wechat_gateway.py send --text "…思路…" --file $B/solution.go
$PY $OA/wechat_gateway.py send --text "…思路…

\`\`\`go
<完整代码>
\`\`\`"
```

## 5. 异常处理手册

| 现象 | 处理 |
|---|---|
| 起了推送脚本但一直没被唤醒 | **正常**：没有新事件它就不推。别重启、别用 `--once` 轮询试探；确认截图端和网关在跑（`/health`、`job_list` 里 job 还是 `running`）|
| 横幅写「推送未启用：…」 | 通道没配好（缺 url/会话 id）：先 `curl -s http://127.0.0.1:3080/relay/health`；插件不在就**停下来告诉用户装 `relay-wake`**，别自己写轮询顶着 |
| 日志「推送失败（…）：N 条事件保留…」 | 不用管，脚本 5 秒后自己重试。连续失败才看：`404 session-not-live` = 目标会话 agent 不在了（在 GUI 里打开该会话）；`401 bad-token` = token 与插件配置不一致 |
| `job_list` 里推送 job 变成 `completed` | 脚本崩了（唯一需要重开的信号）。看它的输出定位原因，再按步骤 1 重起一次 |
| `send` 报 **会话尚未建立 / prepare failed** | 让用户在微信里先给机器人发一条消息（随便一个字），再重发——机器人不能对没打过招呼的人主动开口 |
| `send` 报 **频率限制（ret=-2）** | 等 10~30s 再发；长内容合并成一条，别连发多条 |
| `send` 报 **HTTP 4xx / 网络错误** | `$PY $OA/wechat_gateway.py doctor`；token 失效就 `login --new` 重新扫码（**二维码必须用户本人扫**）|
| 用户说「这是别人的包 / 我要用自己的微信」 | `$PY $OA/wechat_gateway.py login --new`：申请**全新二维码**，扫完就是用户自己的机器人 |
| 图片发出去是**灰块** | aes_key 参数被改坏了。别自己改协议，恢复 `ilink_client.py` 后重发 |
| 图片读不出来（坏文件/半截） | 跳过它继续处理其余事件，并在微信里说明「这张图读取失败，麻烦重截」 |
| 截图里**没有题目**（截到桌面/聊天窗/文档） | 发一条微信「没识别到题目，请把题面放到屏幕可见处再按一次」，然后结束本轮，不要假装答过 |
| 用户在微信里发的是**纯聊天/催问** | 直接回，不用走解题流程 |
| 网关挂了 | `$PY $OA/wechat_gateway.py serve` 重新拉起；收件箱是追加写，重启不丢消息 |
| 截图端端口被占 | `--print-cmd` 会打印占用者 PID。默认自动避让；要固定端口把 `capture.auto_port` 设 `false`（报错退出）|
| 改了端口后收不到图 | 端口只影响截图端；确认 `capture.save_dir`（或 `shot_dirs[0]`）与轮询的 `shot_dirs` 是同一个目录 |
| `vscode_notify.py` 退出码 2 / 3 | **正常**（没开编辑器 / 令牌过期）：跳过，不重试、不提示用户装扩展；用户问起才 `doctor` |
| 用户说底框里没东西 | `$PY $OA/vscode_notify.py status`：有面板就让他点底部「答题板」标签；没面板就是编辑器没开 |
| bash 报 `[sandbox: file access denied]` | 说明这条命令写到 `$OA/` 外面了：产物指回 `$OA/work/`、临时文件用 `/tmp` 重跑。**不要**为此问用户 |

## 6. 省上下文的纪律

- 只读**本次新到**的图；`read_image` 一张就够，别重读、别翻 `shots/` 里的历史图。
- 长代码/长推导**落盘到 `$B/`**，对话里只留结论（要发出去的内容 + 2~3 行理由）。
- 发完就忘：不复述已发内容，直接结束本轮。
- 一次唤醒带多个事件时按顺序处理，别把多张图的推理混在一起。

## 7. 双向消息（用户在微信里提问）

- 微信来的问题当普通题目处理；回复默认发到默认会话，只有该行带 `[chat=…]` 才用 `--to <chat_id>` 发回那个会话。
- 用户连发多条 → 可能一次唤醒里出现多条 `WX`，合并理解后回一条完整答案。
- 需要长时间思考时可先 `$PY $OA/wechat_gateway.py typing --status start` 显示「正在输入…」，发完 `--stop`（可选）。

## 8. 命令速查

```bash
P=/Users/fch/project/dp-workspace/.venv/bin/python; cd /Users/fch/project/dp-workspace/one-person-agent

$P wait_events.py --push                  # 常驻推送（唯一跑法：后台起一次，之后别管它）
$P wait_events.py --push --debounce 15    # 改静默去抖时长
$P wait_events.py --status                # 看账本 + 推送计数
$P wait_events.py --once                  # 只扫一遍（调试；--push 下扫完就推、推完即退）
curl -s http://127.0.0.1:3080/relay/health   # 推送通道：唤醒计数 / 活着的会话列表

$P start_capture.py                       # 起截图端（端口/目录读 wait_events.json 的 capture 段）
$P start_capture.py --print-cmd           # 看最终用哪个端口/目录，不启动
$P wechat_gateway.py serve                # 起微信网关（收发 + HTTP API）
$P wechat_gateway.py doctor               # 自检
$P wechat_gateway.py send --text "..."    # 发文本；--image/--file 可多次
$P wechat_gateway.py inbox --since 0      # 看收件箱
$P wechat_gateway.py chats                # 已知会话（拿 chat_id）
$P code_image.py --in a.py --out a.png --lang python --title "T1"   # 代码转图片
$P vscode_notify.py push -t "第3题" -f $B/answer.md   # 铺到编辑器底部「答题板」
$P selftest.py                            # 全量离线自测

curl -s http://127.0.0.1:8799/health      # 网关状态
```

配置文件：`wait_events.json`（监听源、`shot_dirs`、`poll_interval_sec`、`push` 段、截图端 `capture` 段）、
`wechat_gateway.json`（token、默认收件人、HTTP 端口）。
**VSCode 底框**（`vscode-answer-panel/`）不需要你启动：编辑器激活扩展后自己监听，端口和令牌写在
`~/.answer-panel/bridge.json`，`vscode_notify.py` 自动去读；它不在跑也不影响主流程。
Windows 上把路径换成对应形式即可——全部脚本跨平台（无 shell 依赖、无 POSIX 专有调用）。
