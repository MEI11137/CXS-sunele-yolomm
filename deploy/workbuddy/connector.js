/* 海巡智检 · 本机连接器（在跑后端的这台机器上执行，无第三方依赖）
 *
 *   node connector.js --public https://<公网域名> [--upstream http://127.0.0.1:8080]
 *
 * 它主动外连云端入口，取走 /api/** 请求，转给本机后端，再把结果按原样回传。
 * 只转发 GET/HEAD/POST/PUT/PATCH/DELETE 的 /api/**，永远不转发 /api/service/**。
 * 不写日志到磁盘，不打印 Cookie / 令牌。
 */
'use strict'

const http = require('node:http')
const https = require('node:https')
const fs = require('node:fs')
const path = require('node:path')
const { URL } = require('node:url')

function parseArgs(argv) {
  const out = {}
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i]
    if (!item.startsWith('--')) continue
    const key = item.slice(2)
    const next = argv[i + 1]
    if (!next || next.startsWith('--')) out[key] = true
    else { out[key] = next; i += 1 }
  }
  return out
}

const args = parseArgs(process.argv.slice(2))
const RAW_PUBLIC = String(args.public || process.env.HX17_PUBLIC || '')
// 平台分享链接形如 https://xxx/?sharecode=YYYY：去掉查询串，把 sharecode 单独留着，
// 之后每个隧道请求都带上（边缘只认“完整分享参数”，业务请求由浏览器 Cookie 承担）。
let PUBLIC = RAW_PUBLIC
let SHARECODE = String(args.sharecode || process.env.HX17_SHARECODE || '')
try {
  const parsed = new URL(RAW_PUBLIC)
  SHARECODE = SHARECODE || parsed.searchParams.get('sharecode') || ''
  PUBLIC = parsed.origin + parsed.pathname.replace(/\/+$/, '')
} catch {
  PUBLIC = RAW_PUBLIC.replace(/\/+$/, '')
}
const UPSTREAM = String(args.upstream || process.env.HX17_UPSTREAM || 'http://127.0.0.1:8080').replace(/\/+$/, '')
const LANES = Math.max(1, Math.min(Number(args.lanes || process.env.HX17_LANES || 4), 12))
const STREAM_THRESHOLD = 2 * 1024 * 1024
const MAX_BUFFER = 48 * 1024 * 1024

if (!PUBLIC) {
  console.error('usage: node connector.js --public https://<public-host> [--upstream http://127.0.0.1:8080]')
  process.exit(2)
}

function readToken() {
  const fromEnv = (process.env.HX17_TUNNEL_TOKEN || '').trim()
  if (fromEnv) return fromEnv
  try {
    return fs.readFileSync(path.join(__dirname, '.tunnel-token'), 'utf8').trim()
  } catch {
    return ''
  }
}

const TOKEN = readToken()
if (!TOKEN) {
  console.error('missing .tunnel-token (or set HX17_TUNNEL_TOKEN)')
  process.exit(2)
}

const publicUrl = new URL(PUBLIC)
const publicClient = publicUrl.protocol === 'https:' ? https : http
// 到公网入口必须复用连接：否则每个转发帧都要重新 TLS 握手，交互会明显变慢。
const publicAgent = new (publicUrl.protocol === 'https:' ? https.Agent : http.Agent)({
  keepAlive: true,
  maxSockets: LANES + 8,
  keepAliveMsecs: 30000,
})
const upstreamUrl = new URL(UPSTREAM)
const upstreamAgent = new http.Agent({ keepAlive: true, maxSockets: 12, keepAliveMsecs: 30000 })

let mode = 'stream' // stream = 长挂起上行；fast = 短轮询
let stopping = false
const stats = { requests: 0, replied: 0, errors: 0, startedAt: Date.now() }

function log(...items) {
  console.log(new Date().toISOString(), ...items)
}

function tunnelRequest(pathname, method, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body), 'utf8')
    const withCode = SHARECODE
      ? pathname + (pathname.includes('?') ? '&' : '?') + 'sharecode=' + encodeURIComponent(SHARECODE)
      : pathname
    const req = publicClient.request({
      protocol: publicUrl.protocol,
      hostname: publicUrl.hostname,
      port: publicUrl.port || (publicUrl.protocol === 'https:' ? 443 : 80),
      method,
      path: withCode,
      agent: publicAgent,
      headers: Object.assign(
        { 'x-hx17-tunnel': TOKEN, accept: 'application/x-ndjson, application/json' },
        payload ? { 'content-type': 'application/json', 'content-length': String(payload.length) } : {},
      ),
      timeout: timeoutMs || 120000,
    }, (res) => resolve(res))
    req.on('timeout', () => req.destroy(new Error('tunnel timeout')))
    req.on('error', reject)
    if (payload) req.write(payload)
    req.end()
  })
}

function readAll(stream, limit) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    stream.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        reject(new Error('响应超过 ' + Math.round(limit / 1048576) + 'MB，已中断'))
        stream.destroy()
        return
      }
      chunks.push(chunk)
    })
    stream.on('end', () => resolve(Buffer.concat(chunks)))
    stream.on('error', reject)
  })
}

async function postJson(pathname, body) {
  const res = await tunnelRequest(pathname, 'POST', body)
  const raw = await readAll(res, 1024 * 1024)
  if (res.statusCode !== 200) {
    throw new Error(`${pathname} -> HTTP ${res.statusCode} ${raw.toString('utf8').slice(0, 200)}`)
  }
  return raw
}

async function postJsonData(pathname, body) {
  const raw = await postJson(pathname, body)
  try {
    return JSON.parse(raw.toString('utf8'))
  } catch {
    return {}
  }
}

async function sendHead(id, status, headers, stream) {
  const out = await postJsonData('/__hx17/head', { id, status, headers, stream })
  if (out.closed) throw Object.assign(new Error('client closed'), { code: 'CLIENT_CLOSED' })
}

async function sendChunk(id, buffer) {
  const out = await postJsonData('/__hx17/chunk', { id, data_b64: buffer.toString('base64') })
  if (out.closed) throw Object.assign(new Error('client closed'), { code: 'CLIENT_CLOSED' })
}

async function sendEnd(id) {
  await postJson('/__hx17/end', { id })
}

async function sendReply(id, status, headers, body) {
  return postJson('/__hx17/reply', { id, status, headers, body_b64: body.toString('base64') })
}

function upstreamRequest(payload) {
  return new Promise((resolve, reject) => {
    const target = new URL(payload.path, UPSTREAM)
    if (target.pathname === '/api/service' || target.pathname.startsWith('/api/service/')) {
      return reject(Object.assign(new Error('blocked'), { code: 'BLOCKED' }))
    }
    const headers = {}
    for (const [key, value] of Object.entries(payload.headers || {})) {
      const lower = key.toLowerCase()
      if (['connection', 'keep-alive', 'transfer-encoding', 'upgrade', 'proxy-authorization'].includes(lower)) continue
      headers[lower] = value
    }
    headers.host = payload.headers && payload.headers.host ? payload.headers.host
      : (UPSTREAM ? new URL(UPSTREAM).host : '127.0.0.1:8080')
    headers.connection = 'keep-alive'
    const body = payload.body_b64 ? Buffer.from(payload.body_b64, 'base64') : null
    const hasFile = Boolean(payload.body_file)
    if (hasFile) headers['content-length'] = String(payload.body_size || 0)
    else if (body) headers['content-length'] = String(body.length)

    const req = http.request({
      protocol: upstreamUrl.protocol,
      hostname: upstreamUrl.hostname,
      port: upstreamUrl.port || 80,
      method: payload.method,
      path: target.pathname + target.search,
      headers,
      agent: upstreamAgent,
      timeout: hasFile ? 900000 : 300000,
    }, resolve)
    req.on('timeout', () => req.destroy(new Error('backend timeout')))
    req.on('error', reject)
    if (hasFile) {
      // 大请求体不经过 base64 内存，直接从隧道流进后端（网关那边是临时文件）。
      tunnelRequest('/__hx17/body?id=' + encodeURIComponent(payload.body_file), 'GET', undefined, 900000)
        .then((src) => {
          if (src.statusCode !== 200) {
            req.destroy(new Error('request body unavailable: HTTP ' + src.statusCode))
            return
          }
          src.on('error', (error) => req.destroy(error))
          src.pipe(req)
        })
        .catch((error) => req.destroy(error))
    } else {
      if (body) req.write(body)
      req.end()
    }
  })
}

function pickResponseHeaders(raw) {
  const out = {}
  for (const [key, value] of Object.entries(raw)) {
    const lower = key.toLowerCase()
    if (['connection', 'keep-alive', 'transfer-encoding', 'content-length'].includes(lower)) continue
    out[lower] = value
  }
  return out
}

async function handleRequest(payload) {
  const id = payload.id
  stats.requests += 1
  // 先认领：网关已超时/浏览器已取消时不要再打后端，避免写操作被“迟到执行”。
  // 认领只做一次，网关不会重复投递同一条请求。
  try {
    const ack = await postJsonData('/__hx17/ack', { id })
    if (ack.gone) return
  } catch (error) {
    log('[warn] ack failed:', error.message)
    return
  }
  let res
  try {
    res = await upstreamRequest(payload)
  } catch (error) {
    stats.errors += 1
    const blocked = error && error.code === 'BLOCKED'
    const body = Buffer.from(JSON.stringify({
      code: blocked ? 403 : 502,
      error: blocked ? 'SERVICE_BLOCKED' : 'UPSTREAM_UNREACHABLE',
      msg: blocked ? '服务管理接口不允许通过公网入口访问' : `本机后端未连通：${error.message}`,
      data: null,
    }), 'utf8')
    try {
      await sendReply(id, blocked ? 403 : 502, { 'content-type': 'application/json; charset=utf-8' }, body)
      stats.replied += 1
    } catch (sendError) {
      log('[warn] reply failed:', sendError.message)
    }
    return
  }

  const headers = pickResponseHeaders(res.headers)
  const contentType = String(res.headers['content-type'] || '')
  const isSse = contentType.includes('text/event-stream')
  const length = Number(res.headers['content-length'] || 0)
  const chunked = Boolean(res.headers['transfer-encoding'])
  // 长度未知的分块响应也要走流式，否则会被 48MB 的整段缓冲上限截断。
  const mustStream = isSse || length > STREAM_THRESHOLD || (!length && chunked)

  try {
    if (!mustStream) {
      const body = await readAll(res, MAX_BUFFER)
      await sendReply(id, res.statusCode, headers, body)
      stats.replied += 1
      return
    }

    await sendHead(id, res.statusCode, headers, true)
    let buffer = []
    let bufferedBytes = 0
    let lastFlush = Date.now()

    const flush = async () => {
      if (!bufferedBytes) return
      const chunk = Buffer.concat(buffer, bufferedBytes)
      buffer = []
      bufferedBytes = 0
      lastFlush = Date.now()
      await sendChunk(id, chunk)
    }

    for await (const chunk of res) {
      buffer.push(chunk)
      bufferedBytes += chunk.length
      const elapsed = Date.now() - lastFlush
      if (!isSse) {
        if (bufferedBytes >= 512 * 1024) await flush()
      } else if (elapsed >= 120 || bufferedBytes >= 32 * 1024) {
        await flush()
      }
    }
    await flush()
    await sendEnd(id)
    stats.replied += 1
  } catch (error) {
    stats.errors += 1
    log('[warn] forward aborted', id.slice(0, 8), error.message)
    if (error && error.code === 'CLIENT_CLOSED') {
      try { res.destroy() } catch { /* ignore */ }
    }
    try { await sendEnd(id) } catch { /* 请求可能已经超时 */ }
  }
}

function readFrames(res, onFrame) {
  return new Promise((resolve, reject) => {
    let buffer = ''
    let first = 0
    const started = Date.now()
    res.setEncoding('utf8')
    res.on('data', (text) => {
      if (!first) first = Date.now()
      buffer += text
      let index = buffer.indexOf('\n')
      while (index >= 0) {
        const line = buffer.slice(0, index).trim()
        buffer = buffer.slice(index + 1)
        if (line) {
          try { onFrame(JSON.parse(line), first ? first - started : 0) } catch { /* 忽略坏帧 */ }
        }
        index = buffer.indexOf('\n')
      }
    })
    res.on('end', () => resolve())
    res.on('error', reject)
  })
}

async function lane(index) {
  while (!stopping) {
    const wait = mode === 'stream' ? 20000 : 0
    let opened = 0
    try {
      // 上行要短超时：平台重发布/回收会让旧连接变成“半开”，不报错也不再送数据，
      // 只有硬超时才能把这一条通道救回来。
      const res = await tunnelRequest(`/__hx17/uplink?mode=${mode}&wait=${wait}&lane=${index}`, 'GET', undefined, wait + 12000)
      if (res.statusCode === 403) {
        log('[fatal] tunnel token rejected; check .tunnel-token matches the cloud side')
        stopping = true
        return
      }
      if (res.statusCode !== 200) {
        await readAll(res, 4096).catch(() => {})
        await sleep(1000)
        continue
      }
      let job = null
      await readFrames(res, (frame, ttfb) => {
        if (frame.type === 'open') {
          opened = Date.now()
          // 首帧如果被平台缓冲，会等到整个响应结束才到；据此退回短轮询。
          if (mode === 'stream' && ttfb > 3500) {
            mode = 'fast'
            log('[mode] edge buffers responses; switch to short poll (slightly higher latency)')
          }
        } else if (frame.type === 'request') {
          job = frame
        }
      })
      if (job) await handleRequest(job)
      else if (mode === 'fast') await sleep(60)
      else if (!opened) await sleep(500)
    } catch (error) {
      if (stopping) return
      log('[warn] uplink broken: ' + error.message)
      await sleep(1200)
    }
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function selfCheck() {
  try {
    const res = await tunnelRequest('/__hx17/health', 'GET')
    const raw = await readAll(res, 65536)
    const body = JSON.parse(raw.toString('utf8'))
    log('[cloud] tunnel status:', JSON.stringify(body))
    return true
  } catch (error) {
    log('[cloud] cannot reach public entry: ' + error.message)
    return false
  }
}

async function checkUpstream() {
  try {
    const res = await new Promise((resolve, reject) => {
      const req = http.request(`${UPSTREAM}/api/health`, { agent: upstreamAgent, timeout: 5000 }, resolve)
      req.on('timeout', () => req.destroy(new Error('timeout')))
      req.on('error', reject)
      req.end()
    })
    const raw = await readAll(res, 65536)
    log('[upstream]', res.statusCode, raw.toString('utf8').slice(0, 200))
    return res.statusCode === 200
  } catch (error) {
    log('[upstream] cannot reach local backend: ' + error.message)
    return false
  }
}

async function main() {
  log(`[hx17] connector up: cloud=${PUBLIC} upstream=${UPSTREAM} lanes=${LANES}`)
  const upstreamOk = await checkUpstream()
  if (!upstreamOk) {
    log('[warn] local backend not responding yet; will recover automatically')
  }
  await selfCheck()
  const lanes = []
  for (let i = 0; i < LANES; i += 1) lanes.push(lane(i))
  process.on('SIGINT', () => {
    stopping = true
    log('[hx17] stop signal received, handled ' + stats.requests + ' requests')
    setTimeout(() => process.exit(0), 300)
  })
  setInterval(() => {
    log(`[stat] req=${stats.requests} replied=${stats.replied} err=${stats.errors} mode=${mode}`)
  }, 60000).unref?.()
  await Promise.all(lanes)
}

if (args.check) {
  checkUpstream().then((okFlag) => {
    if (!okFlag) process.exit(1)
    return selfCheck().then((cloudOk) => process.exit(cloudOk ? 0 : 1))
  })
} else {
  main().catch((error) => {
    console.error('[fatal]', error && error.message)
    process.exit(1)
  })
}
