#!/usr/bin/env node
/* WorkBuddy 云端入口：本机隔离验证（不连公网、不用真实令牌、不启动第二条公网通道）
 *
 *   node scripts/workbuddy_gateway_check.mjs [--out <json 路径>]
 *
 * 覆盖：
 *  1. prepare 能从「含中文与空格」的路径生成 web/；
 *  2. prepare 在 dist 缺失 / dist 里混入私有文件时明确失败，不静默复制；
 *  3. 网关：SPA 深路由、/api/service 拦截（含变形）、伪造 Origin 拒绝、连接器离线 503；
 *  4. 四个 js/mjs 文件的 node --check 语法检查。
 *
 * 所有子进程都用随机端口与随机令牌；不读取 deploy/workbuddy/.tunnel-token。
 */
import { spawn } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs/promises'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..')
const GW = path.join(REPO, 'deploy', 'workbuddy')
const checks = []
const report = { at: new Date().toISOString(), product: path.basename(REPO), checks }

function argOf(name) {
  const index = process.argv.indexOf(name)
  return index > 0 && process.argv[index + 1] ? process.argv[index + 1] : ''
}

async function check(name, fn) {
  try {
    const detail = await fn()
    checks.push(detail ? { name, passed: true, detail } : { name, passed: true })
  } catch (error) {
    checks.push({ name, passed: false, error: error?.message || String(error) })
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

function freePort() {
  return new Promise((resolve) => {
    const server = http.createServer()
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function request(port, pathname, options = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port,
      path: pathname,
      method: options.method || 'GET',
      headers: options.headers || {},
      timeout: options.timeout || 15000,
    }, (res) => {
      const chunks = []
      res.on('data', (chunk) => chunks.push(chunk))
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, text: Buffer.concat(chunks).toString('utf8') }))
    })
    req.on('timeout', () => req.destroy(new Error('请求超时：' + pathname)))
    req.on('error', reject)
    if (options.body) req.end(options.body)
    else req.end()
  })
}

function run(command, args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { ...options, stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('close', (code) => resolve({ code, stdout, stderr }))
  })
}

/** 回环假健康端点：用来证明 preflight 不会把“同端口上的别的服务”当成自己。 */
function startFakeHealth(payload) {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const body = Buffer.from(JSON.stringify(payload), 'utf8')
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': String(body.length) })
      res.end(body)
    })
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }))
  })
}

function runPreflight(extraArgs, env = {}) {
  return run(process.execPath, [path.join(GW, 'preflight.mjs'), ...extraArgs], {
    env: { ...process.env, HX17_TUNNEL_TOKEN: 'f'.repeat(48), ...env },
  })
}

async function copyGateway(destDir) {
  await fs.mkdir(destDir, { recursive: true })
  for (const name of ['server.js', 'connector.js', 'prepare.mjs', 'preflight.mjs', 'package.json', 'README.md']) {
    await fs.copyFile(path.join(GW, name), path.join(destDir, name))
  }
}

async function writeFakeDist(distDir, options = {}) {
  await fs.mkdir(path.join(distDir, 'assets'), { recursive: true })
  await fs.writeFile(path.join(distDir, 'index.html'), '<!doctype html><html><body><div id="app"></div><script src="/assets/app.js"></script></body></html>\n')
  await fs.writeFile(path.join(distDir, 'assets', 'app.js'), 'console.log("fake dist")\n')
  if (options.withPrivate) {
    await fs.writeFile(path.join(distDir, 'assets', '.tunnel-token'), 'should-never-be-copied\n')
  }
  if (options.withEnv) {
    await fs.writeFile(path.join(distDir, '.env'), 'SYNTHETIC_ONLY=1\n')
  }
}

async function main() {
  const work = await fs.mkdtemp(path.join(os.tmpdir(), 'hx17-wb-'))
  const spaced = path.join(work, '海巡 智检 v1.7 测试')
  const token = crypto.randomBytes(24).toString('hex')
  let gateway = null
  let connector = null
  let fakeUpstream = null

  try {
    // ---------------------------------------------------------------- 语法
    await check('四个入口文件 node --check 通过', async () => {
      for (const name of ['server.js', 'connector.js', 'prepare.mjs', 'preflight.mjs']) {
        const result = await run(process.execPath, ['--check', path.join(GW, name)])
        assert(result.code === 0, `${name} 语法检查失败：${result.stderr.trim().slice(0, 200)}`)
      }
      return 'server.js / connector.js / prepare.mjs / preflight.mjs'
    })

    // ---------------------------------------------------------------- 启动器静态检查
    await check('启动器：GBK 可解码、CRLF、标签齐全、无开发机绝对路径、无令牌', async () => {
      const file = path.join(REPO, '启动WorkBuddy云端连接.cmd')
      const bytes = await fs.readFile(file)
      const text = new TextDecoder('gbk', { fatal: true }).decode(bytes)
      assert(text.startsWith('@echo off'), '不是以 @echo off 开头')
      assert(text.includes('chcp 936'), '缺少 chcp 936（GBK 控制台）')
      assert(!(bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf), '文件带了 UTF-8 BOM，cmd.exe 会解析错位')
      const loneLf = /(?<!\r)\n/.test(text)
      assert(!loneLf, '存在单独的 LF，必须是 CRLF')
      const labels = new Set([...text.matchAll(/^:([A-Za-z0-9_]+)\s*$/gm)].map((m) => m[1]))
      const gotos = [...text.matchAll(/goto\s+:([A-Za-z0-9_]+)/g)].map((m) => m[1])
      const missing = gotos.filter((name) => !labels.has(name))
      assert(missing.length === 0, '跳转到不存在的标签：' + missing.join(', '))
      assert(!/[A-Za-z]:\\智能体创新应用大赛/.test(text), '写死了开发机绝对路径')
      assert(!/\b[0-9a-f]{48}\b/i.test(text), '疑似把 48 位十六进制令牌写进了脚本')
      return `${bytes.length} 字节，${gotos.length} 个 goto / ${labels.size} 个标签`
    })

    await check('启动器：失败分支不返回 0（静态）', async () => {
      const text = new TextDecoder('gbk').decode(await fs.readFile(path.join(REPO, '启动WorkBuddy云端连接.cmd')))
      const labelAt = (name) => {
        const match = new RegExp('^:' + name + '\\s*$', 'm').exec(text)
        assert(match, `找不到标签 :${name}`)
        return match.index
      }
      const segment = (from, to) => {
        const start = labelAt(from)
        const end = labelAt(to)
        assert(end > start, `标签顺序异常：:${from} 在 :${to} 之后`)
        return text.slice(start, end)
      }
      assert(
        text.includes('if "%HX17_EXIT%"=="0" goto :connector_stopped'),
        '连接器退出码判断缺失或被改动',
      )
      // preflight 的退出码（2/3/4/5）与连接器的退出码都必须经 :failed 原样返回。
      for (const [from, to] of [['preflight_failed', 'connector_failed'], ['connector_failed', 'failed']]) {
        const body = segment(from, to)
        assert(/goto\s+:failed/.test(body), `:${from} 没有转到失败出口 :failed`)
        assert(!/goto\s+:ok/.test(body), `:${from} 走回了成功出口 :ok`)
      }
      const failedBody = segment('failed', 'ok')
      assert(
        /endlocal\s*&\s*exit \/b %HX17_EXIT%/.test(failedBody),
        '失败出口没有把 HX17_EXIT 返回给调用方',
      )
      const okBody = text.slice(labelAt('ok'))
      assert(/endlocal\s*&\s*exit \/b 0/.test(okBody), '成功出口不是退出码 0')
      return 'preflight / connector 失败均经 :failed 保留原退出码'
    })

    // ---------------------------------------------------------------- prepare：正常路径
    await check('prepare 在含中文与空格的路径下生成 web/', async () => {
      await copyGateway(path.join(spaced, 'deploy', 'workbuddy'))
      await writeFakeDist(path.join(spaced, 'frontend', 'dist'))
      const result = await run(process.execPath, [path.join(spaced, 'deploy', 'workbuddy', 'prepare.mjs')], { cwd: spaced })
      assert(result.code === 0, `prepare 失败：${result.stderr.trim()}`)
      const index = await fs.readFile(path.join(spaced, 'deploy', 'workbuddy', 'web', 'index.html'), 'utf8')
      assert(index.includes('<div id="app">'), '生成的 web/index.html 不是期望内容')
      const asset = await fs.stat(path.join(spaced, 'deploy', 'workbuddy', 'web', 'assets', 'app.js'))
      assert(asset.isFile(), 'assets/app.js 没有复制过去')
      return path.relative(work, spaced)
    })

    // ---------------------------------------------------------------- prepare：缺 dist
    await check('prepare 在缺少 dist 时明确失败（不是空产物）', async () => {
      const bare = path.join(work, '没有构建')
      await copyGateway(path.join(bare, 'deploy', 'workbuddy'))
      const result = await run(process.execPath, [path.join(bare, 'deploy', 'workbuddy', 'prepare.mjs')], { cwd: bare })
      assert(result.code === 1, `期望退出码 1，实际 ${result.code}`)
      assert(result.stderr.includes('找不到前端构建产物'), '错误信息没有指出缺少构建产物')
      const exists = await fs.stat(path.join(bare, 'deploy', 'workbuddy', 'web')).then(() => true, () => false)
      assert(!exists, '缺 dist 时不应生成 web/ 目录')
    })

    // ---------------------------------------------------------------- prepare：拒绝私有文件
    await check('prepare 拒绝把 .tunnel-token 之类私有文件带进 web/', async () => {
      const dirty = path.join(work, '含私密文件')
      await copyGateway(path.join(dirty, 'deploy', 'workbuddy'))
      await writeFakeDist(path.join(dirty, 'frontend', 'dist'), { withPrivate: true })
      const result = await run(process.execPath, [path.join(dirty, 'deploy', 'workbuddy', 'prepare.mjs')], { cwd: dirty })
      assert(result.code === 1, `期望退出码 1，实际 ${result.code}`)
      assert(/不该发布的文件/.test(result.stderr), '错误信息没有说明文件不该发布')
      assert(/隧道令牌/.test(result.stderr), '错误信息没有指出是令牌文件')
      const leaked = await fs.stat(path.join(dirty, 'deploy', 'workbuddy', 'web', 'assets', '.tunnel-token'))
        .then(() => true, () => false)
      assert(!leaked, '私有文件仍然被复制进了 web/')
      const anyWeb = await fs.stat(path.join(dirty, 'deploy', 'workbuddy', 'web')).then(() => true, () => false)
      assert(!anyWeb, '校验失败时不应该生成 web/ 目录')
    })

    // ---------------------------------------------------------------- prepare：更多私有文件与安全边界
    await check('prepare 拒绝 .env / .git / 日志 / 本地配置 / 数据库', async () => {
      const dir = path.join(work, '脏产物多种')
      const gw = path.join(dir, 'deploy', 'workbuddy')
      await copyGateway(gw)
      const dist = path.join(dir, 'frontend', 'dist')
      await writeFakeDist(dist)
      const names = ['.env', '.env.production', 'trace.log', 'app.local.json', 'site.db', 'server.pem']
      for (const name of names) {
        await fs.writeFile(path.join(dist, 'assets', name), 'SYNTHETIC_ONLY\n')
        const result = await run(process.execPath, [path.join(gw, 'prepare.mjs')], { cwd: dir })
        assert(result.code === 1, `${name} 没有被拦住（退出码 ${result.code}）`)
        await fs.rm(path.join(dist, 'assets', name), { force: true })
      }
      await fs.mkdir(path.join(dist, '.git'), { recursive: true })
      const dotGit = await run(process.execPath, [path.join(gw, 'prepare.mjs')], { cwd: dir })
      assert(dotGit.code === 1, '.git 目录没有被拦住')
      return names.join(' / ') + ' / .git'
    })

    await check('prepare 失败时不破坏上一份可用 web/', async () => {
      const dir = path.join(work, '保留旧产物')
      const gw = path.join(dir, 'deploy', 'workbuddy')
      await copyGateway(gw)
      await fs.mkdir(path.join(gw, 'web'), { recursive: true })
      await fs.writeFile(path.join(gw, 'web', 'index.html'), 'KEEP-ME\n')
      await fs.writeFile(path.join(gw, 'web', 'app.js'), 'old build\n')
      await writeFakeDist(path.join(dir, 'frontend', 'dist'), { withEnv: true })
      const result = await run(process.execPath, [path.join(gw, 'prepare.mjs')], { cwd: dir })
      assert(result.code === 1, `期望失败，实际退出码 ${result.code}`)
      const kept = await fs.readFile(path.join(gw, 'web', 'index.html'), 'utf8')
      assert(kept.includes('KEEP-ME'), '失败后旧 web/index.html 被改掉了')
      const keptJs = await fs.readFile(path.join(gw, 'web', 'app.js'), 'utf8')
      assert(keptJs.includes('old build'), '失败后旧 web/app.js 被改掉了')
      return '失败后旧产物完整保留'
    })

    await check('prepare 拒绝 --dist 与 web/ 的祖先/后代重叠', async () => {
      const dir = path.join(work, '重叠路径')
      const gw = path.join(dir, 'deploy', 'workbuddy')
      await copyGateway(gw)
      await writeFakeDist(path.join(dir, 'frontend', 'dist'))
      const first = await run(process.execPath, [path.join(gw, 'prepare.mjs')], { cwd: dir })
      assert(first.code === 0, `首次生成失败：${first.stderr.trim()}`)

      const inside = await run(process.execPath, [path.join(gw, 'prepare.mjs'), '--dist', path.join(gw, 'web', 'assets')], { cwd: dir })
      assert(inside.code === 1, `指向 web 子目录没有被拒绝（退出码 ${inside.code}）`)
      assert(/重叠/.test(inside.stderr), '没有说明是路径重叠')

      const ancestor = await run(process.execPath, [path.join(gw, 'prepare.mjs'), '--dist', dir], { cwd: dir })
      assert(ancestor.code === 1, `指向仓库根（web 的祖先）没有被拒绝（退出码 ${ancestor.code}）`)
      assert(/重叠/.test(ancestor.stderr), '没有说明是路径重叠')
      return 'web 子目录 / 仓库根 两个方向都拒绝'
    })

    // ---------------------------------------------------------------- prepare：换名故障注入
    await check('prepare 换名失败会回滚旧 web/（故障注入）', async () => {
      const dir = path.join(work, '换名失败回滚')
      const gw = path.join(dir, 'deploy', 'workbuddy')
      await copyGateway(gw)
      await writeFakeDist(path.join(dir, 'frontend', 'dist'))
      await fs.mkdir(path.join(gw, 'web'), { recursive: true })
      await fs.writeFile(path.join(gw, 'web', 'index.html'), 'OLD-BUILD\n')
      const result = await run(process.execPath, [path.join(gw, 'prepare.mjs')], {
        cwd: dir,
        env: { ...process.env, HX17_PREPARE_FAULT: 'swap' },
      })
      assert(result.code === 1, `期望退出码 1，实际 ${result.code}`)
      const kept = await fs.readFile(path.join(gw, 'web', 'index.html'), 'utf8')
      assert(kept.includes('OLD-BUILD'), '回滚后 web/index.html 不是旧内容')
      const leftovers = (await fs.readdir(gw)).filter((name) => name.startsWith('.web-'))
      assert(leftovers.length === 0, '回滚成功后仍留下临时目录：' + leftovers.join(','))
      return '旧产物已还原，无残留临时目录'
    })

    await check('prepare 换名与回滚都失败时保留旧备份并报路径（故障注入）', async () => {
      const dir = path.join(work, '换名回滚都失败')
      const gw = path.join(dir, 'deploy', 'workbuddy')
      await copyGateway(gw)
      await writeFakeDist(path.join(dir, 'frontend', 'dist'))
      await fs.mkdir(path.join(gw, 'web'), { recursive: true })
      await fs.writeFile(path.join(gw, 'web', 'index.html'), 'ONLY-COPY\n')
      const result = await run(process.execPath, [path.join(gw, 'prepare.mjs')], {
        cwd: dir,
        env: { ...process.env, HX17_PREPARE_FAULT: 'swap+rollback' },
      })
      assert(result.code === 1, `期望退出码 1，实际 ${result.code}`)
      assert(/旧产物保留在|旧产物备份仍在/.test(result.stderr), '没有明确报告保留路径')
      const backups = (await fs.readdir(gw)).filter((name) => name.startsWith('.web-old-'))
      assert(backups.length === 1, `期望恰好一个 .web-old-* 备份，实际 ${backups.length}`)
      const kept = await fs.readFile(path.join(gw, backups[0], 'index.html'), 'utf8')
      assert(kept.includes('ONLY-COPY'), '备份里的内容不是旧产物')
      const webExists = await fs.stat(path.join(gw, 'web')).then(() => true, () => false)
      assert(!webExists, '两次失败后 web/ 不应存在（旧内容只在备份里）')
      const staging = (await fs.readdir(gw)).filter((name) => name.startsWith('.web-prepare-'))
      assert(staging.length === 0, '自有 staging 没有清理：' + staging.join(','))
      return `备份 ${backups[0]} 保留，staging 已清理`
    })

    // ---------------------------------------------------------------- preflight：不认错服务
    await check('preflight 拒绝同端口上的无关服务（不是只看 HTTP 200）', async () => {
      const cases = [
        { payload: { code: 0, data: { service: 'unrelated-service', status: 'ok', version: '9.9.9' } }, expect: /不是 haixun-1\.7-backend/ },
        { payload: { code: 0, data: { service: 'haixun-1.7-backend', status: 'degraded', version: '1.7.0' } }, expect: /data\.status/ },
        { payload: { code: 0, data: { service: 'haixun-1.7-backend', status: 'ok' } }, expect: /data\.version/ },
        { payload: { code: 500, data: { service: 'haixun-1.7-backend', status: 'ok', version: '1.7.0' } }, expect: /code=500/ },
      ]
      for (const item of cases) {
        const fake = await startFakeHealth(item.payload)
        try {
          const result = await runPreflight(['--upstream', `http://127.0.0.1:${fake.port}`])
          assert(result.code === 4, `期望退出码 4，实际 ${result.code}（payload=${JSON.stringify(item.payload)}）`)
          assert(item.expect.test(result.stderr), `错误信息没有匹配 ${item.expect}`)
        } finally {
          fake.server.close()
        }
      }
      return '4 种冒名响应全部拒绝'
    })

    await check('preflight 接受符合契约的健康响应（防止“永远失败”）', async () => {
      const fake = await startFakeHealth({ code: 0, data: { service: 'haixun-1.7-backend', status: 'ok', version: '1.7.0' } })
      try {
        const result = await runPreflight(['--upstream', `http://127.0.0.1:${fake.port}`])
        assert(result.code === 0, `期望通过，实际退出码 ${result.code}：${result.stderr.trim()}`)
        assert(result.stdout.includes('haixun-1.7-backend 1.7.0'), '没有打印识别到的服务与版本')
      } finally {
        fake.server.close()
      }
    })

    await check('preflight 拒绝 https、远程主机与非法地址（不崩溃）', async () => {
      const https = await runPreflight(['--upstream', 'https://127.0.0.1:8080'])
      assert(https.code === 4, `https 期望退出码 4，实际 ${https.code}`)
      assert(/只支持本机 HTTP/.test(https.stderr), 'https 没有被明确拒绝')

      const remote = await runPreflight(['--upstream', 'http://example.com:8080'])
      assert(remote.code === 4, `远程主机期望退出码 4，实际 ${remote.code}`)
      assert(/回环/.test(remote.stderr), '远程主机没有被明确拒绝')

      const broken = await runPreflight(['--upstream', 'not-a-url'])
      assert(broken.code === 4, `非法地址期望退出码 4，实际 ${broken.code}`)
      assert(/不是合法地址/.test(broken.stderr), '非法地址没有给出可读错误')
      assert(!/TypeError|UnhandledPromiseRejection/.test(broken.stderr), '出现了未捕获异常堆栈')
      return 'https / example.com / not-a-url 均 4'
    })

    // ---------------------------------------------------------------- 网关
    const realWeb = await fs.stat(path.join(GW, 'web', 'index.html')).then(() => true, () => false)
    if (!realWeb) {
      const prepared = await run(process.execPath, [path.join(GW, 'prepare.mjs')], { cwd: REPO })
      assert(prepared.code === 0, `仓库内 prepare 失败：${prepared.stderr.trim()}`)
    }

    const port = await freePort()
    gateway = spawn(process.execPath, ['server.js'], {
      cwd: GW,
      env: { ...process.env, PORT: String(port), HX17_TUNNEL_TOKEN: token, HX17_PUBLIC_ORIGIN: 'https://example.test' },
      stdio: 'ignore',
    })
    let ready = false
    for (let i = 0; i < 60; i += 1) {
      try {
        await request(port, '/')
        ready = true
        break
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 100))
      }
    }
    if (!ready) throw new Error('临时网关没有起来（端口 ' + port + '）')

    await check('SPA 深路由返回前端外壳', async () => {
      const res = await request(port, '/projects/prj_x/datasets')
      assert(res.status === 200, `状态码 ${res.status}`)
      assert(res.text.includes('<div id="app">'), '返回的不是 Vue 外壳')
      return 'GET /projects/prj_x/datasets → 200'
    })

    await check('/api/service 及其变形都不会被转发（403，或歧义地址 400）', async () => {
      const seen = []
      for (const pathname of ['/api/service/status', '//api/service/status', '/api/%73ervice/status', '/api/./service/status']) {
        const res = await request(port, pathname)
        // 403 = 明确识别为管理路由；400 BAD_URL = 地址有歧义，网关直接拒绝解析。
        // 两者都表示“没有进隧道、没有到后端”，不能要求实现为同一个状态码。
        assert(res.status === 403 || res.status === 400, `${pathname} 返回 ${res.status}`)
        assert(!res.text.includes('haixun-1.7-backend'), `${pathname} 疑似被转发到了后端`)
        seen.push(`${pathname} → ${res.status}`)
      }
      return seen.join('，')
    })

    await check('伪造 Origin 被网关拒绝（不进入隧道）', async () => {
      const res = await request(port, '/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Origin: 'https://evil.example', 'X-HX17-Tunnel': token },
        body: '{}',
      })
      assert(res.status === 403, `状态码 ${res.status}`)
      assert(res.text.includes('ORIGIN_REJECTED'), '没有返回 ORIGIN_REJECTED')
      return '403 ORIGIN_REJECTED'
    })

    await check('连接器不在时 /api 快速 503 TUNNEL_OFFLINE', async () => {
      const started = Date.now()
      const res = await request(port, '/api/health')
      const elapsed = Date.now() - started
      assert(res.status === 503, `状态码 ${res.status}`)
      assert(res.text.includes('TUNNEL_OFFLINE'), '没有返回 TUNNEL_OFFLINE')
      assert(elapsed < 8000, `耗时 ${elapsed}ms，期望快速失败`)
      return `503，${elapsed}ms`
    })

    await check('网关不认未带令牌的隧道握手', async () => {
      const res = await request(port, '/__hx17/health')
      assert(res.status === 403, `状态码 ${res.status}`)
    })

    // 最后一项要起一个**回环**连接器：证明分块上传的块哈希头与原始字节
    // 通过真实 connector.js 也能原样抵达上游（不是只在 Flask 直连时能用）。
    await check('网关经真实连接器转发 x-chunk-sha256 与原始字节（回环假上游）', async () => {
      const seen = []
      fakeUpstream = http.createServer((req, res) => {
        const chunks = []
        req.on('data', (chunk) => chunks.push(chunk))
        req.on('end', () => {
          const body = Buffer.concat(chunks)
          seen.push({
            headers: req.headers,
            sha: crypto.createHash('sha256').update(body).digest('hex'),
            bytes: body.length,
          })
          const payload = Buffer.from(JSON.stringify({ code: 0, msg: '', data: { ok: true } }), 'utf8')
          res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': String(payload.length) })
          res.end(payload)
        })
      })
      const upPort = await new Promise((resolve) => {
        fakeUpstream.listen(0, '127.0.0.1', () => resolve(fakeUpstream.address().port))
      })

      connector = spawn(
        process.execPath,
        ['connector.js', '--public', `http://127.0.0.1:${port}`, '--upstream', `http://127.0.0.1:${upPort}`, '--lanes', '2'],
        { cwd: GW, env: { ...process.env, HX17_TUNNEL_TOKEN: token }, stdio: 'ignore' },
      )
      let online = false
      for (let i = 0; i < 100; i += 1) {
        const probe = await request(port, '/api/health').catch(() => null)
        if (probe && probe.status !== 503) { online = true; break }
        await new Promise((resolve) => setTimeout(resolve, 150))
      }
      assert(online, '连接器在 15s 内没有上线')

      const chunk = crypto.randomBytes(64 * 1024)
      const digest = crypto.createHash('sha256').update(chunk).digest('hex')
      const res = await request(port, '/api/projects/prj_x/uploads/upl_x/chunks/0', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/octet-stream',
          'Content-Length': String(chunk.length),
          'X-Chunk-Sha256': digest,
          'X-Hx17-Extra': 'should-not-pass',
          Origin: 'https://example.test',
        },
        body: chunk,
      })
      assert(res.status === 200, `网关返回 ${res.status}：${res.text.slice(0, 200)}`)
      const chunksSeen = seen.filter((item) => item.headers['x-chunk-sha256'])
      assert(chunksSeen.length === 1, `带块哈希的请求数 ${chunksSeen.length}（期望 1）`)
      const got = chunksSeen[0]
      assert(
        got.headers['x-chunk-sha256'] === digest,
        `上游没收到 x-chunk-sha256（实际 ${got.headers['x-chunk-sha256']}）`,
      )
      assert(got.bytes === chunk.length && got.sha === digest, `上游收到的字节不一致（${got.bytes} 字节）`)
      assert(!got.headers['x-hx17-extra'], '非白名单头被透传了（请求头白名单被放宽）')
      return `${chunk.length} 字节、x-chunk-sha256 与字节都原样抵达；非白名单头未透传`
    })
  } finally {
    if (connector) connector.kill()
    if (fakeUpstream) fakeUpstream.close()
    if (gateway) gateway.kill()
    await fs.rm(work, { recursive: true, force: true }).catch(() => {})
  }

  report.passed = checks.filter((item) => item.passed).length
  report.total = checks.length
  const out = argOf('--out')
  const text = JSON.stringify(report, null, 2)
  if (out) {
    await fs.mkdir(path.dirname(out), { recursive: true })
    await fs.writeFile(out, text + '\n', 'utf8')
  }
  console.log(text)
  if (report.passed !== report.total) process.exitCode = 1
}

await main()
