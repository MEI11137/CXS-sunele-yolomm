<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import SidebarNav from '@/components/SidebarNav.vue'
import AssistantPanel from '@/components/AssistantPanel.vue'
import { useRuntimeStore } from '@/stores/runtime'
import { useWorkspaceStore } from '@/stores/workspace'

const runtime = useRuntimeStore()
const workspace = useWorkspaceStore()
const route = useRoute()

const projectId = computed(() => String(route.params.projectId ?? ''))
const sessionId = computed(() => String(route.params.sessionId ?? ''))

onMounted(() => {
  void workspace.loadProjects()
  if (projectId.value) void workspace.loadSessions(projectId.value)
})
</script>

<template>
  <div class="app-shell">
    <SidebarNav />
    <main class="app-main">
      <div v-if="workspace.error" class="banner banner-error" role="alert">
        {{ workspace.error }}
        <button class="link" type="button" @click="workspace.error = ''">知道了</button>
      </div>
      <div v-if="runtime.error" class="banner banner-warn" role="status">
        运行信息读取失败：{{ runtime.error }}（不影响业务操作）
      </div>
      <router-view :key="`${projectId}:${sessionId}`" />
    </main>
    <AssistantPanel />
  </div>
</template>
