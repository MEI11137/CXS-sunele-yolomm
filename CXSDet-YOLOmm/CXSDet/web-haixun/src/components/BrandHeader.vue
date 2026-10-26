<script setup lang="ts">
import WorkspaceSwitcher from './WorkspaceSwitcher.vue'

defineProps<{ workspaceName: string; inline?: boolean }>()
</script>

<template>
  <div class="brand" :class="{ inline }">
    <div class="brand-row">
      <div class="logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3v4" />
          <path d="M12 7l-5 9h10l-5-9z" />
          <path d="M12 16v4" />
          <path d="M4 21c1.5-2 3-3 5-3" opacity="0.6" />
          <path d="M20 21c-1.5-2-3-3-5-3" opacity="0.6" />
        </svg>
      </div>
      <span class="brand-name">海巡智检</span>
    </div>
    <WorkspaceSwitcher v-if="!inline" :workspace-name="workspaceName" />
  </div>
</template>

<style scoped>
.brand {
  padding: 16px;
  border-bottom: 1px solid var(--border);
}
.brand.inline {
  border: none;
  padding: 0;
  flex: 1;
}
.brand-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.brand.inline .brand-row {
  margin-bottom: 0;
}
.logo {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  background: var(--btn-primary);
  color: var(--btn-primary-text);
  display: flex;
  align-items: center;
  justify-content: center;
}
.brand-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
</style>
