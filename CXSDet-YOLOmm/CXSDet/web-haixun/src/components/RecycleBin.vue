<script setup lang="ts">
import { ref } from 'vue'
import { getAdapter } from '@/services'
import type { RecycleContents } from '@/services/types'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSessionStore } from '@/stores/session'
const workspace = useWorkspaceStore(), session = useSessionStore()
const show = ref(false), busy = ref(false), error = ref('')
const contents = ref<RecycleContents>({ projects: [], sessions: [] })
async function refresh() { contents.value = await getAdapter().listRecycle() }
async function open() { show.value = true; error.value = ''; try { await refresh() } catch (e) { error.value = (e as Error).message } }
async function restore(kind: 'project' | 'session', id: string, projectId: string) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    if (kind === 'project') { await getAdapter().restoreProject(id); workspace.loaded = false; await workspace.load() }
    else await getAdapter().restoreSession(id)
    await session.revive(projectId, kind === 'session' ? id : undefined)
    await refresh()
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
</script>
<template>
  <button class="recycle-trigger" @click="open">回收站</button>
  <Teleport to="body"><div v-if="show" class="overlay" @click.self="!busy && (show = false)"><section class="dialog recycle-dialog" role="dialog" aria-modal="true" aria-label="项目与会话回收站">
    <header class="dialog-header"><h3 class="dialog-title">回收站</h3><button class="btn btn-ghost" aria-label="关闭回收站" :disabled="busy" @click="show = false">✕</button></header>
    <div class="dialog-body"><p>删除后保留的项目和会话可以在这里恢复，本机文件不会被删除。</p><p v-if="error" class="error" role="alert">{{ error }}</p>
      <div v-for="project in contents.projects" :key="project.id" class="deleted-row"><div><strong>{{ project.name }}</strong><small>项目 · {{ project.updated_at }}</small></div><button class="btn" :disabled="busy" @click="restore('project',project.id,project.id)">恢复项目</button></div>
      <div v-for="chat in contents.sessions" :key="chat.id" class="deleted-row"><div><strong>{{ chat.title }}</strong><small>{{ chat.project_name }} · {{ chat.deleted_at }}</small></div><button class="btn" :disabled="busy" @click="restore('session',chat.id,chat.project_id)">恢复会话</button></div>
      <p v-if="!contents.projects.length && !contents.sessions.length && !error">回收站为空</p>
    </div>
  </section></div></Teleport>
</template>
<style scoped>
.recycle-trigger { background: transparent; border: 0; padding: 10px 18px; color: var(--text-secondary); cursor: pointer; text-align: left; font-size: 13px; }
.recycle-dialog { width: min(600px,calc(100vw - 32px)); max-height: 80vh; overflow-y: auto; }
.dialog-body p { font-size: 12px; color: var(--text-secondary); line-height: 1.8; }
.deleted-row { display: flex; align-items: center; gap: 12px; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid var(--border); }
.deleted-row strong, .deleted-row small { display: block; overflow-wrap: anywhere; }
.deleted-row small { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }
.dialog-body .error { color: #b44239; }
</style>
