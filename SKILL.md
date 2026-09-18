---
name: study-wechat-relay
description: 学习场景「截图 → 解题 → 微信回传」中继循环。用户在前台按 Ctrl+` 截图（或直接在微信里给机器人发消息），Agent 用阻塞脚本等待新事件 → 读图识题 → 解题验证 → 把答案发回微信 → 再回到阻塞等待，如此往复。Use when the user asks for 刷题助手 / 截图答题 / 答案发微信 / 学习中继 / 帮我盯着截图和微信 / study relay.
metadata: { "tags": "study, relay, wechat, screenshot, blocking-wait, exam" }
---

# study-wechat-relay — 学习场景截图答题中继

一套已经落地的工具链 + 编排纪律。所有脚本都在 `D:\screenpeer-kit_self\one-person-agent\`，
**不要重写、不要自己写轮询循环**，直接用下面的命令。

```
用户按 Ctrl+` 截图 ──► shots/*.png ─┐
                                    ├─► wait_events.py（阻塞）──► Agent 识图解题 ──► wechat_gateway.py ──► 微信
用户在微信里发消息 ──► 收件箱 JSONL ─┘                                ▲                                    │
                                                                      └────────── 再次阻塞等待 ◄────────────┘
```

> 开跑之前先读一遍全文，特别是「铁律」和「异常处理手册」——这套流程的成败几乎全在纪律上。

## 0. 开工前必须确认的两件事

```powershell
$PY = "D:\Develop\Anaconda3\envs\python310\python.exe"; $OA = "D:\screenpeer-kit_self\one-person-agent"
```

> ⚠️ **每次 PowerShell 调用都是全新进程，`$PY`/`$OA` 不会跨调用保留**——本文所有用 `& $PY "$OA\..."` 的命令，
> 每条前面都要再带上上面这一行（或直接写绝对路径），否则会报「找不到文件」。
> 命令名来自变量时必须加调用运算符 `&`：写成 `$PY $OA\wait_events.py` 会报 `Unexpected token`。

这个流程靠两个**常驻进程**供血。它们不在跑，你会一直等不到任何事件：

| 常驻 | 起它的命令（后台跑，**不设超时**） | 不在跑的症状 |
|---|---|---|
| 微信网关 | `& $PY "$OA\wechat_gateway.py" serve` | 永远等不到 `WX` 事件（`/health` 的 `count` 不涨）|
| 截图端 | `& $PY "$OA\start_capture.py"` | 用户按 Ctrl+` 也没用（`shots\` 不出新图）|

各查一句，缺哪个起哪个：

```powershell
Invoke-RestMethod http://127.0.0.1:8799/health    # ok:true → 网关在跑（原来用 curl -s …/health）
& $PY "$OA\start_capture.py" --print-cmd          # 看截图端会用哪个端口/目录（只看不启动）
```

起完这两个，你的活就是 §2 的五步循环；`wait_events.py` 是**阻塞**的，零事件时永远不退出。

### 配置在哪（只有用户让你改端口/目录时才需要看）

截图侧的端口、对端、落盘目录都在 `wait_events.json`：

```jsonc
"shot_dirs": ["shots"],          // 截图落盘 = 轮询监听（同一个目录）
"capture": {
  "peer": "self",                // "self"=本机自收；或对方 IP（局域网互传）
  "port": 9119,                  // ← 换端口改这里；局域网互传两端必须填一样
  "cooldown_sec": 2.0,
  "auto_port": true,             // 被占：true=自动避让并提示；false=报错退出，绝不偷偷换
  "save_dir": ""                 // 留空 = shot_dirs[0]
}
```

微信侧（token、默认收件人、HTTP 端口）在 `wechat_gateway.json`，与上面无关。
**改配置要重启对应常驻进程才生效**；`--port` / `--dir` / `--sources` 等命令行参数可临时覆盖。

## 1. 铁律（违反任何一条，这个流程都会崩）

1. **等待脚本必须阻塞，永远不设短超时。** 它是「等人出题」的，安静是正常的，不是卡死。
2. **同一张图/同一条消息只处理一次。** 脚本的账本保证不重复打印；你自己也不要去重读旧图。
3. **题号以图为准。** 图片里写「第 3 题」就回「第 3 题」，不要自己重排编号；一图多题就逐题分开回。
4. **算法题必须先跑通样例再发。** 没验证过的代码不许发微信。
5. **算法题正文必须自带完整代码**（用围栏包起来）。代码图、`solution.<ext>` 只是补充——
   考试/答题框只能粘贴文字，图片是拿不走的。**图或文件发不出去时才降级**（图片失败 → 只发文件 → 只发代码片段）。
6. **一轮处理完立刻回到阻塞等待**，不要在两次等待之间插入 `sleep` 轮询或短超时试探。
7. **不要自己在代码里做 LLM 路由**：双向消息由「你这个 Agent」处理，脚本只负责搬运。
8. **图就是唯一信息源**：整图读一遍即可，**不要裁剪/放大局部反复看，也不要上网搜原题**——
   图里的题面/样例/语言/模式已经够了，搜来的题可能跟图不符，反而答错（除非用户明说「搜一下」）。
9. **每轮被叫醒后都要重新挂上等待脚本**，并待在这个回合里（`job_output(job_id, wait: true)`），不要提前收工——
   它是唯一能通知你的信号源，退出后没人会再叫你。

## 2. SOP（每一轮都走这五步）

### 步骤 1 — 阻塞等待（唯一的等待方式）

优先用后台任务 + 阻塞读取（DSH / 支持后台 job 的运行时）：

```
1) 后台跑（run_in_background: true，真正的命令是 & $PY "$OA\wait_events.py"）   → 拿到 job_id
2) 立刻进入等待循环：反复 job_output(job_id, wait: true)
     · 返回 [status: running]  → 继续调，这是正常现象，不是失败
     · 返回 [status: completed] → 取本轮输出，进入步骤 2
3) 整轮期间：不要 job_kill、不要因为「等太久」重开一个、不要改用 --once 反复问
```

如果运行时没有后台任务，就在前台跑，并把 `timeoutMs` 设成运行允许的最大值；
万一被超时杀掉，**直接再跑一次就行**：账本保证已打印过的不重复打印，而被杀掉那一轮
**已经打印但没来得及被你看到**的，会在下次运行的 `REPLAY` 行里补给你（见 §3）。

- 想看单轮快照（调试/自测）才用：`& $PY "$OA\wait_events.py" --once`
- **节奏参数不用背**：脚本启动横幅会把 轮询间隔 / 空闲阈值 / 最长等待 全部打印出来，照横幅理解即可。
  零事件时它一直阻塞、到最长等待才退出一次；打印过事件之后空闲阈值到点就退出——**这两种退出都是正常节奏，不是故障**，
  退出后重新挂上就行。超时退出不会丢事件（下次运行以 `REPLAY` 补给你）。
- 输出里只有以 `IMG ` / `WX ` 开头的行才是事件；横幅与结尾统计忽略即可。
- **独占冲突交给脚本判断**：它会在输出里直接告诉你结论（例如「事件流已被 … 接管」「本会话已经有一个等待脚本在跑」），
  **照那句话做**就行——提示已被接管就不要重启本脚本。内部判定逻辑不用你去推演。

### 步骤 2 — 读图 / 读消息

- `IMG <绝对路径>` → 用 `read_image` 读那张图。
- `WX <正文>` → 用户从微信发来的话；带 `[图片]/[文件]` 时后面就是本地路径，同样 `read_image` 读。

**读图一次到位**：整图读一遍，题面/样例/语言/模式全在里面——不裁剪、不放大、不重复读、不搜原题。

**图被截断时**：若题面/样例明显不完整（题目太长一屏放不下），而本轮还有后续 `IMG`，就接着读下一张（多半是同一题剩下的部分）；若后面没有新图了，就用手上这张直接做，不要干等。

判题：先判题型（单选 / 多选 / 不定项 / 判断 / 填空 / 简答 / 算法），
**抄下题号原文**（「3.」「第 3 题」「(3)」都算），算法题另外记下：

- 题目描述、输入输出格式、**全部样例**、数据范围
- **要求语言**（图片里代码模板的语言，再没有就用 Go 并说明）
- **模式**：`ACM`（完整程序读 stdin 写 stdout）还是 `核心代码`（只交类/函数，LeetCode 风格）

### 步骤 3 — 解题与**本地验证**

产物落盘，每题一个批次目录：

```powershell
$B = Join-Path "$OA\work" (Get-Date -Format "yyyyMMdd_HHmmss"); New-Item -ItemType Directory -Force $B | Out-Null; $B
# 记下这个绝对路径；$B 同样不跨调用保留（后面每条命令重新算一次，或直接写这个绝对路径）
```

- **算法题**：写 `$B\solution.<ext>`，然后真的跑样例：
  - ACM 模式：把样例输入存 `$B\t1.in`，直接跑并比对输出（PowerShell 不支持 `<` 输入重定向，所以套一层 `cmd`）——
    Go：`cmd /c "go run $B\solution.go < $B\t1.in"`；
    Python：`cmd /c "$PY $B\solution.py < $B\t1.in"`；C++：本机没有 `g++`/`cl`，跑不了（别假装验证过）。
  - 核心代码模式：写个 `$B\run_tests.py` 直接调用你的函数/类，断言所有样例。
  - **全部样例通过**才继续；不过就改，改完再跑（最多绕 3 轮，仍不过就如实告诉用户卡在哪）。
- **非算法题**：直接在脑子里核对选项/空；不确定的，把依据写清楚。

### 步骤 4 — 发到微信

```powershell
$PY = "D:\Develop\Anaconda3\envs\python310\python.exe"; $OA = "D:\screenpeer-kit_self\one-person-agent"
$B = "D:\screenpeer-kit_self\one-person-agent\work\<批次时间戳>"     # 换成步骤 3 打印出来的绝对路径

# 非算法题
$text = @'
第 3 题（单选）｜答案：B
简析：……
'@
& $PY "$OA\wechat_gateway.py" send --text $text

# 算法题（顺序：思路文字 → 代码图 → 源码文件）
& $PY "$OA\code_image.py" --in "$B\solution.go" --out "$B\code.png" `
    --lang go --title "T1 · 两数之和" --subtitle "Go · 核心代码模式" `
    --footer "时间 O(n) / 空间 O(1)" --highlight 5,6
$text = @'
题目：两数之和｜模式：核心代码｜语言：Go
思路：哈希表边遍历边记下标……
复杂度：时间 O(n)，空间 O(n)
样例：2/2 通过
'@
& $PY "$OA\wechat_gateway.py" send --text $text --image "$B\code.png" --file "$B\solution.go"
```

发送成功判定：命令输出 `[结果] 成功 …`（退出码 0）。失败见 §5。
**注意**：不要向用户提问或给出选项卡，用户发现长时间没有收到微信消息，会在微信中输入消息，激活通信，在此期间，你保持阻塞脚本运行并等待

### 步骤 5 — 回到步骤 1

只要本轮还有没处理完的事件（一次等待可能返回多张图 / 多条消息），先把它们处理完，
然后**立刻**再次进入步骤 1 的阻塞等待。不要问用户「还要继续吗」，不要打印总结后就停下。

## 3. 事件格式与解析

```
IMG D:\screenpeer-kit_self\one-person-agent\shots\20260916_120705_542.png                  # 新截图
IMG D:\screenpeer-kit_self\one-person-agent\shots\20260916_115900_777.png REPLAY           # 上一轮被中断，补打给你的
WX 帮我看看这题                                                                             # 微信消息：只有用户说的话
WX 这题用 Java 再写一遍 [图片] D:\screenpeer-kit_self\one-person-agent\wechat_media\a.jpg   # 带附件时给本地路径
WX 在吗 [chat=xxx@im.wechat]                                                               # 只有非默认会话才带 chat 提示
WX 在吗 REPLAY
```

- `REPLAY` = 上一轮脚本被强杀、这些事件已打印但没能确认送达。**照常处理**，处理完同样要回复用户。
- `WX` 行默认**只给用户说的话**（没有时间/msg_id/seq 等噪音）。带 `[图片]` `[文件]` 时后面是**本地路径**，直接 `read_image`。
- 只有出现 `[chat=…]` 时，才需要用 `--to <chat_id>` 指定回复目标；否则直接回默认会话。
- 需要完整 JSON（调试）时加 `--wechat-meta`。

## 4. 回复模板（照着写，别自由发挥）

**非算法题**（题号必须与图片一致，答案先行）：

```
第 3 题（单选）｜答案：B
简析：……

第 5 题（多选）｜答案：ABD
简析：……

第 7 题（填空）｜答案：(1) 3  (2) 5
简析：……

第 9 题（判断）｜答案：错误
简析：……
```

- **简析要简单一点，直击要点就行。**
- 一图多题：可以合并成一条消息（按题号分行），也可以一题一条 —— 但**顺序按题号**。
- 一图只有一题时：别加多余前缀，短平快最好。

**算法题**（默认三条一起发，顺序固定）：

1. 文本：题目名 + 模式（ACM/核心代码）+ 语言 + 3~6 行思路 + 复杂度 + 样例验证结论
   **+ 完整代码（围栏包起来）**——这是用户唯一能直接复制粘贴的东西，**不能省**。
2. 代码图：`code_image.py` 渲染（深色、带行号、标题写题号，页脚写复杂度）
3. 文件：`solution.<ext>`（方便用户在电脑上直接跑）

```powershell
# 默认（第一行就是主路径）：文本自带完整代码 + 代码图 + 源码文件。
# 文案里只要出现 ASCII 双引号（"）就一律走 --text-file：
# 直接 --text 传给原生程序时会被 PowerShell 拆成多个参数（实测报 unrecognized arguments，rc=2）。
& $PY "$OA\wechat_gateway.py" send --text-file "$B\reply.txt" --image "$B\code.png" --file "$B\solution.go"

# 降级①：图片发不出去 → 文本 + 文件
& $PY "$OA\wechat_gateway.py" send --text-file "$B\reply.txt" --file "$B\solution.go"

# 降级②：文件也发不出去 → 只发文本（正文里已经有完整代码，所以信息没丢）
& $PY "$OA\wechat_gateway.py" send --text-file "$B\reply.txt"
```

代码走正文时用围栏包起来（网关会按代码块边界切片，不会把围栏切成两半）。
**注意**：正文里的代码是「唯一可复制」的那一份，降级时**不能**把它当成可选项去掉。

## 5. 异常处理手册

| 现象 | 处理 |
|---|---|
| `send` 报 **会话尚未建立 / prepare failed** | 让用户在微信里先给机器人发一条消息（随便一个字），再重发。机器人不能对没打过招呼的人主动开口 |
| `send` 报 **频率限制（ret=-2）** | 等 10~30s 再发；把长内容合并成一条，别连发多条 |
| `send` 报 **HTTP 4xx/网络错误** | `& $PY "$OA\wechat_gateway.py" doctor`：token 失效就 `& $PY "$OA\wechat_gateway.py" login --new` 重新扫码（**二维码必须用户本人扫**，Agent 不能代扫）|
| `send` 报 **`unrecognized arguments` / 参数被拆碎** | 文案里有 **ASCII 双引号 `"`**，经 PowerShell 传给原生程序时被拆成了多个参数。**改用 `--text-file`**（把文案写进文件再发）；中文引号「」不受影响 |
| 事件到了却半天没人处理 | 看铁律 9：等待脚本是不是退出了、没人重新挂上 |
| 用户说「这是别人的包 / 我要用自己的微信」 | 跑 `& $PY "$OA\wechat_gateway.py" login --new`：它会申请**全新二维码**，扫完就是用户自己的机器人；不加 `--new` 只会沿用包里原有的身份 |
| 图片发出去是**灰块** | 说明 aes_key 参数被改坏了。别自己改协议，恢复 `ilink_client.py` 后重发 |
| `wait_events` 一直没输出（几分钟甚至几十分钟） | **正常**：零事件时它就是要一直阻塞。确认网关活着（`/health` 的 `count` 是否增长）与截图端是否在跑即可；**不要**改短超时、不要重开 |
| 图片读不出来（坏文件/半截） | 跳过它，继续处理其余事件；在图里说明「这张图读取失败，麻烦重截」 |
| 截图里**没有题目**（截到了桌面/聊天窗/文档） | 发一条微信说明「没识别到题目，请把题面放到屏幕可见处再按一次」，然后**立刻回去等待**，不要假装答过 |
| 用户在微信里发的是**纯聊天/催问** | 直接回，不用走解题流程；同时继续等待 |
| 网关挂了 | `& $PY "$OA\wechat_gateway.py" serve` 重新拉起（后台跑）；收件箱是追加写，重启不会丢消息 |
| 截图端端口被占 | `& $PY "$OA\start_capture.py" --print-cmd` 会打印占用者 PID。默认自动避让并提示；要「就固定这个端口」把 `wait_events.json` 的 `capture.auto_port` 设 `false`（报错退出，绝不偷偷换）。局域网互传模式**永不自动换端口**（两端必须一致）|
| 改了端口后收不到图 | 端口只影响截图端；轮询脚本只看目录。确认 `capture.save_dir`（或 `shot_dirs[0]`）与轮询的 `shot_dirs` 是同一个目录即可 |

## 6. 省上下文的纪律（很重要）

- 只读**本次新打印**的图；`read_image` 一张就够，不要重复读、不要读 `shots\` 里的历史图。
- 长代码/长推导**落盘到 `$B\`**，对话里只留结论（微信要发的内容 + 必要的 2~3 行理由）。
- 发完就忘：不要把发过的内容再复述一遍，直接回到等待。
- 一次等待返回多个事件时，**按顺序处理**，不要把多张图的推理过程混在一起。

## 7. 双向消息（用户在微信里提问）

`wait_events.py` 已经把两个源合成一次阻塞，所以**不需要你同时监听两个脚本**：

- 微信来的问题 → 当普通题目处理；回复**默认会话直接发**（不带 `--to`），只有该行带了 `[chat=…]` 才用 `--to <chat_id>` 发回那个会话。
- 用户连续发多条 → 可能一次等待里出现多条 `WX`，合并理解后再回一条完整答案。
- 需要长时间思考时，可以先 `& $PY "$OA\wechat_gateway.py" typing --status start`
  让微信端显示「正在输入…」，发完再 `--status stop`（可选，默认发送时会自动带）。

## 8. 命令速查

```powershell
$P = "D:\Develop\Anaconda3\envs\python310\python.exe"; Set-Location "D:\screenpeer-kit_self\one-person-agent"

& $P start_capture.py                      # 起截图端（端口/目录读 wait_events.json 的 capture 段）
& $P start_capture.py --print-cmd           # 看最终用哪个端口/目录，不启动
& $P start_capture.py --port 6000           # 临时换端口（不改配置）
& $P start_capture.py --no-auto-port        # 端口被占就报错退出，不自动避让
& $P wechat_gateway.py serve               # 起微信网关（收发 + HTTP API）
& $P wechat_gateway.py doctor              # 自检
& $P wechat_gateway.py send --text "..."   # 发文本；--image/--file 可多次
& $P wechat_gateway.py inbox --since 0     # 看收件箱
& $P wechat_gateway.py chats               # 已知会话（拿 chat_id）
& $P wait_events.py                        # 阻塞等待（默认配置）
& $P wait_events.py --status               # 看账本
& $P wait_events.py --once                 # 只扫一遍（调试）
& $P code_image.py --in a.py --out a.png --lang python --title "T1"   # 代码转图片
& $P selftest.py                           # 全量离线自测

Invoke-RestMethod http://127.0.0.1:8799/health     # 网关状态（原来用 curl -s …/health）
Invoke-RestMethod -Method Post http://127.0.0.1:8799/send -ContentType "application/json" `
    -Body '{"text":"hello","images":["D:\\screenpeer-kit_self\\one-person-agent\\work\\code.png"]}'
```

配置文件：`wait_events.json`（间隔 2s / 空闲 5s / `max_wait_sec` 300 / 监听源 / **截图端 `capture` 段：port、peer、cooldown、auto_port、save_dir**）、
`wechat_gateway.json`（token、默认收件人、HTTP 端口）。两个配置里的路径都是相对本目录的，换机器不用改；
全部脚本都是跨平台的（无 shell 依赖、无 POSIX 专有调用），本机是 Windows PowerShell 5.1
（`<` 输入重定向要套 `cmd /c`，命令名来自变量要加 `&`；文案含 ASCII 双引号一律走 `--text-file`）。
