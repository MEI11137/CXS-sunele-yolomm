import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { AuthUser } from '@/types'
import { CURRENT_MODE, getAdapter, getToken, onUnauthorized, resetAdapter, setToken } from '@/services'
import { useWorkspaceStore } from './workspace'
import { useSessionStore } from './session'
import { useInspectionStore } from './inspection'

// 登录状态：真实鉴权由后端完成，前端只保存 Token 与非敏感资料。
//
// 约定：
// * 提交登录必须经过后端验证，失败不产生登录态。
// * 不登录 = 只读演示模式，只能看界面，不能执行真实本机检测/训练。
// * 登出或切换账号会清掉该账号的工作区、会话、记录与附件缓存。
const AUTH_KEY = 'haixun.auth'
const DISMISS_KEY = 'haixun.authDismissed'
const WS_KEY = 'haixun.currentWorkspace'

/** 只保存非敏感资料，Token 单独存放在 haixun.token。 */
type StoredUser = Omit<AuthUser, 'token'>

function readUser(): StoredUser | null {
  try {
    const raw = localStorage.getItem(AUTH_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as StoredUser
    return parsed && parsed.username ? parsed : null
  } catch {
    return null
  }
}

function writeUser(user: StoredUser | null) {
  try {
    if (user) localStorage.setItem(AUTH_KEY, JSON.stringify(user))
    else localStorage.removeItem(AUTH_KEY)
  } catch {
    /* ignore */
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
  const user = ref<StoredUser | null>(readUser())
  const dismissed = ref(readDismissed())
  const modalOpen = ref(false)
  const loading = ref(false)
  const error = ref('')
  const bootstrapped = ref(false)

  /** 真实模式必须有 Token 才算登录；演示模式只看本地资料。 */
  const isLoggedIn = computed(() => {
    if (!user.value) return false
    if (CURRENT_MODE === 'mock') return true
    return !!getToken()
  })

  /** 未登录只能只读浏览，不能执行真实检测 */
  const readOnlyDemo = computed(() => !isLoggedIn.value)

  function shouldShowOnEntry(): boolean {
    return !isLoggedIn.value && !dismissed.value
  }

  function openModal() {
    modalOpen.value = true
    error.value = ''
  }

  function closeModal() {
    modalOpen.value = false
  }

  function dismiss() {
    dismissed.value = true
    modalOpen.value = false
    try {
      localStorage.setItem(DISMISS_KEY, '1')
    } catch {
      /* ignore */
    }
  }

  function applyUser(next: AuthUser) {
    const { token, ...rest } = next
    user.value = rest
    writeUser(rest)
    if (token) setToken(token)
    else setToken(null)
    dismissed.value = false
    modalOpen.value = false
    error.value = ''
    try {
      localStorage.removeItem(DISMISS_KEY)
    } catch {
      /* ignore */
    }
  }

  /**
   * 清掉与账号绑定的本地缓存。
   * 会话、附件、选中项目都按账号隔离，不能留给下一个登录的人。
   */
  function clearAccountState() {
    setToken(null)
    writeUser(null)
    try {
      localStorage.removeItem(WS_KEY)
      localStorage.removeItem(DISMISS_KEY)
    } catch {
      /* ignore */
    }

    const workspace = useWorkspaceStore()
    workspace.reset()

    const session = useSessionStore()
    session.reset()

    const inspection = useInspectionStore()
    inspection.defects = []
    inspection.models = []
    inspection.annotations = {}
    inspection.loaded = false

    resetAdapter()
  }

  /** 启动时用已保存的 Token 校验身份，失败即视为未登录。 */
  async function bootstrap(): Promise<void> {
    if (bootstrapped.value) return
    bootstrapped.value = true
    if (CURRENT_MODE === 'mock') return
    if (!getToken()) {
      user.value = null
      writeUser(null)
      return
    }
    try {
      const current = await getAdapter().currentUser()
      if (current) {
        applyUser(current)
        return
      }
    } catch {
      /* 下面按未登录处理 */
    }
    user.value = null
    writeUser(null)
    setToken(null)
  }

  async function login(username: string, password: string): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      // 切换账号前先清掉上一个账号的缓存
      if (user.value && user.value.username !== username.trim()) {
        clearAccountState()
      }
      const next = await getAdapter().login(username.trim(), password)
      applyUser(next)
      const workspace = useWorkspaceStore()
      workspace.reset()
      // 登录成功后重载本人项目，不依赖页面重新挂载，也不能沿用匿名空列表。
      await workspace.load().catch(e => console.warn('[海巡智检] 登录后项目加载失败', e))
    } catch (e) {
      // 登录失败不产生登录态
      error.value = (e as Error).message || '登录失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function register(input: {
    username: string
    password: string
    realName?: string
  }): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      await getAdapter().register(input)
    } catch (e) {
      error.value = (e as Error).message || '注册失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function logout(): Promise<void> {
    try {
      await getAdapter().logout()
    } catch {
      // 后端不可达时也要能退出本地登录态
    } finally {
      clearAccountState()
      user.value = null
      modalOpen.value = false
    }
  }

  // 任何请求返回 401 都立即失效登录态，避免界面以为还在登录
  onUnauthorized(() => {
    user.value = null
    writeUser(null)
    if (!dismissed.value) modalOpen.value = true
  })

  return {
    user,
    dismissed,
    modalOpen,
    loading,
    error,
    isLoggedIn,
    readOnlyDemo,
    shouldShowOnEntry,
    bootstrap,
    openModal,
    closeModal,
    dismiss,
    login,
    register,
    logout,
  }
})
