# dsh-plugin-relay-wake

让**外部常驻脚本**的一次推送变成一次**真正的唤醒**——空闲会话也会立刻开一个新 turn。

## 为什么需要它

DSH 的后台任务结算通知走 `tool-jobs` 的 `maxConsecutiveWakes` 预算（默认连续 3 次）：
预算用完后通知降级为 `inject`，只进 next-step 收件箱、**不唤醒**。于是「脚本退出 → 唤醒
Agent → Agent 重启脚本」这条链在第 4 次就会静默断掉，空闲会话长时间没有任何监听者
（曾实测出现 47 分 54 秒空窗）。

本插件开一条独立通道，不受该预算约束：

```
常驻脚本 --HTTP--> POST /relay/wake --> ctx.agents.get(sessionId) --> Agent.followup(message)
```

`followup` 的语义（`packages/core/agent/src/runtime-types.ts`）：会话空闲时**必然**开一个新
turn；正在跑时排一个后续 turn，不会丢。

## 接口

只挂在**本机回环**上，复用 web GUI 自己的端口（默认 3080），不新开端口。

```sh
# 状态：插件版本、唤醒计数、活着的会话列表
curl -s http://127.0.0.1:3080/relay/health

# 唤醒：session 就是 DSH_SESSION_ID；text 是给模型看的事件正文
curl -s -X POST http://127.0.0.1:3080/relay/wake \
  -H 'content-type: application/json' \
  -H 'x-relay-token: <token>' \
  -d '{"session":"session-xxxx","text":"IMG /abs/shots/a.png\nWX 你好"}'
```

返回：`200 {"ok":true,"woke":true,"session":...,"status":"idle","wakes":12}`
错误码：`401 bad-token` / `403 forbidden-host`（Host 不是回环）/ `400 missing-session|missing-text|bad-json`
/ `404 session-not-live`（该会话没有活着的 agent）/ `405 method-not-allowed` / `429 wake-budget-exhausted|too-soon` / `500 followup-failed`。

## 配置键（patch 行 / bundle 行里的 `config`）

| 键 | 默认 | 说明 |
|---|---|---|
| `token` | `''` | 非空时要求请求头 `x-relay-token` 完全一致（**必须与 `wait_events.json` 的 `push.token` 相同**） |
| `stateFile` | 插件目录下 `state.json` | 可观测性状态文件（写失败不影响唤醒） |
| `maxWakesPerHour` | `120` | 滚动一小时窗口的唤醒上限，`0` = 不限 |
| `minIntervalSec` | `0` | 两次唤醒最小间隔秒数 |
| `maxTextChars` | `12000` | 单条正文上限，超出截断并标注 |
| `dryRun` | `false` | 只校验不投递（联调脚本时用） |

## 安装（macOS / Windows 通用）

先生成 token，两个地方要用同一个值（插件 config + 中继脚本配置）：

```sh
python -c "import secrets;print(secrets.token_hex(16))"
```

### 方式 A：官方 bundle 安装（可分发；需要重启一次 `dsh web`）

```sh
# ① 在本机把本目录放到任意位置（跟着 one-person-agent 仓库走即可）
#    macOS:   /Users/you/project/one-person-agent/dsh-plugin-relay-wake
#    Windows: C:/work/one-person-agent/dsh-plugin-relay-wake

# ② 在 DSH 检出目录里安装（`dsh` 已全局安装的话去掉 pnpm 前缀）
cd /path/to/deepseek-harness                     # Windows: cd C:\work\deepseek-harness
pnpm dsh plugin --profile web add /Users/you/project/one-person-agent/dsh-plugin-relay-wake
# Windows 用正斜杠路径，同一条命令：
pnpm dsh plugin --profile web add C:/work/one-person-agent/dsh-plugin-relay-wake

# ③ 验证清单（会自动追加进 dsh.profile.bundles）
cat ~/.dsh/profiles/web/package.json             # Windows: type %USERPROFILE%\.dsh\profiles\web\package.json

# ④ 重启（bundles 只在 boot 时读）
pnpm dsh web
```

装上之后用 **id 定向覆盖**配 token（写进 `~/.dsh/profiles/web/cordis.patch.yml`；
Windows: `%USERPROFILE%\.dsh\profiles\web\cordis.patch.yml`）：

```yaml
- id: relay-wake
  config:
    token: '<上面生成的 token>'
    maxWakesPerHour: 120        # ⚠ config 是整键覆盖、不深合并：需要的键都要重述一遍
```

这个 patch 文件是 **live 层**（保存即热载），改 token 不用再重启。

卸载：`pnpm dsh plugin --profile web remove dsh-plugin-relay-wake`（同样要重启才生效）。

### 方式 B：零安装热载（本机自用最省事；**不用重启**）

直接在 `~/.dsh/profiles/web/cordis.patch.yml` 里 insert 一行，`name` 写 `index.js` 的
**绝对路径**（Windows 用 `C:/...` 正斜杠写法）：

```yaml
- insert:
    - id: relay-wake
      name: /Users/you/project/one-person-agent/dsh-plugin-relay-wake/index.js
      # Windows: C:/work/one-person-agent/dsh-plugin-relay-wake/index.js
      config:
        token: '<上面生成的 token>'
        maxWakesPerHour: 120
```

保存即生效（实测几秒内插件就活了）。回滚：删掉这一段即可；改坏也只是这一次重放被拒，
运行中的 app 不受影响。

### ⚠️ 两种方式二选一，别同时留

bundle 行和 patch insert 行的 `id` 都是 `relay-wake`，**同时存在会在下次启动时撞 id**。
从 B 切到 A 的正确顺序：

1. `pnpm dsh plugin --profile web add <路径>`（此时运行中的实例仍在用 patch 行，不受影响）
2. 把 `cordis.patch.yml` 里那段 `insert` 删掉（保存后插件会被卸载，直到重启）
3. `pnpm dsh web` 重启

重启会**杀掉当时的后台任务**：起来后重新拉起截图端、微信网关和 `wait_events.py --push`。

## 双平台注意事项

| 事项 | 说明 |
|---|---|
| 依赖 | 纯 JS、零依赖，只用 `node:crypto/fs/path/url` 内置模块，Windows 与 macOS 行为一致 |
| 路径 | 一律用正斜杠 `C:/work/...`；`path.resolve` / `pathToFileURL` 在 Windows 上同样识别 |
| 状态文件 | 写在插件目录下 `state.json`；目录只读时写失败**只影响可观测性**，唤醒照常 |
| 通道 | 复用 `dsh web` 的端口（默认 3080，随 `--port` 变）。中继脚本侧要同步改 `wait_events.json` 的 `push.url` |
| 验证命令 | macOS `curl`；Windows 用 `curl.exe`（Win10+ 自带）或 `Invoke-RestMethod` |
| 中继脚本 | `wait_events.py --push` 是纯 Python 标准库（urllib），Windows 直接跑，无需额外依赖 |
| 防火墙 | 只监听回环、不新开端口，不触发 Windows 防火墙弹窗 |

## 设计要点

- **零依赖**：不 import 任何 `@deepseek-ai/*`，放在 dsh 安装树之外也能加载；消息对象按
  `createUserMessage` 的同形状手工构造（`{id, role:'user', content, source:{kind:'plugin'}}`），
  并深冻结。
- **只认回环 Host**：挡掉 DNS rebinding 与跨站表单提交；再加可选 token 头（自定义头会触发
  CORS 预检，网页侧无法伪造）。
- **具名导出，绝不 default export**：default 保留给 Service 类，误用会静默丢掉 `inject`。
- **存活校验**：`ctx.agents.get(id)` 为空即 404（agent 已 dispose 时残留句柄的 followup 是
  静默无效的，所以必须先查）。
- **自建唤醒预算**：`maxWakesPerHour` 防外部脚本失控把会话打成自激链。
