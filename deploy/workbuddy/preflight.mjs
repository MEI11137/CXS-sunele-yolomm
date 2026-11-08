#!/usr/bin/env node
/* 启动前的本机自检（不连公网、不启动连接器、不打印任何凭据）
 *
 *   node deploy/workbuddy/preflight.mjs [--upstream http://127.0.0.1:8080]
 *
 * 只认本机 HTTP 上游，并且必须真的是本产品：HTTP 200 + code=0 + data.status=ok
 * + data.service=haixun-1.7-backend + 有版本字段。别的服务在同一端口也返回 200 时，
 * 这里必须拒绝——否则连接器会把业务请求转发给一个无关服务。
 *
 * 退出码：0 通过 / 2 Node 版本不够 / 3 缺隧道令牌 / 4 上游不可用或不是本产品 / 5 目录不完整
 */
import fs from 'node:fs/promises'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EXPECTED_SERVICE = 'haixun-1.7-backend'
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])

function argOf(name) {
  const index = process.argv.indexOf(name)
  return index > 0 && process.argv[index + 1] ? process.argv[index + 1] : ''
}

function goodbye(code, message, hint) {
  console.error('[preflight] 失败：' + message)
  if (hint) console.error('[preflight] 处理：' + hint)
  process.exit(code)
}

async function readToken() {
  const fromEnv = String(process.env.HX17_TUNNEL_TOKEN || '').trim()
  if (fromEnv) return { token: fromEnv, source: '环境变量 HX17_TUNNEL_TOKEN' }
  const file = path.join(HERE, '.tunnel-token')
  try {
    const text = (await fs.readFile(file, 'utf8')).trim()
    return { token: text, source: '本目录 .tunnel-token' }
  } catch {
    return { token: '', source: '' }
  }
}

/** 上游地址只允许本机 HTTP：https、远程主机、非法 URL 都给出可操作错误。 */
function parseUpstream(raw) {
  let url
  try {
    url = new URL(raw)
  } catch {
    return { error: `UPSTREAM 不是合法地址：${raw || '(空)'}`, hint: '写成 http://127.0.0.1:8080 这样的形式。' }
  }
  if (url.protocol !== 'http:') {
    return { error: `只支持本机 HTTP 上游，当前是 ${url.protocol}//`, hint: '后端是本机 Waitress（http://127.0.0.1:8080）；不要在连接器这一侧再加 TLS。' }
  }
  if (!LOOPBACK.has(url.hostname)) {
    return {
      error: `只支持本机回环地址，当前是 ${url.hostname}`,
      hint: '连接器的上游必须是这台机器自己的后端（127.0.0.1 / localhost）；跨机访问请改后端部署，不要在这里填远程地址。',
    }
  }
  if (!url.port) {
    return { error: 'UPSTREAM 没有写端口', hint: '写成 http://127.0.0.1:8080（或你实际使用的端口）。' }
  }
  return { url }
}

function probe(url) {
  return new Promise((resolve) => {
    const target = new URL('/api/health', url.origin)
    const req = http.get(target, { timeout: 4000 }, (res) => {
      const chunks = []
      res.on('data', (chunk) => chunks.push(chunk))
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        if (res.statusCode !== 200) {
          resolve({ ok: false, reason: `${target.href} 返回 HTTP ${res.statusCode}` })
          return
        }
        let payload
        try {
          payload = JSON.parse(text)
        } catch {
          resolve({ ok: false, reason: `${target.href} 的响应不是 JSON，这个端口上不一定是本产品的后端` })
          return
        }
        if (payload?.code !== 0) {
          resolve({ ok: false, reason: `${target.href} 返回 code=${payload?.code ?? '?'}，不是正常响应` })
          return
        }
        const data = payload?.data ?? {}
        if (data.status !== 'ok') {
          resolve({ ok: false, reason: `健康响应里 data.status=${JSON.stringify(data.status)}，不是 ok` })
          return
        }
        if (data.service !== EXPECTED_SERVICE) {
          resolve({
            ok: false,
            reason: `这个地址上的服务自报 "${data.service ?? '(空)'}"，不是 ${EXPECTED_SERVICE}`,
          })
          return
        }
        if (!data.version) {
          resolve({ ok: false, reason: '健康响应缺少 data.version，无法确认是本产品的哪个版本' })
          return
        }
        resolve({ ok: true, detail: `${data.service} ${data.version}` })
      })
    })
    req.on('timeout', () => req.destroy(new Error('4 秒内没有响应')))
    req.on('error', (error) => resolve({ ok: false, reason: `${target.href} 连不上（${error.code || error.message}）` }))
  })
}

async function main() {
  const major = Number(process.versions.node.split('.')[0])
  if (!Number.isFinite(major) || major < 18) {
    goodbye(2, `当前 Node 是 ${process.versions.node}，低于要求的 18`, '安装 Node 22 LTS，或设置 HX17_NODE 指向可用的 node.exe。')
  }

  for (const name of ['server.js', 'connector.js', 'package.json']) {
    try {
      await fs.access(path.join(HERE, name))
    } catch {
      goodbye(5, `deploy/workbuddy 下缺少 ${name}`, '确认是在完整仓库里运行；git clone 后不要删除 deploy/workbuddy 的源码。')
    }
  }

  const { token, source } = await readToken()
  if (!token || token.length < 16) {
    goodbye(
      3,
      token ? '隧道令牌长度异常（少于 16 个字符）' : '没有找到隧道令牌',
      '把本机匹配的 .tunnel-token 私密复制到 deploy/workbuddy/（不要重新生成、不要贴进聊天或命令行）。',
    )
  }

  const upstream = String(argOf('--upstream') || process.env.HX17_UPSTREAM || 'http://127.0.0.1:8080').trim()
  const parsed = parseUpstream(upstream)
  if (parsed.error) goodbye(4, `上游地址不可用：${parsed.error}`, parsed.hint)

  const health = await probe(parsed.url)
  if (!health.ok) {
    goodbye(
      4,
      `本机后端不可用：${health.reason}`,
      '先双击 启动海巡智检.cmd 并等它打印监听地址；端口不是 8080 时用 HX17_UPSTREAM 指定。',
    )
  }

  console.log('[preflight] 通过')
  console.log(`  Node     ${process.versions.node}（${process.execPath}）`)
  console.log(`  后端     ${parsed.url.origin} → ${health.detail}`)
  console.log(`  令牌     已就位（来源：${source}，${token.length} 字符，不打印内容）`)
}

try {
  await main()
} catch (error) {
  // 任何未预料异常都要变成可读错误 + 非零退出，不能静默成功。
  goodbye(4, `自检过程出错：${error?.message || error}`, '把这段输出发给维护者；不要靠重试掩盖。')
}
