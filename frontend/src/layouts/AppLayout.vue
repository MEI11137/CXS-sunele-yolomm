<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import SidebarNav from '@/components/SidebarNav.vue'
import AssistantPanel from '@/components/AssistantPanel.vue'
import { useRuntimeStore } from '@/stores/runtime'
import { useWorkspaceStore } from '@/stores/workspace'

const runtime = useRuntimeStore()
const workspace = useWorkspaceStore()
const route = useRoute()

const projectId = computed(() => String(route.params.projectId ?? ''))
/**
 * 只有“业务页”才把对话停靠到右侧 360px（人工复核、数据集等，`meta.dock`）。
 * 会话页的对话在主区，由 SessionView 自己渲染，这里不再重复渲染一份。
 */
const docked = computed(() => route.meta.dock === true)

onMounted(() => {
  void workspace.loadProjects()
  if (projectId.value) void workspace.loadSessions(projectId.value)
})

watch(projectId, (value) => {
  if (value) void workspace.loadSessions(value)
})
</script>

<template>
  <div class="app-shell" :class="{ 'with-dock': docked }">
    <SidebarNav />
    <main class="app-main" :class="{ 'app-main-chat': route.name === 'session' }">
      <div v-if="workspace.error" class="banner banner-error" role="alert">
        {{ workspace.error }}
        <button class="link" type="button" @click="workspace.error = ''">知道了</button>
      </div>
      <div v-if="runtime.error" class="banner banner-warn" role="status">
        运行信息读取失败：{{ runtime.error }}（不影响业务操作）
      </div>
      <router-view :key="`${projectId}:${String(route.params.sessionId ?? '')}`" />
    </main>
    <AssistantPanel v-if="docked" variant="dock" />
  </div>
</template>
