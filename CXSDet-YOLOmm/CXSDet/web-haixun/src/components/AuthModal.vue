<script setup lang="ts">
import { ref, computed } from 'vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const mode = ref<'login' | 'register'>('login')
const username = ref('')
const password = ref('')
const confirmPassword = ref('')
const notice = ref('')

const isRegister = computed(() => mode.value === 'register')
const canSubmit = computed(() => {
  if (!username.value.trim() || !password.value) return false
  if (isRegister.value && password.value !== confirmPassword.value) return false
  return true
})

// 模拟登录/注册：仅前端占位，真实账号体系由后端实现
function submit() {
  if (!canSubmit.value) return
  if (isRegister.value && password.value !== confirmPassword.value) {
    notice.value = '两次输入的密码不一致'
    return
  }
  auth.login(username.value)
}
</script>

<template>
  <div v-if="auth.modalOpen" class="auth-overlay" role="dialog" aria-modal="true" aria-label="登录">
    <div class="auth-dialog">
      <button class="close" aria-label="关闭" title="暂不登录，使用本地模式" @click="auth.dismiss">×</button>

      <div class="brand">
        <svg class="brand-mark" viewBox="0 0 24 24" aria-hidden="true">
          <line x1="12" y1="12" x2="12" y2="22" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          <line x1="12" y1="12" x2="12" y2="3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          <line x1="12" y1="12" x2="4.2" y2="16.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          <line x1="12" y1="12" x2="19.8" y2="16.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          <circle cx="12" cy="12" r="1.6" fill="currentColor" />
        </svg>
        <span class="brand-name">海巡智检</span>
      </div>

      <h2 class="title">{{ isRegister ? '创建账号' : '登录' }}</h2>
      <p class="sub">登录后可云端查看；不登录则使用本地模式</p>

      <div class="mode-tabs" role="tablist">
        <button class="mode-tab" :class="{ active: !isRegister }" role="tab" @click="mode = 'login'">登录</button>
        <button class="mode-tab" :class="{ active: isRegister }" role="tab" @click="mode = 'register'">注册</button>
      </div>

      <form class="form" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">用户名</span>
          <input v-model="username" class="input" type="text" autocomplete="username" placeholder="请输入用户名" />
        </label>
        <label class="field">
          <span class="field-label">密码</span>
          <input v-model="password" class="input" type="password" :autocomplete="isRegister ? 'new-password' : 'current-password'" placeholder="请输入密码" />
        </label>
        <label v-if="isRegister" class="field">
          <span class="field-label">确认密码</span>
          <input v-model="confirmPassword" class="input" type="password" autocomplete="new-password" placeholder="请再次输入密码" />
        </label>

        <p v-if="notice" class="notice">{{ notice }}</p>

        <button type="submit" class="submit" :disabled="!canSubmit">{{ isRegister ? '注册' : '登录' }}</button>
      </form>

      <button class="skip" @click="auth.dismiss">暂不登录，使用本地模式</button>

      <p class="mock-hint">交互原型 · 模拟数据 — 账号体系待后端接入</p>
    </div>
  </div>
</template>

<style scoped>
.auth-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(17, 23, 29, 0.42);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.auth-dialog {
  position: relative;
  width: 100%;
  max-width: 380px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 28px 26px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.16);
}
.close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  border-radius: 6px;
}
.close:hover {
  background: var(--bg-selected);
  color: var(--text);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-mark {
  width: 20px;
  height: 20px;
  color: var(--accent);
}
.brand-name {
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}
.title {
  font-size: 20px;
  font-weight: 600;
  color: var(--text);
  margin: 16px 0 4px;
}
.sub {
  font-size: 12px;
  color: var(--text-secondary);
  margin: 0 0 16px;
}
.mode-tabs {
  display: flex;
  background: var(--panel-secondary);
  border-radius: 6px;
  padding: 3px;
  margin-bottom: 18px;
}
.mode-tab {
  flex: 1;
  padding: 7px 0;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
  border-radius: 5px;
}
.mode-tab.active {
  background: var(--panel);
  color: var(--text);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.input {
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border-control);
  border-radius: 6px;
  background: var(--panel);
  color: var(--text);
  font-size: 14px;
}
.input:focus {
  outline: none;
  border-color: var(--accent);
}
.notice {
  font-size: 12px;
  color: var(--warn-text);
  margin: 0;
}
.submit {
  height: 38px;
  border: none;
  border-radius: 6px;
  background: var(--btn-primary);
  color: var(--btn-primary-text);
  font-size: 14px;
  cursor: pointer;
}
.submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.skip {
  width: 100%;
  margin-top: 12px;
  height: 36px;
  border: 1px solid var(--border-control);
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
  border-radius: 6px;
}
.skip:hover {
  color: var(--text);
  border-color: var(--text-secondary);
}
.mock-hint {
  margin: 16px 0 0;
  font-size: 11px;
  color: var(--text-secondary);
  text-align: center;
}
</style>
