---
name: study-wechat-relay
description: 学习场景「截图 → 解题 → 微信回传」中继循环。用户在前台按 Ctrl+` 截图（或直接在微信里给机器人发消息），Agent 用阻塞脚本等待新事件 → 读图识题 → 解题验证 → 把答案发回微信 → 再回到阻塞等待，如此往复。Use when the user asks for 刷题助手 / 截图答题 / 答案发微信 / 学习中继 / 帮我盯着截图和微信 / study relay.
metadata: { "tags": "study, relay, wechat, screenshot, blocking-wait, exam" }
---

# study-wechat-relay — 学习场景截图答题中继

一套已经落地的工具链 + 编排纪律。所有脚本都在 `/Users/fch/project/dp-workspace/one-person-agent/`，
**不要重写、不要自己写轮询循环**，直接用下面的命令。

```
用户按 Ctrl+` 截图 ──► shots/*.png ─┐
                                    ├─► wait_events.py（阻塞）──► Agent 识图解题 ──► wechat_gateway.py ──► 微信
用户在微信里发消息 ──► 收件箱 JSONL ─┘                                ▲                                    │
                                                                      └────────── 再次阻塞等待 ◄────────────┘
```

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
4. **算法题必须先跑通样例再发。** 没验证过的代码不许发微信。
5. **发微信按三级降级**：代码图 → 文件 → 代码片段（文本），前一级失败才降级。
6. **一轮处理完立刻回到阻塞等待**，不要在两次等待之间插入 `sleep` 轮询或短超时试探。
7. **不要自己在代码里做 LLM 路由**：双向消息由「你这个 Agent」处理，脚本只负责搬运。
8. **图就是唯一信息源**：整图读一遍即可，**不要裁剪/放大局部反复看，也不要上网搜原题**——
   图里的题面/样例/语言/模式已经够了，搜来的题可能跟图不符，反而答错（除非用户明说「搜一下」）。

## 2. SOP（每一轮都走这五步）

### 步骤 1 — 阻塞等待（唯一的等待方式）

优先用后台任务 + 阻塞读取（DSH / 支持后台 job 的运行时）：

```
1) bash({ command: "$PY $OA/wait_events.py", run_in_background: true })   → 拿到 job_id
2) 立刻进入等待循环：反复 job_output(job_id, wait: true)
     · 返回 [status: running]  → 继续调，这是正常现象，不是失败
     · 返回 [status: completed] → 取本轮输出，进入步骤 2
3) 整轮期间：不要 job_kill、不要因为「等太久」重开一个、不要改用 --once 反复问
```

如果运行时没有后台任务，就在前台跑，并把 `timeoutMs` 设成运行允许的最大值；
万一被超时杀掉，**直接再跑一次就行**：账本保证已打印过的不重复打印，而被杀掉那一轮
**已经打印但没来得及被你看到**的，会在下次运行的 `REPLAY` 行里补给你（见 §3）。

- 想看单轮快照（调试/自测）才用：`$PY $OA/wait_events.py --once`
- **空闲退出的准确语义**：`--idle-timeout` 默认 10s，但计时**从打印出第一个事件之后才开始**。
  - 一个事件都没打印过 → **它永远不退出**，一直阻塞等第一次截图/第一条微信。此时屏幕上只有横幅、
    什么都没有，这是**完全正常**的，不是卡死：不要重开、不要加超时、不要改成 `--once` 去轮询试探。
  - 打印过事件之后 → 最后一条事件之后空闲 10s 就安全退出。
  - 只有想给整轮加个总时长保险时才用 `--max-wait <秒>`（默认 0 = 不限，不用动；它是零事件阻塞时唯一的自动退出口）。
- 超时退出不会丢事件：下次运行会把没处理完的以 `REPLAY` 补给你。
- 输出里只有以 `IMG ` / `WX ` 开头的行才是事件；横幅与结尾统计忽略即可。
- **同一时刻只有一个等待脚本**：脚本带独占登记，新会话启动会自动接管。被接管的旧脚本会**静默退出**
  （提示「事件流已被 … 接管」，退出码 4；再启动则码 3 且提示「本会话已被 … 接管」）——
  看到这两条就**不要重启本脚本**，这个会话已经不是监听者，事件交给当前会话即可，一条都不会丢。
  脚本空闲退出后 10 分钟内事件流仍保留给本会话（防止两个会话在"解题空窗期"互相抢），
  确实要强行换会话监听才用 `--force-owner`。

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

### 步骤 4 — 发到微信

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

### 步骤 5 — 回到步骤 1

只要本轮还有没处理完的事件（一次等待可能返回多张图 / 多条消息），先把它们处理完，
然后**立刻**再次进入步骤 1 的阻塞等待。不要问用户「还要继续吗」，不要打印总结后就停下。

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
| `wait_events` 一直没输出（几分钟甚至几十分钟） | **正常**：零事件时它就是要一直阻塞。确认网关活着（`/health` 的 `count` 是否增长）与截图端是否在跑即可；**不要**改短超时、不要重开 |
| 图片读不出来（坏文件/半截） | 跳过它，继续处理其余事件；在图里说明「这张图读取失败，麻烦重截」 |
| 截图里**没有题目**（截到了桌面/聊天窗/文档） | 发一条微信说明「没识别到题目，请把题面放到屏幕可见处再按一次」，然后**立刻回去等待**，不要假装答过 |
| 用户在微信里发的是**纯聊天/催问** | 直接回，不用走解题流程；同时继续等待 |
| 网关挂了 | `$PY $OA/wechat_gateway.py serve` 重新拉起（后台跑）；收件箱是追加写，重启不会丢消息 |
| 截图端端口被占 | `$PY $OA/start_capture.py --print-cmd` 会打印占用者 PID。默认自动避让并提示；要「就固定这个端口」把 `wait_events.json` 的 `capture.auto_port` 设 `false`（报错退出，绝不偷偷换）。局域网互传模式**永不自动换端口**（两端必须一致）|
| 改了端口后收不到图 | 端口只影响截图端；轮询脚本只看目录。确认 `capture.save_dir`（或 `shot_dirs[0]`）与轮询的 `shot_dirs` 是同一个目录即可 |

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
$P wait_events.py                        # 阻塞等待（默认配置）
$P wait_events.py --status               # 看账本
$P wait_events.py --once                 # 只扫一遍（调试）
$P code_image.py --in a.py --out a.png --lang python --title "T1"   # 代码转图片
$P selftest.py                           # 全量离线自测

curl -s http://127.0.0.1:8799/health     # 网关状态
curl -s -X POST http://127.0.0.1:8799/send -H 'Content-Type: application/json' \
     -d '{"text":"hello","images":["/abs/code.png"]}'                # HTTP 方式发送
```

配置文件：`wait_events.json`（间隔 2s / 空闲 10s / 监听源 / **截图端 `capture` 段：port、peer、cooldown、auto_port、save_dir**）、
`wechat_gateway.json`（token、默认收件人、HTTP 端口）。Windows 上把路径换成对应形式即可，
全部脚本都是跨平台的（无 shell 依赖、无 POSIX 专有调用）。
