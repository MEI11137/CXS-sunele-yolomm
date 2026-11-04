<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AssistantPanel from '@/components/AssistantPanel.vue'
import { useWorkspaceStore } from '@/stores/workspace'

/**
 * 会话页：**对话就在主区**。
 *
 * 项目配置（目录、改名、删除）不属于日常操作，收进“项目详情”入口，
 * 不再占据中间位置；进入复核/数据集等业务页时，对话会改为右侧 360px 停靠。
 */
const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const sessionId = computed(() => String(route.params.sessionId ?? ''))
const project = computed(() => (projectId.value ? workspace.projectById(projectId.value) : null))
const session = computed(
  () =>
    (workspace.sessionsByProject[projectId.value] ?? []).find(
      (item) => item.id === sessionId.value,
    ) ?? null,
)

function openDetails(): void {
  void router.push({
    name: 'project',
    params: { projectId: projectId.value },
    query: { details: '1' },
  })
}
</script>

<template>
  <section class="page chat-page">
    <header class="chat-head">
      <div class="chat-head-text">
        <h1>{{ session?.title || '会话' }}</h1>
        <p class="subtitle">
          {{ project?.name || '项目' }}
          <span v-if="project?.root_path" class="mono crumb">· {{ project.root_path }}</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" @click="openDetails">项目详情</button>
      </div>
    </header>

    <AssistantPanel variant="center" />
  </section>
</template>
