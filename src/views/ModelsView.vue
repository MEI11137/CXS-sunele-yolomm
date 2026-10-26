<script setup lang="ts">
import { computed } from 'vue'
import { useInspectionStore } from '@/stores/inspection'

const inspection = useInspectionStore()

const systemModels = computed(() => inspection.models.filter((m) => m.type === 'system'))
const personalModels = computed(() => inspection.models.filter((m) => m.type === 'personal'))
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">模型管理</h1>
      <span class="tag">交互原型 · 模拟数据</span>
    </div>

    <section class="section">
      <h2 class="section-title">系统基线模型（只读）</h2>
      <div class="model-list">
        <div v-for="m in systemModels" :key="m.id" class="panel model-card">
          <div class="model-head">
            <div class="model-name">{{ m.name }} <span class="mono">{{ m.version }}</span></div>
            <span class="tag tag-accent">系统 · 只读</span>
          </div>
          <div v-if="m.metrics" class="metrics-grid">
            <div class="metric-item"><span class="metric-label">mAP50</span><span class="metric-value num">{{ m.metrics.mAP50.toFixed(3) }}</span></div>
            <div class="metric-item"><span class="metric-label">mAP50-95</span><span class="metric-value num">{{ m.metrics.mAP5095.toFixed(3) }}</span></div>
            <div class="metric-item"><span class="metric-label">Precision</span><span class="metric-value num">{{ m.metrics.precision.toFixed(3) }}</span></div>
            <div class="metric-item"><span class="metric-label">Recall</span><span class="metric-value num">{{ m.metrics.recall.toFixed(3) }}</span></div>
          </div>
          <div v-else class="no-metrics">暂无指标</div>
        </div>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">个人模型</h2>
      <div class="model-list">
        <div v-for="m in personalModels" :key="m.id" class="panel model-card">
          <div class="model-head">
            <div class="model-name">{{ m.name }} <span class="mono">{{ m.version }}</span></div>
            <span class="tag" :class="m.status === 'training' ? 'tag-warn' : 'tag-success'">
              {{ m.status === 'training' ? '训练中' : m.status === 'available' ? '可用' : '不可用' }}
            </span>
          </div>
          <div class="model-status">
            <span v-if="m.status === 'training'" class="spinner"></span>
            <span class="hint">{{ m.status === 'training' ? '正在训练，完成后可用' : 'PT 附件需实际导入成功才能标为可用' }}</span>
          </div>
        </div>
        <div v-if="!personalModels.length" class="empty panel">
          <div class="empty-title">暂无个人模型</div>
          <div class="empty-desc">通过训练或导入 PT 文件可创建个人模型</div>
        </div>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">训练与发布</h2>
      <div class="panel notice">
        训练、对比与发布由业务服务（第三人）提供。此处展示模型状态；训练卡、对比卡和发布影响需接入真实接口后展示。
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 1000px;
  margin: 0 auto;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.model-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.model-card {
  padding: 16px;
}
.model-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.model-name {
  font-size: 15px;
  font-weight: 600;
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.metric-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.metric-label {
  font-size: 11px;
  color: var(--text-secondary);
}
.metric-value {
  font-size: 16px;
  font-weight: 600;
}
.no-metrics {
  font-size: 13px;
  color: var(--text-secondary);
}
.model-status {
  display: flex;
  align-items: center;
  gap: 8px;
}
.hint {
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
