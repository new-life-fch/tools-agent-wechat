/**
 * dsh-plugin-relay-wake — 让「外部常驻脚本」的一次推送变成一次真正的唤醒。
 *
 * 为什么需要它：后台任务的结算通知走 tool-jobs 的 `maxConsecutiveWakes` 预算
 * （默认连续 3 次），预算用完后降级为 `inject`（只进 next-step 收件箱，不唤醒），
 * 于是空闲会话可能长时间没有任何监听者。本插件开一条独立通道：
 *
 *     本机 HTTP 推送  →  ctx.agents.get(sessionId)  →  Agent.followup(message)
 *
 * `followup` 在会话空闲时必然开一个新 turn（正在跑则排一个后续 turn，不会丢），
 * 因此这就是「主动通知」语义，不受 job 预算约束。
 *
 * 接口（只接受回环 Host，复用 web GUI 自己的端口，不新开端口）：
 *   GET  /relay/health  → 插件状态与唤醒计数
 *   POST /relay/wake    → body { session, text, summary? }，头 x-relay-token: <token>
 *
 * 载入方式（免安装、免重启）：profile 的 cordis.patch.yml 里 insert 一行，
 * `name` 写本文件的**绝对路径**即可（loader 会转成 file:// 直接 import）。
 *
 * 零依赖：不 import 任何 `@deepseek-ai/*`，所以放在 dsh 安装树之外也能加载；
 * 消息对象按 `@deepseek-ai/dsh-llm` 的 `createUserMessage` 同形状手工构造。
 */

import { randomUUID } from 'node:crypto'
import { mkdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'relay-wake'
export const inject = ['webServer', 'agents']

const VERSION = '0.1.0'
const HERE = dirname(fileURLToPath(import.meta.url))
const ROUTE_HEALTH = '/relay/health'
const ROUTE_WAKE = '/relay/wake'
const SUMMARY_MAX_CHARS = 120          // 与 dsh-llm 的 CONTEXT_SUMMARY_MAX_CHARS 对齐
const BODY_MAX_BYTES = 1 << 20         // 1 MB：推送正文是事件清单，不该更大
const HOUR_MS = 3600_000

const DEFAULTS = {
  /** 共享密钥：非空时要求 `x-relay-token` 头完全一致（默认关闭，靠回环 + Host 围栏） */
  token: '',
  /** 可观测性状态文件；默认写在插件目录下 */
  stateFile: join(HERE, 'state.json'),
  /** 每小时最多唤醒次数（0 = 不限）；防外部脚本失控把会话打成自激链 */
  maxWakesPerHour: 120,
  /** 两次唤醒之间的最小间隔秒数（0 = 不限） */
  minIntervalSec: 0,
  /** 单条推送正文上限，超出截断并标注 */
  maxTextChars: 12000,
  /** 只校验不投递（联调脚本时用） */
  dryRun: false,
}

function log(...args) {
  console.log('[relay-wake]', ...args)
}

/** 与 dsh-llm 的 freezeMessage 同语义：深冻结，避免下游改到共享对象。 */
function deepFreeze(value) {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) return value
  for (const key of Object.keys(value)) deepFreeze(value[key])
  return Object.freeze(value)
}

/** 与 `createUserMessage({ content, source })` 同形状（id 由 randomUUID 补齐、role 固定 user）。 */
function createRelayMessage(text, summary) {
  return deepFreeze({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: {
      kind: 'plugin',
      plugin: 'relay-wake',
      form: 'notice',
      summary: String(summary).slice(0, SUMMARY_MAX_CHARS),
    },
  })
}

/** 只接受本机回环 Host：挡掉 DNS rebinding 与跨站表单提交。 */
function isLoopbackHost(host) {
  return /^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i.test(String(host || '').trim())
}

function sendJson(res, code, body) {
  const text = `${JSON.stringify(body)}\n`
  res.statusCode = code
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(text)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > BODY_MAX_BYTES) {
        reject(new Error('body too large'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config && typeof config === 'object' ? config : {}) }
  const maxText = Number.isFinite(cfg.maxTextChars) && cfg.maxTextChars > 0
    ? Math.floor(cfg.maxTextChars)
    : DEFAULTS.maxTextChars

  const state = {
    plugin: 'relay-wake',
    version: VERSION,
    pid: process.pid,
    loadedAt: new Date().toISOString(),
    wakes: 0,
    rejected: 0,
    lastWakeAt: null,
    lastSession: null,
    lastStatus: null,
    lastError: null,
    unloadedAt: null,
  }
  const wakeTimes = []          // 滚动一小时窗口内的唤醒时刻

  const persist = () => {
    try {
      mkdirSync(dirname(cfg.stateFile), { recursive: true })
      const tmp = `${cfg.stateFile}.${process.pid}.tmp`      // 唯一临时名：Windows 上 rename 不会撞车
      writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`)
      renameSync(tmp, cfg.stateFile)
    } catch {
      // 状态文件只用于可观测性，写失败不影响唤醒
    }
  }
  persist()

  const reject = (res, code, error, extra) => {
    state.rejected += 1
    persist()
    sendJson(res, code, { ok: false, error, ...extra })
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: ROUTE_HEALTH,
    handler: (req, res) => {
      if (!isLoopbackHost(req.headers.host)) return sendJson(res, 403, { ok: false, error: 'forbidden-host' })
      if (req.method !== 'GET') return sendJson(res, 405, { ok: false, error: 'method-not-allowed', allow: 'GET' })
      let liveSessions = []
      try {
        liveSessions = ctx.agents.roots().map(agent => ({ id: agent.id, status: agent.status }))
      } catch (error) {
        state.lastError = `roots() 失败: ${String(error)}`
      }
      sendJson(res, 200, {
        ok: true,
        ...state,
        port: ctx.webServer.port ?? null,
        tokenRequired: Boolean(cfg.token),
        maxWakesPerHour: cfg.maxWakesPerHour,
        wakesInWindow: wakeTimes.length,
        dryRun: Boolean(cfg.dryRun),
        liveSessions,
      })
    },
  }), `relay-wake: GET ${ROUTE_HEALTH}`)

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: ROUTE_WAKE,
    handler: async (req, res) => {
      if (!isLoopbackHost(req.headers.host)) return sendJson(res, 403, { ok: false, error: 'forbidden-host' })
      if (req.method !== 'POST') return sendJson(res, 405, { ok: false, error: 'method-not-allowed', allow: 'POST' })
      if (cfg.token && String(req.headers['x-relay-token'] || '') !== String(cfg.token)) {
        return reject(res, 401, 'bad-token')
      }

      let payload
      try {
        const raw = (await readBody(req)).toString('utf8')
        payload = raw.trim() ? JSON.parse(raw) : {}
      } catch (error) {
        return reject(res, 400, 'bad-json', { message: String(error && error.message ? error.message : error) })
      }

      const session = typeof payload.session === 'string' ? payload.session.trim() : ''
      let text = typeof payload.text === 'string' ? payload.text : ''
      if (!session) return reject(res, 400, 'missing-session')
      if (!text.trim()) return reject(res, 400, 'missing-text')
      if (text.length > maxText) {
        text = `${text.slice(0, maxText)}\n…[relay-wake 已截断 ${text.length - maxText} 字]`
      }

      const now = Date.now()
      while (wakeTimes.length > 0 && now - wakeTimes[0] > HOUR_MS) wakeTimes.shift()
      if (cfg.maxWakesPerHour > 0 && wakeTimes.length >= cfg.maxWakesPerHour) {
        return reject(res, 429, 'wake-budget-exhausted', { limit: cfg.maxWakesPerHour, windowSec: 3600 })
      }
      if (cfg.minIntervalSec > 0 && state.lastWakeAt) {
        const elapsed = now - Date.parse(state.lastWakeAt)
        if (Number.isFinite(elapsed) && elapsed < cfg.minIntervalSec * 1000) {
          return reject(res, 429, 'too-soon', { minIntervalSec: cfg.minIntervalSec })
        }
      }

      const agent = ctx.agents.get(session)
      if (agent === undefined) {
        return reject(res, 404, 'session-not-live', {
          session,
          hint: '该会话没有活着的 agent：先在 GUI 里打开它，或核对 session id（Agent 身份就等于 session id）',
        })
      }

      const summary = typeof payload.summary === 'string' && payload.summary.trim()
        ? payload.summary.trim()
        : text.replace(/\s+/g, ' ').trim()

      if (cfg.dryRun) {
        log(`dry-run：本应唤醒 ${session}（${text.length} 字，status=${agent.status}）`)
        return sendJson(res, 200, { ok: true, woke: false, dryRun: true, session, status: agent.status })
      }

      try {
        agent.followup(createRelayMessage(text, summary))
      } catch (error) {
        state.lastError = String(error && error.message ? error.message : error)
        persist()
        log('followup 失败:', state.lastError)
        return sendJson(res, 500, { ok: false, error: 'followup-failed', message: state.lastError })
      }

      wakeTimes.push(now)
      state.wakes += 1
      state.lastWakeAt = new Date(now).toISOString()
      state.lastSession = session
      state.lastStatus = agent.status
      state.lastError = null
      persist()
      log(`唤醒 ${session}（第 ${state.wakes} 次，${text.length} 字，status=${agent.status}）`)
      sendJson(res, 200, { ok: true, woke: true, session, status: agent.status, wakes: state.wakes })
    },
  }), `relay-wake: POST ${ROUTE_WAKE}`)

  ctx.effect(() => () => {
    state.unloadedAt = new Date().toISOString()
    persist()
    log('已卸载')
  }, 'relay-wake: state teardown')

  log(`已加载 pid=${process.pid} port=${ctx.webServer.port ?? '?'} `
    + `token=${cfg.token ? 'on' : 'off'} dryRun=${Boolean(cfg.dryRun)} state=${cfg.stateFile}`)
}
