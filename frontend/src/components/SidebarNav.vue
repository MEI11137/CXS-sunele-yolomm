<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import DialogShell from '@/components/DialogShell.vue'
import NewProjectDialog from '@/components/NewProjectDialog.vue'
import SidebarIcon from '@/components/SidebarIcon.vue'
import { useAuthStore } from '@/stores/auth'
import { useAgentStore } from '@/stores/agent'
import { useConnectionStore } from '@/stores/connection'
import { useRuntimeStore } from '@/stores/runtime'
import { useWorkspaceStore } from '@/stores/workspace'
import type { ChatSession, Project } from '@/types'

/**
 * 左侧栏按 DSH（DeepSeek Harness）开源侧栏的“工作区”式组织重做：
 *
 *   品牌 → 新会话主按钮（Ctrl N，先选工作区）→ 工作区块头（搜索 / 排序 / 新建工作区）
 *   → 工作区树（工作区行 + 缩进的会话行，悬停才出现菜单与新会话）→ 底部全局入口与账号。
 *
 * 只搬结构与交互，不搬 DSH 自己的插件/记忆/自动化入口；颜色、字体仍用 v1.7 主题变量，
 * 主对话区与业务页完全不动。工作区 = 项目，会话 = 项目下的对话，路由与后端接口都不变。
 *
 * @param 无（组件从 route、store 取数据）
 * @returns 侧栏元素
 */
const auth = useAuthStore()
const runtime = useRuntimeStore()
const workspace = useWorkspaceStore()
const connection = useConnectionStore()
const agent = useAgentStore()
const router = useRouter()
const route = useRoute()

/** 每个工作区的展开状态；当前路由所在的工作区由 watch 自动展开。 */
const expanded = reactive<Record<string, boolean>>({})
const search = ref('')
const searchOpen = ref(false)
const searchInput = ref<HTMLInputElement | null>(null)
const createOpen = ref(false)
const sortMode = ref<'recent' | 'name'>('recent')
const creating = ref('')
/** 相对时间的基准：每分钟推一次，避免页面挂着不刷新导致“刚刚”永远不变。 */
const now = ref(Date.now())
let clock = 0

const version = computed(() => runtime.meta?.version ?? runtime.health?.version ?? '—')
const query = computed(() => search.value.trim().toLocaleLowerCase())
const initial = computed(() => (auth.user?.display_name || auth.user?.username || '?').slice(0, 1))

interface Group {
  project: Project
  /** 当前搜索/展开状态下要显示的会话。 */
  chats: ChatSession[]
  /** 该工作区的会话总数（含被搜索过滤掉的）。 */
  total: number
  /** 排序用的“最近活动时间”：项目自身更新与会话更新的较大值。 */
  activeAt: number
}

const allGroups = computed<Group[]>(() =>
  workspace.projects.map((project) => {
    const all = workspace.sessionsByProject[project.id] ?? []
    const projectTime = Date.parse(project.updated_at) || 0
    const sessionTime = all.reduce((max, item) => Math.max(max, Date.parse(item.updated_at) || 0), 0)
    return { project, chats: all, total: all.length, activeAt: Math.max(projectTime, sessionTime) }
  }),
)

/** 搜索命中的工作区显示其全部会话；仅会话命中时只显示命中的那几条。 */
const groups = computed<Group[]>(() => {
  const keyword = query.value
  const rows = allGroups.value
    .map((group) => {
      if (!keyword) return group
      const nameMatched = group.project.name.toLocaleLowerCase().includes(keyword)
      const chats = nameMatched
        ? group.chats
        : group.chats.filter((item) => item.title.toLocaleLowerCase().includes(keyword))
      return { ...group, chats }
    })
    .filter((group) => !keyword || group.chats.length > 0 || group.project.name.toLocaleLowerCase().includes(keyword))

  rows.sort((a, b) =>
    sortMode.value === 'name'
      ? a.project.name.localeCompare(b.project.name, 'zh-Hans-CN')
      : b.activeAt - a.activeAt,
  )
  return rows
})

const projectId = computed(() => String(route.params.projectId ?? ''))
const sessionId = computed(() => String(route.params.sessionId ?? ''))

/** 搜索时强制展开所有命中的工作区，否则按用户手动展开状态。 */
function isOpen(id: string): boolean {
  return Boolean(query.value) || Boolean(expanded[id])
}

function toggle(id: string): void {
  expanded[id] = !expanded[id]
  if (expanded[id] && !workspace.sessionsByProject[id]) void workspace.loadSessions(id)
}

function enterProject(id: string): void {
  if (projectId.value === id) {
    toggle(id)
    return
  }
  expanded[id] = true
  if (!workspace.sessionsByProject[id]) void workspace.loadSessions(id)
  void router.push({ name: 'project', params: { projectId: id } })
}

function enterSession(pid: string, sid: string): void {
  if (projectId.value === pid && sessionId.value === sid) return
  expanded[pid] = true
  void router.push({ name: 'session', params: { projectId: pid, sessionId: sid } })
}

/** 侧栏直接新建会话：建完立刻进入，不用先回项目页。 */
async function newSession(pid: string): Promise<void> {
  menu.value = null
  if (creating.value) return
  creating.value = pid
  try {
    const created = await workspace.createSession(pid, '新会话')
    if (!created) return
    expanded[pid] = true
    await router.push({ name: 'session', params: { projectId: pid, sessionId: created.id } })
  } finally {
    creating.value = ''
  }
}

// ---- 搜索：点图标原地展开，Esc 或清空后收起
function toggleSearch(): void {
  if (searchOpen.value) {
    closeSearch()
    return
  }
  searchOpen.value = true
  void nextTick(() => searchInput.value?.focus())
}

function closeSearch(): void {
  searchOpen.value = false
  search.value = ''
}

// ---- 浮动菜单（Teleport 到 body，避免被滚动容器裁掉）
type Menu =
  | { kind: 'new'; x: number; y: number; width: number }
  | { kind: 'sort'; x: number; y: number }
  | { kind: 'project'; x: number; y: number; projectId: string; name: string }
  | { kind: 'session'; x: number; y: number; projectId: string; sessionId: string; title: string }

const menu = ref<Menu | null>(null)
const newButton = ref<HTMLButtonElement | null>(null)

const menuStyle = computed(() => {
  const current = menu.value
  if (!current) return {}
  const style: Record<string, string> = { top: `${current.y}px`, left: `${current.x}px` }
  if (current.kind === 'new') style.width = `${current.width}px`
  return style
})

function clampX(x: number, width = 200): number {
  return Math.max(8, Math.min(Math.round(x), window.innerWidth - width - 8))
}

function clampY(y: number, height = 120): number {
  return Math.max(8, Math.min(Math.round(y), window.innerHeight - height - 8))
}

/** 新会话：没有工作区就直接去建；只有一个就直接建；多个先选工作区。 */
function startNewSession(): void {
  menu.value = null
  if (!workspace.projects.length) {
    createOpen.value = true
    return
  }
  if (workspace.projects.length === 1) {
    void newSession(workspace.projects[0].id)
    return
  }
  const anchor = newButton.value
  if (!anchor) return
  const rect = anchor.getBoundingClientRect()
  menu.value = {
    kind: 'new',
    x: Math.round(rect.left),
    y: Math.round(rect.bottom + 6),
    width: Math.round(rect.width),
  }
}

function openSortMenu(event: MouseEvent): void {
  if (menu.value?.kind === 'sort') {
    menu.value = null
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menu.value = { kind: 'sort', x: clampX(rect.right - 196, 196), y: clampY(rect.bottom + 4, 110) }
}

function setSort(mode: 'recent' | 'name'): void {
  sortMode.value = mode
  menu.value = null
}

function openProjectMenu(event: MouseEvent, project: Project): void {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menu.value = {
    kind: 'project',
    projectId: project.id,
    name: project.name,
    x: clampX(rect.left, 200),
    y: clampY(rect.bottom + 4, 110),
  }
}

function openSessionMenu(event: MouseEvent, pid: string, session: ChatSession): void {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menu.value = {
    kind: 'session',
    projectId: pid,
    sessionId: session.id,
    title: session.title,
    x: clampX(rect.left, 200),
    y: clampY(rect.bottom + 4, 110),
  }
}

// ---- 重命名：侧栏内弹一个小对话框，成功后直接更新列表
const renaming = ref<null | { kind: 'project' | 'session'; id: string; projectId: string; original: string }>(null)
const renameValue = ref('')
const renameBusy = ref(false)
const renameInput = ref<HTMLInputElement | null>(null)

function beginRename(target: { kind: 'project' | 'session'; id: string; projectId: string; original: string }): void {
  menu.value = null
  renaming.value = target
  renameValue.value = target.original
  void nextTick(() => {
    renameInput.value?.focus()
    renameInput.value?.select()
  })
}

function startProjectRename(): void {
  const current = menu.value
  if (!current || current.kind !== 'project') return
  beginRename({ kind: 'project', id: current.projectId, projectId: current.projectId, original: current.name })
}

function startSessionRename(pid?: string, session?: ChatSession): void {
  const current = menu.value
  const project = pid ?? (current?.kind === 'session' ? current.projectId : '')
  const id = session?.id ?? (current?.kind === 'session' ? current.sessionId : '')
  const title = session?.title ?? (current?.kind === 'session' ? current.title : '')
  if (!project || !id) return
  beginRename({ kind: 'session', id, projectId: project, original: title })
}

async function submitRename(): Promise<void> {
  const target = renaming.value
  if (!target || renameBusy.value) return
  const name = renameValue.value.trim()
  if (!name) return
  if (name === target.original) {
    renaming.value = null
    return
  }
  renameBusy.value = true
  try {
    const done = target.kind === 'project'
      ? await workspace.renameProject(target.id, name)
      : await workspace.renameSession(target.projectId, target.id, name)
    if (done) renaming.value = null
  } finally {
    renameBusy.value = false
  }
}

// ---- 删除：软删进回收站，确认文案与其他页面一致
async function removeProject(): Promise<void> {
  const current = menu.value
  if (!current || current.kind !== 'project') return
  menu.value = null
  if (!window.confirm(`把「${current.name}」移入回收站？之后可以在回收站恢复。`)) return
  const id = current.projectId
  const ok = await workspace.deleteProject(id)
  if (ok && projectId.value === id) await router.replace({ name: 'projects' })
}

async function removeSession(): Promise<void> {
  const current = menu.value
  if (!current || current.kind !== 'session') return
  menu.value = null
  if (!window.confirm(`把「${current.title}」移入回收站？之后可以在回收站恢复。`)) return
  const { projectId: pid, sessionId: sid } = current
  const ok = await workspace.deleteSession(pid, sid)
  if (ok && sessionId.value === sid) await router.replace({ name: 'project', params: { projectId: pid } })
}

// ---- 时间与全局键盘
function relativeTime(iso: string): string {
  const time = Date.parse(iso)
  if (!Number.isFinite(time)) return ''
  const diff = Math.max(0, now.value - time)
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diff < minute) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / minute)}分钟`
  if (diff < day) return `${Math.floor(diff / hour)}小时`
  if (diff < 30 * day) return `${Math.floor(diff / day)}天`
  if (diff < 365 * day) return `${Math.floor(diff / (30 * day))}个月`
  return `${Math.floor(diff / (365 * day))}年`
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    menu.value = null
    if (searchOpen.value) closeSearch()
    return
  }
  if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === 'n') {
    event.preventDefault()
    startNewSession()
  }
}

// 当前项目展开；首次进入把已有会话取回来（只读，不会创建会话）。
watch(
  projectId,
  (id) => {
    if (id) expanded[id] = true
  },
  { immediate: true },
)

watch(
  () => workspace.projects.map((item) => item.id).join(','),
  () => {
    for (const item of workspace.projects) {
      if (!workspace.sessionsByProject[item.id]) void workspace.loadSessions(item.id)
    }
  },
  { immediate: true },
)

onMounted(() => {
  clock = window.setInterval(() => {
    now.value = Date.now()
  }, 60_000)
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.clearInterval(clock)
  window.removeEventListener('keydown', onKeydown)
})

async function logout(): Promise<void> {
  await auth.logout()
  agent.reset()
  workspace.reset()
  connection.reset()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <aside class="sidebar">
    <div class="sb-brand">
      <span class="sb-brand-mark" aria-hidden="true">✳</span>
      <span class="sb-brand-name">海巡智检</span>
      <span class="sb-version" title="当前版本">v{{ version }}</span>
    </div>

    <div class="sb-new-wrap">
      <button
        ref="newButton"
        class="sb-new"
        type="button"
        aria-haspopup="menu"
        :aria-expanded="menu?.kind === 'new'"
        @click="startNewSession"
      >
        <SidebarIcon name="chat-plus" :size="15" />
        <span>新会话</span>
        <span class="sb-kbd" aria-hidden="true">Ctrl N</span>
      </button>
    </div>

    <div class="sb-section" :class="{ 'sb-searching': searchOpen }">
      <span v-show="!searchOpen" class="sb-section-label">工作区</span>
      <div class="sb-search">
        <button
          class="sb-icon"
          type="button"
          :aria-label="searchOpen ? '收起搜索' : '搜索工作区或会话'"
          :aria-expanded="searchOpen"
          @click="toggleSearch"
        >
          <SidebarIcon name="search" :size="15" />
        </button>
        <span v-if="searchOpen" class="sb-search-box">
          <input
            ref="searchInput"
            v-model="search"
            class="sb-search-input"
            type="search"
            placeholder="搜索工作区或会话"
            aria-label="搜索工作区或会话"
            @keydown.esc="closeSearch"
          />
          <button
            v-if="search"
            class="sb-icon sb-search-clear"
            type="button"
            aria-label="清空搜索"
            @click="search = ''"
          >
            ×
          </button>
        </span>
      </div>
      <div v-show="!searchOpen" class="sb-actions">
        <button
          class="sb-icon"
          type="button"
          aria-label="排序"
          :aria-expanded="menu?.kind === 'sort'"
          @click="openSortMenu"
        >
          <SidebarIcon name="sort" :size="15" />
        </button>
        <button class="sb-icon" type="button" aria-label="新建工作区" @click="createOpen = true">
          <SidebarIcon name="folder-plus" :size="16" />
        </button>
      </div>
    </div>

    <div class="sb-tree" role="tree" aria-label="工作区与会话">
      <template v-for="group in groups" :key="group.project.id">
        <div
          class="sb-workspace"
          :class="{
            'sb-current': projectId === group.project.id,
            'sb-menu-open': menu?.kind === 'project' && menu.projectId === group.project.id,
          }"
          role="treeitem"
          tabindex="0"
          :aria-expanded="isOpen(group.project.id)"
          :title="group.project.root_path || group.project.name"
          @click="enterProject(group.project.id)"
          @keydown.enter="enterProject(group.project.id)"
        >
          <button
            class="sb-chevron"
            :class="{ 'sb-chevron-open': isOpen(group.project.id) }"
            type="button"
            :aria-label="`${isOpen(group.project.id) ? '折叠' : '展开'}工作区 ${group.project.name}`"
            @click.stop="toggle(group.project.id)"
          >
            <SidebarIcon name="chevron" :size="10" />
          </button>
          <span class="sb-folder"><SidebarIcon name="folder" :size="16" /></span>
          <span class="sb-workspace-name">{{ group.project.name }}</span>
          <span class="sb-count">{{ group.total }}</span>
          <span class="sb-row-actions">
            <button
              class="sb-icon sb-row-icon"
              type="button"
              :aria-label="`${group.project.name} 的菜单`"
              @click.stop="openProjectMenu($event, group.project)"
            >
              <SidebarIcon name="dots" :size="15" />
            </button>
            <button
              class="sb-icon sb-row-icon"
              type="button"
              :aria-label="`在 ${group.project.name} 新建会话`"
              :disabled="creating === group.project.id"
              @click.stop="newSession(group.project.id)"
            >
              <SidebarIcon name="chat-plus" :size="15" />
            </button>
          </span>
        </div>

        <div v-if="isOpen(group.project.id)" class="sb-sessions" role="group">
          <div
            v-for="session in group.chats"
            :key="session.id"
            class="sb-session"
            :class="{
              'sb-session-active': projectId === group.project.id && sessionId === session.id,
              'sb-menu-open': menu?.kind === 'session' && menu.sessionId === session.id,
            }"
            role="treeitem"
            tabindex="0"
            :aria-selected="projectId === group.project.id && sessionId === session.id"
            :title="session.title"
            @click="enterSession(group.project.id, session.id)"
            @keydown.enter="enterSession(group.project.id, session.id)"
            @dblclick.stop="startSessionRename(group.project.id, session)"
          >
            <span class="sb-session-title">{{ session.title }}</span>
            <span class="sb-time">{{ relativeTime(session.updated_at) }}</span>
            <span class="sb-row-actions">
              <button
                class="sb-icon sb-row-icon"
                type="button"
                :aria-label="`${session.title} 的菜单`"
                @click.stop="openSessionMenu($event, group.project.id, session)"
              >
                <SidebarIcon name="dots" :size="15" />
              </button>
            </span>
          </div>
          <p v-if="!group.chats.length" class="sb-empty">{{ query ? '没有匹配的会话' : '暂无会话' }}</p>
        </div>
      </template>

      <p v-if="!groups.length" class="sb-empty sb-empty-root">
        {{ query ? '没有匹配的工作区或会话' : '还没有工作区。点右上角文件夹图标新建。' }}
      </p>
    </div>

    <footer class="sb-footer">
      <nav class="sb-nav" aria-label="全局入口">
        <RouterLink class="sb-navrow" :class="{ active: route.name === 'trash' }" :to="{ name: 'trash' }">
          <SidebarIcon name="trash" :size="15" />
          <span>回收站</span>
        </RouterLink>
        <RouterLink class="sb-navrow" :class="{ active: route.name === 'settings' }" :to="{ name: 'settings' }">
          <SidebarIcon name="gear" :size="15" />
          <span>设置</span>
        </RouterLink>
        <RouterLink
          v-if="auth.isAdmin"
          class="sb-navrow"
          :class="{ active: route.name === 'admin' }"
          :to="{ name: 'admin' }"
        >
          <SidebarIcon name="shield" :size="15" />
          <span>本机管理</span>
        </RouterLink>
      </nav>
      <div class="sb-user">
        <span class="sb-avatar" aria-hidden="true">{{ initial }}</span>
        <span class="sb-user-name" :title="auth.user?.username">{{ auth.user?.display_name || auth.user?.username }}</span>
        <span class="sb-role">{{ auth.isAdmin ? '管理员' : '普通用户' }}</span>
        <button class="sb-icon" type="button" aria-label="退出登录" title="退出登录" @click="logout">
          <SidebarIcon name="logout" :size="15" />
        </button>
      </div>
    </footer>

    <NewProjectDialog :open="createOpen" @close="createOpen = false" />

    <Teleport to="body">
      <div v-if="menu" class="sb-menu-scrim" @click="menu = null" @contextmenu.prevent="menu = null"></div>
      <div v-if="menu" class="sb-pop" :style="menuStyle" role="menu">
        <template v-if="menu.kind === 'new'">
          <p class="sb-pop-title">选择工作区开始新会话</p>
          <button
            v-for="group in groups"
            :key="group.project.id"
            class="sb-pop-item"
            type="button"
            role="menuitem"
            @click="newSession(group.project.id)"
          >
            <SidebarIcon name="folder" :size="15" />
            <span class="sb-pop-item-text">{{ group.project.name }}</span>
          </button>
          <div class="sb-pop-sep"></div>
          <button class="sb-pop-item" type="button" role="menuitem" @click="menu = null; createOpen = true">
            <SidebarIcon name="folder-plus" :size="15" />
            <span class="sb-pop-item-text">新建工作区</span>
          </button>
        </template>

        <template v-else-if="menu.kind === 'sort'">
          <p class="sb-pop-title">排序</p>
          <button class="sb-pop-item" type="button" role="menuitem" @click="setSort('recent')">
            <span class="sb-pop-item-text">最近更新</span>
            <span v-if="sortMode === 'recent'" class="sb-check">✓</span>
          </button>
          <button class="sb-pop-item" type="button" role="menuitem" @click="setSort('name')">
            <span class="sb-pop-item-text">名称</span>
            <span v-if="sortMode === 'name'" class="sb-check">✓</span>
          </button>
        </template>

        <template v-else-if="menu.kind === 'project'">
          <button class="sb-pop-item" type="button" role="menuitem" @click="startProjectRename">重命名工作区</button>
          <div class="sb-pop-sep"></div>
          <button class="sb-pop-item sb-danger" type="button" role="menuitem" @click="removeProject">删除工作区</button>
        </template>

        <template v-else>
          <button class="sb-pop-item" type="button" role="menuitem" @click="startSessionRename()">重命名会话</button>
          <div class="sb-pop-sep"></div>
          <button class="sb-pop-item sb-danger" type="button" role="menuitem" @click="removeSession">删除会话</button>
        </template>
      </div>
    </Teleport>

    <DialogShell
      :open="renaming !== null"
      :title="renaming?.kind === 'project' ? '重命名工作区' : '重命名会话'"
      @close="renaming = null"
    >
      <label class="field">
        <span>名称</span>
        <input
          ref="renameInput"
          v-model="renameValue"
          :placeholder="renaming?.original"
          maxlength="80"
          @keydown.enter.prevent="submitRename"
        />
      </label>
      <template #footer>
        <button class="ghost" type="button" @click="renaming = null">取消</button>
        <button class="primary" type="button" :disabled="renameBusy || !renameValue.trim()" @click="submitRename">
          保存
        </button>
      </template>
    </DialogShell>
  </aside>
</template>

<style scoped>
/* 侧栏：浅灰底 + 32px 行高 + 悬停才出现的行内操作，结构对齐 DSH 工作区侧栏。 */
.sidebar {
  width: var(--nav-w);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 0;
  padding: 10px 10px 8px;
  background: var(--panel-secondary);
  border-right: 1px solid var(--border);
  color: var(--text);
  font-size: 13.5px;
  -webkit-user-select: none;
  user-select: none;
}

.sb-brand {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 40px;
  padding: 0 6px 2px;
}

.sb-brand-mark {
  font-size: 19px;
  line-height: 1;
  color: var(--accent);
}

.sb-brand-name {
  font-size: 14.5px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.sb-version {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.sb-new-wrap {
  flex: none;
  padding: 6px 2px 10px;
}

.sb-new {
  position: relative;
  width: 100%;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
  color: var(--text);
  font: inherit;
  font-size: 13.5px;
  font-weight: 550;
  cursor: pointer;
  box-shadow: var(--shadow-sm);
  transition: background 120ms ease, border-color 120ms ease;
}

.sb-new:hover {
  background: var(--bg-selected);
}

.sb-new:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.sb-kbd {
  position: absolute;
  right: 9px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.02em;
  color: var(--text-secondary);
}

.sb-section {
  flex: none;
  display: flex;
  align-items: center;
  gap: 2px;
  height: 30px;
  padding: 0 2px 0 8px;
  color: var(--text-secondary);
}

.sb-section-label {
  flex: 1;
  min-width: 0;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}

.sb-search {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 0 0 auto;
  min-width: 0;
}

.sb-searching .sb-search {
  flex: 1;
}

.sb-search-box {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  position: relative;
}

.sb-search-input {
  flex: 1;
  min-width: 0;
  height: 26px;
  padding: 0 24px 0 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--panel);
  color: var(--text);
  font: inherit;
  font-size: 12.5px;
  outline: none;
}

.sb-search-input::-webkit-search-cancel-button {
  -webkit-appearance: none;
  appearance: none;
}

.sb-search-input:focus {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}

.sb-search-clear {
  position: absolute;
  right: 2px;
  top: 2px;
  width: 22px;
  height: 22px;
  font-size: 15px;
  line-height: 1;
  color: var(--text-secondary);
}

.sb-actions {
  flex: none;
  display: flex;
  align-items: center;
  gap: 2px;
}

.sb-icon {
  flex: none;
  display: inline-grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.sb-icon:hover {
  background: var(--bg-selected);
  color: var(--text);
}

.sb-icon:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.sb-icon:disabled {
  opacity: 0.45;
  cursor: default;
}

.sb-tree {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 2px 0 10px;
  margin: 0 -2px;
}

.sb-workspace {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 6px;
  border-radius: var(--radius-md);
  cursor: pointer;
  color: var(--text);
}

.sb-workspace:hover,
.sb-workspace.sb-current {
  background: var(--bg-selected);
}

.sb-workspace:focus-visible {
  outline: none;
  box-shadow: inset 0 0 0 2px var(--accent);
}

.sb-chevron {
  flex: none;
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
  transition: transform 140ms ease;
}

.sb-chevron-open {
  transform: rotate(90deg);
}

.sb-folder {
  flex: none;
  display: grid;
  place-items: center;
  color: var(--text-secondary);
}

.sb-workspace:hover .sb-folder {
  color: var(--text);
}

.sb-workspace-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13.5px;
}

.sb-count {
  flex: none;
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.sb-workspace:hover .sb-count,
.sb-workspace.sb-menu-open .sb-count {
  display: none;
}

.sb-row-actions {
  display: none;
  align-items: center;
  gap: 0;
  flex: none;
  margin-left: auto;
}

.sb-workspace:hover .sb-row-actions,
.sb-workspace.sb-menu-open .sb-row-actions,
.sb-session:hover .sb-row-actions,
.sb-session.sb-menu-open .sb-row-actions {
  display: flex;
}

.sb-row-icon {
  width: 22px;
  height: 22px;
}

.sb-sessions {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 1px 0 6px 24px;
}

.sb-session {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 6px;
  border-radius: var(--radius-md);
  cursor: pointer;
  color: var(--text-secondary);
}

.sb-session:hover,
.sb-session.sb-menu-open {
  background: var(--bg-selected);
  color: var(--text);
}

.sb-session.sb-session-active {
  background: var(--bg-selected);
  color: var(--text);
  font-weight: 600;
}

.sb-session:focus-visible {
  outline: none;
  box-shadow: inset 0 0 0 2px var(--accent);
}

.sb-session-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

.sb-time {
  flex: none;
  font-size: 10.5px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.sb-session:hover .sb-time,
.sb-session.sb-menu-open .sb-time {
  display: none;
}

.sb-empty {
  margin: 0;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.sb-empty-root {
  padding: 12px 8px;
  line-height: 1.6;
}

.sb-footer {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}

.sb-nav {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.sb-navrow {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 8px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 12.5px;
}

.sb-navrow:hover {
  background: var(--bg-selected);
  color: var(--text);
}

.sb-navrow.active {
  background: var(--bg-selected);
  color: var(--text);
  font-weight: 600;
}

.sb-user {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 4px 0 6px;
  margin-top: 2px;
}

.sb-avatar {
  flex: none;
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--bg-selected);
  color: var(--text);
  font-size: 11px;
  font-weight: 600;
}

.sb-user-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
}

.sb-role {
  flex: none;
  font-size: 10px;
  color: var(--text-secondary);
}

.sb-user .sb-icon {
  width: 24px;
  height: 24px;
}

/* 浮动菜单：点空白处关闭；Teleport 到 body 后 scoped 样式仍然生效。 */
.sb-menu-scrim {
  position: fixed;
  inset: 0;
  z-index: 60;
}

.sb-pop {
  position: fixed;
  z-index: 61;
  min-width: 196px;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
  box-shadow: var(--shadow-md);
}

.sb-pop-title {
  margin: 2px 0 4px;
  padding: 3px 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}

.sb-pop-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 30px;
  padding: 0 8px;
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text);
  font: inherit;
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
}

.sb-pop-item:hover {
  background: var(--bg-selected);
}

.sb-pop-item-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sb-pop-sep {
  height: 1px;
  margin: 4px 6px;
  background: var(--border);
}

.sb-danger {
  color: var(--danger);
}

.sb-check {
  margin-left: auto;
  color: var(--accent);
}

@media (max-width: 980px) {
  /* 抽屉：默认收在左侧之外，只占自身 280px，主区拿到完整视口宽度。
     打开入口见 AppLayout 的“工作区”按钮；不用 overflow:hidden 掩藏控件。 */
  .sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 70;
    width: 280px;
    max-width: 86vw;
    transform: translateX(-100%);
    transition: transform 160ms ease;
    box-shadow: 18px 0 40px rgba(0, 0, 0, 0.16);
    border-right: 1px solid var(--border);
    border-bottom: 0;
  }

  .sb-tree {
    max-height: none;
  }
}
</style>