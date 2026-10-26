<script setup lang="ts">
import { onMounted, watch } from 'vue'
import AppLayout from '@/components/AppLayout.vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useInspectionStore } from '@/stores/inspection'
import { useGuideStore } from '@/stores/guide'
import { useAuthStore } from '@/stores/auth'

const workspace = useWorkspaceStore()
const inspection = useInspectionStore()
const guide = useGuideStore()
const auth = useAuthStore()

onMounted(async () => {
  // 先用已保存的 Token 校验身份，再决定能加载什么数据
  await auth.bootstrap()
  try {
    await Promise.all([workspace.load(), inspection.load()])
  } catch (e) {
    // 未登录时接口会 401：保留空状态，由登录浮层引导，不伪造数据
    console.warn('[海巡智检] 首屏数据加载失败：', e)
  }
  // 首次进入：未登录且未选择跳过时，先弹登录浮层
  if (auth.shouldShowOnEntry()) {
    requestAnimationFrame(() => auth.openModal())
    return
  }
  // 已登录 / 已跳过登录后，进入引导逻辑
  if (guide.initFromUrl()) return
  if (guide.shouldAutoShow) {
    requestAnimationFrame(() => guide.open(true))
  }
})

// 登录浮层关闭后（登录成功或跳过），再进入首次引导
watch(
  () => auth.modalOpen,
  (open) => {
    if (!open && guide.shouldAutoShow) {
      requestAnimationFrame(() => guide.open(true))
    }
  },
)

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
