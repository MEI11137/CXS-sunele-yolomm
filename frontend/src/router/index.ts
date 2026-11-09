import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

// 路由按“项目维度”组织：业务页面都属于某个项目，设置与管理独立。
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/projects' },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录' },
    },
    {
      path: '/projects',
      name: 'projects',
      component: () => import('@/views/ProjectHome.vue'),
      meta: { title: '项目' },
    },
    {
      path: '/projects/:projectId',
      name: 'project',
      component: () => import('@/views/ProjectHome.vue'),
      props: true,
      meta: { title: '项目' },
    },
    {
      // 对话在中间主区；项目配置走“项目详情”入口，不占主位。
      path: '/projects/:projectId/sessions/:sessionId',
      name: 'session',
      component: () => import('@/views/SessionView.vue'),
      props: true,
      meta: { title: '会话' },
    },
    {
      // 手动图片巡检：不经过对话也能跑一次真实检测（同一个检测服务）。
      path: '/projects/:projectId/inspect',
      name: 'inspect',
      component: () => import('@/views/InspectView.vue'),
      meta: { title: '图片巡检', dock: true },
    },
    {
      // 本轮人工队列：可按 flow_id 或 record_ids 定位。
      path: '/projects/:projectId/review',
      name: 'reviewQueue',
      component: () => import('@/views/ReviewQueueView.vue'),
      meta: { title: '人工复核队列', dock: true },
    },
    {
      // 人工复核：RGB / 配图 / 结果图对照 + 标注编辑；提交/驳回只能在这里做。
      path: '/projects/:projectId/records/:recordId/review',
      name: 'review',
      component: () => import('@/views/ReviewView.vue'),
      meta: { title: '人工复核', dock: true },
    },
    {
      // 业务页把对话停靠到右侧，任何一页都不会“把助手弄丢”。
      path: '/projects/:projectId/records',
      name: 'records',
      component: () => import('@/views/RecordsView.vue'),
      meta: { title: '识别记录', dock: true },
    },
    {
      // 数据回流：把已确认结论变成真实数据集版本（预览不写文件）。
      path: '/projects/:projectId/reflow',
      name: 'reflow',
      component: () => import('@/views/ReflowView.vue'),
      meta: { title: '数据回流', dock: true },
    },
    {
      path: '/projects/:projectId/datasets',
      name: 'datasets',
      component: () => import('@/views/DatasetsView.vue'),
      meta: { title: '数据集', dock: true },
    },
    {
      path: '/projects/:projectId/models',
      name: 'models',
      component: () => import('@/views/ModelsView.vue'),
      meta: { title: '模型', dock: true },
    },
    {
      path: '/trash',
      name: 'trash',
      component: () => import('@/views/TrashView.vue'),
      meta: { title: '回收站' },
    },
    {
      path: '/admin',
      name: 'admin',
      component: () => import('@/views/AdminView.vue'),
      meta: { title: '本机管理' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
      meta: { title: '设置' },
    },
    { path: '/:pathMatch(.*)*', redirect: '/projects' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  // 首次导航前确认登录态，避免先渲染业务页再跳登录。
  if (auth.state === 'unknown') await auth.bootstrap()
  // 服务不可用不是“未登录”：保留当前目标路由，由 App 显示错误/重试页。
  if (auth.state === 'error') return true
  if (!auth.isAuthenticated && to.name !== 'login') {
    return { name: 'login', query: { next: to.fullPath } }
  }
  if (auth.isAuthenticated && to.name === 'login') return { name: 'projects' }
  if (to.name === 'admin' && !auth.isAdmin) return { name: 'projects' }
  return true
})

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? '项目')} · 海巡智检 v1.7`
})

export default router
