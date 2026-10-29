<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { useThemeStore } from '@/stores/theme'
import BrandHeader from './BrandHeader.vue'
import AccountFooter from './AccountFooter.vue'
import ProjectConversations from './ProjectConversations.vue'
import WorkspaceSwitcher from './WorkspaceSwitcher.vue'
import RecycleBin from './RecycleBin.vue'
defineProps<{ mobile?: boolean }>()
const emit = defineEmits<{ conversation: [] }>()
const workspace = useWorkspaceStore()
const theme = useThemeStore()
const router = useRouter()
const drawerOpen = ref(false)
function openConversation() { drawerOpen.value = false; emit('conversation') }
</script>

<template>
  <header v-if="mobile" class="mobile-header">
    <button class="menu-btn" aria-label="打开项目列表" @click="drawerOpen = true">☰</button>
    <BrandHeader :workspace-name="workspace.current?.name || ''" inline />
    <button class="menu-btn" aria-label="切换主题" @click="theme.toggle">◐</button>
  </header>
  <div v-if="mobile && drawerOpen" class="drawer-scrim" @click="drawerOpen = false"></div>
  <aside class="sidenav" :class="{ drawer: mobile, open: drawerOpen }" aria-label="项目导航">
    <button v-if="mobile" class="drawer-close" aria-label="关闭项目列表" @click="drawerOpen = false">✕</button>
    <BrandHeader :workspace-name="workspace.current?.name || ''" />
    <div class="create-project"><WorkspaceSwitcher /></div>
    <ProjectConversations @select="openConversation" />
    <RecycleBin />
    <button class="settings-link" @click="router.push('/settings'); drawerOpen = false">设置</button>
    <AccountFooter />
  </aside>
</template>

<style scoped>
.sidenav { width: var(--nav-w); flex-shrink: 0; display: flex; flex-direction: column; background: var(--panel); border-right: 1px solid var(--border); height: 100vh; min-height: 0; }
.create-project { padding: 12px 12px 4px; }
.settings-link { background: transparent; border: 0; color: var(--text-secondary); padding: 10px 18px; cursor: pointer; font-size: 13px; text-align: left; }
.settings-link:hover { color: var(--text); background: var(--panel-secondary); }
.mobile-header { display: flex; align-items: center; gap: 12px; padding: 10px 12px; background: var(--panel); border-bottom: 1px solid var(--border); flex-shrink: 0; }
.menu-btn, .drawer-close { background: transparent; color: var(--text); border: 0; cursor: pointer; padding: 8px; }
.drawer { position: fixed; top: 0; left: 0; z-index: 900; transform: translateX(-100%); transition: transform .15s; }
.drawer.open { transform: translateX(0); }
.drawer-scrim { position: fixed; inset: 0; background: var(--scrim); z-index: 899; }
.drawer-close { position: absolute; right: 5px; top: 7px; z-index: 1; }
</style>
