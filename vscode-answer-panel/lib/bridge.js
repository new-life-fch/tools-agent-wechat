'use strict';

const http = require('http');
const crypto = require('crypto');

const HOST = '127.0.0.1';
const MAX_BODY = 8 * 1024 * 1024; // 8 MB，足够塞下很长的答案
const PORT_TRIES = 20;

function newToken() {
  return crypto.randomBytes(16).toString('hex');
}

function sendJson(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store'
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let chunks = [];
    let size = 0;
    let tooLarge = false;
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        tooLarge = true;
        chunks = []; // 立刻丢掉已收内容，避免内存被撑爆
        return;
      }
      if (!tooLarge) chunks.push(c);
    });
    req.on('end', () => {
      if (tooLarge) {
        reject(Object.assign(new Error('body too large'), { code: 'ETOOLARGE' }));
        return;
      }
      resolve(Buffer.concat(chunks).toString('utf8'));
    });
    req.on('error', reject);
  });
}

/**
 * 起一个只监听回环地址的 HTTP 服务。
 * handlers: { health(), push(payload), clear(), state() }
 */
async function start(opts) {
  const basePort = Number(opts.port) || 5178;
  const token = opts.token || newToken();
  const handlers = opts.handlers || {};
  const requireToken = opts.requireToken !== false;

  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      const code = err && err.code === 'ETOOLARGE' ? 413 : 500;
      try {
        sendJson(res, code, { ok: false, error: String((err && err.message) || err) });
      } catch (_) {
        /* 连接可能已经断了，忽略 */
      }
    });
  });

  async function handle(req, res) {
    let url;
    try {
      url = new URL(req.url, `http://${HOST}`);
    } catch (_) {
      return sendJson(res, 400, { ok: false, error: 'bad url' });
    }
    const route = url.pathname.replace(/\/+$/, '') || '/';

    if (route === '/health' && req.method === 'GET') {
      // app 是协议标识，最后盖章，任何 handler 都覆盖不掉它
      return sendJson(res, 200, Object.assign({ ok: true }, handlers.health ? handlers.health() : {}, { app: 'answer-panel' }));
    }

    if (requireToken) {
      const got = req.headers['x-answer-panel-token'] || url.searchParams.get('token') || '';
      if (got !== token) return sendJson(res, 401, { ok: false, error: 'bad token' });
    }

    if (route === '/state' && req.method === 'GET') {
      return sendJson(res, 200, Object.assign({ ok: true }, handlers.state ? handlers.state() : {}));
    }
    if (route === '/push' && req.method === 'POST') {
      const raw = await readBody(req);
      const ctype = String(req.headers['content-type'] || '');
      let payload;
      if (ctype.includes('application/json')) {
        try {
          payload = JSON.parse(raw || '{}');
        } catch (err) {
          return sendJson(res, 400, { ok: false, error: 'bad json: ' + err.message });
        }
      } else {
        // 非 JSON 一律当纯文本，方便 curl --data-binary @answer.md
        payload = { text: raw };
      }
      if (url.searchParams.get('title')) payload.title = url.searchParams.get('title');
      if (url.searchParams.get('id')) payload.id = url.searchParams.get('id');
      if (payload.text == null && payload.title == null) {
        return sendJson(res, 400, { ok: false, error: 'empty payload' });
      }
      const out = handlers.push ? handlers.push(payload) : {};
      return sendJson(res, 200, Object.assign({ ok: true }, out));
    }
    if (route === '/clear' && req.method === 'POST') {
      const out = handlers.clear ? handlers.clear() : {};
      return sendJson(res, 200, Object.assign({ ok: true }, out));
    }
    return sendJson(res, 404, { ok: false, error: 'not found', routes: ['GET /health', 'GET /state', 'POST /push', 'POST /clear'] });
  }

  const port = await listenWithFallback(server, basePort);
  return { server, port, token, host: HOST };
}

function listenWithFallback(server, basePort) {
  return new Promise((resolve, reject) => {
    let attempt = 0;
    const tryPort = (p) => {
      const onError = (err) => {
        server.removeListener('listening', onListening);
        if (err && err.code === 'EADDRINUSE' && attempt < PORT_TRIES) {
          attempt += 1;
          tryPort(p + 1);
          return;
        }
        reject(err);
      };
      const onListening = () => {
        server.removeListener('error', onError);
        resolve(server.address().port);
      };
      server.once('error', onError);
      server.once('listening', onListening);
      server.listen(p, HOST);
    };
    tryPort(basePort);
  });
}

module.exports = { start, newToken, HOST, PORT_TRIES, MAX_BODY };
