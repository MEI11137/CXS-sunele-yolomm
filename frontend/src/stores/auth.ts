import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError, errorMessage, http } from '@/api/http'
import type { MetaData, User } from '@/types'

export type AuthState = 'unknown' | 'anonymous' | 'authenticated'

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
      meta.value = await http.get<MetaData>('/meta')
    } catch (failure) {
      // 元信息失败不阻断登录页，但要让用户看到原因。
      error.value = errorMessage(failure)
    }
  }

  async function bootstrap(): Promise<void> {
    if (!meta.value) await loadMeta()
    try {
      const data = await http.get<{ user: User }>('/auth/me')
      user.value = data.user
      state.value = 'authenticated'
      error.value = ''
    } catch (failure) {
      user.value = null
      state.value = 'anonymous'
      if (!(failure instanceof ApiError) || failure.status !== 401) {
        error.value = errorMessage(failure)
      }
    }
  }

  async function login(username: string, password: string): Promise<void> {
    busy.value = true
    error.value = ''
    try {
      const data = await http.post<{ user: User }>('/auth/login', { username, password })
      user.value = data.user
      state.value = 'authenticated'
    } catch (failure) {
      error.value = errorMessage(failure)
      throw failure
    } finally {
      busy.value = false
    }
  }

  async function logout(): Promise<void> {
    try {
      await http.post('/auth/logout', {})
    } finally {
      user.value = null
      state.value = 'anonymous'
    }
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
    login,
    logout,
    changePassword,
    reset,
  }
})
