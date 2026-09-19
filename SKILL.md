---
name: study-wechat-relay
description: 学习场景「截图 → 解题 → 微信回传」中继循环。用户在前台按 Ctrl+` 截图（或直接在微信里给机器人发消息），Agent 起一个**常驻推送脚本**，有新事件时脚本会主动唤醒 Agent（最多静默 8 秒）→ 读图识题 → 解题验证 → 把答案发回微信 → 继续等下一批，如此往复。Use when the user asks for 刷题助手 / 截图答题 / 答案发微信 / 学习中继 / 帮我盯着截图和微信 / study relay.
metadata: { "tags": "study, relay, wechat, screenshot, resident-push, exam" }
---

# study-wechat-relay — 学习场景截图答题中继

一套已经落地的工具链 + 编排纪律。所有脚本都在 `/Users/fch/project/dp-workspace/one-person-agent/`，
**不要重写、不要自己写轮询循环**，直接用下面的命令。

```
用户按 Ctrl+` 截图 ──► shots/*.png ─┐
                                    ├─► wait_events.py --push（常驻）──► 主动唤醒 Agent 识图解题
用户在微信里发消息 ──► 收件箱 JSONL ─┘         │                              │
                                               │                              ├─► wechat_gateway.py ──► 微信（主通道）
                                               │                              └─► vscode_notify.py  ──► 编辑器底部「答题板」（附送）
                                               └── 下一批事件自动唤醒 ◄────────┘
```

**两种运行模式**（由 `--push` 决定，别搞混）：

| | 推送模式 `--push`（推荐） | 阻塞模式（默认，备用） |
|---|---|---|
| 脚本 | **常驻不退出**；新事件静默 N 秒后主动唤醒你 | 打印事件 → 空闲 N 秒 → 自己退出 |
| 唤醒靠 | `relay-wake` 插件调 `Agent.followup()` → **直接开新 turn** | DSH 的 job 结算通知（受 `maxConsecutiveWakes` 预算限制：连续 3 次后降级为不唤醒） |
| 你的活 | 起一次就别管；被唤醒 → 处理 → 结束本轮 | 每次拿到 `completed` 都要**再起一轮** |
| 已知风险 | 脚本崩了要你自己发现（`job_list` 里没了） | 预算耗尽 → 长时间没人接（实测 47 分钟空窗） |

> 开跑之前先读一遍全文，特别是「铁律」和「异常处理手册」——这套流程的成败几乎全在纪律上。

## 0. 开工前必须确认的两件事

```bash
export PY=/Users/fch/project/dp-workspace/.venv/bin/python OA=/Users/fch/project/dp-workspace/one-person-agent
```

> ⚠️ **每次 bash 调用都是全新 shell，`$PY`/`$OA` 不会跨调用保留**——本文所有用 `$PY $OA/...` 的命令，
> 每条前面都要再带上上面这一行（或直接写绝对路径），否则会报「找不到文件」。

这个流程靠两个**常驻进程**供血。它们不在跑，你会一直等不到任何事件：

| 常驻 | 起它的命令（后台跑，**不设超时**） | 不在跑的症状 |
|---|---|---|
| 微信网关 | `$PY $OA/wechat_gateway.py serve` | 永远等不到 `WX` 事件（`/health` 的 `count` 不涨）|
| 截图端 | `$PY $OA/start_capture.py` | 用户按 Ctrl+` 也没用（`shots/` 不出新图）|

各查一句，缺哪个起哪个：

```bash
curl -s http://127.0.0.1:8799/health      # ok:true → 网关在跑
$PY $OA/start_capture.py --print-cmd      # 看截图端会用哪个端口/目录（只看不启动）
```

起完这两个，你的活就是 §2 的五步循环；`wait_events.py` 是**阻塞**的，零事件时永远不退出。

> 编辑器里的「答题板」底框**不是第三个常驻进程**——它由编辑器自己在启动时激活。你不用管它，
> 只需要在步骤 4 顺手把答案推过去（推不上就算了，见 §1 铁律 9）。

### 配置在哪（只有用户让你改端口/目录时才需要看）

截图侧的端口、对端、落盘目录都在 `wait_events.json`：

```jsonc
"shot_dirs": ["shots"],          // 截图落盘 = 轮询监听（同一个目录）
"capture": {
  "peer": "self",                // "self"=本机自收；或对方 IP（局域网互传）
  "port": 5077,                  // ← 换端口改这里；局域网互传两端必须填一样
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
4. **算法题必须先跑通样例再发**，且 **Go 固定 I/O 写法**（输入 `bufio.NewReader`、输出 `fmt.Print` 系列，
   见「算法题语言要求」）。没验证过的代码不许发微信。
5. **发微信按三级降级**：代码图 → 文件 → 代码片段（文本），前一级失败才降级。
6. **一轮处理完立刻回到阻塞等待**，不要在两次等待之间插入 `sleep` 轮询或短超时试探。
7. **不要自己在代码里做 LLM 路由**：双向消息由「你这个 Agent」处理，脚本只负责搬运。
8. **图就是唯一信息源**：整图读一遍即可，**不要裁剪/放大局部反复看，也不要上网搜原题**——
   图里的题面/样例/语言/模式已经够了，搜来的题可能跟图不符，反而答错（除非用户明说「搜一下」）。
9. **VSCode 底框是附送通道**：推送失败（编辑器没开/扩展没装）一律静默跳过，
   **绝不允许**因此重试、阻塞、或提示用户去装扩展；微信才是必须发成功的那条。
10. **权限被拒不要问用户，自己绕过去。** 正常解题全程都在工作区内——在 `$OA/work/` 里编译运行
    Go / C++ / Python，读写 `shots/`、`.state/`、`work/`——**不会**遇到权限问题。万一看到
    `[sandbox: file access denied]`，那一定是这条命令写到 `$OA/` 外面去了：把产物和缓存指回
    `$OA/work/`（临时文件用 `/tmp`）、换个不越界的写法重跑即可。**绝不**为此弹选项卡、提问、
    或停下来等用户——用户经常不在电脑前，你一停这一轮就废了。

## 2. SOP（每一轮都走这五步）

### 步骤 1 — 阻塞等待（唯一的等待方式）

优先用后台任务 + 阻塞读取（DSH / 支持后台 job 的运行时）：

```
1) bash({ command: "$PY $OA/wait_events.py --push", run_in_background: true })   → 拿到 job_id
2) 起完这一轮就可以收工：**不要** job_output(wait) 死等它、**不要**因为「它在跑但没输出」重开
     · 有新事件：它静默 N 秒（默认 8s）后自己唤醒你 → 新一轮的对话里直接带事件清单（IMG/WX 行）
     · 没新事件：它什么都不做（不推送、不唤醒），这是设计，不是卡死
3) 只有两种情况才重开：① `job_list` 显示那个 job 已经 completed（脚本崩了），② 用户明确要求重启
```

推送失败不会丢：脚本把事件留在账本里，5 秒后重试；推不出去就一直在（重启后也会补推）。

**没插件时退回阻塞模式**（`curl -s http://127.0.0.1:3080/relay/health` 不通、或启动横幅写「推送未启用」）：

```
1) bash({ command: "$PY $OA/wait_events.py", run_in_background: true })   → 拿到 job_id
2) 反复 job_output(job_id, wait: true)：running = 继续等（正常）；completed = 取输出，进入步骤 2
3) 处理完必须**再起一轮**（见步骤 5）
```

如果运行时没有后台任务，就在前台跑，并把 `timeoutMs` 设成运行允许的最大值；
万一被超时杀掉，**直接再跑一次就行**：账本保证已打印过的不重复打印，而被杀掉那一轮
**已经打印但没来得及被你看到**的，会在下次运行的 `REPLAY` 行里补给你（见 §3）。

- 想看单轮快照（调试/自测）才用：`$PY $OA/wait_events.py --once`（`--push` 下它会扫完就推、推完即退）
- 输出里只有以 `IMG ` / `WX ` 开头的行才是事件；横幅与结尾统计忽略即可。
- 推送模式横幅长这样：`模式: 常驻推送（不空闲退出）` + `推送: http://127.0.0.1:3080/relay/wake → 会话 session-…`
  + `静默去抖 8.0s`。看到这三行就说明插件通道是活的。
- **同一时刻只有一个等待脚本**：脚本带独占登记，新会话启动会自动接管。被接管的旧脚本会**静默退出**
  （提示「事件流已被 … 接管」，退出码 4；再启动则码 3 且提示「本会话已被 … 接管」）——
  看到这两条就**不要重启本脚本**，这个会话已经不是监听者，事件交给当前会话即可，一条都不会丢。

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

```bash
B=$OA/work/$(date +%Y%m%d_%H%M%S) && mkdir -p $B   # 记下这个绝对路径；$B 同样不跨 bash 调用保留
```

- **算法题**：写 `$B/solution.<ext>`，然后真的跑样例：
  - ACM 模式：把样例输入存 `$B/t1.in`，直接跑并比对输出——Go：`go run $B/solution.go < $B/t1.in`；
    Python：`$PY $B/solution.py < $B/t1.in`；C++：`g++ -O2 -o $B/a $B/solution.cpp && $B/a < $B/t1.in`。
  - 核心代码模式：写个 `$B/run_tests.py` 直接调用你的函数/类，断言所有样例。
  - **全部样例通过**才继续；不过就改，改完再跑（最多绕 3 轮，仍不过就如实告诉用户卡在哪）。
- **非算法题**：直接在脑子里核对选项/空；不确定的，把依据写清楚。

### 算法题语言要求（硬规则，按平台判定口径）

**Go —— 唯一有硬性写法的语言**（别的语言暂时不管，按各自常规写法即可）：

| | 必须这样 | 不要这样 |
|---|---|---|
| 输入 | `in := bufio.NewReader(os.Stdin)` + `fmt.Fscan(in, &x)` / `in.ReadString('\n')` | `fmt.Scan(&x)`：每次无缓冲读，q=2×10⁵ 时实测 1.34s，有超时风险 |
| 输出 | `fmt.Println(...)` / `fmt.Print(...)` / `fmt.Printf(...)` | `bufio.NewWriter(os.Stdout)` + `Flush()`、`os.Stdout.Write(...)`：**平台检测不到**，会被判成没输出 |

```go
in := bufio.NewReader(os.Stdin)
var q int
fmt.Fscan(in, &q)
for ; q > 0; q-- {
    var x int
    fmt.Fscan(in, &x)
    fmt.Println(x)        // ← 一律走 fmt.Print 系列；需要拼行就用 strings.Builder 拼好再一次 fmt.Print
}
```

> 这条是**用户明确要求**的判题口径：Go 写的解，输入必须缓冲读、输出必须 `fmt.Print` 系列。
> 发代码图和源码文件前自查一遍：`grep -n "bufio.NewWriter\|os.Stdout.Write\|fmt.Scan(" solution.go` 不该有命中。

### 步骤 4 — 发到微信（+ 顺手铺到 VSCode 底框）

先铺 VSCode 底框（本地、2 秒超时、失败闭嘴），再发微信（主通道）。用户在编辑器里刷题时，
底框能让他不掏手机就看到答案；微信照发不误。

```bash
# ① 铺到编辑器底部「答题板」——best-effort
$PY $OA/vscode_notify.py push --title "第 3 题" --text "第 3 题（单选）｜答案：B
简析：……"

# ② 发微信（主通道）
$PY $OA/wechat_gateway.py send --text "第 3 题（单选）｜答案：B
简析：……"
```

- 正文长或带代码 → 先落盘再 `--file`，别在命令行里塞多行：

  ```bash
  # 算法题推底框的版本 = 思路 + 完整代码，围栏包住即可（底框只有文本，不需要代码图）
  $PY $OA/vscode_notify.py push --title "T1 · 两数之和" --file $B/answer.md
  ```

- `--title` 写图里的**题号原文**；一题一条，多题按题号依次推。
- 退出码：`0` 推到了 / `2` 编辑器没开或没装扩展 / `3` 连上了但被拒 —— 后两种**都是正常现象，
  直接跳过继续解题**，不要重试、不要提醒用户装扩展。想看细节才用 `$PY $OA/vscode_notify.py doctor`。

然后发微信：

```bash
# 非算法题
$PY $OA/wechat_gateway.py send --text "第 3 题（单选）｜答案：B
简析：……"

# 算法题（顺序：思路文字 → 代码图 → 源码文件）
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

发送成功判定：命令输出 `[结果] 成功 …`（退出码 0）。失败见 §5。
**注意**：不要向用户提问或给出选项卡，用户发现长时间没有收到微信消息，会在微信中输入消息，激活通信，在此期间，你保持阻塞脚本运行并等待

### 步骤 5 — 回到等待

- **推送模式（默认）**：什么都不用做——脚本还在跑，处理完这批事件就结束本轮；
  下一批事件到来时它会自己唤醒你。**不要重启脚本**（除非 `job_list` 里那个 job 已经不在了）。
- **阻塞模式（备用）**：先把本轮没处理完的事件处理完，**立刻**再起一轮等待（步骤 1 的备用写法）。

两边都一样：不要问用户「还要继续吗」，不要打印总结后就停下。

## 3. 事件格式与解析

```
IMG /abs/path/shots/20260916_120705_542.png                 # 新截图
IMG /abs/path/shots/20260916_115900_777.png REPLAY          # 上一轮被中断，补打给你的
WX 帮我看看这题                                             # 微信消息：只有用户说的话
WX 这题用 Java 再写一遍 [图片] /abs/.../wechat_media/a.jpg   # 带附件时给本地路径
WX 在吗 [chat=xxx@im.wechat]                                # 只有非默认会话才带 chat 提示
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

**算法题**（三条，顺序固定）：

1. 文本：题目名 + 模式（ACM/核心代码）+ 语言 + 3~6 行思路 + 复杂度 + 样例验证结论
2. 代码图：`code_image.py` 渲染（深色、带行号、标题写题号，页脚写复杂度）
3. 文件：`solution.<ext>`（方便用户在电脑上直接跑）

```bash
# 三级降级：图片失败 → 只发文件；文件也失败 → 只发代码片段
$PY $OA/wechat_gateway.py send --text "…思路…" --image $B/code.png --file $B/solution.go
$PY $OA/wechat_gateway.py send --text "…思路…" --file $B/solution.go
$PY $OA/wechat_gateway.py send --text "…思路…

\`\`\`go
<完整代码>
\`\`\`"
```

代码片段走文本时用围栏包起来（网关会按代码块边界切片，不会把围栏切成两半）。

## 5. 异常处理手册

| 现象 | 处理 |
|---|---|
| `send` 报 **会话尚未建立 / prepare failed** | 让用户在微信里先给机器人发一条消息（随便一个字），再重发。机器人不能对没打过招呼的人主动开口 |
| `send` 报 **频率限制（ret=-2）** | 等 10~30s 再发；把长内容合并成一条，别连发多条 |
| `send` 报 **HTTP 4xx/网络错误** | `$PY $OA/wechat_gateway.py doctor`：token 失效就 `$PY $OA/wechat_gateway.py login --new` 重新扫码（**二维码必须用户本人扫**，Agent 不能代扫）|
| 用户说「这是别人的包 / 我要用自己的微信」 | 跑 `$PY $OA/wechat_gateway.py login --new`：它会申请**全新二维码**，扫完就是用户自己的机器人；不加 `--new` 只会沿用包里原有的身份 |
| 图片发出去是**灰块** | 说明 aes_key 参数被改坏了。别自己改协议，恢复 `ilink_client.py` 后重发 |
| `wait_events --push` 起来了但一直没被唤醒 | **正常**：没有新事件它就不推、不唤醒。别重启、别改成 `--once` 去轮询试探；确认截图端/网关在跑即可（`/health`、`job_list` 里那个 job 还是 running） |
| `--push` 横幅写「推送未启用：…」 | 推送通道没配好（缺 url/会话 id）。先 `curl -s http://127.0.0.1:3080/relay/health` 看插件在不在；不在就退回阻塞模式（步骤 1 备用），并告诉用户需要装/启 `relay-wake` 插件 |
| 日志出现「推送失败（…）：N 条事件保留…」 | 不用管，脚本 5 秒后自己重试；连续失败才需要看：`404 session-not-live` = 目标会话的 agent 不在了（在 GUI 里打开该会话即可恢复），`401 bad-token` = token 与插件配置不一致 |
| `job_list` 里那个 `--push` job 已经是 **completed** | 脚本崩了（唯一需要重开的信号）。看它的输出定位原因，然后按步骤 1 重开一次 |
| `wait_events` 一直没输出（阻塞模式，几分钟甚至几十分钟） | **正常**：零事件时它就是要一直阻塞。确认网关活着（`/health` 的 `count` 是否增长）与截图端是否在跑即可；**不要**改短超时、不要重开 |
| 图片读不出来（坏文件/半截） | 跳过它，继续处理其余事件；在图里说明「这张图读取失败，麻烦重截」 |
| 截图里**没有题目**（截到了桌面/聊天窗/文档） | 发一条微信说明「没识别到题目，请把题面放到屏幕可见处再按一次」，然后**立刻回去等待**，不要假装答过 |
| 用户在微信里发的是**纯聊天/催问** | 直接回，不用走解题流程；同时继续等待 |
| 网关挂了 | `$PY $OA/wechat_gateway.py serve` 重新拉起（后台跑）；收件箱是追加写，重启不会丢消息 |
| 截图端端口被占 | `$PY $OA/start_capture.py --print-cmd` 会打印占用者 PID。默认自动避让并提示；要「就固定这个端口」把 `wait_events.json` 的 `capture.auto_port` 设 `false`（报错退出，绝不偷偷换）。局域网互传模式**永不自动换端口**（两端必须一致）|
| 改了端口后收不到图 | 端口只影响截图端；轮询脚本只看目录。确认 `capture.save_dir`（或 `shot_dirs[0]`）与轮询的 `shot_dirs` 是同一个目录即可 |
| `vscode_notify.py` 退出码 **2**（NO-PANEL） | **正常**：编辑器没开，或答题板扩展没装/没激活。跳过即可，**不要**提示用户去装、不要重试 |
| `vscode_notify.py` 退出码 **3** | 连上了但被拒（多半是注册文件里的令牌过期）。同样跳过；用户问起才让他跑 `doctor` |
| 用户说底框里没东西 | 先 `$PY $OA/vscode_notify.py status`：有面板说明通道正常，让用户点底部面板区的「答题板」标签（或命令面板「答题板: 打开面板」）；没面板就是编辑器没开 |
| bash 报 `[sandbox: file access denied]` | **正常解题不会遇到**：编译运行 Go/C++/Python、读写 `shots/`/`.state/`/`work/` 全在 `$OA/` 内。被拒 = 这条命令越界了，把产物/缓存指回 `$OA/work/`、临时文件用 `/tmp` 重跑即可。**不要**为此向用户提问或弹选项卡 |

## 6. 省上下文的纪律（很重要）

- 只读**本次新打印**的图；`read_image` 一张就够，不要重复读、不要读 `shots/` 里的历史图。
- 长代码/长推导**落盘到 `$B/`**，对话里只留结论（微信要发的内容 + 必要的 2~3 行理由）。
- 发完就忘：不要把发过的内容再复述一遍，直接回到等待。
- 一次等待返回多个事件时，**按顺序处理**，不要把多张图的推理过程混在一起。

## 7. 双向消息（用户在微信里提问）

`wait_events.py` 已经把两个源合成一次阻塞，所以**不需要你同时监听两个脚本**：

- 微信来的问题 → 当普通题目处理；回复**默认会话直接发**（不带 `--to`），只有该行带了 `[chat=…]` 才用 `--to <chat_id>` 发回那个会话。
- 用户连续发多条 → 可能一次等待里出现多条 `WX`，合并理解后再回一条完整答案。
- 需要长时间思考时，可以先 `$PY $OA/wechat_gateway.py typing --status start`
  让微信端显示「正在输入…」，发完再 `--status stop`（可选，默认发送时会自动带）。

## 8. 命令速查

```bash
P=/Users/fch/project/dp-workspace/.venv/bin/python; cd /Users/fch/project/dp-workspace/one-person-agent

$P start_capture.py                      # 起截图端（端口/目录读 wait_events.json 的 capture 段）
$P start_capture.py --print-cmd           # 看最终用哪个端口/目录，不启动
$P start_capture.py --port 6000           # 临时换端口（不改配置）
$P start_capture.py --no-auto-port        # 端口被占就报错退出，不自动避让
$P wechat_gateway.py serve               # 起微信网关（收发 + HTTP API）
$P wechat_gateway.py doctor              # 自检
$P wechat_gateway.py send --text "..."   # 发文本；--image/--file 可多次
$P wechat_gateway.py inbox --since 0     # 看收件箱
$P wechat_gateway.py chats               # 已知会话（拿 chat_id）
$P wait_events.py                        # 阻塞等待（备用模式：打印 → 空闲退出，靠 job 通知唤醒）
$P wait_events.py --push                 # 常驻推送（推荐：新事件静默 8s 后主动唤醒 Agent）
$P wait_events.py --push --debounce 15    # 改静默时长（持续有新事件就一直不推）
$P wait_events.py --status               # 看账本 + 推送计数
$P wait_events.py --once                 # 只扫一遍（调试；--push 下扫完就推、推完即退）
curl -s http://127.0.0.1:3080/relay/health   # relay-wake 插件：唤醒计数 / 活着的会话列表
$P code_image.py --in a.py --out a.png --lang python --title "T1"   # 代码转图片
$P vscode_notify.py push -t "第3题" -f $B/answer.md   # 铺到编辑器底部「答题板」
$P vscode_notify.py status                # 看有没有活着的面板（退出码 2 = 没有）
$P vscode_notify.py doctor                # 面板连通性诊断
$P vscode_notify.py clear                 # 清空面板
$P selftest.py                           # 全量离线自测

curl -s http://127.0.0.1:8799/health     # 网关状态
curl -s -X POST http://127.0.0.1:8799/send -H 'Content-Type: application/json' \
     -d '{"text":"hello","images":["/abs/code.png"]}'                # HTTP 方式发送
```

配置文件：`wait_events.json`（间隔 2s / 空闲 10s / 监听源 / **截图端 `capture` 段：port、peer、cooldown、auto_port、save_dir**）、
`wechat_gateway.json`（token、默认收件人、HTTP 端口）。
**VSCode 底框**（`vscode-answer-panel/`）不需要你启动：编辑器激活扩展后自己监听，端口和令牌写在
`~/.answer-panel/bridge.json`，`vscode_notify.py` 自动去读；它不在跑也不影响主流程。
Windows 上把路径换成对应形式即可，全部脚本都是跨平台的（无 shell 依赖、无 POSIX 专有调用）。
