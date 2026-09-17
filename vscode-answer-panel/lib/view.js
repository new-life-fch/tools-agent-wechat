'use strict';

const vscode = require('vscode');
const { renderHtml } = require('./html');

const VIEW_ID = 'answerPanel.main';

/** 底部面板（WebviewView）与编辑器标签备用视图，共用同一份 HTML 与消息协议。 */
class PanelViewProvider {
  constructor(store, getInfo) {
    this.store = store;
    this.getInfo = getInfo;
    this.view = null;
    this.panel = null;
    this._disposables = [];
  }

  /** 给 webview 的初始化数据 */
  _initPayload() {
    const info = this.getInfo();
    return { type: 'init', items: this.store.list(), info, count: this.store.count() };
  }

  _attach(webview, isPanel) {
    this._disposables.push(
      webview.onDidReceiveMessage((msg) => {
        if (!msg) return;
        if (msg.type === 'ready') {
          webview.postMessage(this._initPayload());
        } else if (msg.type === 'copy') {
          vscode.env.clipboard.writeText(String(msg.text == null ? '' : msg.text));
          vscode.window.setStatusBarMessage('答题板：已复制到剪贴板', 2000);
        } else if (msg.type === 'clear') {
          vscode.commands.executeCommand('answerPanel.clear');
        }
      })
    );
    return isPanel;
  }

  // ---- 底部面板 ----
  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [] };
    view.webview.html = renderHtml(view.webview, this.getInfo());
    this._attach(view.webview, false);
    view.onDidDispose(() => {
      if (this.view === view) this.view = null;
    });
  }

  // ---- 编辑器标签备用视图 ----
  openInEditor() {
    if (this.panel) {
      this.panel.reveal(undefined, true);
      this.panel.webview.postMessage(this._initPayload());
      return;
    }
    const panel = vscode.window.createWebviewPanel('answerPanel.editor', '答题板', vscode.ViewColumn.Beside, {
      enableScripts: true,
      retainContextWhenHidden: true
    });
    this.panel = panel;
    panel.webview.html = renderHtml(panel.webview, this.getInfo());
    this._attach(panel.webview, true);
    panel.onDidDispose(() => {
      if (this.panel === panel) this.panel = null;
    });
  }

  post(msg) {
    if (this.view) this.view.webview.postMessage(msg);
    if (this.panel) this.panel.webview.postMessage(msg);
  }

  async reveal() {
    if (this.view) {
      try {
        this.view.show(true); // true = 保留焦点，不抢你正在输入的终端
        return;
      } catch (_) {
        /* 某些分支版本可能没有 show() */
      }
    }
    try {
      await vscode.commands.executeCommand(`${VIEW_ID}.focus`);
    } catch (_) {
      /* 面板容器不可用时静默忽略 */
    }
  }

  dispose() {
    for (const d of this._disposables) {
      try {
        d.dispose();
      } catch (_) {}
    }
    this._disposables = [];
  }
}

module.exports = { PanelViewProvider, VIEW_ID };
