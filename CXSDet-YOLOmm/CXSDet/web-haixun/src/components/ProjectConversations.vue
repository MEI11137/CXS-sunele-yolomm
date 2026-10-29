<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSessionStore } from '@/stores/session'
import { useRouter } from 'vue-router'
import { getAdapter } from '@/services'
import { projectPath } from '@/utils/projectRoutes'
import ConfirmDeleteDialog from './ConfirmDeleteDialog.vue'

const emit = defineEmits<{ select: [] }>()
const workspace = useWorkspaceStore()
const session = useSessionStore()
const router = useRouter()
const deletion = ref<{ id: string; projectId: string; name: string; kind: 'project' | 'session' } | null>(null)
const deleting = ref(false)
const deleteError = ref('')
const search = ref('')
const expanded = ref<Record<string, boolean>>({})
const error = ref('')
const groups = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  return workspace.workspaces.filter(project => project.origin !== 'system').map(project => {
    const all = session.projectSessions(project.id)
    const nameMatches = project.name.toLocaleLowerCase().includes(query)
    return { project, count: all.length, chats: query && !nameMatches
      ? all.filter(chat => chat.title.toLocaleLowerCase().includes(query)) : all }
  }).filter(group => !query || group.project.name.toLocaleLowerCase().includes(query) || group.chats.length)
})

watch(() => workspace.workspaces.map(project => project.id), ids => {
  for (const id of ids) void session.loadProjectHistory(id).catch(() => {})
}, { immediate: true })
watch(() => workspace.currentId, id => { if (id) expanded.value[id] = true }, { immediate: true })

function chooseProject(id: string) {
  expanded.value[id] = true
  void router.push(projectPath(id))
  emit('select')
}

async function openChat(projectId: string, id: string) {
  error.value = ''
  try { await router.push(projectPath(projectId, 'chat/' + encodeURIComponent(id))); await session.selectSession(id); emit('select') }
  catch (e) { error.value = (e as Error).message }
}

async function newChat(projectId: string) {
  error.value = ''
  expanded.value[projectId] = true
  try { await router.push(projectPath(projectId)); const chat = await session.createSession(projectId); if (workspace.currentId === projectId) { await router.push(projectPath(projectId,'chat/' + chat.id)); emit('select') } }
  catch (e) { error.value = (e as Error).message }
}

function dateLabel(timestamp: number) {
  return new Date(timestamp).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
async function confirmDelete() {
  const target = deletion.value
  if (!target || deleting.value) return
  deleting.value = true; deleteError.value = ''
  try {
    if (target.kind === 'project') {
      await getAdapter().deleteWorkspace(target.id)
      session.forgetProject(target.id); workspace.forget(target.id)
      if (String(router.currentRoute.value.params.projectId || '') === target.id)
        await router.push(workspace.currentId ? projectPath(workspace.currentId) : '/projects')
    } else {
      await getAdapter().deleteSession(target.id)
      const selected = session.currentId === target.id
      session.forgetSession(target.id)
      if (selected) await router.replace(projectPath(target.projectId, session.currentId ? 'chat/' + session.currentId : 'chat'))
    }
    deletion.value = null
  } catch (e) { deleteError.value = (e as Error).message } finally { deleting.value = false }
}
</script>

<template>
  <section class="projects" aria-label="项目对话历史">
    <div class="projects-heading">项目 <span>{{ workspace.workspaces.filter(p => p.origin !== 'system').length }}</span></div>
    <input v-model="search" class="project-search" aria-label="搜索项目或会话" placeholder="搜索项目或会话" />
    <p v-if="error" class="project-error" role="alert">{{ error }}</p>
    <div class="project-groups">
      <div v-for="group in groups" :key="group.project.id" class="project-group">
        <div class="project-row" :class="{ active: group.project.id === workspace.currentId }">
          <button class="project-fold" :aria-label="`${expanded[group.project.id] ? '折叠' : '展开'}${group.project.name}`" @click="expanded[group.project.id] = !expanded[group.project.id]">{{ expanded[group.project.id] ? '▾' : '▸' }}</button>
          <button class="project-toggle" :aria-expanded="!!expanded[group.project.id] || !!search.trim()" :title="group.project.root_directory || group.project.name" @click="chooseProject(group.project.id)">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="M1.5 4h5l1-1.5h6.5V13h-12z" /></svg>
            <span class="project-name">{{ group.project.name }}</span>
            <span class="project-count">{{ group.count }}</span>
          </button>
          <button class="project-new" :aria-label="`在${group.project.name}中新建会话`" :title="`在「${group.project.name}」中新建会话`" :disabled="session.creating[group.project.id]" @click="newChat(group.project.id)">+</button>
          <button class="delete-item" :aria-label="`删除项目${group.project.name}`" title="删除项目" @click="deleteError = ''; deletion = { id: group.project.id, projectId: group.project.id, name: group.project.name, kind: 'project' }">×</button>
        </div>
        <div v-if="expanded[group.project.id] || search.trim()" class="project-chats">
          <p v-if="session.projectLoading[group.project.id]" class="project-hint">加载历史中…</p>
          <div v-else-if="session.projectErrors[group.project.id]" class="project-error">
            {{ session.projectErrors[group.project.id] }}
            <button class="retry" @click="session.loadProjectHistory(group.project.id, true).catch(() => {})">重试</button>
          </div>
          <div v-for="chat in group.chats" :key="chat.id" class="chat-entry"><button class="chat-row" :class="{ selected: chat.id === session.currentId && group.project.id === workspace.currentId }"
            :aria-current="chat.id === session.currentId && group.project.id === workspace.currentId ? 'true' : undefined"
            :title="`${chat.title} · ${dateLabel(chat.updated_at || chat.created_at)}`" @click="openChat(group.project.id, chat.id)">
            <span class="chat-name">{{ chat.title }}</span>
            <span class="chat-time">{{ dateLabel(chat.updated_at || chat.created_at) }}</span>
          </button><button class="delete-item" :aria-label="`删除会话${chat.title}`" title="删除会话" @click="deleteError = ''; deletion = { id: chat.id, projectId: group.project.id, name: chat.title, kind: 'session' }">×</button></div>
          <p v-if="!session.projectLoading[group.project.id] && !session.projectErrors[group.project.id] && !group.chats.length" class="project-hint">{{ search.trim() ? '没有匹配的会话' : '暂无会话，点击 + 开始' }}</p>
          <p v-if="group.count === 200" class="project-hint">当前显示最近 200 条会话</p>
        </div>
      </div>
      <p v-if="!groups.length" class="project-hint">{{ search.trim() ? '没有找到匹配的项目或会话' : '请先创建工作区' }}</p>
    </div>
  </section>
  <ConfirmDeleteDialog v-if="deletion" :name="deletion.name" :kind="deletion.kind" :busy="deleting" :error="deleteError" @cancel="deletion = null" @confirm="confirmDelete" />
</template>

<style scoped>
.projects { display: flex; flex: 1; flex-direction: column; min-height: 100px; padding: 8px; overflow: hidden; }
.projects-heading { display: flex; justify-content: space-between; padding: 0 8px 8px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
.projects-heading span, .project-count { font-size: 11px; opacity: .7; }
.project-search { width: 100%; box-sizing: border-box; background: var(--panel-secondary); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; font-size: 12px; margin-bottom: 8px; }
.project-groups { flex: 1; min-height: 0; overflow-y: auto; }
.project-row { display: flex; align-items: center; border-radius: 6px; }
.project-fold, .delete-item { border: 0; background: transparent; color: var(--text-secondary); cursor: pointer; padding: 4px; flex-shrink: 0; }
.delete-item:hover { color: #b44239; }
.chat-entry { display: flex; align-items: center; }
.chat-entry .chat-row { flex: 1; min-width: 0; }
.project-row.active { background: var(--bg-selected); }
.project-toggle { display: flex; align-items: center; gap: 5px; flex: 1; min-width: 0; padding: 9px 4px; }
.project-toggle, .project-new, .chat-row { background: transparent; border: 0; color: var(--text); cursor: pointer; text-align: left; }
.project-toggle svg { flex-shrink: 0; }
.project-chevron { color: var(--text-secondary); font-size: 11px; }
.project-name, .chat-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.project-name { font-size: 12px; flex: 1; }
.project-new { width: 26px; height: 28px; font-size: 20px; text-align: center; flex-shrink: 0; border-radius: 4px; }
.project-new:disabled { cursor: wait; opacity: .4; }
.project-chats { padding: 2px 0 5px 22px; }
.chat-row { width: 100%; display: flex; flex-direction: column; gap: 3px; padding: 7px 8px; border-radius: 5px; }
.chat-name { max-width: 100%; font-size: 12px; }
.chat-time { font-size: 10px; color: var(--text-secondary); }
.project-new:hover, .chat-row:hover { background: var(--panel-secondary); }
.chat-row.selected { background: var(--bg-selected); box-shadow: inset 2px 0 var(--accent); }
.project-hint { font-size: 11px; line-height: 1.6; color: var(--text-secondary); padding: 2px 6px; margin: 2px 0; }
.project-error { color: #b44239; font-size: 11px; padding: 4px; overflow-wrap: anywhere; }
.retry { cursor: pointer; font-size: 11px; }
</style>
