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
  -d '{"session":"session-xxxx","text":"S 2026-09-19_13-20-01.png\nWX 你好"}'
```

返回：`200 {"ok":true,"woke":true,"session":...,"status":"idle","wakes":12}`
错误码：`401 bad-token` / `403 forbidden-host`（Host 不是回环）/ `400 missing-session|missing-text|bad-json`
/ `404 session-not-live`（该会话没有活着的 agent）/ `405 method-not-allowed` / `429 wake-budget-exhausted|too-soon` / `500 followup-failed`。

## 配置键（patch 行里的 `config`）

| 键 | 默认 | 说明 |
|---|---|---|
| `token` | `''` | 非空时要求请求头 `x-relay-token` 完全一致 |
| `stateFile` | 插件目录下 `state.json` | 可观测性状态文件（写失败不影响唤醒） |
| `maxWakesPerHour` | `120` | 滚动一小时窗口的唤醒上限，`0` = 不限 |
| `minIntervalSec` | `0` | 两次唤醒最小间隔秒数 |
| `maxTextChars` | `12000` | 单条正文上限，超出截断并标注 |
| `dryRun` | `false` | 只校验不投递（联调脚本时用） |

## 载入方式

**A. 免安装（推荐先这么用）**——在 `~/.dsh/profiles/web/cordis.patch.yml` 里 insert 一行，
`name` 写本目录 `index.js` 的**绝对路径**：

```yaml
- insert:
    - id: relay-wake
      name: /abs/path/to/dsh-plugin-relay-wake/index.js
      config:
        token: <随机 hex>
```

profile patch 是 **live 层**（`watchUserPatches`），**保存即热载，不用重启 `dsh web`**；
改坏也只是这一次重放被拒，运行中的 app 不受影响，把文件改回即可。

**B. 正规安装成 bundle（需重启一次）**：

```sh
cd <deepseek-harness 检出目录>
pnpm dsh plugin --profile web add /abs/path/to/dsh-plugin-relay-wake
pnpm dsh web        # dsh.profile.bundles 只在 boot 时读
```

`package.json` 里的 `dsh.bundle.patch` 让 `dsh plugin add` 自动把本包追加进 profile 的
`dsh.profile.bundles`；`cordis.patch.yml` 用**包名**引用入口。A 与 B 二选一，别同时留
（同一行 id 会撞）。

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
