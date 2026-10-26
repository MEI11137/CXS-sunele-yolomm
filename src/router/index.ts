import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/overview', name: 'overview', component: () => import('@/views/OverviewView.vue'), meta: { title: '工作区总览' } },
    { path: '/inspection', name: 'inspection', component: () => import('@/views/InspectionView.vue'), meta: { title: '图片巡检' } },
    { path: '/records', name: 'records', component: () => import('@/views/RecordsView.vue'), meta: { title: '识别记录' } },
    { path: '/review', name: 'review', component: () => import('@/views/ReviewView.vue'), meta: { title: '人工复核' } },
    { path: '/review/:defectId', name: 'review-item', component: () => import('@/views/ReviewView.vue'), meta: { title: '人工复核' } },
    { path: '/datasets', name: 'datasets', component: () => import('@/views/DatasetsView.vue'), meta: { title: '数据集' } },
    { path: '/models', name: 'models', component: () => import('@/views/ModelsView.vue'), meta: { title: '模型管理' } },
    { path: '/settings', name: 'settings', component: () => import('@/views/SettingsView.vue'), meta: { title: '设置' } },
    { path: '/manual', name: 'manual', component: () => import('@/views/ManualView.vue'), meta: { title: '资料与手册' } },
    { path: '/extensions', name: 'extensions', component: () => import('@/views/ExtensionsView.vue'), meta: { title: '扩展与连接' } },
  ],
})

router.afterEach((to) => {
  const title = (to.meta.title as string) || ''
  document.title = title ? `${title} · 海巡智检` : '海巡智检 · 海上风电叶片巡检平台'
})

export default router
