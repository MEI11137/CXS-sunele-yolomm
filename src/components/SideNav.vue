<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { useThemeStore } from '@/stores/theme'
import { useInspectionStore } from '@/stores/inspection'
import BrandHeader from './BrandHeader.vue'
import AccountFooter from './AccountFooter.vue'

defineProps<{ mobile?: boolean }>()

const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()
const theme = useThemeStore()
const inspection = useInspectionStore()
const pendingCount = computed(() => inspection.pendingCount())

const drawerOpen = ref(false)

const mainNav = [
  { path: '/overview', label: '工作区总览', icon: 'grid' },
  { path: '/inspection', label: '图片巡检', icon: 'image' },
  { path: '/records', label: '识别记录', icon: 'list' },
  { path: '/review', label: '人工复核', icon: 'check', guide: 'review' },
  { path: '/datasets', label: '数据集', icon: 'db' },
  { path: '/models', label: '模型管理', icon: 'model' },
]

const bottomNav = [
  { path: '/manual', label: '资料与手册', icon: 'book' },
  { path: '/extensions', label: '扩展与连接', icon: 'plug', guide: 'extensions' },
  { path: '/settings', label: '设置', icon: 'gear' },
]

function isActive(path: string) {
  return route.path.startsWith(path)
}

function go(path: string) {
  router.push(path)
  drawerOpen.value = false
}
</script>

<template>
  <template v-if="!mobile">
    <aside class="sidenav">
      <BrandHeader :workspace-name="workspace.current?.name ?? '未选择'" />
      <nav class="nav-list" aria-label="主导航">
        <button
          v-for="item in mainNav"
          :key="item.path"
          class="nav-item"
          :class="{ active: isActive(item.path) }"
          :data-guide="item.guide"
          @click="go(item.path)"
        >
          <span class="nav-icon" :data-icon="item.icon"></span>
          <span>{{ item.label }}</span>
          <span v-if="item.path === '/review' && pendingCount > 0" class="badge num">{{ pendingCount }}</span>
        </button>
      </nav>
      <div class="nav-divider"></div>
      <nav class="nav-list bottom" aria-label="底部导航">
        <button
          v-for="item in bottomNav"
          :key="item.path"
          class="nav-item"
          :class="{ active: isActive(item.path) }"
          :data-guide="item.guide"
          @click="go(item.path)"
        >
          <span class="nav-icon" :data-icon="item.icon"></span>
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <AccountFooter />
    </aside>
  </template>

  <template v-else>
    <header class="mobile-header">
      <button class="menu-btn" aria-label="打开导航" @click="drawerOpen = true">
        <span class="nav-icon" data-icon="menu"></span>
      </button>
      <BrandHeader :workspace-name="workspace.current?.name ?? '未选择'" inline />
      <button class="menu-btn" aria-label="切换主题" @click="theme.toggle">
        <span class="nav-icon" :data-icon="theme.theme === 'light' ? 'moon' : 'sun'"></span>
      </button>
    </header>
    <div v-if="drawerOpen" class="drawer-scrim" @click="drawerOpen = false"></div>
    <aside class="sidenav drawer" :class="{ open: drawerOpen }">
      <button class="drawer-close" aria-label="关闭导航" @click="drawerOpen = false">
        <span class="nav-icon" data-icon="x"></span>
      </button>
      <BrandHeader :workspace-name="workspace.current?.name ?? '未选择'" />
      <nav class="nav-list" aria-label="主导航">
        <button
          v-for="item in mainNav"
          :key="item.path"
          class="nav-item"
          :class="{ active: isActive(item.path) }"
          @click="go(item.path)"
        >
          <span class="nav-icon" :data-icon="item.icon"></span>
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <div class="nav-divider"></div>
      <nav class="nav-list bottom" aria-label="底部导航">
        <button
          v-for="item in bottomNav"
          :key="item.path"
          class="nav-item"
          :class="{ active: isActive(item.path) }"
          @click="go(item.path)"
        >
          <span class="nav-icon" :data-icon="item.icon"></span>
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <AccountFooter />
    </aside>
  </template>
</template>

<style scoped>
.sidenav {
  width: var(--nav-w);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border-right: 1px solid var(--border);
  height: 100vh;
}

.nav-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px;
}
.nav-list.bottom {
  margin-top: auto;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 10px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  text-align: left;
  position: relative;
  transition: background 0.12s, color 0.12s;
}
.nav-item:hover {
  background: var(--panel-secondary);
  color: var(--text);
}
.nav-item.active {
  background: var(--bg-selected);
  color: var(--text);
  font-weight: 500;
}
.badge {
  margin-left: auto;
  min-width: 18px;
  height: 18px;
  border-radius: 9px;
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 5px;
}

.nav-divider {
  height: 1px;
  background: var(--border);
  margin: 8px;
}

/* 图标 */
.nav-icon {
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  position: relative;
}
.nav-icon[data-icon]::after {
  content: '';
  position: absolute;
  inset: 2px;
  border: 1.5px solid currentColor;
  border-radius: 2px;
}
.nav-icon[data-icon='grid']::after {
  border: none;
  background:
    linear-gradient(currentColor, currentColor) 2px 2px / 4px 4px no-repeat,
    linear-gradient(currentColor, currentColor) 9px 2px / 4px 4px no-repeat,
    linear-gradient(currentColor, currentColor) 2px 9px / 4px 4px no-repeat,
    linear-gradient(currentColor, currentColor) 9px 9px / 4px 4px no-repeat;
}
.nav-icon[data-icon='image']::after {
  border: 1.5px solid currentColor;
  background: radial-gradient(circle at 5px 5px, currentColor 1.5px, transparent 2px);
}
.nav-icon[data-icon='list']::after {
  border: none;
  background:
    linear-gradient(currentColor, currentColor) 2px 4px / 12px 1.5px no-repeat,
    linear-gradient(currentColor, currentColor) 2px 9px / 12px 1.5px no-repeat,
    linear-gradient(currentColor, currentColor) 2px 14px / 8px 1.5px no-repeat;
}
.nav-icon[data-icon='check']::after {
  border: none;
  background: linear-gradient(45deg, transparent 45%, currentColor 45%, currentColor 55%, transparent 55%),
    linear-gradient(-45deg, transparent 42%, currentColor 42%, currentColor 60%, transparent 60%);
}
.nav-icon[data-icon='db']::after {
  border-radius: 50%;
  border: 1.5px solid currentColor;
  background:
    linear-gradient(currentColor, currentColor) 50% 4px / 8px 1.5px no-repeat,
    linear-gradient(currentColor, currentColor) 50% 12px / 8px 1.5px no-repeat;
}
.nav-icon[data-icon='model']::after {
  border: none;
  background:
    linear-gradient(currentColor, currentColor) 2px 4px / 12px 1.5px no-repeat,
    linear-gradient(currentColor, currentColor) 2px 9px / 8px 1.5px no-repeat,
    radial-gradient(circle at 13px 12px, currentColor 2px, transparent 2.5px);
}
.nav-icon[data-icon='book']::after {
  border: none;
  background: linear-gradient(90deg, transparent 40%, currentColor 40%, currentColor 42%, transparent 42%);
  box-shadow: inset 0 0 0 1.5px currentColor;
  border-radius: 2px;
}
.nav-icon[data-icon='plug']::after {
  border: none;
  background:
    linear-gradient(currentColor, currentColor) 4px 2px / 2px 5px no-repeat,
    linear-gradient(currentColor, currentColor) 12px 2px / 2px 5px no-repeat,
    linear-gradient(currentColor, currentColor) 4px 9px / 2px 5px no-repeat,
    linear-gradient(currentColor, currentColor) 12px 9px / 2px 5px no-repeat;
}
.nav-icon[data-icon='gear']::after {
  border: none;
  background: radial-gradient(circle, currentColor 2px, transparent 2.5px);
  box-shadow:
    0 -6px 0 -0.5px currentColor, 0 6px 0 -0.5px currentColor,
    5px -3.5px 0 -0.5px currentColor, -5px -3.5px 0 -0.5px currentColor,
    5px 3.5px 0 -0.5px currentColor, -5px 3.5px 0 -0.5px currentColor;
}
.nav-icon[data-icon='menu']::after {
  border: none;
  background:
    linear-gradient(currentColor, currentColor) 2px 5px / 14px 1.5px no-repeat,
    linear-gradient(currentColor, currentColor) 2px 9px / 14px 1.5px no-repeat,
    linear-gradient(currentColor, currentColor) 2px 13px / 14px 1.5px no-repeat;
}
.nav-icon[data-icon='x']::after {
  border: none;
  background: linear-gradient(45deg, transparent 45%, currentColor 45%, currentColor 55%, transparent 55%),
    linear-gradient(-45deg, transparent 45%, currentColor 45%, currentColor 55%, transparent 55%);
}
.nav-icon[data-icon='moon']::after {
  border-radius: 50%;
  border: 1.5px solid currentColor;
  background: currentColor;
  clip-path: circle(50% at 65% 40%);
}
.nav-icon[data-icon='sun']::after {
  border: none;
  background: radial-gradient(circle, currentColor 2.5px, transparent 3px);
  box-shadow: 0 -6px 0 -1px currentColor, 0 6px 0 -1px currentColor,
    6px 0 0 -1px currentColor, -6px 0 0 -1px currentColor;
}

/* 移动端 */
.mobile-header {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 52px;
  padding: 0 12px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.menu-btn {
  width: 32px;
  height: 32px;
  border: 1px solid var(--border-control);
  background: var(--panel);
  border-radius: var(--radius-sm);
  cursor: pointer;
  color: var(--text);
  display: flex;
  align-items: center;
  justify-content: center;
}
.drawer {
  position: fixed;
  left: 0;
  top: 0;
  bottom: 0;
  z-index: 950;
  transform: translateX(-100%);
  transition: transform 0.2s ease;
  box-shadow: var(--shadow-md);
}
.drawer.open {
  transform: translateX(0);
}
.drawer-scrim {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  z-index: 940;
}
.drawer-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
