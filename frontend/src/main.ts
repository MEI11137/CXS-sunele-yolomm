import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { setUnauthorizedListener } from './api/http'
import { useAuthStore } from './stores/auth'
import { resetUserScopedStores } from './stores/sessionReset'
import './styles.css'

const pinia = createPinia()
const app = createApp(App).use(pinia).use(router)

// 任意受保护接口返回 401（含上传/分块/文档等直接 fetch 入口）都走这里：
// 先同步通知当前页面备份未保存编辑（复核页会写按 user/project/record 隔离的
// 临时草稿），再清掉本地登录态与工作区缓存，并统一保留 next 跳登录。不能继续用
// 旧用户渲染侧栏/项目，也不能把 401 当成“空数据”展示；不把 403（无权限）当掉线。
setUnauthorizedListener(() => {
  const auth = useAuthStore(pinia)
  if (auth.state !== 'authenticated') return
  const userId = auth.user?.id ?? null
  // 同步派发，调用方在各页面卸载前同步把 dirty 状态写入本地备份。
  window.dispatchEvent(new CustomEvent('hx17:session-expiring', { detail: { userId } }))
  resetUserScopedStores()
  auth.expireSession()
  const current = router.currentRoute.value
  if (current.name === 'login') return
  void router.replace({ name: 'login', query: { next: current.fullPath } })
})

app.mount('#app')