<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useInspectionStore } from '@/stores/inspection'

const router = useRouter()
const inspection = useInspectionStore()

const search = ref('')
const statusFilter = ref<'all' | 'pending' | 'reviewing' | 'done'>('all')

const filtered = computed(() => {
  return inspection.defects.filter((d) => {
    const matchSearch =
      !search.value ||
      d.turbineId.toLowerCase().includes(search.value.toLowerCase()) ||
      d.bladeFile.toLowerCase().includes(search.value.toLowerCase()) ||
      d.suspectedType.includes(search.value)
    const matchStatus = statusFilter.value === 'all' || d.reviewStatus === statusFilter.value
    return matchSearch && matchStatus
  })
})

function statusLabel(s: string) {
  return s === 'pending' ? '待复核' : s === 'reviewing' ? '复核中' : '已完成'
}

function statusClass(s: string) {
  return s === 'pending' ? 'tag-warn' : s === 'reviewing' ? 'tag-accent' : 'tag-success'
}

// CSV 导出：转义特殊字符 + 防止公式注入
function csvEscape(v: string): string {
  // 防止公式注入：以 = + - @ 开头的单元格加单引号前缀
  if (/^[=+\-@\t\r]/.test(v)) {
    v = "'" + v
  }
  if (/[",\n]/.test(v)) {
    v = '"' + v.replace(/"/g, '""') + '"'
  }
  return v
}

function exportCsv() {
  const header = ['风机编号', '叶片/文件', '疑似缺陷', '置信度', '复核状态']
  const rows = filtered.value.map((d) => [
    csvEscape(d.turbineId),
    csvEscape(d.bladeFile),
    csvEscape(d.suspectedType),
    (d.confidence * 100).toFixed(0) + '%',
    csvEscape(statusLabel(d.reviewStatus)),
  ])
  const csv = [header.map(csvEscape).join(','), ...rows.map((r) => r.join(','))].join('\r\n')
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '识别记录.csv'
  a.click()
  URL.revokeObjectURL(url)
}

function goReview(id: string) {
  router.push(`/review/${id}`)
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">识别记录</h1>
      <button class="btn" @click="exportCsv">导出 CSV</button>
    </div>

    <div class="filters panel">
      <input v-model="search" class="input search-input" type="search" placeholder="搜索风机编号 / 文件名 / 缺陷类型" />
      <div class="status-tabs" role="tablist">
        <button
          v-for="s in [['all', '全部'], ['pending', '待复核'], ['reviewing', '复核中'], ['done', '已完成']]"
          :key="s[0]"
          class="status-tab"
          :class="{ active: statusFilter === s[0] }"
          role="tab"
          :aria-selected="statusFilter === s[0]"
          @click="statusFilter = s[0] as any"
        >
          {{ s[1] }}
        </button>
      </div>
    </div>

    <div class="panel table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th>风机编号</th>
            <th>叶片 / 文件</th>
            <th>疑似缺陷</th>
            <th>置信度</th>
            <th>复核状态</th>
            <th class="action-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in filtered" :key="d.id">
            <td><span class="mono num">{{ d.turbineId }}</span></td>
            <td class="file-cell">{{ d.bladeFile }}</td>
            <td><span class="tag">{{ d.suspectedType }}</span></td>
            <td class="num">{{ (d.confidence * 100).toFixed(0) }}%</td>
            <td><span class="tag" :class="statusClass(d.reviewStatus)">{{ statusLabel(d.reviewStatus) }}</span></td>
            <td class="action-col">
              <button v-if="d.reviewStatus !== 'done'" class="btn btn-sm" @click="goReview(d.id)">复核</button>
              <span v-else class="done-hint">已完成</span>
            </td>
          </tr>
          <tr v-if="!filtered.length">
            <td colspan="6">
              <div class="empty">
                <div class="empty-title">没有匹配的记录</div>
                <div class="empty-desc">调整搜索条件或筛选状态后重试</div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 1100px;
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
.filters {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  margin-bottom: 16px;
}
.search-input {
  flex: 1;
  max-width: 360px;
}
.status-tabs {
  display: flex;
  gap: 4px;
}
.status-tab {
  height: 28px;
  padding: 0 12px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.status-tab:hover {
  background: var(--panel-secondary);
}
.status-tab.active {
  background: var(--bg-selected);
  color: var(--text);
  font-weight: 500;
}
.table-wrap {
  overflow-x: auto;
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th {
  text-align: left;
  padding: 10px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.data-table td {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.data-table tbody tr:last-child td {
  border-bottom: none;
}
.data-table tbody tr:hover {
  background: var(--panel-secondary);
}
.file-cell {
  color: var(--text-secondary);
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.action-col {
  text-align: right;
}
.done-hint {
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
