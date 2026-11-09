<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import AppLayout from '@/layouts/AppLayout.vue'
import { useAuthStore } from '@/stores/auth'
import { useRuntimeStore } from '@/stores/runtime'
import { useUiStore } from '@/stores/ui'

const auth = useAuthStore()
const runtime = useRuntimeStore()
const route = useRoute()
const router = useRouter()
const retrying = ref(false)
// 初始化主题（读取本机偏好并写入 data-theme）。
useUiStore()

onMounted(() => {
  void runtime.load()
})

async function retry(): Promise<void> {
  retrying.value = true
  try {
    await auth.retry()
    if (auth.state === 'anonymous') {
      // 重试后确认 Cookie 已失效：必须正式去登录页，并保留当前目标；
      // 不能在受保护路由上直接以匿名状态渲染 RouterView。
      const next = route.fullPath.startsWith('/login') ? '/projects' : route.fullPath
      await router.replace({ name: 'login', query: { next } })
    } else if (auth.state === 'authenticated') {
      void runtime.load()
    }
  } finally {
    retrying.value = false
  }
}
</script>

<template>
  <div v-if="auth.state === 'unknown'" class="boot">正在检查登录状态…</div>
  <section v-else-if="auth.state === 'error'" class="boot boot-error" role="alert">
    <h1>暂时无法连接后端</h1>
    <p>{{ auth.error || '服务暂时不可用，请稍后重试。' }}</p>
    <p class="hint">登录状态没有被清除。服务恢复后点“重试”即可继续当前页面；如果后端已经重启，也可以重新登录。</p>
    <div class="form-actions">
      <button class="primary" type="button" :disabled="retrying" @click="retry">
        {{ retrying ? '正在重试…' : '重试' }}
      </button>
      <a class="ghost" href="/login">重新登录</a>
    </div>
  </section>
  <RouterView v-else-if="!auth.isAuthenticated" />
  <AppLayout v-else />
</template>