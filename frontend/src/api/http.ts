import type { ApiEnvelope } from '@/types'

/** 接口错误：保留 HTTP 状态与后端机器码，界面据此区分“未登录/无权限/真失败”。 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  /** 后端在 data 里给的结构化细节（例如类别映射需要的两份清单），没有就是 null。 */
  readonly details: unknown

  constructor(status: number, message: string, code = '', details: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

// 生产同源，开发靠 vite 代理；都走 /api，不在业务代码里写端口。
// 允许测试/嵌入式宿主通过 globalThis.__HX17_API_BASE__ 指定绝对地址（浏览器里保持默认）。
const OVERRIDE = (globalThis as { __HX17_API_BASE__?: string }).__HX17_API_BASE__
const BASE = OVERRIDE || '/api'

/** 业务适配器需要自己发请求时（例如 multipart 上传）用它拼地址，别手写 `/api` 前缀。 */
export function apiUrl(path: string): string {
  return BASE + path
}

/**
 * 业务请求收到 401 时统一通知（登录态失效），避免只有启动/刷新才发现掉线。
 *
 * 放在 http 层而不是每个 store 各写一遍：multipart 上传、分块上传、文档上传等
 * 直接 fetch 的入口也要走同一条恢复路径，否则会继续显示旧用户/空数据。
 * 用回调注入而不是 import auth store，避免 http ⇄ auth 的循环依赖。
 */
type UnauthorizedListener = () => void
let unauthorizedListener: UnauthorizedListener | null = null
let unauthorizedNotified = false
/**
 * 账号代次：登录/退出/失效时 +1。请求发出时记下代次，401 回来时只有代次没变
 * 才允许通知失效——否则旧账号的慢 401 会把刚登录的新账号踢下线。
 */
let authScope = 0

export function captureAuthScope(): number {
  return authScope
}

export function bumpAuthScope(): void {
  authScope += 1
}

export function setUnauthorizedListener(listener: UnauthorizedListener): void {
  unauthorizedListener = listener
}

/** 收到 401 时调用；同一轮失效只通知一次，避免并发请求触发多次跳转。 */
export function notifyUnauthorized(status: number): void {
  if (status !== 401 || unauthorizedNotified || !unauthorizedListener) return
  unauthorizedNotified = true
  unauthorizedListener()
}

/** 登录成功/主动退出后重置，下一次会话失效仍能通知。 */
export function resetUnauthorizedNotification(): void {
  unauthorizedNotified = false
}

function readCookie(name: string): string {
  const prefix = `${name}=`
  for (const part of document.cookie.split(';')) {
    const item = part.trim()
    if (item.startsWith(prefix)) return decodeURIComponent(item.slice(prefix.length))
  }
  return ''
}

/**
 * 写操作带 CSRF 头：后端要求它与同名 Cookie 双提交一致。
 * 令牌由后端在任意 GET 响应里下发（非 HttpOnly），刷新页面后依然可用。
 */
function authHeaders(method: string, extra?: HeadersInit): Headers {
  const headers = new Headers(extra)
  headers.set('Accept', 'application/json')
  if (method !== 'GET' && method !== 'HEAD') {
    headers.set('Content-Type', 'application/json')
    const token = readCookie('hx17_csrf')
    if (token) headers.set('X-CSRF-Token', token)
  }
  return headers
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs?: number): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const scope = authScope
  const controller = timeoutMs ? new AbortController() : null
  // 超时必须覆盖“响应体读取”全过程：headers 到了但 body 挂起同样要能恢复。
  const timer = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : null
  const timedOut = () =>
    new ApiError(0, `请求超时（后端 ${Math.round((timeoutMs ?? 0) / 1000)} 秒内没有响应）`, 'TIMEOUT')
  try {
    let response: Response
    try {
      response = await fetch(BASE + path, {
        ...init,
        method,
        headers: authHeaders(method, init.headers),
        credentials: 'same-origin',
        ...(controller ? { signal: controller.signal } : {}),
      })
    } catch (error) {
      if (scope !== authScope) throw new ApiError(0, '登录账号已切换，已忽略旧请求结果', 'STALE_AUTH')
      if (controller?.signal.aborted) throw timedOut()
      throw new ApiError(0, '无法连接后端：' + (error as Error).message, 'NETWORK')
    }
    // 旧账号的慢成功也不能回填新账号：直接终止该调用，调用方不得拿到数据。
    if (scope !== authScope) throw new ApiError(0, '登录账号已切换，已忽略旧请求结果', 'STALE_AUTH')

    let payload: unknown = null
    // 登录表单的 401 是“账号密码错误”，启动时的 auth/me 由 auth store 自己区分
    // 匿名/服务不可用；其余受保护接口的 401 一律按会话失效处理。
    if (
      response.status === 401 &&
      scope === authScope &&
      !path.startsWith('/auth/login') &&
      !path.startsWith('/auth/me') &&
      !path.startsWith('/meta')
    ) {
      notifyUnauthorized(response.status)
    }
    try {
      payload = await response.json()
    } catch (error) {
      if (controller?.signal.aborted) throw timedOut()
      // 非 JSON 或空响应体不在这里失败；下面按 HTTP 状态给明确错误。
      payload = null
    }
    // response.json() 也是 await：body 慢返回期间账号可能已经切换，解析后再校验一次。
    if (scope !== authScope) throw new ApiError(0, '登录账号已切换，已忽略旧请求结果', 'STALE_AUTH')
    const envelope = payload as ApiEnvelope<T> | null
    if (!response.ok || !envelope || envelope.code !== 0) {
      throw new ApiError(
        response.status,
        envelope?.msg || `请求失败（HTTP ${response.status}）`,
        envelope?.error ?? '',
        (envelope as { data?: unknown } | null)?.data ?? null,
      )
    }
    return envelope.data
  } finally {
    if (timer !== null) window.clearTimeout(timer)
  }
}

export const http = {
  // timeoutMs 只给启动/登录态这类轻量 GET 用；上传、训练、长任务不设统一短超时。
  get: <T>(path: string, options: { timeoutMs?: number } = {}) =>
    request<T>(path, {}, options.timeoutMs),
  post: <T>(path: string, payload: unknown = {}) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(payload ?? {}) }),
  put: <T>(path: string, payload: unknown = {}) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(payload ?? {}) }),
  patch: <T>(path: string, payload: unknown = {}) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(payload ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : String(error)
}

export function errorCode(error: unknown): string {
  return error instanceof ApiError ? error.code : ''
}

/**
 * HTTP 状态码。0 表示**请求没有到服务器**（断网/后端挂了），结果未知；
 * 其余表示服务端明确给了答复，界面不该把它当成“稍后自动会好”。
 */
export function errorStatus(error: unknown): number {
  return error instanceof ApiError ? error.status : 0
}
