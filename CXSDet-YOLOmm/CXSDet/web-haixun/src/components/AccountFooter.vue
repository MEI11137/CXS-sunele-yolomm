<script setup lang="ts">
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { CURRENT_MODE } from '@/services'

const auth = useAuthStore()

const displayName = computed(() => {
  if (!auth.user) return '未登录'
  return auth.user.realName || auth.user.username
})

const initial = computed(() => {
  const name = displayName.value
  return name ? name.charAt(0) : '访'
})

// 不承诺任何"云端同步"；本机部署就是本机部署
const roleLabel = computed(() => {
  if (CURRENT_MODE === 'mock') return auth.isLoggedIn ? '演示模式 · 已登录' : '演示模式 · 只读'
  return auth.isLoggedIn ? '本机账号' : '只读浏览'
})

function goLogin() {
  auth.openModal()
}

function doLogout() {
  void auth.logout()
}
</script>

<template>
  <div class="account">
    <div class="avatar" aria-hidden="true">{{ initial }}</div>
    <div class="account-info">
      <div class="account-name">{{ displayName }}</div>
      <div class="account-role">{{ roleLabel }}</div>
    </div>
    <button v-if="!auth.isLoggedIn" class="account-action" @click="goLogin">登录</button>
    <button v-else class="account-action" @click="doLogout">退出</button>
  </div>
</template>

<style scoped>
.account {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}
.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--bg-selected);
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  flex-shrink: 0;
}
.account-info {
  flex: 1;
  min-width: 0;
}
.account-name {
  font-size: 13px;
  color: var(--text);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.account-role {
  font-size: 11px;
  color: var(--text-secondary);
}
.account-action {
  border: 1px solid var(--border-control);
  background: var(--panel-secondary);
  color: var(--text);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 5px;
  cursor: pointer;
  flex-shrink: 0;
}
.account-action:hover {
  border-color: var(--accent);
}
</style>
