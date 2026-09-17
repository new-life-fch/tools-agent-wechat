'use strict';

/**
 * 离线端到端测试：不需要 VSCode。
 *   1. Store 语义（新增 / 同 id 更新 / 淘汰 / 清空 / 持久化往返）
 *   2. registry 读写与死 pid 过滤
 *   3. bridge HTTP 服务 + 真实 vscode_notify.py 打进去（Python ↔ JS 契约）
 *
 * 跑法：node test/offline.test.js
 * 注意：全部异步，绝不能用 spawnSync —— 那会阻塞本进程事件循环，
 *       导致同进程里的 HTTP 服务无法应答。
 */

const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const PROJECT = path.resolve(ROOT, '..');
const { Store } = require(path.join(ROOT, 'lib', 'store.js'));
const registry = require(path.join(ROOT, 'lib', 'registry.js'));
const bridge = require(path.join(ROOT, 'lib', 'bridge.js'));
const { renderHtml } = require(path.join(ROOT, 'lib', 'html.js'));

const PYTHON = process.env.PYTHON || '/Users/fch/project/dp-workspace/.venv/bin/python';
const NOTIFY = path.join(PROJECT, 'vscode_notify.py');
const TMP = path.join(ROOT, '.tmp-test');

const queue = [];
const test = (name, fn) => queue.push({ name, fn });

function runNotify(args, opts) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, [NOTIFY].concat(args), Object.assign({ cwd: PROJECT }, opts || {}));
    let out = '';
    let err = '';
    child.stdout.on('data', (c) => (out += c));
    child.stderr.on('data', (c) => (err += c));
    child.on('close', (code) => resolve({ code, out: out.trim(), err: err.trim() }));
    child.on('error', (e) => resolve({ code: -1, out: '', err: String(e && e.message) }));
    if (opts && opts.input != null) {
      child.stdin.end(opts.input);
    }
  });
}

function req(port, method, pathname, { token, body, contentType } = {}) {
  return new Promise((resolve) => {
    const headers = {};
    if (token) headers['X-Answer-Panel-Token'] = token;
    if (body != null) headers['Content-Type'] = contentType || 'application/json';
    const r = http.request({ host: '127.0.0.1', port, path: pathname, method, headers }, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => {
        let parsed = null;
        try {
          parsed = JSON.parse(data);
        } catch (_) {}
        resolve({ status: res.statusCode, body: data, json: parsed });
      });
    });
    r.on('error', (e) => resolve({ status: 0, body: '', json: null, error: String(e && e.message) }));
    r.end(body);
  });
}

// ============================================================ 单元组

console.log('\n[store]');
test('push 追加并递增 seq', () => {
  const s = new Store(10);
  const a = s.push({ text: 'a' });
  const b = s.push({ text: 'b' });
  assert.strictEqual(a.seq, 1);
  assert.strictEqual(b.seq, 2);
  assert.strictEqual(s.count(), 2);
});

test('同 id 再推 = 就地更新，不新增', () => {
  const s = new Store(10);
  s.push({ id: 'q3', title: '第3题', text: '草稿' });
  const b = s.push({ id: 'q3', text: '最终答案' });
  assert.strictEqual(s.count(), 1);
  assert.strictEqual(s.list()[0].text, '最终答案');
  assert.strictEqual(s.list()[0].title, '第3题');
  assert.strictEqual(b.seq, 2);
});

test('超过 limit 淘汰最旧的并发 reset 事件', () => {
  const s = new Store(3);
  const events = [];
  s.onDidChange((e) => events.push(e.type));
  for (let i = 0; i < 5; i += 1) s.push({ text: 'x' + i });
  assert.strictEqual(s.count(), 3);
  assert.strictEqual(s.list()[0].text, 'x2');
  assert.ok(events.includes('reset'), '应该发过 reset 事件');
});

test('clear 清空并广播', () => {
  const s = new Store(5);
  s.push({ text: 'a' });
  let got = null;
  s.onDidChange((e) => (got = e.type));
  assert.strictEqual(s.clear(), 1);
  assert.strictEqual(s.count(), 0);
  assert.strictEqual(got, 'clear');
});

test('snapshot/restore 往返一致', () => {
  const a = new Store(5);
  a.push({ id: 'k', title: 'T', text: 'line1\nline2' });
  const b = new Store(5);
  b.restore(a.snapshot());
  assert.strictEqual(b.count(), 1);
  assert.strictEqual(b.list()[0].text, 'line1\nline2');
  assert.strictEqual(b.seq, a.seq);
  assert.strictEqual(b.push({ text: 'z' }).seq, a.seq + 1);
});

test('toPlainText 带标题且保留正文', () => {
  const s = new Store(5);
  s.push({ title: '第1题', text: 'A' });
  s.push({ text: 'B' });
  const t = s.toPlainText();
  assert.ok(t.includes('【第1题】'));
  assert.ok(t.includes('A') && t.includes('B'));
});

console.log('\n[registry]');
const REG = path.join(TMP, 'bridge.json');

test('register / unregister 往返', () => {
  const r1 = registry.register(REG, { pid: process.pid, port: 5178, token: 'tok', app: 'Test' });
  assert.strictEqual(r1.instances.length, 1);
  registry.unregister(REG, process.pid);
  assert.strictEqual(registry.read(REG).instances.length, 0);
});

test('重复注册同 pid 不产生重复条目', () => {
  registry.register(REG, { pid: process.pid, port: 5178 });
  registry.register(REG, { pid: process.pid, port: 5179 });
  const live = registry.liveInstances(registry.read(REG));
  assert.strictEqual(live.length, 1);
  assert.strictEqual(live[0].port, 5179);
  registry.unregister(REG, process.pid);
});

test('死 pid 被过滤掉', () => {
  registry.write(REG, [
    { pid: 999999, port: 65000, updated_at: Date.now() },
    { pid: process.pid, port: 5178, updated_at: Date.now() }
  ]);
  const live = registry.liveInstances(registry.read(REG));
  assert.strictEqual(live.length, 1);
  assert.strictEqual(live[0].pid, process.pid);
});

test('坏 JSON 不抛异常', () => {
  fs.writeFileSync(REG, '{ this is not json');
  assert.deepStrictEqual(registry.read(REG).instances, []);
  assert.deepStrictEqual(registry.liveInstances(registry.read(REG)), []);
});

test('isAlive 认自己、否认不存在的 pid', () => {
  assert.strictEqual(registry.isAlive(process.pid), true);
  assert.strictEqual(registry.isAlive(999999), false);
  assert.strictEqual(registry.isAlive(null), false);
});

console.log('\n[html]');
test('renderHtml 有 CSP nonce 且无模板残留', () => {
  const html = renderHtml({}, { endpoint: '127.0.0.1:5178' });
  assert.ok(/script-src 'nonce-[0-9a-f]{32}'/.test(html), 'CSP nonce 缺失');
  assert.ok(!html.includes('${'), '有未替换的模板变量');
  assert.ok(html.includes('跟随最新') && html.includes('复制全部'));
  assert.ok(html.includes('pre.code'), '代码块样式缺失');
});

// ============================================================ E2E 组

const store = new Store(50);
const TOKEN = 'test-token-abc123';
let started = null;

test('本地 HTTP 服务能起来', async () => {
  started = await bridge.start({
    port: 5178,
    token: TOKEN,
    requireToken: true,
    handlers: {
      // 故意返回一个会跟协议标识撞车的 app 字段，验证 bridge 会把它盖回 answer-panel
      // （真实事故：扩展把 vscode.env.appName 塞进 app，导致推送方认不出这个端口）
      health: () => ({
        port: started ? started.port : null,
        pid: process.pid,
        app: 'Visual Studio Code',
        editor: 'Qoder',
        vscode: '1.106.3',
        items: store.count(),
        workspace: PROJECT
      }),
      state: () => ({ count: store.count(), items: store.list() }),
      push: (p) => {
        const it = store.push(p);
        return { seq: it.seq, count: store.count() };
      },
      clear: () => ({ cleared: store.clear() })
    }
  });
  assert.ok(Number.isInteger(started.port) && started.port > 0, '端口无效');
});

test('/health 不带令牌也通（供探测用）', async () => {
  const r = await req(started.port, 'GET', '/health');
  assert.strictEqual(r.status, 200, r.error || r.body);
  assert.strictEqual(r.json.app, 'answer-panel', 'app 必须是协议标识，不能被编辑器名字覆盖');
  assert.strictEqual(r.json.editor, 'Qoder');
  assert.strictEqual(r.json.pid, process.pid);
});

test('/push 无令牌 → 401', async () => {
  const r = await req(started.port, 'POST', '/push', { body: '{"text":"x"}' });
  assert.strictEqual(r.status, 401);
});

test('未知路由 → 404', async () => {
  const r = await req(started.port, 'GET', '/nope', { token: TOKEN });
  assert.strictEqual(r.status, 404);
});

test('CLI --registry 指向不存在的文件 → 退出码 2', async () => {
  const r = await runNotify(['--registry', path.join(TMP, 'nope.json'), 'push', '--text', 'hi']);
  assert.strictEqual(r.code, 2, '期望 2，实际 ' + r.code + ' / ' + r.err);
  assert.ok(r.out.includes('NO-PANEL'), r.out);
});

test('CLI push 成功，退出码 0，seq=1', async () => {
  registry.write(REG, [
    { pid: process.pid, port: started.port, token: TOKEN, app: 'TestEditor', vscode: '1.106.3', workspace: PROJECT, updated_at: Date.now() }
  ]);
  const r = await runNotify(['--registry', REG, 'push', '--title', '第3题', '--text', '答案正文']);
  assert.strictEqual(r.code, 0, r.err || r.out);
  assert.ok(r.out.startsWith('OK'), r.out);
  assert.ok(r.out.includes('seq=1'), r.out);
  assert.strictEqual(store.count(), 1);
  assert.strictEqual(store.list()[0].title, '第3题');
  assert.strictEqual(store.list()[0].text, '答案正文');
});

test('CLI --file 读多行正文（含中文与代码块）', async () => {
  const f = path.join(TMP, 'answer.md');
  fs.writeFileSync(f, '思路：\n\n```python\nprint("你好")\n```\n', 'utf8');
  const r = await runNotify(['--registry', REG, 'push', '--title', '第4题', '--file', f]);
  assert.strictEqual(r.code, 0, r.err || r.out);
  assert.strictEqual(store.list()[1].text, '思路：\n\n```python\nprint("你好")\n```\n');
});

test('CLI 从标准输入读正文', async () => {
  const r = await runNotify(['--registry', REG, 'push', '-t', '第5题'], { input: '从 stdin 来的答案' });
  assert.strictEqual(r.code, 0, r.err || r.out);
  assert.strictEqual(store.list()[2].text, '从 stdin 来的答案');
});

test('CLI 同 id 再推 = 就地更新', async () => {
  const before = store.count();
  await runNotify(['--registry', REG, 'push', '--id', 'q6', '--title', '第6题', '--text', '草稿']);
  const r = await runNotify(['--registry', REG, 'push', '--id', 'q6', '--text', '最终版']);
  assert.strictEqual(r.code, 0, r.err || r.out);
  assert.strictEqual(store.count(), before + 1);
  assert.strictEqual(store.list()[store.count() - 1].text, '最终版');
});

test('CLI status 看得到面板', async () => {
  const r = await runNotify(['--registry', REG, 'status']);
  assert.strictEqual(r.code, 0, r.err || r.out);
  assert.ok(r.out.includes('Qoder'), r.out); // 显示编辑器名（来自 /health 的 editor）
  assert.ok(r.out.includes(String(started.port)), r.out);
});

test('CLI status --json（参数放子命令之后）', async () => {
  const r = await runNotify(['--registry', REG, 'status', '--json']);
  assert.strictEqual(r.code, 0, r.err || r.out);
  const data = JSON.parse(r.out);
  assert.strictEqual(data.count, 1);
  assert.strictEqual(data.instances[0].port, started.port);
});

test('CLI doctor 全绿', async () => {
  const r = await runNotify(['--registry', REG, 'doctor']);
  assert.strictEqual(r.code, 0, r.err || r.out);
  assert.ok(r.out.includes('存活面板'), r.out);
});

test('CLI 坏令牌 → 退出码 3（通道错误，区别于没面板）', async () => {
  registry.write(REG, [
    { pid: process.pid, port: started.port, token: 'WRONG', app: 'TestEditor', workspace: PROJECT, updated_at: Date.now() }
  ]);
  const r = await runNotify(['--registry', REG, 'push', '--text', 'x']);
  assert.strictEqual(r.code, 3, '期望 3，实际 ' + r.code + ' / ' + r.err);
  registry.write(REG, [
    { pid: process.pid, port: started.port, token: TOKEN, app: 'TestEditor', workspace: PROJECT, updated_at: Date.now() }
  ]);
});

test('非 JSON 正文按纯文本收（curl --data-binary 路径）', async () => {
  const payload = '裸文本\n第二行';
  const r = await req(started.port, 'POST', '/push', { token: TOKEN, body: payload, contentType: 'text/plain' });
  assert.strictEqual(r.status, 200, r.body);
  assert.strictEqual(store.list()[store.count() - 1].text, payload);
});

test('坏 JSON 正文 → 400', async () => {
  const r = await req(started.port, 'POST', '/push', { token: TOKEN, body: '{ oops' });
  assert.strictEqual(r.status, 400);
});

test('超大正文 → 413 且不污染内容', async () => {
  const before = store.count();
  const big = Buffer.alloc(9 * 1024 * 1024, 0x61);
  const r = await req(started.port, 'POST', '/push', { token: TOKEN, body: big, contentType: 'text/plain' });
  assert.strictEqual(r.status, 413, '期望 413，实际 ' + r.status);
  assert.strictEqual(store.count(), before);
});

test('CLI clear 清空', async () => {
  const r = await runNotify(['--registry', REG, 'clear']);
  assert.strictEqual(r.code, 0, r.err || r.out);
  assert.strictEqual(store.count(), 0);
});

test('工作区匹配：cwd 命中 workspace 时选中该窗口', async () => {
  const r = await runNotify(['--registry', REG, 'status', '--json']);
  assert.strictEqual(JSON.parse(r.out).instances[0].port, started.port);
});

// ============================================================ 执行

(async () => {
  fs.rmSync(TMP, { recursive: true, force: true });
  fs.mkdirSync(TMP, { recursive: true });
  let passed = 0;
  const failures = [];
  for (const t of queue) {
    try {
      await t.fn();
      passed += 1;
      console.log('  ok   ' + t.name);
    } catch (err) {
      failures.push(t.name + ' → ' + (err && err.message));
      console.log('  FAIL ' + t.name + '\n       ' + (err && err.message));
    }
  }
  if (started) {
    try {
      started.server.close();
    } catch (_) {}
  }
  fs.rmSync(TMP, { recursive: true, force: true });
  const failed = failures.length;
  console.log('\n' + '='.repeat(56));
  console.log('offline.test.js  ' + passed + ' passed, ' + failed + ' failed');
  if (failed) {
    console.log('\n失败明细：');
    for (const f of failures) console.log('  - ' + f);
  }
  console.log('='.repeat(56));
  process.exit(failed ? 1 : 0);
})();
