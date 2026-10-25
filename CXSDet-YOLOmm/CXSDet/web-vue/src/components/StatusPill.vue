<!--
  状态徽章：一个圆点 + 一句人话。
  不堆彩色标签，靠点位和文案区分「机器怎么说」和「人怎么判」。
-->
<template>
  <span class="status-pill" :class="[`is-${meta.tone}`, size === 'sm' ? 'is-sm' : '']" :title="showHint ? meta.meaning : undefined">
    <span class="dot" aria-hidden="true" />
    <span class="label">{{ meta.label }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StatusMeta, Tone } from '@/constants/review'

const props = withDefaults(
  defineProps<{
    meta: StatusMeta
    size?: 'sm' | 'md'
    /** 鼠标悬停是否展示该状态的含义 */
    showHint?: boolean
  }>(),
  { size: 'md', showHint: true }
)

const tone = computed<Tone>(() => props.meta.tone)
</script>

<style scoped>
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 8px 1px 7px;
  border-radius: 2px;
  border: 1px solid transparent;
  font-size: 12px;
  line-height: 20px;
  white-space: nowrap;
  vertical-align: middle;
}

.status-pill.is-sm {
  font-size: 11px;
  line-height: 18px;
  padding: 0 6px 0 5px;
  gap: 5px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
  background: currentColor;
}

.label {
  font-weight: 500;
}

/* 灰色：还没开始 / 不适用 */
.is-neutral {
  color: var(--hx-text-3);
  background: #f2f5f4;
  border-color: #e2e8e7;
}

/* 青色：进行中的辅助结论，不代表已结束 */
.is-progress {
  color: var(--hx-sea);
  background: var(--hx-sea-soft);
  border-color: var(--hx-sea-line);
}

/* 航标橙：需要人来做判断 */
.is-attention {
  color: var(--hx-beacon);
  background: var(--hx-beacon-soft);
  border-color: #ecd6ac;
}

.is-ok {
  color: var(--hx-ok);
  background: var(--hx-ok-soft);
  border-color: #c9ddd1;
}

.is-danger {
  color: var(--hx-defect);
  background: var(--hx-defect-soft);
  border-color: #e8c9c4;
}
</style>
