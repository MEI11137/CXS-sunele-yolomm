import type { ApiEnvelope } from '@/types'

/** 接口错误：保留 HTTP 状态与后端机器码，界面据此区分“未登录/无权限/真失败”。 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, message: string, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

// 生产同源，开发靠 vite 代理；都走 /api，不在业务代码里写端口。
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api'

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
