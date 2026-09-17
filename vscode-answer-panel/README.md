# 答题板 Answer Panel

在编辑器**底部面板**里显示外部命令推来的文本，可自由上下滚动。
纯文本渲染：保留缩进、空行、代码块边界，**不做语法着色**。

适配任何 VSCode 系编辑器（VSCode / Qoder / Cursor / Trae / Windsurf / VSCodium）——只用稳定 API，
零运行时依赖，`.vsix` 只有几十 KB。

## 它解决什么问题

Agent 在后台解题，答案要送到你眼前。微信是异步的、要切窗口；这个插件把答案直接铺在
你正在看的编辑器底部，一屏滚动看完，不打断你手上的事。

## 架构

```
Agent / 脚本
   │  HTTP POST 127.0.0.1:<port>/push     （端口与令牌见 ~/.answer-panel/bridge.json）
   ▼
扩展内的本机 HTTP 服务（只监听回环地址）
   ▼
内存消息仓库 Store
   ▼
底部面板 WebviewView（可滚动 / 跟随最新 / 复制 / 清空）
```

* **端口注册文件** `~/.answer-panel/bridge.json`：`{ instances: [{pid, port, token, app, vscode, workspace}] }`。
  推送方按 pid 存活 + `/health` 探测筛选，多窗口时按工作区匹配。
* **令牌**：默认开启。注册文件里带着，本地进程读得到，网页 `fetch` 打不进来。
* **`/health` 不需要令牌**，用于探测。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探测（免令牌），返回 `{ok, app:"answer-panel", port, pid, vscode, items}` |
| POST | `/push` | 推一段文本。JSON `{title?, text, id?, source?}`；非 JSON 正文按纯文本收 |
| GET | `/state` | 当前全部消息 |
| POST | `/clear` | 清空 |

**同 `id` 再推 = 就地更新那一条**，适合「先给个思路 → 再替换成最终答案」。

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('$HOME/.answer-panel/bridge.json'))['instances'][0]['token'])")
PORT=$(python3 -c "import json;print(json.load(open('$HOME/.answer-panel/bridge.json'))['instances'][0]['port'])")

curl -X POST "http://127.0.0.1:$PORT/push" \
  -H "X-Answer-Panel-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"第3题","text":"答案正文"}'

# 也可以直接甩原始文本
curl -X POST "http://127.0.0.1:$PORT/push" \
  -H "X-Answer-Panel-Token: $TOKEN" \
  --data-binary @answer.md
```

## 安装

```bash
./scripts/build.sh              # 跑测试 + 打 .vsix
./scripts/install.sh            # 侧载进本机所有 VSCode 系编辑器
```

然后 **Reload Window**。面板出现在底部面板区的「答题板」标签；也能用命令面板：

| 命令 | 作用 |
|---|---|
| `答题板: 打开面板` | 展开底部面板 |
| `答题板: 清空` | 清空 |
| `答题板: 复制全部` | 全文复制到剪贴板 |
| `答题板: 显示服务信息` | 输出监听地址 / 令牌 / 注册文件路径 |
| `答题板: 在编辑器标签中打开（备用）` | 万一某个分支版本不认底部面板，用这个走编辑器区 |

## 设置

| 键 | 默认 | 说明 |
|---|---|---|
| `answerPanel.port` | `5178` | 起始端口，占用则 +1 顺延（最多试 20 个） |
| `answerPanel.autoReveal` | `true` | 收到推送自动展开面板（保留焦点，不抢你输入） |
| `answerPanel.requireToken` | `true` | 要求令牌 |
| `answerPanel.bridgeFile` | `""` | 自定义注册文件路径，留空 = `~/.answer-panel/bridge.json` |
| `answerPanel.maxItems` | `200` | 最多保留多少条 |
| `answerPanel.persist` | `true` | 窗口重载后保留内容 |

## 开发者

```bash
node test/offline.test.js      # 31 项离线测试，不需要 VSCode
```

测试覆盖 Store 语义、注册文件读写与死 pid 过滤、以及**真的用 Python CLI 打进真的 HTTP 服务**
（Python ↔ JS 契约）。

## 兼容性备注

* 只用 `statusBar` 之外的基础 API：`WebviewView`（1.49+）、`onStartupFinished`（1.75+）、
  `workspace.fs`、`env.clipboard`。`engines.vscode` 声明为 `^1.85.0`。
* **不用**任何 Microsoft 专有 / proposed API（`lm`、`chat`、`authentication`），
  因此在非微软市场（OpenVSX 等）的分支编辑器里同样能跑。
* 扩展宿主只 require `http` / `fs` / `path` / `os` / `crypto` 内置模块，无第三方依赖，
  不受各分支的 Node 版本差异影响。
