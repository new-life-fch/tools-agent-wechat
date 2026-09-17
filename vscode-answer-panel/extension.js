'use strict';

const vscode = require('vscode');
const path = require('path');
const os = require('os');

const { Store } = require('./lib/store');
const registry = require('./lib/registry');
const bridge = require('./lib/bridge');
const { PanelViewProvider, VIEW_ID } = require('./lib/view');

const STATE_KEY = 'answerPanel.snapshot';
const STARTED_AT = Date.now();

let store = null;
let provider = null;
let server = null;
let serverInfo = null; // { host, port, token }
let bridgeFile = '';
let output = null;

function cfg() {
  const c = vscode.workspace.getConfiguration('answerPanel');
  return {
    port: c.get('port', 5178),
    autoReveal: c.get('autoReveal', true),
    requireToken: c.get('requireToken', true),
    bridgeFile: c.get('bridgeFile', ''),
    maxItems: c.get('maxItems', 200),
    persist: c.get('persist', true)
  };
}

function resolveBridgeFile(c) {
  const raw = c.bridgeFile && String(c.bridgeFile).trim();
  if (raw) return path.resolve(String(raw).replace(/^~(?=$|[\\/])/, os.homedir()));
  return registry.DEFAULT_FILE;
}

function workspacePaths() {
  return (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
}

function endpoint() {
  return serverInfo ? `${serverInfo.host}:${serverInfo.port}` : '';
}

/** 面板 / 状态查询用的公开信息 */
function infoFn() {
  const c = cfg();
  return {
    endpoint: endpoint(),
    app: vscode.env.appName || 'VSCode',
    appHost: safeAppHost(),
    vscode: vscode.version,
    bridgeFile,
    requireToken: !!c.requireToken,
    items: store ? store.count() : 0
  };
}

function safeAppHost() {
  try {
    return vscode.env.appHost || '';
  } catch (_) {
    return '';
  }
}

/** 写进端口注册文件的自身条目 */
function selfEntry() {
  const c = cfg();
  return {
    pid: process.pid,
    port: serverInfo ? serverInfo.port : null,
    token: serverInfo ? serverInfo.token : null,
    app: vscode.env.appName || 'VSCode',
    app_host: safeAppHost(),
    vscode: vscode.version,
    workspace: workspacePaths().join(path.delimiter),
    require_token: !!c.requireToken,
    endpoint: endpoint(),
    started_at: STARTED_AT,
    updated_at: Date.now()
  };
}

function healthInfo() {
  // 注意：这里**不能**出现 app 字段。health 响应里的 app 是协议标识（固定 "answer-panel"，
  // 由 bridge.js 盖章），推送方靠它确认「这个端口确实是我们」。编辑器名字放 editor。
  return {
    port: serverInfo ? serverInfo.port : null,
    pid: process.pid,
    editor: vscode.env.appName || 'VSCode',
    vscode: vscode.version,
    items: store ? store.count() : 0,
    seq: store ? store.seq : 0,
    workspace: workspacePaths().join(path.delimiter),
    require_token: cfg().requireToken
  };
}

function log(line) {
  if (!output) output = vscode.window.createOutputChannel('答题板');
  output.appendLine(`[${new Date().toLocaleTimeString()}] ${line}`);
}

async function startServer() {
  const c = cfg();
  const started = await bridge.start({
    port: c.port,
    token: serverInfo ? serverInfo.token : bridge.newToken(),
    requireToken: c.requireToken,
    handlers: {
      health: healthInfo,
      state: () => ({ count: store.count(), items: store.list() }),
      push: (payload) => {
        const item = store.push({
          id: payload.id,
          title: payload.title,
          text: payload.text,
          source: payload.source || 'http'
        });
        if (c.autoReveal && provider) provider.reveal();
        log(`push #${item.seq}${item.title ? ` 「${item.title}」` : ''} ${item.text.length} 字 → 共 ${store.count()} 条`);
        return { seq: item.seq, count: store.count(), port: started.port };
      },
      clear: () => {
        const n = store.clear();
        log(`clear → 清掉 ${n} 条`);
        return { cleared: n, port: started.port };
      }
    }
  });

  if (server) {
    try {
      server.close();
    } catch (_) {}
  }
  server = started.server;
  serverInfo = { host: started.host, port: started.port, token: started.token };
  log(`HTTP 监听 http://${started.host}:${started.port}（令牌${c.requireToken ? '已启用' : '已关闭'}）`);
  return started;
}

function writeRegistry() {
  try {
    registry.register(bridgeFile, selfEntry());
  } catch (err) {
    log(`写注册文件失败：${err && err.message}`);
    vscode.window.showWarningMessage(`答题板：无法写入 ${bridgeFile} — ${err && err.message}`);
  }
}

function shutdownServer() {
  try {
    registry.unregister(bridgeFile, process.pid);
  } catch (_) {}
  if (server) {
    try {
      server.close();
    } catch (_) {}
    server = null;
  }
}

async function activate(context) {
  output = vscode.window.createOutputChannel('答题板');
  const c = cfg();
  bridgeFile = resolveBridgeFile(c);

  store = new Store(c.maxItems);
  if (c.persist) {
    try {
      store.restore(context.workspaceState.get(STATE_KEY));
    } catch (_) {}
  }

  provider = new PanelViewProvider(store, infoFn);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(VIEW_ID, provider, {
      webviewOptions: { retainContextWhenHidden: true }
    })
  );

  try {
    await startServer();
  } catch (err) {
    log(`监听失败：${err && err.stack}`);
    vscode.window.showErrorMessage(`答题板：无法监听端口 — ${err && err.message}`);
  }
  writeRegistry();

  // 内容变化 → 推给 webview + 落盘持久化
  let saveTimer = null;
  const stopWatch = store.onDidChange((ev) => {
    try {
      provider.post(Object.assign({ info: infoFn(), count: store.count() }, ev));
    } catch (err) {
      log(`推送 webview 失败：${err && err.message}`);
    }
    if (!cfg().persist) return;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      try {
        context.workspaceState.update(STATE_KEY, store.snapshot());
      } catch (_) {}
    }, 800);
  });

  context.subscriptions.push(
    { dispose: () => (saveTimer ? clearTimeout(saveTimer) : undefined) },
    { dispose: stopWatch },
    vscode.commands.registerCommand('answerPanel.focus', () => provider.reveal()),
    vscode.commands.registerCommand('answerPanel.openInEditor', () => provider.openInEditor()),
    vscode.commands.registerCommand('answerPanel.clear', () => {
      const n = store.clear();
      vscode.window.setStatusBarMessage(`答题板：已清空 ${n} 条`, 2000);
    }),
    vscode.commands.registerCommand('answerPanel.copyAll', async () => {
      await vscode.env.clipboard.writeText(store.toPlainText());
      vscode.window.setStatusBarMessage('答题板：已复制全部', 2000);
    }),
    vscode.commands.registerCommand('answerPanel.showInfo', () => {
      const s = selfEntry();
      output.clear();
      output.appendLine(
        [
          `应用          ：${s.app}${s.app_host ? ` (${s.app_host})` : ''}`,
          `VSCode 内核   ：${s.vscode}`,
          `监听地址      ：http://${endpoint()}`,
          `令牌          ：${s.require_token ? s.token : '（未启用）'}`,
          `注册文件      ：${bridgeFile}`,
          `工作区        ：${s.workspace || '（未打开文件夹）'}`,
          `消息数        ：${store.count()}`,
          `PID           ：${s.pid}`,
          ``,
          `推送示例      ：`,
          `  curl -X POST http://${endpoint()}/push \\`,
          `    -H "X-Answer-Panel-Token: ${s.token || ''}" \\`,
          `    -H "Content-Type: application/json" \\`,
          `    -d '{"title":"第1题","text":"答案正文"}'`
        ].join('\n')
      );
      output.show(true);
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration('answerPanel')) return;
      const nc = cfg();
      const oldFile = bridgeFile;
      bridgeFile = resolveBridgeFile(nc);
      if (store) store.limit = nc.maxItems > 0 ? Math.floor(nc.maxItems) : 200;
      if (oldFile !== bridgeFile) {
        try {
          registry.unregister(oldFile, process.pid);
        } catch (_) {}
      }
      const needsRestart = e.affectsConfiguration('answerPanel.port') || e.affectsConfiguration('answerPanel.requireToken');
      const done = () => writeRegistry();
      if (needsRestart) {
        startServer()
          .then(done)
          .catch((err) => log(`重开监听失败：${err && err.message}`));
      } else {
        done();
      }
    }),
    { dispose: shutdownServer }
  );

  log(`答题板启动：${vscode.env.appName} ${vscode.version}，注册文件 ${bridgeFile}`);
}

function deactivate() {
  shutdownServer();
  if (provider) provider.dispose();
}

module.exports = { activate, deactivate };
