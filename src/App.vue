<script setup lang="ts">
import { onMounted, watch } from 'vue'
import AppLayout from '@/components/AppLayout.vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useInspectionStore } from '@/stores/inspection'
import { useGuideStore } from '@/stores/guide'

const workspace = useWorkspaceStore()
const inspection = useInspectionStore()
const guide = useGuideStore()

onMounted(async () => {
  await Promise.all([workspace.load(), inspection.load()])
  // URL 指定步骤（演示/测试）
  if (guide.initFromUrl()) return
  // 首次进入且未看过引导时，自动弹出
  if (guide.shouldAutoShow) {
    requestAnimationFrame(() => {
      guide.open(true)
    })
  }
})

// 切换工作区后，缺陷数据归属变化（mock 阶段返回同一批演示数据）
watch(
  () => workspace.currentId,
  () => {
    inspection.loaded = false
    inspection.load()
  },
)
</script>

<template>
  <AppLayout />
</template>
