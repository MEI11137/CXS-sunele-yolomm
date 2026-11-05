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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  let response: Response
  try {
    response = await fetch(BASE + path, {
      ...init,
      method,
      headers: authHeaders(method, init.headers),
      credentials: 'same-origin',
    })
  } catch (error) {
    throw new ApiError(0, '无法连接后端：' + (error as Error).message, 'NETWORK')
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
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
}

export const http = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, payload: unknown = {}) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(payload ?? {}) }),
  put: <T>(path: string, payload: unknown = {}) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(payload ?? {}) }),
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
