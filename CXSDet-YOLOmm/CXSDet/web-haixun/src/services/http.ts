// 统一请求层：所有真实接口都从这里出去。
//
// 约定（与后端 agent/routes.py 一致）：
// * 成功响应为 {code, msg, data}，code 为 200（部分旧接口为 0）。
// * 旧接口存在 HTTP 200 但 code=401/403 的情况，因此必须同时看 HTTP 状态
//   和业务 code，不能只看其中一个。
// * 失败时 data 是稳定错误对象 {code, message, execution_state, details?}，
//   前端按 code 分支，不按 message 文案匹配。
// * 未登录/过期统一清掉本地登录态，不留半登录状态。

export const TOKEN_KEY = 'haixun.token'

/** 后端稳定错误码（与 agent/errors.py 的 Code 对应，只做展示分支用）。 */
export interface ApiErrorBody {
  code?: string
  message?: string
  execution_state?: string
  details?: Record<string, unknown>
}

export type ExecutionState = 'not_executed' | 'partial' | 'executed' | 'unknown' | 'cancelled'

export class ApiError extends Error {
  /** HTTP 状态码；网络失败为 0。 */
  readonly httpStatus: number
  /** 业务错误码（如 AGENT_UNAUTHENTICATED）；无业务码时为 HTTP_<status>。 */
  readonly code: string
  /** 动作是否已执行；未知不得当成未执行。 */
  readonly executionState: ExecutionState
  readonly details: Record<string, unknown>

  constructor(
    message: string,
    options: {
      httpStatus?: number
      code?: string
      executionState?: ExecutionState
      details?: Record<string, unknown>
    } = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.httpStatus = options.httpStatus ?? 0
    this.code = options.code ?? (options.httpStatus ? `HTTP_${options.httpStatus}` : 'NETWORK_ERROR')
    this.executionState = options.executionState ?? 'unknown'
    this.details = options.details ?? {}
  }

  get isUnauthenticated(): boolean {
    return this.httpStatus === 401 || this.code === 'AGENT_UNAUTHENTICATED'
  }

  get isForbidden(): boolean {
    return this.httpStatus === 403 || this.code === 'AGENT_FORBIDDEN'
  }
}

/** 接口地址前缀：默认空，走 Vite 的 /api 代理，避免跨域与硬编码端口。 */
const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/+$/, '')

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* localStorage 不可用时按无登录态处理 */
  }
}

type UnauthorizedHandler = () => void
const unauthorizedHandlers = new Set<UnauthorizedHandler>()

/** 订阅登录失效（401）。返回取消订阅函数。 */
export function onUnauthorized(handler: UnauthorizedHandler): () => void {
  unauthorizedHandlers.add(handler)
  return () => unauthorizedHandlers.delete(handler)
}

function notifyUnauthorized(): void {
  setToken(null)
  for (const handler of unauthorizedHandlers) {
    try {
      handler()
    } catch {
      /* 订阅方异常不影响请求本身 */
    }
  }
}

type QueryValue = string | number | boolean | undefined | null

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: Record<string, QueryValue>
  /** JSON 请求体。与 form 互斥。 */
  body?: unknown
  /** multipart 请求体，用于文件上传。与 body 互斥。 */
  form?: FormData
  signal?: AbortSignal
  /** 默认 json；blob 用于受保护图片/下载。 */
  responseType?: 'json' | 'blob' | 'text'
  /** 是否附带登录 Token，默认 true。 */
  auth?: boolean
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = `${BASE}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    search.append(key, String(value))
  }
  const qs = search.toString()
  return qs ? `${url}?${qs}` : url
}

function parseBusinessError(status: number, payload: unknown, fallback: string): ApiError {
  const envelope = payload as { code?: unknown; msg?: unknown; message?: unknown; data?: unknown } | null
  const business =
    envelope && typeof envelope.data === 'object' && envelope.data !== null
      ? (envelope.data as ApiErrorBody)
      : undefined

  const code = typeof business?.code === 'string' ? business.code : undefined
  const message =
    (typeof business?.message === 'string' && business.message) ||
    (typeof envelope?.msg === 'string' && envelope.msg) ||
    (typeof envelope?.message === 'string' && envelope.message) ||
    fallback
  const executionState = business?.execution_state as ExecutionState | undefined

  return new ApiError(message, {
    httpStatus: status,
    code,
    executionState: executionState ?? 'unknown',
    details: business?.details ?? {},
  })
}

/**
 * 发一次请求并把 {code, msg, data} 拆开，只返回 data。
 * 真实模式下失败一律抛 ApiError，不返回看起来成功的空值。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, form, signal, responseType = 'json', auth = true } = options

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: form ?? (body === undefined ? undefined : JSON.stringify(body)),
      signal,
      // 文件会话 cookie（cxs_file_session）需要同源携带
      credentials: 'include',
    })
  } catch (error) {
    if ((error as DOMException)?.name === 'AbortError') throw error
    throw new ApiError('无法连接后端服务，请确认服务已启动', { httpStatus: 0 })
  }

  if (response.status === 401) {
    notifyUnauthorized()
  }

  if (responseType === 'blob') {
    if (!response.ok) {
      const payload = await safeJson(response)
      throw parseBusinessError(response.status, payload, `请求失败（HTTP ${response.status}）`)
    }
    return (await response.blob()) as T
  }

  if (responseType === 'text') {
    const text = await response.text()
    if (!response.ok) {
      throw parseBusinessError(response.status, tryParse(text), `请求失败（HTTP ${response.status}）`)
    }
    return text as T
  }

  // 空响应（204 等）
  if (response.status === 204) return undefined as T

  const payload = await safeJson(response)
  if (payload === undefined) {
    if (!response.ok) {
      throw new ApiError(`请求失败（HTTP ${response.status}）`, { httpStatus: response.status })
    }
    return undefined as T
  }

  const envelope = payload as { code?: unknown; msg?: unknown; data?: unknown }
  const hasEnvelope = typeof envelope === 'object' && envelope !== null && 'code' in envelope

  if (!response.ok) {
    throw parseBusinessError(response.status, payload, `请求失败（HTTP ${response.status}）`)
  }

  if (hasEnvelope) {
    const rawCode = envelope.code as number | string
    const ok = rawCode === 200 || rawCode === 0 || rawCode === '200' || rawCode === '0'
    if (!ok) {
      // HTTP 200 但业务 code 是 401/403：仍然算失败，不能当成功
      const status = typeof rawCode === 'number' ? rawCode : Number(rawCode) || 400
      if (status === 401) notifyUnauthorized()
      throw parseBusinessError(status, payload, '请求失败')
    }
    return envelope.data as T
  }

  return payload as T
}

async function safeJson(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return undefined
  return tryParse(text)
}

function tryParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return { msg: text.slice(0, 300) }
  }
}

/** 便捷方法。 */
export const http = {
  get: <T>(path: string, options: Omit<RequestOptions, 'method' | 'body' | 'form'> = {}) =>
    request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'PATCH', body }),
  del: <T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'DELETE' }),
  upload: <T>(path: string, form: FormData, options: Omit<RequestOptions, 'method' | 'form'> = {}) =>
    request<T>(path, { ...options, method: 'POST', form }),
  /** 受保护图片：不能给 <img> 塞 Bearer 头，走带 Token 的 blob 请求再转 object URL。 */
  blobUrl: async (path: string, options: Omit<RequestOptions, 'method'> = {}) => {
    const blob = await request<Blob>(path, { ...options, method: 'GET', responseType: 'blob' })
    return URL.createObjectURL(blob)
  },
}
