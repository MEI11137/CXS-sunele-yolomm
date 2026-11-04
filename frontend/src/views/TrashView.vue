<script setup lang="ts">
import { onMounted } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'

const workspace = useWorkspaceStore()

onMounted(() => {
  void workspace.loadTrash()
})

async function restoreProject(id: string): Promise<void> {
  await workspace.restoreProject(id)
}

async function restoreSession(projectId: string, id: string): Promise<void> {
  await workspace.restoreSession(projectId, id)
}

function projectName(projectId: string): string {
  return workspace.projectById(projectId)?.name ?? projectId
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>回收站</h1>
        <p class="subtitle">软删除的项目与会话保留在这里，可随时恢复；本轮没有永久删除入口。</p>
      </div>
      <button class="ghost" type="button" @click="workspace.loadTrash()">刷新</button>
    </header>

    <section class="card">
      <h2>已删除项目</h2>
      <ul v-if="workspace.deletedProjects.length" class="trash-list">
        <li v-for="item in workspace.deletedProjects" :key="item.id" class="trash-row">
          <div>
            <strong>{{ item.name }}</strong>
            <span class="hint">{{ item.deleted_at }}</span>
          </div>
          <button class="ghost small" type="button" @click="restoreProject(item.id)">恢复</button>
        </li>
      </ul>
      <p v-else class="empty-hint">没有已删除的项目。</p>
    </section>

    <section class="card">
      <h2>已删除会话</h2>
      <ul v-if="workspace.deletedSessions.length" class="trash-list">
        <li v-for="item in workspace.deletedSessions" :key="item.id" class="trash-row">
          <div>
            <strong>{{ item.title }}</strong>
            <span class="hint">{{ projectName(item.project_id) }} · {{ item.deleted_at }}</span>
          </div>
          <button class="ghost small" type="button" @click="restoreSession(item.project_id, item.id)">
            恢复
          </button>
        </li>
      </ul>
      <p v-else class="empty-hint">没有已删除的会话。</p>
    </section>
  </section>
</template>
