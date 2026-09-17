'use strict';

/**
 * 底部面板的 HTML。零外部资源、零依赖，只用内联样式与一段内联脚本。
 * 文本渲染原则：保留原样结构（缩进/空行/代码块），不做语法着色。
 */

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const STYLE = `
* { box-sizing: border-box; }
html, body {
  height: 100%; margin: 0; padding: 0;
  background: var(--vscode-panel-background, var(--vscode-editor-background));
  color: var(--vscode-foreground);
  font-family: var(--vscode-font-family, sans-serif);
  font-size: var(--vscode-font-size, 13px);
}
#app { display: flex; flex-direction: column; height: 100%; }

header {
  flex: 0 0 auto; display: flex; align-items: center; gap: 10px;
  padding: 5px 10px; border-bottom: 1px solid var(--vscode-panel-border);
  background: var(--vscode-panel-background, var(--vscode-editor-background));
  font-size: 12px; user-select: none;
}
header .title { font-weight: 600; }
header .meta { color: var(--vscode-descriptionForeground); font-family: var(--vscode-editor-font-family, monospace); font-size: 11px; }
header .spacer { flex: 1 1 auto; }
header button {
  font: inherit; font-size: 11px; padding: 2px 8px; cursor: pointer;
  color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
  background: var(--vscode-button-secondaryBackground, transparent);
  border: 1px solid var(--vscode-panel-border); border-radius: 3px;
}
header button:hover { background: var(--vscode-button-secondaryHoverBackground, var(--vscode-button-background)); }
header button.on { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border-color: transparent; }

#list { flex: 1 1 auto; overflow-y: auto; overflow-x: hidden; padding: 6px 0 14px; scrollbar-width: thin; }

.item { padding: 4px 12px 12px; border-bottom: 1px dashed var(--vscode-panel-border); }
.item:last-child { border-bottom: none; }
.item .head {
  display: flex; align-items: baseline; gap: 8px; margin: 2px 0 4px;
  font-size: 11px; color: var(--vscode-descriptionForeground); user-select: none;
}
.item .head .t { font-weight: 600; color: var(--vscode-foreground); font-size: 12px; }
.item .head .time { font-family: var(--vscode-editor-font-family, monospace); }
.item .head .spacer { flex: 1 1 auto; }
.item .head button {
  font: inherit; font-size: 10px; padding: 0 6px; cursor: pointer; opacity: .55;
  background: transparent; color: inherit; border: 1px solid var(--vscode-panel-border); border-radius: 3px;
}
.item .head button:hover { opacity: 1; }

pre {
  margin: 0; white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere;
  font-family: var(--vscode-editor-font-family, ui-monospace, Menlo, Consolas, monospace);
  font-size: var(--vscode-editor-font-size, 12px);
  line-height: 1.5; tab-size: 4;
}
pre.code {
  border-left: 3px solid var(--vscode-panel-border);
  background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.10));
  padding: 6px 10px; margin: 6px 0; border-radius: 2px;
}
pre.txt { padding: 0; }

#empty { padding: 18px 16px; color: var(--vscode-descriptionForeground); font-size: 12px; line-height: 1.8; }
#empty code {
  font-family: var(--vscode-editor-font-family, monospace);
  background: var(--vscode-textCodeBlock-background, rgba(127,127,127,.12));
  padding: 1px 5px; border-radius: 3px; user-select: text;
}
#empty b { color: var(--vscode-foreground); }
`;

const SCRIPT = `
const vscode = acquireVsCodeApi();
const list = document.getElementById('list');
const meta = document.getElementById('meta');
const btnFollow = document.getElementById('btn-follow');
let follow = true;

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// 只做两件事：把代码块圈出来，其余原样保留。不着色、不解析 markdown。
function renderText(text) {
  const lines = String(text == null ? '' : text).replace(/\\r\\n?/g, '\\n').split('\\n');
  let out = '', plain = [], code = [], inCode = false;
  const flushPlain = () => { if (plain.length) { out += '<pre class="txt">' + esc(plain.join('\\n')) + '</pre>'; plain = []; } };
  const flushCode = () => { if (code.length) { out += '<pre class="code">' + esc(code.join('\\n')) + '</pre>'; code = []; } };
  for (const line of lines) {
    if (/^\\s*(\`\`\`|~~~)/.test(line)) {
      if (inCode) { flushCode(); inCode = false; } else { flushPlain(); inCode = true; }
      continue;
    }
    if (inCode) code.push(line); else plain.push(line);
  }
  if (inCode) flushCode();
  flushPlain();
  return out || '<pre class="txt"></pre>';
}

function fmtTime(ts) {
  const d = new Date(ts || Date.now());
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function makeItem(item) {
  const el = document.createElement('article');
  el.className = 'item';
  el.dataset.seq = String(item.seq);
  if (item.id) el.dataset.id = item.id;
  const head = document.createElement('div');
  head.className = 'head';
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = item.title || '';
  if (!item.title) t.style.display = 'none';
  const time = document.createElement('span');
  time.className = 'time';
  time.textContent = fmtTime(item.createdAt);
  const sp = document.createElement('span');
  sp.className = 'spacer';
  const copy = document.createElement('button');
  copy.textContent = '复制';
  copy.title = '复制这一条';
  copy.onclick = () => vscode.postMessage({ type: 'copy', text: item.text });
  head.append(t, time, sp, copy);
  const body = document.createElement('div');
  body.className = 'body';
  body.innerHTML = renderText(item.text);
  el.append(head, body);
  return el;
}

function nearBottom() {
  return list.scrollHeight - list.scrollTop - list.clientHeight < 40;
}
function scrollToBottom() {
  list.scrollTop = list.scrollHeight;
}
function syncFollow() {
  btnFollow.classList.toggle('on', follow);
  btnFollow.textContent = follow ? '跟随最新 ✓' : '跟随最新';
}
function updateMeta(info, count) {
  const bits = [];
  if (info) bits.push(info.endpoint || '');
  if (typeof count === 'number') bits.push(count + ' 条');
  meta.textContent = bits.join('  ·  ');
}

function renderEmpty(info) {
  const ep = (info && info.endpoint) || '127.0.0.1:5178';
  list.innerHTML =
    '<div id="empty">' +
    '<div><b>答题板已就绪</b>，等待文本推送。</div>' +
    '<div style="margin-top:10px">监听地址：<code>' + esc(ep) + '</code></div>' +
    '<div style="margin-top:10px">推送示例：</div>' +
    '<div style="margin-top:4px"><code>curl -H "X-Answer-Panel-Token: $TOKEN" -H "Content-Type: application/json" \\\\<br>' +
    '&nbsp;&nbsp;-d \\'{"title":"第1题","text":"答案…"}\\' ' + esc('http://' + ep + '/push') + '</code></div>' +
    '<div style="margin-top:10px">令牌与端口见 <code>~/.answer-panel/bridge.json</code></div>' +
    '</div>';
}

function apply(payload) {
  const info = payload.info;
  if (payload.type === 'init' || payload.type === 'reset') {
    list.innerHTML = '';
    const items = payload.items || [];
    if (!items.length) renderEmpty(info);
    else for (const it of items) list.append(makeItem(it));
    updateMeta(info, items.length);
  } else if (payload.type === 'append') {
    if (list.querySelector('#empty')) list.innerHTML = '';
    list.append(makeItem(payload.item));
    updateMeta(info, payload.count);
  } else if (payload.type === 'update') {
    const old = list.querySelector('[data-seq="' + payload.item.seq + '"]') ||
      (payload.item.id ? list.querySelector('[data-id="' + CSS.escape(payload.item.id) + '"]') : null);
    if (old) old.replaceWith(makeItem(payload.item));
    else { if (list.querySelector('#empty')) list.innerHTML = ''; list.append(makeItem(payload.item)); }
    updateMeta(info, payload.count);
  } else if (payload.type === 'clear') {
    renderEmpty(info);
    updateMeta(info, 0);
  } else if (payload.type === 'info') {
    updateMeta(info, payload.count);
  }
  if (follow) requestAnimationFrame(scrollToBottom);
}

list.addEventListener('scroll', () => {
  const atBottom = nearBottom();
  if (!atBottom && follow) { follow = false; syncFollow(); }
  else if (atBottom && !follow) { follow = true; syncFollow(); }
});
btnFollow.onclick = () => { follow = !follow; syncFollow(); if (follow) scrollToBottom(); };
document.getElementById('btn-copy').onclick = () => {
  const parts = Array.from(list.querySelectorAll('.item')).map((el) => {
    const t = el.querySelector('.head .t');
    const body = el.querySelector('.body');
    const head = t && t.textContent ? '【' + t.textContent + '】\\n' : '';
    return head + body.innerText;
  });
  vscode.postMessage({ type: 'copy', text: parts.join('\\n\\n------------------------\\n\\n') });
};
document.getElementById('btn-clear').onclick = () => vscode.postMessage({ type: 'clear' });
window.addEventListener('message', (e) => apply(e.data || {}));
syncFollow();
vscode.postMessage({ type: 'ready' });
`;

function renderHtml(webview, info) {
  const nonce = require('crypto').randomBytes(16).toString('hex');
  const csp = [
    "default-src 'none'",
    "style-src 'unsafe-inline'",
    `script-src 'nonce-${nonce}'`,
    "img-src data:"
  ].join('; ');
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>答题板</title>
<style>${STYLE}</style>
</head>
<body>
<div id="app">
  <header>
    <span class="title">答题板</span>
    <span class="meta" id="meta"></span>
    <span class="spacer"></span>
    <button id="btn-follow" title="新内容到达时自动滚到底部">跟随最新</button>
    <button id="btn-copy" title="复制全部内容">复制全部</button>
    <button id="btn-clear" title="清空面板">清空</button>
  </header>
  <div id="list"></div>
</div>
<script nonce="${nonce}">${SCRIPT}</script>
</body>
</html>`;
}

module.exports = { renderHtml, esc };
