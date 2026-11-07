<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import AssistantPanel from '@/components/AssistantPanel.vue'
import ProjectDetailsDrawer from '@/components/ProjectDetailsDrawer.vue'
import SidebarNav from '@/components/SidebarNav.vue'
import { BUSINESS_PAGES, dockState, navOwner } from '@/lib/layout'
import { useRuntimeStore } from '@/stores/runtime'
import { useUiStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'

/**
 * 应用外壳，结构与 v1.6 一致：
 *
 *   左侧项目/会话  |  项目顶部唯一业务导航 +（主对话 或 业务页 | 右侧助手）
 *
 * 业务入口只在顶部出现一次；左侧不再重复整套业务菜单。
 * 助手宽度可拖（260–640，默认 336），收起后留“助手”入口，纯显示偏好。
 */
const runtime = useRuntimeStore()
const workspace = useWorkspaceStore()
const ui = useUiStore()
const route = useRoute()
const router = useRouter()

const projectId = computed(() => String(route.params.projectId ?? ''))
/** 窄屏（<=980px）：左侧导航变抽屉、助手栏变覆盖层，主区拿满宽度。 */
const NARROW_MAX = 980
const isNarrow = ref(typeof window !== 'undefined' && window.innerWidth <= NARROW_MAX)
const sidebarOpen = ref(false)
const narrowDockOpen = ref(false)
const project = computed(() =>
  projectId.value ? workspace.projectById(projectId.value) : null,
)
/** 只有“业务页”才把对话停靠到右侧（`meta.dock`）；会话页的对话在主区。 */
const dockBase = computed(() =>
  dockState({ dockable: route.meta.dock === true, collapsed: ui.dockCollapsed }),
)
const dock = computed(() => {
  if (!isNarrow.value) return dockBase.value
  // 会话页助手在主区（dockState 返回 none），不能凭空长出右侧列。
  if (dockBase.value === 'none') return 'none'
  return narrowDockOpen.value ? 'open' : 'closed'
})
const chatRoute = computed(() => route.name === 'session' || route.name === 'project')
const detailsOpen = computed(() => route.query.details === '1')
const dockStyle = computed(() => ({ '--dock-w': `${ui.assistantWidth}px` }))

function openDock(): void {
  if (isNarrow.value) narrowDockOpen.value = true
  else ui.toggleDock()
}

function onWindowResize(): void {
  const narrow = window.innerWidth <= NARROW_MAX
  isNarrow.value = narrow
  if (!narrow) {
    sidebarOpen.value = false
    narrowDockOpen.value = false
  }
}

function openDetails(): void {
  if (route.query.details === '1') return
  void router.replace({ query: { ...route.query, details: '1' } })
}

function closeDetails(): void {
  const query = { ...route.query }
  delete query.details
  void router.replace({ query })
}

/** 高亮归属：数据回流算「数据集」，单条复核算「人工复核」。 */
function isChosen(name: string): boolean {
  return navOwner(route.name) === name
}

// ---- 助手栏拖拽（与 v1.6 同一套交互：拖动改宽、双击恢复默认）
const dragging = ref(false)

function startDrag(event: MouseEvent): void {
  if (dock.value !== 'open') return
  dragging.value = true
  document.body.classList.add('dock-dragging')
  event.preventDefault()
}

function onDrag(event: MouseEvent): void {
  if (!dragging.value) return
  // 从右边缘往左拖：宽度 = 窗口宽 - 鼠标 X
  ui.setAssistantWidth(window.innerWidth - event.clientX)
}

function endDrag(): void {
  if (!dragging.value) return
  dragging.value = false
  document.body.classList.remove('dock-dragging')
}

onMounted(() => {
  void workspace.loadProjects()
  if (projectId.value) void workspace.loadSessions(projectId.value)
  if (projectId.value) workspace.rememberProject(projectId.value)
  window.addEventListener('mousemove', onDrag)
  window.addEventListener('mouseup', endDrag)
  window.addEventListener('resize', onWindowResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', endDrag)
  window.removeEventListener('resize', onWindowResize)
  document.body.classList.remove('dock-dragging')
})

// 收起助手（助手栏自己的按钮）或切页时，窄屏覆盖层跟着关闭,避免盖住主区。
watch(
  () => ui.dockCollapsed,
  () => {
    if (isNarrow.value) narrowDockOpen.value = false
  },
)
watch(
  () => route.fullPath,
  () => {
    sidebarOpen.value = false
  },
)

watch(projectId, (value) => {
  if (!value) return
  void workspace.loadSessions(value)
  workspace.rememberProject(value)
})

// 后台（Agent）删掉当前项目后，项目列表会少一项：立刻回到项目列表，
// 不把用户留在一条已经不存在的详情的空壳里。首屏加载中（projectsReady=false）不判断。
watch(
  [() => workspace.projectsReady, projectId, () => workspace.projects.length],
  () => {
    if (!workspace.projectsReady) return
    const id = projectId.value
    if (!id || workspace.projectById(id)) return
    void router.replace({ name: 'projects' })
  },
)

// 当前会话被删（本人或 Agent）：会话列表已重新加载后回到项目主页。
// 只在列表**确实读到**时判断，避免列表还没返回就误跳。
watch(
  [
    () => route.params.sessionId,
    projectId,
    () => workspace.sessionsByProject[projectId.value]?.length ?? -1,
  ],
  () => {
    const sessionId = String(route.params.sessionId ?? '')
    if (!sessionId) return
    const list = workspace.sessionsByProject[projectId.value]
    if (!list) return
    if (list.some((item) => item.id === sessionId)) return
    void router.replace({ name: 'project', params: { projectId: projectId.value } })
  },
)
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-open': sidebarOpen }">
    <SidebarNav />
    <button
      v-if="sidebarOpen"
      class="sidebar-scrim"
      type="button"
      aria-label="关闭工作区导航"
      @click="sidebarOpen = false"
    ></button>

    <div class="project-body">
      <header v-if="project" class="project-toolbar">
        <div class="project-heading">
          <button
            class="sidebar-toggle"
            type="button"
            aria-label="打开工作区导航"
            :aria-expanded="sidebarOpen"
            @click="sidebarOpen = !sidebarOpen"
          >
            ☰ 工作区
          </button>
          <h1 :title="project.name">{{ project.name }}</h1>
          <small :title="project.root_path || ''">
            {{ project.root_path || '项目文件夹尚未绑定' }}
          </small>
          <button class="ghost small" type="button" @click="openDetails">项目详情</button>
        </div>
        <nav class="project-tools" aria-label="当前项目功能">
          <RouterLink
            :to="{ name: 'project', params: { projectId } }"
            :class="{ chosen: route.name === 'project' }"
          >
            项目对话
          </RouterLink>
          <RouterLink
            v-for="page in BUSINESS_PAGES"
            :key="page.name"
            :to="{ name: page.name, params: { projectId } }"
            :class="{ chosen: isChosen(page.name) }"
          >
            {{ page.label }}
          </RouterLink>
        </nav>
      </header>

      <div class="project-content">
        <button
          v-if="!project && isNarrow"
          class="sidebar-toggle standalone"
          type="button"
          aria-label="打开工作区导航"
          :aria-expanded="sidebarOpen"
          @click="sidebarOpen = !sidebarOpen"
        >
          ☰ 工作区
        </button>
        <main class="app-main" :class="{ 'app-main-chat': chatRoute }">
          <p v-if="workspace.error" class="banner banner-error" role="alert">
            {{ workspace.error }}
            <button class="link" type="button" @click="workspace.error = ''">知道了</button>
          </p>
          <p v-if="runtime.error" class="banner banner-warn" role="status">
            运行信息读取失败：{{ runtime.error }}（不影响业务操作）
          </p>
          <router-view :key="`${projectId}:${String(route.params.sessionId ?? '')}`" />
        </main>

        <template v-if="dock === 'open'">
          <div
            class="dock-resizer"
            :class="{ dragging }"
            role="separator"
            aria-orientation="vertical"
            aria-label="拖动调整助手栏宽度"
            title="拖动调整宽度，双击恢复默认"
            @mousedown="startDrag"
            @dblclick="ui.resetAssistantWidth()"
          ></div>
          <AssistantPanel variant="dock" :style="dockStyle" />
        </template>
        <button
          v-else-if="dock === 'closed'"
          class="dock-reopen"
          type="button"
          aria-label="打开右侧助手"
          title="打开右侧助手"
          @click="openDock"
        >
          助手
        </button>
      </div>
    </div>

    <ProjectDetailsDrawer v-if="project" :open="detailsOpen" @close="closeDetails" />
  </div>
</template>
