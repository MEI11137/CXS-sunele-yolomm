<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterView } from 'vue-router'
import AppLayout from '@/layouts/AppLayout.vue'
import { useAuthStore } from '@/stores/auth'
import { useRuntimeStore } from '@/stores/runtime'
import { useUiStore } from '@/stores/ui'

const auth = useAuthStore()
const runtime = useRuntimeStore()
// 初始化主题（读取本机偏好并写入 data-theme）。
useUiStore()

onMounted(() => {
  void runtime.load()
})
</script>

<template>
  <div v-if="auth.state === 'unknown'" class="boot">正在检查登录状态…</div>
  <RouterView v-else-if="!auth.isAuthenticated" />
  <AppLayout v-else />
</template>
