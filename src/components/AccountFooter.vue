<template>
  <div class="account">
    <div class="avatar" aria-hidden="true">管</div>
    <div class="account-info">
      <div class="account-name">管理员</div>
      <div class="account-role">本地用户 · 未登录</div>
    </div>
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
}
.account-name {
  font-size: 13px;
  color: var(--text);
  font-weight: 500;
}
.account-role {
  font-size: 11px;
  color: var(--text-secondary);
}
</style>
