<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

function goLogin() {
  router.push('/login')
}

function doLogout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <div class="account">
    <div class="avatar" aria-hidden="true">{{ auth.user ? auth.user.name.charAt(0) : '访' }}</div>
    <div class="account-info">
      <div class="account-name">{{ auth.user ? auth.user.name : '未登录' }}</div>
      <div class="account-role">{{ auth.user ? '已登录' : '本地用户' }}</div>
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
