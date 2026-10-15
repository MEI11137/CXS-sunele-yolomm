// MD5: 46cb5e2c64000b0b52118be0fb0b4ed5
/*
 * 版权所有 © 2025 羊羊小栈 (GJQ) | 作者：羊羊小栈 | 时间：2025-09-06 23:44:27
 *
 * 本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
 * 违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
 */

import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { useUserStore } from '@/stores/user'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/user/Login.vue'),
    meta: {
      title: '登录',
      requiresAuth: false
    }
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('@/views/user/Register.vue'),
    meta: {
      title: '注册',
      requiresAuth: false
    }
  },
  {
    path: '/',
    component: () => import('@/components/Layout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: {
          title: '首页',
          requiresAuth: true
        }
      },
      {
        path: 'profile',
        name: 'Profile',
        component: () => import('@/views/user/Profile.vue'),
        meta: {
          title: '个人中心',
          requiresAuth: true
        }
      },
      {
        path: 'detection',
        name: 'ImageDetection',
        component: () => import('@/views/detection/ImageDetection.vue'),
        meta: {
          title: '图片识别',
          requiresAuth: true
        }
      },
      {
        path: 'detection-records',
        name: 'DetectionRecords',
        component: () => import('@/views/detection/DetectionRecords.vue'),
        meta: {
          title: '识别记录',
          requiresAuth: true
        }
      },
      {
        path: 'video-detection',
        name: 'VideoDetection',
        component: () => import('@/views/video-detection/VideoDetection.vue'),
        meta: {
          title: '视频识别',
          requiresAuth: true
        }
      },
      {
        path: 'video-detection-records',
        name: 'VideoDetectionRecords',
        component: () => import('@/views/video-detection/VideoDetectionRecords.vue'),
        meta: {
          title: '视频识别记录',
          requiresAuth: true
        }
      },
      {
        path: 'realtime-detection',
        name: 'RealtimeDetection',
        component: () => import('@/views/realtime-detection/RealtimeDetection.vue'),
        meta: {
          title: '实时检测',
          requiresAuth: true
        }
      },
      {
        path: 'realtime-detection-records',
        name: 'RealtimeDetectionRecords',
        component: () => import('@/views/realtime-detection/RealtimeDetectionRecords.vue'),
        meta: {
          title: '实时检测记录',
          requiresAuth: true
        }
      }
    ]
  },
  {
    path: '/admin',
    component: () => import('@/components/Layout.vue'),
    meta: {
      requiresAuth: true,
      requiresAdmin: true
    },
    children: [
      {
        path: '',
        name: 'Admin',
        component: () => import('@/views/admin/Dashboard.vue'),
        meta: {
          title: '管理控制台',
          requiresAuth: true,
          requiresAdmin: true
        }
      },
      {
        path: 'users',
        name: 'UserManagement',
        component: () => import('@/views/admin/UserManagement.vue'),
        meta: {
          title: '用户管理',
          requiresAuth: true,
          requiresAdmin: true
        }
      },
      {
        path: 'datasets',
        name: 'DatasetManagement',
        component: () => import('@/views/admin/DatasetManagement.vue'),
        meta: {
          title: '数据集管理',
          requiresAuth: true,
          requiresAdmin: true
        }
      },
      {
        path: 'models',
        name: 'ModelManagement',
        component: () => import('@/views/admin/ModelManagement.vue'),
        meta: {
          title: '模型管理',
          requiresAuth: true,
          requiresAdmin: true
        }
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 路由守卫
router.beforeEach((to, from, next) => {
  const userStore = useUserStore()
  const requiresAuth = to.matched.some(record => record.meta.requiresAuth)
  const requiresAdmin = to.matched.some(record => record.meta.requiresAdmin)

  // 设置页面标题
  document.title = `${to.meta.title} - 光伏板缺陷检测分析系统`

  if (requiresAuth) {
    if (!userStore.isLoggedIn()) {
      next({ name: 'Login', query: { redirect: to.fullPath } })
      return
    }

    if (requiresAdmin && !userStore.isAdmin()) {
      next({ name: 'Dashboard' })
      return
    }
  }

  next()
})

export default router 