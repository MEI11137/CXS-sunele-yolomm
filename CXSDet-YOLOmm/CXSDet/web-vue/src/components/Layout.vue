<!--
  基础布局
  左侧按巡检闭环的四个阶段分组，避免把"检测"和"训练"混在一列里。
  底部是智能体入口：只有配置了 VITE_AGENT_URL 才可点，否则如实标注未接入。
-->
<!--
版权所有 © 2025 羊羊小栈 （GJQ）

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <el-container class="layout-container">
    <a class="skip-link" href="#main-content">跳到主要内容</a>

    <!-- 侧边栏 -->
    <el-aside :width="isCollapse ? '68px' : '228px'" class="aside hx-grid-texture">
      <div class="brand" :class="{ 'brand-collapse': isCollapse }">
        <img src="@/assets/images/logo.png" alt="" class="brand-logo" />
        <div v-show="!isCollapse" class="brand-text">
          <span class="brand-name">海巡智检</span>
          <span class="brand-sub">海上风电场检测 · 模型迭代</span>
        </div>
      </div>

      <el-menu
        :default-active="activeMenu"
        class="menu"
        :router="true"
        :collapse="isCollapse"
        :collapse-transition="false"
      >
        <el-menu-item-group
          v-for="group in visibleGroups"
          :key="group.key"
          :title="group.label"
        >
          <el-menu-item
            v-for="item in group.items"
            :key="item.path"
            :index="item.path"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <template #title>{{ item.label }}</template>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>

      <!-- 智能体入口 -->
      <div v-show="!isCollapse" class="agent-entry">
        <div class="agent-head">
          <span class="agent-signal" :class="{ 'is-live': !!agentUrl }" aria-hidden="true" />
          <span class="agent-title">巡检智能体</span>
          <el-tag v-if="!agentUrl" size="small" class="agent-flag">未接入</el-tag>
        </div>
        <p class="agent-note">
          {{ agentUrl ? '在对话里下达巡检任务，由它串起复核与回流步骤。' : '本版本尚未接入智能体入口，闭环仍按页面逐步操作。' }}
        </p>
        <el-button
          v-if="agentUrl"
          class="agent-btn"
          size="small"
          @click="openAgent"
        >
          打开智能体
          <el-icon class="agent-btn-icon"><TopRight /></el-icon>
        </el-button>
      </div>
    </el-aside>

    <el-container>
      <!-- 头部 -->
      <el-header class="header">
        <div class="header-left">
          <button
            class="collapse-btn"
            type="button"
            :aria-label="isCollapse ? '展开侧边栏' : '收起侧边栏'"
            @click="toggleCollapse"
          >
            <el-icon>
              <Expand v-if="isCollapse" />
              <Fold v-else />
            </el-icon>
          </button>
          <el-breadcrumb separator="/">
            <el-breadcrumb-item :to="{ path: homePath }">{{ isAdmin ? '控制台' : '首页' }}</el-breadcrumb-item>
            <el-breadcrumb-item v-if="roleLabel">{{ roleLabel }}</el-breadcrumb-item>
            <el-breadcrumb-item>{{ route.meta.title }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <AlgoHealthCheck class="health-check" />
          <span class="role-badge" :class="{ 'is-admin': isAdmin }">
            {{ isAdmin ? '管理员' : '巡检员' }}
          </span>
          <el-dropdown @command="handleCommand">
            <button class="user-info" type="button">
              <el-avatar :size="28" :src="userInfo?.avatarUrl">
                {{ userInfo?.realName?.charAt(0) || userInfo?.username?.charAt(0) || 'U' }}
              </el-avatar>
              <span class="user-name">{{ userInfo?.realName || userInfo?.username }}</span>
              <el-icon class="user-caret"><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">个人中心</el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <!-- 内容区 -->
      <el-main id="main-content" class="main" tabindex="-1">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import {
  Monitor, User, Files, Box, Fold, Expand, Camera, Document,
  VideoPlay, Film, View, Clock, TopRight, ArrowDown
} from '@element-plus/icons-vue'
import AlgoHealthCheck from './AlgoHealthCheck.vue'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const activeMenu = computed(() => route.path)
const userInfo = computed(() => userStore.userInfo)
const isAdmin = computed(() => userStore.isAdmin())
const homePath = computed(() => (isAdmin.value ? '/admin' : '/dashboard'))
const isCollapse = ref(false)

/** 智能体入口地址。未配置时不展示可点击按钮，避免出现打不开的假入口。 */
const agentUrl = (import.meta.env.VITE_AGENT_URL || '').trim()

type NavItem = { path: string; label: string; icon: unknown; adminOnly?: boolean }
type NavGroup = { key: string; label: string; adminOnly?: boolean; items: NavItem[] }

const navGroups: NavGroup[] = [
  {
    key: 'inspect',
    label: '巡检作业',
    items: [
      { path: '/detection', label: '图片巡检', icon: Camera },
      { path: '/video-detection', label: '视频巡检', icon: VideoPlay },
      { path: '/realtime-detection', label: '实时巡检', icon: View }
    ]
  },
  {
    key: 'review',
    label: '复核与回流',
    items: [
      { path: '/detection-records', label: '识别记录', icon: Document },
      { path: '/video-detection-records', label: '视频记录', icon: Film },
      { path: '/realtime-detection-records', label: '实时记录', icon: Clock }
    ]
  },
  {
    key: 'train',
    label: '训练与模型',
    adminOnly: true,
    items: [
      { path: '/admin/datasets', label: '数据集管理', icon: Files },
      { path: '/admin/models', label: '模型管理', icon: Box }
    ]
  },
  {
    key: 'system',
    label: '系统',
    items: [
      { path: '/admin', label: '管理控制台', icon: Monitor, adminOnly: true },
      { path: '/dashboard', label: '首页', icon: Monitor },
      { path: '/profile', label: '个人中心', icon: User }
    ]
  }
]

const visibleGroups = computed(() =>
  navGroups
    .filter((group) => !group.adminOnly || isAdmin.value)
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
        if (item.adminOnly && !isAdmin.value) return false
        if (item.path === '/dashboard' && isAdmin.value) return false
        return true
      })
    }))
    .filter((group) => group.items.length > 0)
)

const roleLabel = computed(() => {
  const title = String(route.meta.title || '')
  const group = navGroups.find((g) => g.items.some((i) => i.path === route.path))
  if (!group || group.label === title) return ''
  return group.label
})

const toggleCollapse = () => {
  isCollapse.value = !isCollapse.value
}

const openAgent = () => {
  if (!agentUrl) return
  window.open(agentUrl, '_blank', 'noopener,noreferrer')
}

const handleCommand = (command: string) => {
  switch (command) {
    case 'profile':
      router.push('/profile')
      break
    case 'logout':
      userStore.logout()
      break
  }
}
</script>

<style scoped>
.layout-container {
  height: 100vh;
}

.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 3000;
  padding: 8px 14px;
  background: var(--hx-paper);
  color: var(--hx-text);
  border: 1px solid var(--hx-sea);
}

.skip-link:focus {
  left: 8px;
  top: 8px;
}

/* —— 侧边栏 —— */
.aside {
  display: flex;
  flex-direction: column;
  background-color: var(--hx-ink);
  border-right: 1px solid #08181f;
  transition: width 0.24s ease;
  overflow: hidden;
}

.brand {
  height: 64px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  flex-shrink: 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  overflow: hidden;
  white-space: nowrap;
}

.brand-collapse {
  padding: 0 18px;
}

.brand-logo {
  width: 26px;
  height: 26px;
  flex-shrink: 0;
  object-fit: contain;
  filter: grayscale(1) brightness(2.4);
  opacity: 0.9;
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}

.brand-name {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.16em;
  color: #eaf2f1;
}

.brand-sub {
  font-size: 10px;
  letter-spacing: 0.04em;
  color: #6f8b93;
  margin-top: 2px;
}

.menu {
  flex: 1;
  border-right: none;
  background: transparent;
  padding: 8px 0;
  overflow-y: auto;
  overflow-x: hidden;
}

:deep(.el-menu-item-group__title) {
  padding: 12px 18px 6px !important;
  font-size: 10px;
  letter-spacing: 0.14em;
  color: #5d7a83;
  font-weight: 500;
}

:deep(.el-menu--collapse .el-menu-item-group__title) {
  display: none;
}

:deep(.el-menu-item) {
  height: 38px;
  line-height: 38px;
  margin: 2px 8px;
  padding-left: 12px !important;
  border-radius: 2px;
  color: #a9bcc2;
  font-size: 13px;
  border-left: 2px solid transparent;
}

:deep(.el-menu-item .el-icon) {
  color: #7e979f;
  font-size: 15px;
  margin-right: 9px;
}

:deep(.el-menu-item:hover) {
  background-color: rgba(255, 255, 255, 0.05);
  color: #e6efee;
}

:deep(.el-menu-item.is-active) {
  background-color: rgba(15, 107, 110, 0.28);
  border-left-color: #3fa8a4;
  color: #f2f8f7;
  font-weight: 500;
}

:deep(.el-menu-item.is-active .el-icon) {
  color: #63c2bd;
}

:deep(.el-menu--collapse .el-menu-item) {
  margin: 2px 8px;
  padding-left: 0 !important;
  justify-content: center;
}

/* —— 智能体入口 —— */
.agent-entry {
  flex-shrink: 0;
  margin: 10px;
  padding: 12px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.03);
}

.agent-head {
  display: flex;
  align-items: center;
  gap: 7px;
}

.agent-signal {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #5b7079;
  flex-shrink: 0;
}

.agent-signal.is-live {
  background: #4fc3a1;
  box-shadow: 0 0 0 3px rgba(79, 195, 161, 0.16);
}

.agent-title {
  font-size: 12px;
  font-weight: 600;
  color: #cfe0de;
}

.agent-flag {
  margin-left: auto;
  border: none;
  background: rgba(255, 255, 255, 0.1);
  color: #9fb3b9;
  font-size: 10px;
  height: 18px;
  line-height: 18px;
  padding: 0 5px;
}

.agent-note {
  margin: 7px 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: #7c949b;
}

.agent-btn {
  margin-top: 9px;
  width: 100%;
  background: rgba(15, 107, 110, 0.5);
  border-color: #2d7d7f;
  color: #eaf6f4;
}

.agent-btn:hover {
  background: rgba(15, 107, 110, 0.75);
  border-color: #3fa8a4;
  color: #fff;
}

.agent-btn-icon {
  margin-left: 4px;
}

/* —— 头部 —— */
.header {
  height: 56px;
  background-color: var(--hx-paper);
  border-bottom: 1px solid var(--hx-line);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px 0 14px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.collapse-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  margin-right: 10px;
  border: none;
  background: transparent;
  color: var(--hx-text-2);
  cursor: pointer;
  border-radius: 2px;
  font-size: 17px;
}

.collapse-btn:hover {
  background: var(--hx-mist);
  color: var(--hx-text);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 14px;
}

.health-check {
  padding-right: 14px;
  border-right: 1px solid var(--hx-line);
}

.role-badge {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  font-size: 11px;
  color: var(--hx-text-2);
  border: 1px solid var(--hx-line);
  border-radius: 2px;
  background: #f6f8f7;
}

.role-badge.is-admin {
  color: var(--hx-sea);
  border-color: var(--hx-sea-line);
  background: var(--hx-sea-soft);
}

.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 1px 2px;
  border-radius: 2px;
}

.user-name {
  font-size: 13px;
  color: var(--hx-text);
}

.user-caret {
  font-size: 12px;
  color: var(--hx-text-3);
}

.main {
  background-color: var(--hx-mist);
  padding: 18px;
  overflow-y: auto;
}

/* 路由过渡：只做淡入，避免每页都上滑 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.16s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@media (max-width: 900px) {
  .health-check,
  .role-badge {
    display: none;
  }
}
</style>
