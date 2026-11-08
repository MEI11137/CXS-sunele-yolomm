#!/usr/bin/env node
/* 海巡智检 v1.7 · WorkBuddy 云端入口准备脚本
 *
 *   node deploy/workbuddy/prepare.mjs            # 默认读 <repo>/frontend/dist
 *   node deploy/workbuddy/prepare.mjs --dist X   # 指定别的构建产物目录
 *
 * 行为顺序（先校验、后替换，失败不破坏上一份可用产物）：
 *   1. 校验网关文件齐全；
 *   2. 校验 dist 存在、有 index.html 与 assets/；
 *   3. 拒绝 dist 与 web/ 有祖先/后代重叠（否则会先删掉输入再复制）；
 *   4. **先整棵扫描 dist**：私有文件、符号链接、非普通文件一律中止，此时还没动 web/；
 *   5. 复制到同目录的临时目录 .web-prepare-<pid>，再原子换名成 web/；
 *      换名失败会把旧的 web/ 改回来。
 *
 * 不读也不写 data-dir、var/、数据库、账号、模型凭据；不生成、不复制、不打印 .tunnel-token。
 * 路径含中文/空格按原样处理（全程 Node 文件 API，不拼命令行）。
 */
import { createHash } from 'node:crypto'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..', '..')
const TARGET = path.join(HERE, 'web')

/**
 * 按**文件名**拦截，不看目录层级：导出/临时目录里常见的私有或运行态文件
 * 一旦混进构建产物，就不能发布到云端。
 */
const DENY = [
  { rule: /^\.tunnel-token$/i, why: '隧道令牌' },
  { rule: /^\.env(\..+)?$/i, why: '环境变量文件（可能含 Key）' },
  { rule: /^\.git$/i, why: 'Git 目录' },
  { rule: /\.local\./i, why: '本机私有配置' },
  { rule: /\.(db|sqlite3|key|pem|crt|cer|p12|pfx|log)$/i, why: '数据库/证书/日志' },
  { rule: /^id_(rsa|dsa|ecdsa|ed25519)$/i, why: 'SSH 私钥' },
  { rule: /^secrets$/i, why: '密钥目录' },
  { rule: /^var$/i, why: '运行数据目录' },
]

function denyReason(name) {
  const hit = DENY.find((item) => item.rule.test(name))
  return hit ? hit.why : ''
}

/**
 * 仅供隔离测试的换名故障注入（HX17_PREPARE_FAULT=swap / swap+rollback）。
 * 生产环境不要设置；它只影响两步 rename，不改任何复制逻辑。
 */
const FAULT = String(process.env.HX17_PREPARE_FAULT || '')
function injectFault(stage) {
  if (!FAULT) return
  const wanted = FAULT.split(/[,+\s]+/).filter(Boolean)
  if (wanted.includes(stage)) throw new Error(`注入故障：${stage} 换名失败`)
}

/** 回滚也失败时要保留的旧产物路径；外层 catch 用它把路径明确报出来。 */
let preservedBackup = ''

function fail(message, hint) {
  console.error('[prepare] 失败：' + message)
  if (hint) console.error('[prepare] 处理：' + hint)
  process.exit(1)
}

function argOf(name) {
  const index = process.argv.indexOf(name)
  return index > 0 && process.argv[index + 1] ? process.argv[index + 1] : ''
}

async function statOrNull(target) {
  try {
    return await fs.stat(target)
  } catch {
    return null
  }
}

/** child 是否等于 parent 或位于其内部（用于拒绝祖先/后代重叠）。 */
function contains(parent, child) {
  const rel = path.relative(parent, child)
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
}

/** 只扫描、不写入：任何私有文件/符号链接都在这一步中止。 */
async function scanTree(root, relative = '') {
  const entries = await fs.readdir(path.join(root, relative), { withFileTypes: true })
  const files = []
  for (const entry of entries) {
    const rel = relative ? path.join(relative, entry.name) : entry.name
    const why = denyReason(entry.name)
    if (why) {
      fail(
        `构建产物里出现了不该发布的文件「${rel}」（${why}）`,
        '从 dist 里移除它再重跑；这个脚本不会替你删除源文件，也不会把它复制进 web/。',
      )
    }
    if (entry.isSymbolicLink()) {
      fail(`构建产物里有符号链接「${rel}」`, '发布材料只接受普通文件与目录，请重新构建前端。')
    }
    if (entry.isDirectory()) {
      files.push(...(await scanTree(root, rel)))
      continue
    }
    if (!entry.isFile()) {
      fail(`构建产物里有特殊文件「${rel}」`, '发布材料只接受普通文件。')
    }
    files.push(rel)
  }
  return files
}

function overlapHint() {
  return '把 --dist 指向 frontend/dist；不要指向 repo 根、deploy/workbuddy 或 web 的子目录。'
}

async function main() {
  const dist = path.resolve(argOf('--dist') || path.join(REPO, 'frontend', 'dist'))

  for (const name of ['server.js', 'connector.js', 'package.json']) {
    const file = await statOrNull(path.join(HERE, name))
    if (!file || !file.isFile()) {
      fail(`缺少网关文件 deploy/workbuddy/${name}`, '请在完整仓库内运行本脚本（git clone 后不要删除 deploy/workbuddy 下的源码）。')
    }
  }

  // 源与目标不能互相包含：否则“先清理 web/”会把输入删掉，或把整个仓库复制进 web/。
  if (contains(dist, TARGET) || contains(TARGET, dist)) {
    fail(`构建产物目录与 web/ 有重叠：${dist} ↔ ${TARGET}`, overlapHint())
  }

  const distStat = await statOrNull(dist)
  if (!distStat || !distStat.isDirectory()) {
    fail(
      `找不到前端构建产物：${dist}`,
      '先在 frontend 目录执行 npm ci（或 npm install）与 npm run build，再重跑本脚本。',
    )
  }
  if (!(await statOrNull(path.join(dist, 'index.html')))?.isFile()) {
    fail(`${dist} 里没有 index.html，这不是一个可用的前端构建产物`, '确认构建命令是 npm run build（产物目录 dist）。')
  }
  if (!(await statOrNull(path.join(dist, 'assets')))?.isDirectory()) {
    fail(`${dist} 里没有 assets/ 目录`, '重新执行 npm run build；只有 index.html 不算构建完成。')
  }

  // 先整棵校验：不通过就在这里退出，此时 web/ 还是上一份可用产物。
  const relatives = await scanTree(dist)
  if (!relatives.length) fail(`${dist} 是空目录`, '重新构建前端。')

  const staging = path.join(HERE, `.web-prepare-${process.pid}`)
  const previous = path.join(HERE, `.web-old-${process.pid}`)
  await fs.rm(staging, { recursive: true, force: true })
  await fs.rm(previous, { recursive: true, force: true })
  try {
    let bytes = 0
    for (const rel of relatives) {
      const from = path.join(dist, rel)
      const to = path.join(staging, rel)
      await fs.mkdir(path.dirname(to), { recursive: true })
      const buffer = await fs.readFile(from)
      await fs.writeFile(to, buffer)
      bytes += buffer.length
    }

    // 换名之前再核一次落盘结果（防扫描后被替换的极端情况）。
    const staged = await scanTree(staging)
    if (staged.length !== relatives.length) {
      throw new Error(`复制后的文件数量与源不一致（源 ${relatives.length}，落盘 ${staged.length}）`)
    }

    const hadTarget = Boolean(await statOrNull(TARGET))
    if (hadTarget) await fs.rename(TARGET, previous)
    try {
      injectFault('swap')
      await fs.rename(staging, TARGET)
    } catch (error) {
      let restored = false
      if (hadTarget) {
        try {
          injectFault('rollback')
          await fs.rename(previous, TARGET)
          restored = true
        } catch {
          // 回滚失败：保留 .web-old-*，由外层把路径报出来，绝不在这里删除。
        }
      }
      if (!restored && hadTarget) {
        preservedBackup = previous
        throw new Error(`替换 web/ 失败，且无法自动还原；旧产物保留在 ${previous}（原始错误：${error.message}）`)
      }
      throw error
    }
    // 只有成功换名之后，.web-old-* 才是可以丢弃的旧副本。
    if (hadTarget) await fs.rm(previous, { recursive: true, force: true }).catch(() => {})

    const indexBytes = await fs.readFile(path.join(TARGET, 'index.html'))
    const digest = createHash('sha256').update(indexBytes).digest('hex')
    const token = await statOrNull(path.join(HERE, '.tunnel-token'))

    console.log('[prepare] 完成')
    console.log(`  来源     ${dist}`)
    console.log(`  输出     ${TARGET}`)
    console.log(`  文件     ${relatives.length} 个，共 ${(bytes / 1024).toFixed(1)} KB`)
    console.log(`  index    sha256 ${digest}`)
    console.log(`  令牌     ${token ? '已存在（本机私有，不打印内容、不复制）' : '未找到 .tunnel-token（发布前须单独私密放入）'}`)
    console.log('[prepare] 可发布材料 = 本目录（server.js / connector.js / package.json / web/ / 必要配置）')
  } finally {
    // 只清理本脚本自己的临时目录；.web-old-* 由成功分支清理，
    // 失败/回滚失败时**绝不**在这里删除——那可能是唯一的旧产物备份。
    await fs.rm(staging, { recursive: true, force: true }).catch(() => {})
  }
}

await main().catch((error) => {
  if (preservedBackup) {
    console.error('[prepare] 旧产物备份仍在：' + preservedBackup)
    console.error('[prepare] 处理：确认 web/ 现状后把该目录改名回 web/ 再重试；不要删除这个备份。')
  }
  fail(error?.message || String(error))
})
