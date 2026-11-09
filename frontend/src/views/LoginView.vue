<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { resetUserScopedStores } from '@/stores/sessionReset'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const localError = ref('')

const canSubmit = computed(
  () => auth.initialized && username.value.trim().length > 0 && password.value.length > 0 && !auth.busy,
)

onMounted(() => {
  if (!auth.meta) void auth.loadMeta()
})

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  localError.value = ''
  try {
    await auth.login(username.value.trim(), password.value)
    // 换账号登录：旧账号的助手轮询/业务缓存/选择上下文都不能带进新会话。
    resetUserScopedStores()
    const next = typeof route.query.next === 'string' ? route.query.next : '/projects'
    await router.replace(next)
  } catch {
    localError.value = auth.error
    password.value = ''
  }
}
</script>

<template>
  <div class="login-page">
    <section class="login-card">
      <header class="login-head">
        <span class="brand-mark" aria-hidden="true">✳</span>
        <div>
          <h1>海巡智检</h1>
          <p class="subtitle">v{{ auth.meta?.version ?? '1.8.0' }}</p>
        </div>
      </header>

      <div v-if="!auth.meta" class="banner banner-warn" role="status">正在读取后端状态…</div>

      <div v-else-if="!auth.initialized" class="banner banner-warn" role="status">
        <strong>本机还没有任何账号。</strong>
        <p class="body-text">
          为安全起见这里不提供默认管理员，也不能在线注册。请在这台机器的工程目录执行：
        </p>
        <pre class="code-block">python -m backend.cli create-user --username 你的名字 --role admin
python -m backend.cli add-root "D:\你的数据目录" --label 巡检数据</pre>
        <p class="hint">密码在命令里交互输入或从 stdin 读取，不会写进命令行历史。</p>
      </div>

      <form v-else class="login-form" @submit.prevent="submit">
        <label class="field">
          <span>用户名</span>
          <input v-model="username" autocomplete="username" :disabled="auth.busy" />
        </label>
        <label class="field">
          <span>密码</span>
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            :disabled="auth.busy"
          />
        </label>
        <p v-if="localError" class="form-error" role="alert">{{ localError }}</p>
        <button class="primary wide" type="submit" :disabled="!canSubmit">
          {{ auth.busy ? '登录中…' : '登录' }}
        </button>
      </form>

      <p class="hint">
        登录状态保存在 HttpOnly Cookie 中，页面脚本读不到令牌；退出登录会在服务端吊销会话。
      </p>
    </section>
  </div>
</template>
