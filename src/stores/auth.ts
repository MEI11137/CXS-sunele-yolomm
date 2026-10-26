import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

// 登录状态（前端占位）：真实账号体系由后端实现
// 本 store 仅保存「模拟登录态」，不保存密码/Key 等敏感信息
const AUTH_KEY = 'haixun.auth'

interface AuthUser {
  name: string
  // 后端就绪后替换为真实 userId / token
}

function readUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(AUTH_KEY)
    if (!raw) return null
    const u = JSON.parse(raw)
    return u && u.name ? u : null
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(readUser())

  const isLoggedIn = computed(() => !!user.value)

  // 模拟登录：仅保存用户名，不保存密码；真实鉴权待后端
  function login(name: string) {
    const u: AuthUser = { name: name.trim() || '本地用户' }
    user.value = u
    try {
      localStorage.setItem(AUTH_KEY, JSON.stringify(u))
    } catch {
      /* ignore */
    }
  }

  function logout() {
    user.value = null
    try {
      localStorage.removeItem(AUTH_KEY)
    } catch {
      /* ignore */
    }
  }

  return { user, isLoggedIn, login, logout }
})
