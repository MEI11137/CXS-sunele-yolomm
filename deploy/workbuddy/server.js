/* 海巡智检 · WorkBuddy 云端入口（单端口，无第三方依赖）
 *
 * 职责只有两件事：
 *   1) 直出前端构建产物 web/（SPA 深路由回落 index.html）；
 *   2) 把 /api/** 转给“本机连接器”，由它访问 127.0.0.1:8080 的真实后端。
 *
 * 浏览器始终只看到一个同源域名，Cookie / CSRF 双提交 / 后端 Origin 校验都不放宽。
 * 平台边缘转发进来的 Host 可能是内部主机名，所以发给后端的 Host 必须用**配置的
 * 精确公网域名**（HX17_PUBLIC_ORIGIN）；浏览器真实的 Origin 原样保留，并在网关先按
 * 同一个精确域名校验，不合成、不重写、不为任何来源放行。
 *
 * 请求生命周期只有三种状态，分派只发生一次：
 *   queued   —— 已收完请求体，等连接器来取；
 *   inflight —— 已交给某条上行连接，等 ack/head/chunk/reply/end；
 *   done     —— 已回复、超时或浏览器取消。
 * 只有回复、超时、取消会删除；分派本身不删除，也不重复写同一条响应。
 */
'use strict'

const http = require('node:http')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const zlib = require('node:zlib')

const PORT = Number(process.env.PORT || 3000)
const ROOT = __dirname
const WEB = path.join(ROOT, 'web')
const WAIT_MS = 20000
const QUEUE_TTL_MS = 120000
const ACK_TIMEOUT_MS = 20000
const INFLIGHT_TTL_MS = 600000
const OFFLINE_GRACE_MS = 45000
const MAX_QUEUE = 64
const MAX_INFLIGHT = 128
const MAX_WAITERS = 64
// 与后端 backend/routes/assets.py::MAX_UPLOAD_BYTES 对齐（512 MiB）。
const MAX_UPLOAD = 512 * 1024 * 1024
// 小请求体直接放内存；超过就落临时文件，由连接器按流下载，避免整段 base64 驻留内存。
const INLINE_BODY_LIMIT = 4 * 1024 * 1024
const MAX_INLINE_TOTAL = 64 * 1024 * 1024
const MAX_SPOOL_TOTAL = 1024 * 1024 * 1024
const STARTED_AT = Date.now()

const PUBLIC_ORIGIN = (process.env.HX17_PUBLIC_ORIGIN || 'https://haixun-inspection.app.workbuddy.host')
  .trim()
  .replace(/\/+$/, '')
let PUBLIC_HOST = ''
try {
  const parsed = new URL(PUBLIC_ORIGIN)
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') throw new Error('bad protocol')
  PUBLIC_HOST = parsed.host
} catch {
  console.error('[hx17] HX17_PUBLIC_ORIGIN 不是合法地址：' + PUBLIC_ORIGIN)
  process.exit(1)
}

const TMP_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'hx17-body-'))

function readToken() {
  const fromEnv = (process.env.HX17_TUNNEL_TOKEN || '').trim()
  if (fromEnv) return fromEnv
  try {
    return fs.readFileSync(path.join(ROOT, '.tunnel-token'), 'utf8').trim()
  } catch {
    return ''
  }
}

const TOKEN = readToken()
if (!TOKEN || TOKEN.length < 16) {
  console.error('[hx17] 缺少 .tunnel-token（或 HX17_TUNNEL_TOKEN），拒绝启动')
  process.exit(1)
}
const TOKEN_DIGEST = crypto.createHash('sha256').update(TOKEN).digest()

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.map': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.wasm': 'application/wasm',
}

/** 等待连接器领取的请求 */
const queued = new Map()
/** 已分派、等结果的请求（分派只发生一次，绝不重复投递） */
const inflight = new Map()
/** 正在挂起等任务的上行连接 */
const waiters = []

let queuedBytes = 0
let spooledBytes = 0
let connectorSeen = 0
let connectorMode = 'unknown'
let uplinkCount = 0
let lastEdgeHost = ''
let lastPublicHostHeader = ''
const counters = {
  api: 0, blocked: 0, rejectedOrigin: 0, tunnelErrors: 0,
  queueFull: 0, tooLarge: 0, malformed: 0, static: 0,
}

function log(...args) {
  console.log(new Date().toISOString(), ...args)
}

function tokenOk(req) {
  const supplied = String(req.headers['x-hx17-tunnel'] || '')
  if (!supplied) return false
  const digest = crypto.createHash('sha256').update(supplied).digest()
  return digest.length === TOKEN_DIGEST.length && crypto.timingSafeEqual(digest, TOKEN_DIGEST)
}

function json(res, code, body, extraHeaders) {
  if (res.writableEnded || res.destroyed) return
  const data = Buffer.from(JSON.stringify(body), 'utf8')
  res.writeHead(code, Object.assign({
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': data.length,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  }, extraHeaders || {}))
  res.end(data)
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value)
  } catch {
    return null
  }
}

// --------------------------------------------------------------------- 生命周期

function releaseEntry(entry) {
  if (entry.body) {
    queuedBytes -= entry.body.length
    entry.body = null
  }
  if (entry.file) {
    spooledBytes -= entry.size
    const file = entry.file
    entry.file = null
    fs.unlink(file, () => {})
  }
}

/** 终态：只在这里删队列、清定时器、释放缓冲；调用方保证幂等。 */
function finishEntry(entry) {
  if (entry.finished) return
  entry.finished = true
  entry.state = 'done'
  clearTimeout(entry.timer)
  entry.timer = null
  queued.delete(entry.id)
  inflight.delete(entry.id)
  releaseEntry(entry)
}

function armTimer(entry, ms, onFire) {
  clearTimeout(entry.timer)
  entry.timer = setTimeout(() => {
    if (entry.finished) return
    onFire()
  }, ms)
}

function failEntry(entry, status, error, msg) {
  if (entry.finished) return
  const res = entry.res
  finishEntry(entry)
  if (!res.writableEnded && !res.destroyed) {
    json(res, status, { code: status, error, msg, data: null })
  }
}

function payloadFor(entry) {
  const payload = {
    type: 'request',
    id: entry.id,
    method: entry.method,
    path: entry.path,
    headers: entry.headers,
  }
  if (entry.file) {
    payload.body_file = entry.id
    payload.body_size = entry.size
  } else {
    payload.body_b64 = entry.body ? entry.body.toString('base64') : ''
  }
  return payload
}

/** 把 queued 条目交给一条上行连接；成功返回 true，失败回滚为 queued。 */
function dispatch(entry, res) {
  if (entry.finished || entry.state !== 'queued') return false
  if (res.writableEnded || res.destroyed) return false

  queued.delete(entry.id)
  entry.state = 'inflight'
  entry.dispatchedAt = Date.now()
  entry.acked = false
  inflight.set(entry.id, entry)
  armTimer(entry, ACK_TIMEOUT_MS, () => {
    failEntry(entry, 504, 'CONNECTOR_NO_ACK', '连接器没有确认收到请求（上行可能已断开），请重试')
  })

  if (!writeFrame(res, payloadFor(entry))) {
    clearTimeout(entry.timer)
    entry.timer = null
    inflight.delete(entry.id)
    entry.state = 'queued'
    queued.set(entry.id, entry)
    armTimer(entry, QUEUE_TTL_MS, () => {
      failEntry(entry, 504, 'TUNNEL_TIMEOUT', '本机后端在 120 秒内没有取走请求（连接器可能未启动）')
    })
    return false
  }
  uplinkCount += 1
  connectorSeen = Date.now()
  res.end()
  return true
}

function enqueue(entry) {
  entry.state = 'queued'
  queued.set(entry.id, entry)
  armTimer(entry, QUEUE_TTL_MS, () => {
    failEntry(entry, 504, 'TUNNEL_TIMEOUT', '本机后端在 120 秒内没有取走请求（连接器可能未启动）')
  })

  while (waiters.length) {
    const waiter = waiters.shift()
    clearTimeout(waiter.timer)
    if (waiter.res.writableEnded || waiter.res.destroyed) continue
    if (dispatch(entry, waiter.res)) return
  }
}

// --------------------------------------------------------------------- 隧道

function writeFrame(res, payload) {
  if (res.writableEnded || res.destroyed) return false
  try {
    res.write(JSON.stringify(payload) + '\n')
    return true
  } catch {
    return false
  }
}

function handleUplink(req, res, url) {
  if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
  if (waiters.length >= MAX_WAITERS) {
    return json(res, 503, { code: 503, error: 'TOO_MANY_UPLINKS', msg: '上行连接过多' })
  }
  const mode = String(url.searchParams.get('mode') || 'unknown')
  const wait = Math.min(Number(url.searchParams.get('wait') || WAIT_MS), 55000)

  res.writeHead(200, {
    'Content-Type': 'application/x-ndjson; charset=utf-8',
    'Cache-Control': 'no-store, no-transform',
    'X-Accel-Buffering': 'no',
  })
  // 立刻写一个 open 帧：连接器据此判断“平台边缘是否真的边收边转”。
  writeFrame(res, { type: 'open', at: Date.now(), pending: queued.size, mode })
  connectorSeen = Date.now()
  connectorMode = mode

  for (const entry of queued.values()) {
    if (dispatch(entry, res)) return
    break
  }

  const waiter = { res, timer: null, startedAt: Date.now() }
  waiter.timer = setTimeout(() => {
    const at = waiters.indexOf(waiter)
    if (at >= 0) waiters.splice(at, 1)
    if (!res.writableEnded && !res.destroyed) {
      writeFrame(res, { type: 'empty', at: Date.now(), waited_ms: Date.now() - waiter.startedAt })
      res.end()
    }
  }, wait)
  waiters.push(waiter)
  // 客户端断开要以“响应没写完就关闭”为准；IncomingMessage 的 close 在请求体读完后
  // 就会触发，用它会把正常的挂起上行当成断线。
  res.on('close', () => {
    clearTimeout(waiter.timer)
    const at = waiters.indexOf(waiter)
    if (at >= 0) waiters.splice(at, 1)
  })
  return undefined
}

function handleAck(req, res) {
  if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
  return readJson(req, 64 * 1024).then((meta) => {
    const entry = inflight.get(String(meta.id || ''))
    connectorSeen = Date.now()
    if (!entry) return json(res, 200, { ok: true, gone: true })
    entry.acked = true
    armTimer(entry, INFLIGHT_TTL_MS, () => {
      failEntry(entry, 504, 'UPSTREAM_TIMEOUT', '本机后端 10 分钟内没有返回完整结果')
    })
    return json(res, 200, { ok: true })
  })
}

function handleBody(req, res, url) {
  if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
  const entry = inflight.get(String(url.searchParams.get('id') || ''))
  if (!entry || !entry.file) {
    return json(res, 404, { code: 404, error: 'BODY_GONE', msg: '请求体已过期或不存在' })
  }
  let stat
  try {
    stat = fs.statSync(entry.file)
  } catch {
    return json(res, 404, { code: 404, error: 'BODY_GONE', msg: '请求体已过期或不存在' })
  }
  res.writeHead(200, {
    'Content-Type': 'application/octet-stream',
    'Content-Length': stat.size,
    'Cache-Control': 'no-store',
  })
  const stream = fs.createReadStream(entry.file)
  stream.on('error', () => res.destroy())
  stream.pipe(res)
  return undefined
}

function handleHead(req, res) {
  if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
  return readJson(req, 1024 * 1024).then((meta) => {
    const entry = inflight.get(String(meta.id || ''))
    connectorSeen = Date.now()
    if (!entry) {
      const late = queued.has(String(meta.id || ''))
      return json(res, late ? 409 : 410, { code: late ? 409 : 410, error: late ? 'NOT_DISPATCHED' : 'TUNNEL_GONE', msg: '请求已被取消或超时' })
    }
    if (entry.res.writableEnded || entry.res.destroyed) return json(res, 200, { ok: true, closed: true })
    const headers = sanitizeResponseHeaders(meta.headers || {})
    if (meta.stream) {
      headers['X-Accel-Buffering'] = 'no'
      delete headers['content-length']
    }
    entry.streaming = Boolean(meta.stream)
    try {
      entry.res.writeHead(Number(meta.status) || 200, headers)
      if (entry.streaming && entry.res.flushHeaders) entry.res.flushHeaders()
    } catch {
      return json(res, 200, { ok: true, closed: true })
    }
    return json(res, 200, { ok: true })
  })
}

function handleChunk(req, res) {
  if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
  return readJson(req, 8 * 1024 * 1024).then((meta) => {
    const entry = inflight.get(String(meta.id || ''))
    connectorSeen = Date.now()
    if (!entry || entry.finished || entry.res.writableEnded || entry.res.destroyed) {
      return json(res, 200, { ok: true, closed: true })
    }
    if (meta.data_b64) {
      try {
        entry.res.write(Buffer.from(meta.data_b64, 'base64'))
      } catch {
        return json(res, 200, { ok: true, closed: true })
      }
    }
    return json(res, 200, { ok: true })
  })
}

function handleEnd(req, res) {
  if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
  return readJson(req, 1024 * 1024).then((meta) => {
    const entry = inflight.get(String(meta.id || ''))
    connectorSeen = Date.now()
    if (!entry) return json(res, 200, { ok: true, gone: true })
    const target = entry.res
    finishEntry(entry)
    if (!target.writableEnded && !target.destroyed) target.end()
    return json(res, 200, { ok: true })
  })
}

function handleReply(req, res) {
  if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
  return readJson(req, MAX_UPLOAD).then((meta) => {
    const entry = inflight.get(String(meta.id || ''))
    connectorSeen = Date.now()
    if (!entry) return json(res, 200, { ok: true, gone: true })
    const target = entry.res
    if (entry.finished || target.writableEnded || target.destroyed) {
      finishEntry(entry)
      return json(res, 200, { ok: true, closed: true })
    }
    const body = meta.body_b64 ? Buffer.from(meta.body_b64, 'base64') : Buffer.alloc(0)
    const headers = sanitizeResponseHeaders(meta.headers || {})
    delete headers['content-length']
    headers['Content-Length'] = String(body.length)
    finishEntry(entry)
    try {
      target.writeHead(Number(meta.status) || 200, headers)
      target.end(body)
    } catch {
      return json(res, 200, { ok: true, closed: true })
    }
    return json(res, 200, { ok: true })
  })
}

function readJson(req, limit) {
  return readBody(req, limit).then((stored) => {
    if (stored.file) {
      fs.unlink(stored.file, () => {})
      throw Object.assign(new Error('frame too large'), { code: 'TOO_LARGE' })
    }
    return JSON.parse(stored.body.toString('utf8'))
  })
}

/** 只放行需要回给浏览器的头，避免把平台的内部头带出去。 */
function sanitizeResponseHeaders(input) {
  const out = {}
  const allow = new Set([
    'content-type', 'content-length', 'content-disposition', 'content-encoding',
    'cache-control', 'etag', 'last-modified', 'expires', 'vary',
    'location', 'set-cookie', 'accept-ranges', 'content-range', 'x-request-id',
  ])
  for (const [key, value] of Object.entries(input)) {
    const lower = key.toLowerCase()
    if (!allow.has(lower)) continue
    if (lower === 'set-cookie') {
      out['set-cookie'] = Array.isArray(value) ? value : [String(value)]
      continue
    }
    out[lower] = Array.isArray(value) ? value.join(', ') : String(value)
  }
  return out
}

/**
 * 浏览器要发的请求头：只透传与业务有关的。
 * Host 必须写成配置的公网域名（平台边缘给的是内部主机名，后端 _same_origin 会拿它
 * 和 Origin 比对）；Origin 原样保留，既不合成也不改写。
 */
function pickRequestHeaders(raw) {
  const out = {}
  const allow = new Set([
    'accept', 'accept-language', 'content-type', 'cookie', 'authorization',
    'x-csrf-token', 'user-agent', 'referer', 'range', 'if-none-match',
    'if-modified-since', 'origin', 'x-requested-with',
  ])
  for (const [key, value] of Object.entries(raw)) {
    const lower = key.toLowerCase()
    if (!allow.has(lower)) continue
    out[lower] = Array.isArray(value) ? value.join(', ') : String(value)
  }
  out.host = PUBLIC_HOST
  return out
}

function originAllowed(origin) {
  if (!origin) return true
  try {
    return new URL(origin).origin === PUBLIC_ORIGIN
  } catch {
    return false
  }
}

// --------------------------------------------------------------------- 请求体

/**
 * 读请求体：≤ INLINE_BODY_LIMIT 放内存，超过则落临时文件（连接器按流下载）。
 * 全局缓冲有上限，超了就快速拒绝，不把平台内存拖垮。
 */
function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    let chunks = []
    let size = 0
    let file = null
    let ws = null
    let settled = false

    const cleanup = () => {
      if (ws) {
        try { ws.destroy() } catch { /* ignore */ }
      }
      if (file) fs.unlink(file, () => {})
    }
    const fail = (error) => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    }
    const openFile = () => {
      file = path.join(TMP_DIR, 'body-' + crypto.randomBytes(12).toString('hex'))
      ws = fs.createWriteStream(file, { flags: 'wx' })
      ws.on('error', fail)
      for (const chunk of chunks) ws.write(chunk)
      chunks = []
    }
    const writeChunk = (chunk) => {
      if (!ws) {
        if (size <= INLINE_BODY_LIMIT) {
          chunks.push(chunk)
          return
        }
        openFile()
      }
      if (!ws.write(chunk)) {
        req.pause()
        ws.once('drain', () => req.resume())
      }
    }

    req.on('data', (chunk) => {
      if (settled) return
      size += chunk.length
      if (size > limit) {
        fail(Object.assign(new Error('body too large'), { code: 'TOO_LARGE' }))
        return
      }
      writeChunk(chunk)
    })
    req.on('end', () => {
      if (settled) return
      settled = true
      if (ws) {
        ws.end(() => resolve({ body: null, file, size }))
        return
      }
      resolve({ body: Buffer.concat(chunks), file: null, size })
    })
    req.on('aborted', () => fail(Object.assign(new Error('client aborted'), { code: 'ABORTED' })))
    req.on('error', fail)
  })
}

// --------------------------------------------------------------------- 静态

function serveStatic(req, res, rawPathname) {
  const decoded = safeDecode(rawPathname)
  if (decoded === null || decoded.includes('\0')) {
    counters.malformed += 1
    return json(res, 400, { code: 400, error: 'BAD_PATH', msg: '请求地址包含无法解码的转义字符' })
  }
  const target = path.normalize(path.join(WEB, decoded))
  if (!target.startsWith(WEB + path.sep) && target !== WEB) {
    return json(res, 400, { code: 400, error: 'BAD_PATH', msg: '路径不合法' })
  }
  fs.stat(target, (err, stat) => {
    if (!err && stat.isFile()) return sendFile(req, res, target, stat)
    if (err && err.code !== 'ENOENT' && err.code !== 'ENOTDIR') {
      return json(res, 500, { code: 500, error: 'STATIC_FAILED', msg: '读取静态文件失败' })
    }
    const index = path.join(WEB, 'index.html')
    fs.stat(index, (indexErr, indexStat) => {
      if (indexErr) {
        return json(res, 503, { code: 503, error: 'NO_BUILD', msg: '缺少前端构建产物 web/index.html' })
      }
      return sendFile(req, res, index, indexStat, true)
    })
    return undefined
  })
  return undefined
}

const gzipCache = new Map()
const COMPRESSIBLE = new Set(['.html', '.js', '.mjs', '.css', '.json', '.svg', '.txt', '.map'])

/** 构建产物很小（<1MB），首次请求压好放内存；边缘不带压缩，这里补上。 */
function gzipFor(filePath, stat) {
  const hit = gzipCache.get(filePath)
  if (hit && hit.mtimeMs === stat.mtimeMs && hit.size === stat.size) return hit.buf
  const buf = zlib.gzipSync(fs.readFileSync(filePath), { level: 6 })
  gzipCache.set(filePath, { mtimeMs: stat.mtimeMs, size: stat.size, buf })
  return buf
}

function sendFile(req, res, filePath, stat, isFallback) {
  const ext = path.extname(filePath).toLowerCase()
  const isHashedAsset = filePath.includes(`${path.sep}assets${path.sep}`)
  const headers = {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Last-Modified': stat.mtime.toUTCString(),
    'X-Content-Type-Options': 'nosniff',
    'X-Robots-Tag': 'noindex, nofollow',
    'Referrer-Policy': 'same-origin',
    Vary: 'Accept-Encoding',
  }
  headers['Cache-Control'] = isFallback || ext === '.html'
    ? 'no-store'
    : isHashedAsset
      ? 'public, max-age=31536000, immutable'
      : 'public, max-age=300'

  const wantsGzip = /\bgzip\b/i.test(String(req.headers['accept-encoding'] || ''))
  let body = null
  if (wantsGzip && COMPRESSIBLE.has(ext) && stat.size > 1024) {
    body = gzipFor(filePath, stat)
    headers['Content-Encoding'] = 'gzip'
  }
  if (!body) body = fs.readFileSync(filePath)
  headers['Content-Length'] = String(body.length)

  res.writeHead(200, headers)
  if (req.method === 'HEAD') return res.end()
  return res.end(body)
}

// --------------------------------------------------------------------- 路由

/** 管理入口判定：先解码一次再规范化，挡住 %73、./、//、大小写等变形。 */
function isServicePath(decodedPathname) {
  let normalized = decodedPathname
  try {
    normalized = path.posix.normalize(decodedPathname)
  } catch {
    return true
  }
  const lower = normalized.toLowerCase().replace(/\/{2,}/g, '/')
  return lower === '/api/service' || lower.startsWith('/api/service/')
}

const server = http.createServer((req, res) => {
  try {
    if (typeof req.url !== 'string' || req.url.length > 8192) {
      counters.malformed += 1
      return json(res, 414, { code: 414, error: 'URI_TOO_LONG', msg: '请求地址过长' })
    }
    if (req.url.startsWith('//')) {
      counters.malformed += 1
      return json(res, 400, { code: 400, error: 'BAD_URL', msg: '请求地址不合法' })
    }
    let url
    try {
      url = new URL(req.url, 'http://placeholder')
    } catch {
      counters.malformed += 1
      return json(res, 400, { code: 400, error: 'BAD_URL', msg: '请求地址不合法' })
    }
    const pathname = url.pathname
    const decodedPathname = safeDecode(pathname)
    if (decodedPathname === null) {
      counters.malformed += 1
      return json(res, 400, { code: 400, error: 'BAD_URL', msg: '请求地址包含无法解码的转义字符' })
    }
    lastEdgeHost = String(req.headers.host || '')
    lastPublicHostHeader = String(req.headers['x-forwarded-host'] || '')

    if (pathname === '/__hx17/health') {
      if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
      return json(res, 200, {
        ok: true,
        uptime_s: Math.round((Date.now() - STARTED_AT) / 1000),
        public_origin: PUBLIC_ORIGIN,
        forward_host: PUBLIC_HOST,
        edge_host: lastEdgeHost,
        connector: {
          online: Date.now() - connectorSeen < 45000,
          last_seen_ms: connectorSeen ? Date.now() - connectorSeen : null,
          mode: connectorMode,
        },
        queue: {
          queued: queued.size,
          inflight: inflight.size,
          waiters: waiters.length,
          max_queue: MAX_QUEUE,
          max_inflight: MAX_INFLIGHT,
          queued_bytes: queuedBytes,
          spooled_bytes: spooledBytes,
          max_inline_bytes: MAX_INLINE_TOTAL,
          max_spool_bytes: MAX_SPOOL_TOTAL,
          max_upload_bytes: MAX_UPLOAD,
        },
        uplink_count: uplinkCount,
        counters,
        token_present: true,
      })
    }

    if (pathname === '/__hx17/probe/stream') {
      if (!tokenOk(req)) return json(res, 403, { code: 403, error: 'TUNNEL_TOKEN', msg: '隧道令牌不正确' })
      const frames = Math.min(Number(url.searchParams.get('frames') || 6), 20)
      const gap = Math.min(Number(url.searchParams.get('gap') || 700), 5000)
      res.writeHead(200, {
        'Content-Type': 'text/plain; charset=utf-8',
        'Cache-Control': 'no-store, no-transform',
        'X-Accel-Buffering': 'no',
      })
      let i = 0
      const timer = setInterval(() => {
        i += 1
        res.write(`frame ${i} at ${Date.now()}\n`)
        if (i >= frames) {
          clearInterval(timer)
          res.end()
        }
      }, gap)
      res.on('close', () => clearInterval(timer))
      return undefined
    }

    if (pathname === '/__hx17/uplink') return handleUplink(req, res, url)
    if (pathname === '/__hx17/ack') return handleAck(req, res)
    if (pathname === '/__hx17/body') return handleBody(req, res, url)
    if (pathname === '/__hx17/head') return handleHead(req, res)
    if (pathname === '/__hx17/chunk') return handleChunk(req, res)
    if (pathname === '/__hx17/end') return handleEnd(req, res)
    if (pathname === '/__hx17/reply') return handleReply(req, res)

    if (isServicePath(decodedPathname)) {
      counters.blocked += 1
      return json(res, 403, {
        code: 403,
        error: 'SERVICE_BLOCKED',
        msg: '服务管理接口只在服务器本机可用，公网入口不开放',
        data: null,
      })
    }

    if (pathname === '/api' || pathname.startsWith('/api/')) {
      if (req.method !== 'GET' && req.method !== 'HEAD' && req.method !== 'POST' && req.method !== 'PUT' && req.method !== 'PATCH' && req.method !== 'DELETE') {
        return json(res, 405, { code: 405, error: 'METHOD_NOT_ALLOWED', msg: '不支持的方法' })
      }
      // 浏览器 Origin 必须是配置的精确公网域名；伪造的跨域来源在网关就拒绝，
      // 不会进入隧道，也不会被后端误当成合法写入。
      const origin = String(req.headers.origin || '')
      if (origin && !originAllowed(origin)) {
        counters.rejectedOrigin += 1
        return json(res, 403, {
          code: 403,
          error: 'ORIGIN_REJECTED',
          msg: '请求来源与本站不一致，已拒绝',
          data: null,
        })
      }
      if (String(req.headers['sec-fetch-site'] || '') === 'cross-site') {
        counters.rejectedOrigin += 1
        return json(res, 403, {
          code: 403,
          error: 'ORIGIN_REJECTED',
          msg: '请求来源与本站不一致，已拒绝',
          data: null,
        })
      }
      const declared = Number(req.headers['content-length'] || 0)
      if (declared > MAX_UPLOAD) {
        counters.tooLarge += 1
        return json(res, 413, {
          code: 413,
          error: 'BODY_TOO_LARGE',
          msg: '文件超过 512MB，超过本服务的上传上限',
          data: null,
        })
      }
      if (queued.size >= MAX_QUEUE) {
        counters.queueFull += 1
        return json(res, 503, {
          code: 503,
          error: 'TUNNEL_QUEUE_FULL',
          msg: '排队请求过多，请稍后重试',
          data: null,
        }, { 'Retry-After': '2' })
      }
      if (queuedBytes + spooledBytes + declared > MAX_INLINE_TOTAL + MAX_SPOOL_TOTAL) {
        counters.queueFull += 1
        return json(res, 503, {
          code: 503,
          error: 'TUNNEL_BUFFER_FULL',
          msg: '正在处理的上传过多，请稍后重试',
          data: null,
        }, { 'Retry-After': '5' })
      }
      // 本机服务没连上时立刻说清楚，而不是让页面干等两分钟。
      const silentFor = connectorSeen ? Date.now() - connectorSeen : Number.POSITIVE_INFINITY
      if (silentFor > OFFLINE_GRACE_MS) {
        counters.tunnelErrors += 1
        return json(res, 503, {
          code: 503,
          error: 'TUNNEL_OFFLINE',
          msg: '巡检服务器当前未连接：请在巡检机上双击「启动云端连接」后再试',
          data: null,
        })
      }

      const entry = {
        id: crypto.randomUUID(),
        method: req.method,
        path: pathname + (url.search || ''),
        headers: pickRequestHeaders(req.headers),
        body: null,
        file: null,
        size: 0,
        res,
        state: 'queued',
        acked: false,
        streaming: false,
        finished: false,
        timer: null,
      }
      counters.api += 1
      // 浏览器断开只认“响应没写完就关闭”；IncomingMessage 的 close 在请求体读完
      // 就会触发，不能拿它判断断连（会把排队中的请求误删）。
      res.on('close', () => {
        if (res.writableEnded || entry.finished) return
        finishEntry(entry)
      })
      readBody(req, MAX_UPLOAD)
        .then((stored) => {
          entry.body = stored.body
          entry.file = stored.file
          entry.size = stored.size
          if (entry.body) queuedBytes += entry.body.length
          if (entry.file) spooledBytes += entry.size
          enqueue(entry)
        })
        .catch((error) => {
          if (entry.finished) return
          finishEntry(entry)
          const code = error && error.code
          if (code === 'TOO_LARGE') {
            counters.tooLarge += 1
            return json(res, 413, { code: 413, error: 'BODY_TOO_LARGE', msg: '文件超过 512MB，超过本服务的上传上限' })
          }
          return json(res, 400, { code: 400, error: 'BODY_READ_FAILED', msg: '读取请求体失败' })
        })
      return undefined
    }

    if (req.method !== 'GET' && req.method !== 'HEAD') {
      return json(res, 405, { code: 405, error: 'METHOD_NOT_ALLOWED', msg: '不支持的方法' })
    }
    counters.static += 1
    return serveStatic(req, res, pathname)
  } catch (error) {
    log('[error] request handler:', error && error.stack ? error.stack.split('\n')[0] : String(error))
    if (!res.headersSent) {
      return json(res, 500, { code: 500, error: 'GATEWAY_ERROR', msg: '网关处理请求时出错' })
    }
    try { res.destroy() } catch { /* ignore */ }
    return undefined
  }
})

server.keepAliveTimeout = 65000
server.headersTimeout = 70000
server.requestTimeout = 0
server.timeout = 0

server.on('clientError', (error, socket) => {
  if (!socket.writable) return socket.destroy()
  socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
})

server.listen(PORT, '0.0.0.0', () => {
  log(`[hx17] cloud gateway listening on 0.0.0.0:${PORT}`)
  log(`[hx17] static root: ${WEB}`)
  log(`[hx17] public origin: ${PUBLIC_ORIGIN} (forward host: ${PUBLIC_HOST})`)
})
