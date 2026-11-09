import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError, bumpAuthScope, errorMessage, http, resetUnauthorizedNotification } from '@/api/http'
import type { MetaData, User } from '@/types'

export type AuthState = 'unknown' | 'anonymous' | 'authenticated' | 'error'

/**
 * 登录态。
 *
 * 令牌在 HttpOnly Cookie 里，前端只通过 /auth/me 判断是否有效；
 * 退出登录会先调后端吊销会话，再清空本地状态（工作区、连接等由调用方 reset）。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const meta = ref<MetaData | null>(null)
  const state = ref<AuthState>('unknown')
  const error = ref('')
  const busy = ref(false)

  const isAuthenticated = computed(() => state.value === 'authenticated' && user.value !== null)
  const isAdmin = computed(() => user.value?.role === 'admin')
  const initialized = computed(() => meta.value?.initialized !== false)

  async function loadMeta(): Promise<void> {
    try {
      // 启动阶段的后端可能“接着连接但永不响应”；有界超时让错误页可恢复，
      // 而不是无限停在“正在检查登录状态…”。
      meta.value = await http.get<MetaData>('/meta', { timeoutMs: 8000 })
    } catch (failure) {
      // 元信息失败不阻断登录页，但要让用户看到原因。
      error.value = errorMessage(failure)
    }
  }

  async function bootstrap(): Promise<void> {
    if (!meta.value) await loadMeta()
    try {
      const data = await http.get<{ user: User }>('/auth/me', { timeoutMs: 8000 })
      user.value = data.user
      state.value = 'authenticated'
      error.value = ''
    } catch (failure) {
      user.value = null
      // 只有 401 才是“登录失效”。502/503/断网/超时是服务暂时不可用，
      // 必须保留错误页和重试入口，不能把已登录用户无声送回登录页。
      if (failure instanceof ApiError && failure.status === 401) {
        state.value = 'anonymous'
        error.value = ''
      } else {
        state.value = 'error'
        error.value = errorMessage(failure)
      }
    }
  }

  /** 服务恢复后从错误页重试；成功后当前路由原样显示，无需重新登录。 */
  async function retry(): Promise<void> {
    state.value = 'unknown'
    error.value = ''
    await bootstrap()
  }

  async function login(username: string, password: string): Promise<void> {
    busy.value = true
    error.value = ''
    try {
      const data = await http.post<{ user: User }>('/auth/login', { username, password })
      user.value = data.user
      state.value = 'authenticated'
      bumpAuthScope()
      resetUnauthorizedNotification()
    } catch (failure) {
      error.value = errorMessage(failure)
      throw failure
    } finally {
      busy.value = false
    }
  }

  /** 业务接口返回 401 时调用：只清本地登录态，不调退出接口（会话已失效）。 */
  function expireSession(): void {
    bumpAuthScope()
    user.value = null
    state.value = 'anonymous'
    error.value = ''
    meta.value = null
    resetUnauthorizedNotification()
  }

  async function logout(): Promise<void> {
    // 主动退出也一样：服务端确认吊销成功（或会话本来已 401）后，AppLayout 马上卸载。
    // 因此只有在确认退出成功时才通知复核页备份并清本地用户态；503/断网时保留一致登录态，
    // 由调用方提示“退出未完成”，避免匿名态下旧业务内容还挂在受保护路由上。
    const current = user.value
    try {
      await http.post('/auth/logout', {})
    } catch (exc) {
      if (!(exc instanceof ApiError) || exc.status !== 401) throw exc
    }
    if (current && typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('hx17:session-expiring', { detail: { userId: current.id } }),
      )
    }
    bumpAuthScope()
    user.value = null
    state.value = 'anonymous'
  }

  async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
    const data = await http.post<{ user: User }>('/auth/password', {
      current_password: currentPassword,
      new_password: newPassword,
    })
    user.value = data.user
  }

  function reset(): void {
    user.value = null
    state.value = 'anonymous'
    error.value = ''
  }

  return {
    user,
    meta,
    state,
    error,
    busy,
    isAuthenticated,
    isAdmin,
    initialized,
    loadMeta,
    bootstrap,
    retry,
    login,
    logout,
    expireSession,
    changePassword,
    reset,
  }
})
