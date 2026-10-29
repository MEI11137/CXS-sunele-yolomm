<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSessionStore } from '@/stores/session'
import SideNav from './SideNav.vue'
import AssistantPanel from './AssistantPanel.vue'
import GuideOverlay from './GuideOverlay.vue'
import AuthModal from './AuthModal.vue'
import { useRoute } from 'vue-router'
import { projectPages, projectPath } from '@/utils/projectRoutes'
import WorkspaceSwitcher from './WorkspaceSwitcher.vue'

const assistantOpen = ref(true)
const assistantPanel = ref<InstanceType<typeof AssistantPanel> | null>(null)
const workspace = useWorkspaceStore()
const session = useSessionStore()
const route = useRoute()
const routeProject = computed(() => String(route.params.projectId || ''))
const primaryChat = computed(() => route.name === 'project-chat')
const projectValid = computed(() => !routeProject.value || (workspace.loaded && workspace.workspaces.some(project => project.id === routeProject.value)))
const sessionRouteError = ref('')
watch(() => [routeProject.value, workspace.loaded, workspace.workspaces.length], () => {
  if (routeProject.value && workspace.loaded && projectValid.value) workspace.setCurrent(routeProject.value)
}, { immediate: true, flush: 'sync' })
watch(() => workspace.currentId, id => {
  void session.load(id).catch(() => {})
}, { immediate: true, flush: 'sync' })
watch(() => [route.params.sessionId, routeProject.value, session.loaded], async () => {
  sessionRouteError.value = ''
  const id = String(route.params.sessionId || '')
  if (!id || !session.loaded || session.activeProjectId !== routeProject.value) return
  if (!session.projectSessions(routeProject.value).some(chat => chat.id === id)) { sessionRouteError.value = '此会话不存在、已删除或不属于当前项目'; return }
  try { await session.selectSession(id) } catch (e) { sessionRouteError.value = (e as Error).message }
}, { immediate: true })
function openConversation() {
  assistantOpen.value = true
  assistantPanel.value?.openConversation()
}
const isMobile = ref(false)

// 助手栏可拖拽宽度
const ASSISTANT_MIN = 260
const ASSISTANT_MAX = 640
const ASSISTANT_DEFAULT = 336
const ASSISTANT_KEY = 'haixun.assistantWidth'

function readWidth(): number {
  try {
    const v = parseInt(localStorage.getItem(ASSISTANT_KEY) || '', 10)
    if (!Number.isNaN(v) && v >= ASSISTANT_MIN && v <= ASSISTANT_MAX) return v
  } catch {
    /* ignore */
  }
  return ASSISTANT_DEFAULT
}

const assistantWidth = ref(readWidth())
const dragging = ref(false)

function updateViewport() {
  isMobile.value = window.innerWidth <= 768
}

function startDrag(e: MouseEvent) {
  if (!assistantOpen.value) return
  dragging.value = true
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'col-resize'
  e.preventDefault()
}

function onDrag(e: MouseEvent) {
  if (!dragging.value) return
  // 从右边缘向左拖：宽度 = 窗口宽 - 鼠标X
  const w = window.innerWidth - e.clientX
  assistantWidth.value = Math.max(ASSISTANT_MIN, Math.min(ASSISTANT_MAX, w))
}

function endDrag() {
  if (!dragging.value) return
  dragging.value = false
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
  try {
    localStorage.setItem(ASSISTANT_KEY, String(assistantWidth.value))
  } catch {
    /* ignore */
  }
}

function resetWidth() {
  assistantWidth.value = ASSISTANT_DEFAULT
  try {
    localStorage.setItem(ASSISTANT_KEY, String(ASSISTANT_DEFAULT))
  } catch {
    /* ignore */
  }
}

function controlSidebar(event: Event) {
  const p = (event as CustomEvent).detail
  if (p.source_project_id === workspace.currentId && p.source_session_id === session.currentId && p.command === 'sidebar') {
    if (p.value === 'open') openConversation()
    else if (p.value === 'close') assistantOpen.value = false
  }
}

onMounted(() => {
  updateViewport()
  window.addEventListener('resize', updateViewport)
  window.addEventListener('mousemove', onDrag)
  window.addEventListener('mouseup', endDrag)
  window.addEventListener('haixun:ui', controlSidebar)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', updateViewport)
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', endDrag)
  window.removeEventListener('haixun:ui', controlSidebar)
})

const assistantStyle = computed(() => ({
  width: (assistantOpen.value ? assistantWidth.value : 44) + 'px',
}))
</script>

<template>
  <div class="app-shell">
    <SideNav v-if="!isMobile" @conversation="openConversation" />
    <SideNav v-else class="mobile-nav" :mobile="true" @conversation="openConversation" />

    <div class="project-body">
      <header v-if="routeProject && projectValid" class="project-toolbar">
        <div class="project-heading"><RouterLink :to="projectPath(routeProject)">{{ workspace.current?.name }}</RouterLink><small :title="workspace.current?.root_directory || ''">{{ workspace.current?.root_directory || '项目文件夹尚未绑定' }}</small><WorkspaceSwitcher v-if="!workspace.current?.root_directory" mode="bind" /></div>
        <nav class="project-tools" aria-label="当前项目功能"><RouterLink :to="projectPath(routeProject)" :class="{ chosen: primaryChat }">项目对话</RouterLink><RouterLink v-for="item in projectPages" :key="item.page" :to="projectPath(routeProject, item.page)" :class="{ chosen: route.path.startsWith(projectPath(routeProject,item.page)) }">{{ item.label }}</RouterLink></nav>
      </header>
      <div v-if="routeProject && !projectValid" class="missing-project"><h2>{{ workspace.loaded ? '项目不存在或已移入回收站' : '正在加载项目…' }}</h2><RouterLink to="/projects">返回项目列表</RouterLink></div>
      <div v-else class="project-content">
    <main v-if="!primaryChat" class="layout-main">
      <div class="content-scroll">
        <router-view v-slot="{ Component }">
            <component :is="Component" :key="route.path" />
        </router-view>
      </div>
    </main>

    <template v-if="routeProject && !sessionRouteError && !isMobile">
      <!-- 拖拽分隔条（助手栏展开时显示） -->
      <div
        v-if="assistantOpen && !primaryChat"
        class="resizer"
        :class="{ dragging }"
        role="separator"
        aria-orientation="vertical"
        aria-label="拖动调整助手栏宽度"
        @mousedown="startDrag"
        @dblclick="resetWidth"
        title="拖动调整宽度，双击恢复默认"
      ></div>
      <AssistantPanel
        ref="assistantPanel"
        :open="primaryChat || assistantOpen"
        :primary="primaryChat"
        :style="primaryChat ? { flex: '1', width: '100%' } : assistantStyle"
        @toggle="assistantOpen = !assistantOpen"
      />
    </template>
    <AssistantPanel v-else-if="routeProject && !sessionRouteError" ref="assistantPanel" class="mobile-assistant" :open="primaryChat" :primary="primaryChat" :mobile="!primaryChat" />
    <p v-if="sessionRouteError" class="session-error" role="alert">{{ sessionRouteError }}</p>
      </div>
    </div>

    <GuideOverlay />
    <AuthModal />
  </div>
</template>

<style scoped>
.project-body { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.project-content { display: flex; flex: 1; min-width: 0; min-height: 0; }
.project-toolbar { flex-shrink: 0; border-bottom: 1px solid var(--border); background: var(--panel); padding: 12px 18px; }
.project-heading { display: flex; align-items: center; gap: 12px; min-width: 0; }
.project-heading > a { color: var(--text); font-size: 15px; font-weight: 600; text-decoration: none; }
.project-heading small { color: var(--text-secondary); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.project-heading :deep(.setup-trigger) { width: auto; font-size: 11px; }
.project-tools { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 10px; }
.project-tools a { color: var(--text-secondary); font-size: 12px; padding: 6px 10px; text-decoration: none; border-radius: 5px; }
.project-tools a.chosen { background: var(--bg-selected); color: var(--text); font-weight: 600; }
.project-tools a:hover { background: var(--panel-secondary); }
.missing-project { padding: 36px; }
.session-error { position: absolute; bottom: 100px; right: 25px; color: #b44239; }
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
}

.layout-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.content-scroll {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
}

.resizer {
  width: 5px;
  flex-shrink: 0;
  cursor: col-resize;
  background: transparent;
  position: relative;
  z-index: 5;
}
.resizer::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 1px;
  background: var(--border);
  transition: background 0.12s, width 0.12s;
}
.resizer:hover::after,
.resizer.dragging::after {
  background: var(--accent);
  width: 2px;
  left: 1px;
}

@media (max-width: 768px) {
  .app-shell {
    flex-direction: column;
  }
  .layout-main {
    flex: 1;
    min-height: 0;
  }
}
</style>
