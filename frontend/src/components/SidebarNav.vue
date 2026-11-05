<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useAgentStore } from '@/stores/agent'
import { useConnectionStore } from '@/stores/connection'
import { useRuntimeStore } from '@/stores/runtime'
import { useWorkspaceStore } from '@/stores/workspace'

const auth = useAuthStore()
const runtime = useRuntimeStore()
const workspace = useWorkspaceStore()
const connection = useConnectionStore()
const agent = useAgentStore()
const router = useRouter()
const route = useRoute()

const expanded = reactive<Record<string, boolean>>({})
const version = computed(() => runtime.meta?.version ?? runtime.health?.version ?? '—')

function projectLink(id: string) {
  return { name: 'project', params: { projectId: id } }
}

function sessionLink(projectId: string, sessionId: string) {
  return { name: 'session', params: { projectId, sessionId } }
}

function businessLink(name: 'inspect' | 'records' | 'datasets' | 'models', projectId: string) {
  return { name, params: { projectId } }
}

async function toggle(projectId: string): Promise<void> {
  expanded[projectId] = !expanded[projectId]
  if (expanded[projectId] && !workspace.sessionsByProject[projectId]) {
    await workspace.loadSessions(projectId)
  }
}

function isCurrentProject(id: string): boolean {
  return route.params.projectId === id
}

function isCurrentSession(id: string): boolean {
  // 业务页没有 sessionId 时，仍然高亮“正在看的那个会话”，
  // 否则用户从对话切到复核页就再也找不到自己在哪一轮里。
  return (
    route.params.sessionId === id ||
    (!route.params.sessionId && workspace.currentSessionId === id)
  )
}

function isCurrentBusiness(name: string): boolean {
  return route.name === name
}

// 当前项目自动展开：进入某个项目的任何页面时，侧栏都应当显示它的会话列表。
watch(
  () => String(route.params.projectId ?? ''),
  (projectId) => {
    if (!projectId) return
    expanded[projectId] = true
    if (!workspace.sessionsByProject[projectId]) void workspace.loadSessions(projectId)
  },
  { immediate: true },
)

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
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">✳</span>
      <span class="brand-text">
        <strong>海巡智检</strong>
        <small>v{{ version }}</small>
      </span>
    </div>

    <nav class="nav-group" aria-label="主导航">
      <RouterLink class="nav-item" :to="{ name: 'projects' }" :class="{ active: route.name === 'projects' }">
        项目
      </RouterLink>
      <RouterLink class="nav-item" :to="{ name: 'trash' }" :class="{ active: route.name === 'trash' }">
        回收站
      </RouterLink>
      <RouterLink class="nav-item" :to="{ name: 'settings' }" :class="{ active: route.name === 'settings' }">
        设置
      </RouterLink>
      <RouterLink
        v-if="auth.isAdmin"
        class="nav-item"
        :to="{ name: 'admin' }"
        :class="{ active: route.name === 'admin' }"
      >
        本机管理
      </RouterLink>
    </nav>

    <div class="nav-section">
      <p class="nav-title">我的项目</p>
      <ul v-if="workspace.projects.length" class="project-list">
        <li v-for="project in workspace.projects" :key="project.id">
          <div class="project-row" :class="{ current: isCurrentProject(project.id) }">
            <button
              class="tree-toggle"
              type="button"
              :aria-expanded="expanded[project.id] ? 'true' : 'false'"
              :aria-label="`展开 ${project.name} 的会话`"
              @click="toggle(project.id)"
            >
              {{ expanded[project.id] ? '▾' : '▸' }}
            </button>
            <RouterLink class="project-item" :to="projectLink(project.id)">{{ project.name }}</RouterLink>
          </div>
          <ul v-if="expanded[project.id]" class="session-list business-links">
            <li>
              <RouterLink
                class="session-item"
                :class="{ active: isCurrentBusiness('inspect') }"
                :to="businessLink('inspect', project.id)"
                >图片巡检</RouterLink
              >
            </li>
            <li>
              <RouterLink
                class="session-item"
                :class="{ active: isCurrentBusiness('records') }"
                :to="businessLink('records', project.id)"
                >识别记录</RouterLink
              >
            </li>
            <li>
              <RouterLink
                class="session-item"
                :class="{ active: isCurrentBusiness('datasets') }"
                :to="businessLink('datasets', project.id)"
                >数据集</RouterLink
              >
            </li>
            <li>
              <RouterLink
                class="session-item"
                :class="{ active: isCurrentBusiness('models') }"
                :to="businessLink('models', project.id)"
                >模型</RouterLink
              >
            </li>
          </ul>
          <ul v-if="expanded[project.id]" class="session-list">
            <li v-for="session in workspace.sessionsByProject[project.id] ?? []" :key="session.id">
              <RouterLink
                class="session-item"
                :class="{ active: isCurrentSession(session.id) }"
                :to="sessionLink(project.id, session.id)"
                >{{ session.title }}</RouterLink
              >
            </li>
            <li v-if="!(workspace.sessionsByProject[project.id] ?? []).length" class="session-empty">
              还没有会话
            </li>
          </ul>
        </li>
      </ul>
      <p v-else class="empty-hint">还没有项目。到“项目”页新建一个，再绑定服务器目录。</p>
    </div>

    <footer class="sidebar-footer">
      <div class="user-line">
        <span class="user-name">{{ auth.user?.display_name }}</span>
        <span class="tag" :class="auth.isAdmin ? 'tag-admin' : 'tag-off'">{{
          auth.isAdmin ? '管理员' : '普通用户'
        }}</span>
      </div>
      <button class="link" type="button" @click="logout">退出登录</button>
    </footer>
  </aside>
</template>
