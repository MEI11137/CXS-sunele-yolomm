<script setup lang="ts">
import { ref, computed } from 'vue'
import { useInspectionStore } from '@/stores/inspection'

const inspection = useInspectionStore()

const tab = ref<'images' | 'feedback' | 'drafts'>('images')

const reviewedDefects = computed(() => inspection.defects.filter((d) => d.reviewStatus === 'done'))
const pendingDefects = computed(() => inspection.defects.filter((d) => d.reviewStatus !== 'done'))

// 草稿状态（演示）
const drafts = ref([
  { id: 'dr1', name: '前缘侵蚀样本集', count: 12, source: '已复核结果', created_at: '2026-10-20' },
])
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">数据集</h1>
      <span class="tag">交互原型 · 模拟数据</span>
    </div>

    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'images' }" @click="tab = 'images'">影像</button>
      <button class="tab" :class="{ active: tab === 'feedback' }" @click="tab = 'feedback'">反馈池</button>
      <button class="tab" :class="{ active: tab === 'drafts' }" @click="tab = 'drafts'">候选草稿</button>
    </div>

    <!-- 影像 -->
    <div v-if="tab === 'images'" class="panel">
      <div class="panel-head">
        <span>当前项目影像</span>
        <span class="num">{{ reviewedDefects.length + pendingDefects.length }} 张</span>
      </div>
      <div class="empty">
        <div class="empty-title">影像列表</div>
        <div class="empty-desc">影像文件由文件上传服务（第三人）提供，此处展示已归入项目的影像清单。</div>
      </div>
    </div>

    <!-- 反馈池 -->
    <div v-else-if="tab === 'feedback'" class="panel">
      <div class="panel-head">
        <span>反馈池</span>
        <span class="tag tag-warn">未复核结果不自动作为真值</span>
      </div>
      <div class="feedback-groups">
        <div class="feedback-group">
          <div class="group-title">已复核（可进入草稿）</div>
          <div class="group-count num">{{ reviewedDefects.length }} 项</div>
          <button class="btn btn-sm" :disabled="!reviewedDefects.length" @click="drafts.push({ id: 'dr' + Date.now(), name: '已复核样本集', count: reviewedDefects.length, source: '已复核结果', created_at: new Date().toISOString().slice(0,10) })">加入草稿</button>
        </div>
        <div class="feedback-group">
          <div class="group-title">待复核（不作为真值）</div>
          <div class="group-count num">{{ pendingDefects.length }} 项</div>
          <span class="hint">待复核结果需人工确认后才能进入草稿</span>
        </div>
      </div>
    </div>

    <!-- 候选草稿 -->
    <div v-else class="panel">
      <div class="panel-head">
        <span>候选草稿</span>
        <span class="hint">创建草稿不自动训练</span>
      </div>
      <div v-if="!drafts.length" class="empty">
        <div class="empty-title">暂无草稿</div>
        <div class="empty-desc">从反馈池的已复核结果可创建候选草稿</div>
      </div>
      <table v-else class="data-table">
        <thead>
          <tr><th>草稿名称</th><th>来源</th><th>样本数</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in drafts" :key="d.id">
            <td>{{ d.name }}</td>
            <td>{{ d.source }}</td>
            <td class="num">{{ d.count }}</td>
            <td class="num">{{ d.created_at }}</td>
            <td><button class="btn btn-sm">用于训练</button></td>
          </tr>
        </tbody>
      </table>
    </div>
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
  margin-bottom: 16px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
}
.tab {
  height: 36px;
  padding: 0 16px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 14px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tab.active {
  color: var(--text);
  border-bottom-color: var(--accent);
  font-weight: 500;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
  font-weight: 500;
}
.feedback-groups {
  display: flex;
  gap: 16px;
  padding: 16px;
}
.feedback-group {
  flex: 1;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.group-title {
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 8px;
}
.group-count {
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 12px;
}
.hint {
  font-size: 12px;
  color: var(--text-secondary);
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th,
.data-table td {
  text-align: left;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
</style>
