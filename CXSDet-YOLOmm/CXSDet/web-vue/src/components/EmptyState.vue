<!--
  空态：说明发生了什么 + 下一步能做什么。
  不写"暂无数据"了事，空页面是给使用者的一个入口。
-->
<template>
  <div class="empty-state" role="status">
    <div class="mark" aria-hidden="true">
      <slot name="icon">
        <el-icon><DocumentRemove /></el-icon>
      </slot>
    </div>
    <p class="title">{{ title }}</p>
    <p v-if="description" class="desc">{{ description }}</p>
    <div v-if="$slots.action" class="action">
      <slot name="action" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { DocumentRemove } from '@element-plus/icons-vue'

defineProps<{
  title: string
  description?: string
}>()
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  text-align: center;
}

.mark {
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--hx-line-strong);
  border-radius: 2px;
  color: var(--hx-text-3);
  font-size: 20px;
  margin-bottom: 14px;
  background-image: linear-gradient(to right, rgba(15, 107, 110, 0.06) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(15, 107, 110, 0.06) 1px, transparent 1px);
  background-size: 11px 11px;
}

.title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--hx-text);
}

.desc {
  margin: 6px 0 0;
  max-width: 420px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--hx-text-3);
}

.action {
  margin-top: 16px;
}
</style>
