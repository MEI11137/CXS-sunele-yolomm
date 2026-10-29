import { createRouter, createWebHistory, type RouteLocation } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { projectPath } from '@/utils/projectRoutes'

const pages = [
  { path: 'chat/:sessionId?', name: 'project-chat', component: () => import('@/views/ProjectHome.vue'), meta: { title: '项目对话' } },
  { path: 'inspection', name: 'inspection', component: () => import('@/views/InspectionView.vue'), meta: { title: '图片巡检' } },
  { path: 'records', name: 'records', component: () => import('@/views/RecordsView.vue'), meta: { title: '识别记录' } },
  { path: 'review', name: 'review', component: () => import('@/views/ReviewView.vue'), meta: { title: '人工复核' } },
  { path: 'review/:defectId', name: 'review-item', component: () => import('@/views/ReviewView.vue'), meta: { title: '人工复核' } },
  { path: 'datasets', name: 'datasets', component: () => import('@/views/DatasetsView.vue'), meta: { title: '数据集' } },
  { path: 'models', name: 'models', component: () => import('@/views/ModelsView.vue'), meta: { title: '模型管理' } },
  { path: 'manual', name: 'manual', component: () => import('@/views/ManualView.vue'), meta: { title: '资料与手册' } },
  { path: 'extensions', name: 'extensions', component: () => import('@/views/ExtensionsView.vue'), meta: { title: '扩展与连接' } },
]
const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/', redirect: '/projects' },
  { path: '/overview', redirect: '/projects' },
  { path: '/projects', name: 'projects', component: () => import('@/views/ProjectHome.vue'), meta: { title: '项目' } },
  ...pages.map(page => ({ ...page, path: '/projects/:projectId/' + page.path })),
  { path: '/projects/:projectId', redirect: to => projectPath(String(to.params.projectId)) },
  { path: '/settings', name: 'settings', component: () => import('@/views/SettingsView.vue'), meta: { title: '设置' } },
  ...['inspection','records','review/:defectId?','datasets','models','manual','extensions'].map(page => ({
    path: '/' + page, redirect: (to: RouteLocation) => {
      const id = useWorkspaceStore().currentId
      return id ? { path: `/projects/${encodeURIComponent(id)}${to.path}`, query: to.query, hash: to.hash } : '/projects'
    },
  })),
  { path: '/:pathMatch(.*)*', redirect: '/projects' },
] })
router.afterEach(to => { document.title = `${String(to.meta.title || '项目')} · 海巡智检` })
export default router
