import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

// 登录状态（前端占位）：真实账号体系由后端实现
// 本 store 仅保存「模拟登录态」，不保存密码/Key 等敏感信息
//
// 交互约定：
// - 进入主界面时，未登录会弹出登录浮层，右上角可「×」跳过
// - 不登录 = 本地模式，检测/复核在本地运行
// - 登录后 = 云端模式，可云端查看
const AUTH_KEY = 'haixun.auth'
const DISMISS_KEY = 'haixun.authDismissed'

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

function readDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === '1'
  } catch {
    return false
  }
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(readUser())
  const dismissed = ref(readDismissed())
  const modalOpen = ref(false)

  const isLoggedIn = computed(() => !!user.value)

  // 首次进入主界面时，若未登录且未选择过跳过，则弹出登录浮层
  function shouldShowOnEntry(): boolean {
    return !isLoggedIn.value && !dismissed.value
  }

  function openModal() {
    modalOpen.value = true
  }

  function closeModal() {
    modalOpen.value = false
  }

  // 点「×」跳过：记住选择，本次会话与后续不再自动弹出
  function dismiss() {
    dismissed.value = true
    modalOpen.value = false
    try {
      localStorage.setItem(DISMISS_KEY, '1')
    } catch {
      /* ignore */
    }
  }

  // 模拟登录：仅保存用户名，不保存密码；真实鉴权待后端
  function login(name: string) {
    const u: AuthUser = { name: name.trim() || '本地用户' }
    user.value = u
    dismissed.value = false
    modalOpen.value = false
    try {
      localStorage.setItem(AUTH_KEY, JSON.stringify(u))
      localStorage.removeItem(DISMISS_KEY)
    } catch {
      /* ignore */
    }
  }

  function logout() {
    user.value = null
    modalOpen.value = false
    try {
      localStorage.removeItem(AUTH_KEY)
      localStorage.removeItem(DISMISS_KEY)
    } catch {
      /* ignore */
    }
  }

  return {
    user,
    dismissed,
    modalOpen,
    isLoggedIn,
    shouldShowOnEntry,
    openModal,
    closeModal,
    dismiss,
    login,
    logout,
  }
})
