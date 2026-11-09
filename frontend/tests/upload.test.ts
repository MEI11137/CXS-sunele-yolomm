/**
 * 分块上传的**前端恢复路径**测试。
 *
 * 关心的不是界面，而是三件会直接误导用户的事：
 * 1. 合并请求的响应丢了，但服务端其实已经成功 → 必须把那个资产取回来，
 *    不能报“失败”让用户重传（那会产生第二份资产）；
 * 2. 5xx / UPLOAD_BUSY 是暂时性的 → 重试同一个 upload_id（服务端幂等）；
 * 3. 块缺失这类确定性失败 → 明确报错并取消会话，不留半截上传占槽位。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

const BASE = 'https://hx.test/api'

// business.ts 在模块加载时读取这两个全局量，所以先设好再动态 import。
;(globalThis as { __HX17_API_BASE__?: string }).__HX17_API_BASE__ = BASE
;(globalThis as { document?: unknown }).document = { cookie: 'hx17_csrf=test-token' }

const CHUNK = 4 * 1024 * 1024
const SIZE = 9 * 1024 * 1024 // 超过 8MiB 阈值 → 切成 3 块
const UPLOAD = {
  id: 'upl_test',
  status: 'uploading',
  filename: 'big.zip',
  size: SIZE,
  chunk_size: CHUNK,
  chunk_count: 3,
  received_bytes: 0,
  asset_id: null as string | null,
  expires_at: 0,
}
const ASSET = {
  id: 'ast_recovered',
  kind: 'archive',
  role: 'archive',
  filename: 'big.zip',
  size: SIZE,
}

type Call = { method: string; url: string }

function envelope(data: unknown, status = 200): Response {
  return new Response(JSON.stringify({ code: status === 200 ? 0 : status, msg: '', data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function failure(status: number, error: string, msg = ''): Response {
  return new Response(JSON.stringify({ code: status, msg, error, data: null }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeFile(): File {
  return new File([new Uint8Array(SIZE)], 'big.zip', { type: 'application/zip' })
}

function installFetch(handler: (call: Call) => Promise<Response> | Response): Call[] {
  const calls: Call[] = []
  ;(globalThis as { fetch?: unknown }).fetch = async (input: unknown, init: RequestInit = {}) => {
    const call = { method: (init.method || 'GET').toUpperCase(), url: String(input) }
    calls.push(call)
    return handler(call)
  }
  return calls
}

async function loadUploader() {
  const module = await import('../src/api/business.ts')
  return module.uploadAsset
}

test('finish 响应丢失但服务端已成功：取回同一资产，不误报失败也不 abort', async () => {
  let finishCalls = 0
  const calls = installFetch(({ method, url }) => {
    if (method === 'POST' && url === `${BASE}/projects/prj_1/uploads`) {
      return envelope({ upload: { ...UPLOAD, received_bytes: 0 } })
    }
    if (method === 'PUT' && url.includes('/chunks/')) {
      return envelope({ upload: { ...UPLOAD, received_bytes: CHUNK } })
    }
    if (method === 'POST' && url.endsWith('/finish')) {
      finishCalls += 1
      // 服务端已经合并并登记成功，只是这个响应在网络上丢了。
      throw new TypeError('fetch failed')
    }
    if (method === 'GET' && url === `${BASE}/projects/prj_1/uploads/upl_test`) {
      return envelope({
        upload: { ...UPLOAD, status: 'completed', asset_id: ASSET.id, received_bytes: SIZE },
      })
    }
    if (method === 'GET' && url === `${BASE}/projects/prj_1/assets/ast_recovered`) {
      return envelope({ asset: ASSET })
    }
    throw new Error(`unexpected ${method} ${url}`)
  })

  const uploadAsset = await loadUploader()
  const asset = await uploadAsset('prj_1', makeFile())

  assert.equal(asset.id, ASSET.id)
  assert.equal(finishCalls, 1, '只在状态未知时重试一次，不应重复合并')
  assert.equal(
    calls.some((call) => call.url.endsWith('/abort')),
    false,
    '结果已知成功时不应该取消会话',
  )
})

test('finish 5xx 是暂时性的：同 ID 重试后成功', async () => {
  let finishCalls = 0
  const calls = installFetch(({ method, url }) => {
    if (method === 'POST' && url === `${BASE}/projects/prj_1/uploads`) {
      return envelope({ upload: { ...UPLOAD, received_bytes: 0 } })
    }
    if (method === 'PUT' && url.includes('/chunks/')) {
      return envelope({ upload: { ...UPLOAD, received_bytes: CHUNK } })
    }
    if (method === 'POST' && url.endsWith('/finish')) {
      finishCalls += 1
      if (finishCalls <= 2) return failure(502, 'BAD_GATEWAY', '上游暂时不可用')
      return envelope({ asset: ASSET, upload: { ...UPLOAD, status: 'completed' } })
    }
    if (method === 'GET' && url === `${BASE}/projects/prj_1/uploads/upl_test`) {
      return envelope({ upload: { ...UPLOAD, status: 'assembling' } })
    }
    throw new Error(`unexpected ${method} ${url}`)
  })

  const uploadAsset = await loadUploader()
  const asset = await uploadAsset('prj_1', makeFile())

  assert.equal(asset.id, ASSET.id)
  assert.equal(finishCalls, 3)
  assert.equal(calls.some((call) => call.url.endsWith('/abort')), false)
})

test('块缺失是确定性失败：明确报错并取消会话', async () => {
  const calls = installFetch(({ method, url }) => {
    if (method === 'POST' && url === `${BASE}/projects/prj_1/uploads`) {
      return envelope({ upload: { ...UPLOAD, received_bytes: 0 } })
    }
    if (method === 'PUT' && url.includes('/chunks/')) {
      return envelope({ upload: { ...UPLOAD, received_bytes: CHUNK } })
    }
    if (method === 'POST' && url.endsWith('/finish')) {
      return failure(409, 'CHUNKS_MISSING', '还有 1 块没有上传完成')
    }
    if (method === 'POST' && url.endsWith('/abort')) {
      return envelope({ cancelled: true, tmp_cleaned: true })
    }
    throw new Error(`unexpected ${method} ${url}`)
  })

  const uploadAsset = await loadUploader()
  await assert.rejects(
    () => uploadAsset('prj_1', makeFile()),
    (error: { code?: string }) => error.code === 'CHUNKS_MISSING',
  )
  assert.equal(
    calls.filter((call) => call.url.endsWith('/abort')).length,
    1,
    '确定性失败应取消会话，不留半截上传',
  )
})
