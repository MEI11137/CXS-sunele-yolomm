<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { getAdapter } from '@/services'
import type { DetectionRecordSummary } from '@/types'
import { reviewStatus } from '@/utils/reviewStatus'
import { useResourceRefresh } from '@/utils/resourceLifecycle'
import { request } from '@/services/http'
import { feedbackEligibleRecords } from '@/utils/feedbackSelection'

const router = useRouter()
const route = useRoute()
const records = ref<DetectionRecordSummary[]>([])
const page = ref(1)
const total = ref(0)
const busy = ref(false)
const error = ref('')
const selected = ref<string[]>([])
const reviewingIds = ref<string[]>([])
const reviewResults = ref<Array<{ record_id: string; status: string; error?: string }>>([])
const reviewNotice = ref('')
const feedbackNotice = ref('')
const feedbackResults = ref<Array<{ record_id: string; status: string; error?: string }>>([])
const eligibleFeedback = computed(() => feedbackEligibleRecords(records.value, selected.value))
async function load() {
  busy.value = true
  try { const result = await getAdapter().listRecords({ page: page.value, pageSize: 50 }); records.value = result.items; total.value = result.total; error.value = '' }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
onMounted(load)
useResourceRefresh(load)
const defects = computed(() => records.value.map(r => ({
  id: r.id, turbineId: r.model_name || r.model_id, bladeFile: r.title || r.id,
  suspectedType: [...new Set(r.detections.map(x => x.class_name))].join('、') || '未检出目标',
  confidence: Math.max(0, ...r.detections.map(x => x.confidence)),
  reviewStatus: reviewStatus(r.human_review_status), raw: r,
})))
async function addFeedback(ids: string[]) {
  if (busy.value || reviewingIds.value.length) return
  const unique = [...new Set(ids)]
  const targets = feedbackEligibleRecords(records.value, unique)
  if (!targets.length) { feedbackNotice.value = '所选记录均已加入反馈池，或尚未人工确认，没有新增样本。'; return }
  const skipped = unique.length - targets.length
  if (!window.confirm(`确认将所选 ${targets.length} 条人工确认记录加入反馈池？${skipped ? `\n跳过 ${skipped} 条未确认或已入池的记录。` : ''}\n${targets.map(row => `${row.title || row.id} [${row.id}]`).join('\n')}\n按每条记录检测模型关联的原数据集分别入池。不修改原数据集文件，不自动生成新版本或训练。`)) return
  busy.value = true; error.value = ''; feedbackNotice.value = ''; feedbackResults.value = []
  try {
    const result = await getAdapter().addFeedbackBatch(targets.map(row => row.id))
    feedbackResults.value = (result.items || []) as typeof feedbackResults.value
    const groups = (result.dataset_groups || []) as Array<{ dataset_name: string; record_ids: string[] }>
    const destinations = groups.map(group => `${group.dataset_name}：${group.record_ids.length} 条`).join('；')
    feedbackNotice.value = `本次加入反馈池：成功 ${Number(result.succeeded || 0)} 条，失败 ${Number(result.failed || 0)} 条${skipped ? `，跳过 ${skipped} 条` : ''}。${destinations ? destinations + '。' : ''}尚未生成数据集版本。`
    const completed = new Set(feedbackResults.value.filter(item => item.status === 'success').map(item => item.record_id))
    selected.value = selected.value.filter(id => !completed.has(id))
    await load()
  } catch (e) { error.value = (e as Error).message; feedbackNotice.value = '加入反馈池未全部完成，请核对列表状态；不会自动重复提交。' }
  finally { busy.value = false }
}

const search = ref('')
const statusFilter = ref<'all' | 'pending' | 'reviewing' | 'done' | 'rejected'>('all')
watch(() => route.query.status, value => {
  if (['all', 'pending', 'done', 'rejected'].includes(String(value))) statusFilter.value = String(value) as typeof statusFilter.value
}, { immediate: true })

const filtered = computed(() => {
  return defects.value.filter((d) => {
    const matchSearch =
      !search.value ||
      d.turbineId.toLowerCase().includes(search.value.toLowerCase()) ||
      d.bladeFile.toLowerCase().includes(search.value.toLowerCase()) ||
      d.suspectedType.includes(search.value)
    const matchStatus = statusFilter.value === 'all' || d.reviewStatus === statusFilter.value
    return matchSearch && matchStatus
  })
})
watch(() => filtered.value.map(row => row.id), ids => { selected.value = selected.value.filter(id => ids.includes(id)) })
const allSelected = computed(() => !!filtered.value.length && filtered.value.every(row => selected.value.includes(row.id)))
function selectAll(event: Event) { selected.value = (event.target as HTMLInputElement).checked ? filtered.value.map(row => row.id) : [] }
async function review(ids: string[], batch = false) {
  if (reviewingIds.value.length) return
  const targets = [...new Set(ids)]
  if (!targets.length || targets.length > 50) return
  if (batch && !window.confirm(`仅核查已勾选的 ${targets.length} 条记录，将调用本人模型 API。不会核查未选记录，也不会自动提交人工结论。确认继续？`)) return
  reviewingIds.value = targets; reviewResults.value = []; error.value = ''
  reviewNotice.value = `正在核查 ${targets.length} 条${batch ? '已选记录' : '指定记录'}，未选记录不会处理。`
  try {
    if (batch) {
      const result = await getAdapter().requestAiReviews(targets)
      reviewResults.value = (result.items || []) as typeof reviewResults.value
      reviewNotice.value = `本次仅处理已选 ${targets.length} 条：成功 ${Number(result.succeeded || 0)}，失败 ${Number(result.failed || 0)}。失败项不会自动重试。`
    } else {
      await getAdapter().requestAiReview(targets[0])
      reviewResults.value = [{ record_id: targets[0], status: 'success' }]
      reviewNotice.value = `单条核查完成，仅更新记录 ${targets[0]}，其他记录未核查。`
    }
    await load()
  } catch (e) {
    error.value = (e as Error).message
    reviewNotice.value = '核查未全部完成，不会自动重发请求。'
  } finally { reviewingIds.value = [] }
}
function aiLabel(value: string) {
  return ({ not_requested: '未核查', consistent: '结果一致', possible_false_positive: '疑似误报', possible_missed_detection: '疑似漏报', needs_human_review: '需人工复核', failed: '核查失败', running: '核查中' } as Record<string, string>)[value] || value
}
async function deleteRecords(ids: string[]) {
  if (busy.value || reviewingIds.value.length) return
  const targets = [...new Set(ids)].map(id => records.value.find(row => row.id === id)).filter((row): row is DetectionRecordSummary => !!row)
  if (!targets.length || targets.length > 50) return
  if (!window.confirm(`确认删除所选 ${targets.length} 条记录？\n${targets.map(row => (row.title || row.id) + ' [' + row.id + ']').join('\n')}\n只隐藏这些记录，保留图片、数据集、模型和回流来源，可由助手恢复。`)) return
  busy.value = true; error.value = ''
  try {
    const result = await request<{ count: number }>('/api/detection/records/delete/batch', { method: 'POST', body: {
      records: targets.map(row => ({ record_id: row.id, expected_version: row.review_version ?? 0 })) } })
    reviewNotice.value = `已删除所选 ${result.count} 条记录，文件和历史来源保留。`
    await load()
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}

function statusLabel(s: string) {
  return s === 'rejected' ? '已驳回' : s === 'pending' ? '待复核' : s === 'reviewing' ? '复核中' : '已完成'
}

function statusClass(s: string) {
  return s === 'pending' || s === 'rejected' ? 'tag-warn' : s === 'reviewing' ? 'tag-accent' : 'tag-success'
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
  const header = ['模型', '记录', '疑似缺陷', '最高置信度', '复核状态']
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
      <button class="btn" :disabled="busy" @click="load">刷新</button>
      <button class="btn" @click="exportCsv">导出本页 CSV</button>
    </div>

    <div class="filters panel">
      <input v-model="search" class="input search-input" type="search" placeholder="在本页搜索模型 / 记录 / 缺陷类型" />
      <div class="status-tabs" role="tablist">
        <button
          v-for="s in [['all', '全部'], ['pending', '待复核'], ['reviewing', '复核中'], ['done', '已完成'], ['rejected', '已驳回']]"
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
      <div class="batch-toolbar">
        <span>已选 {{ selected.length }} 条（仅当前页筛选结果）</span>
        <button class="btn btn-sm" :disabled="!selected.length || !!reviewingIds.length || busy" @click="review(selected, true)">批量 AI 核查所选</button>
        <button class="btn btn-sm" :disabled="!eligibleFeedback.length || !!reviewingIds.length || busy" title="仅处理已勾选、已人工确认且尚未加入反馈池的记录" @click="addFeedback(selected)">批量加入反馈所选（{{ eligibleFeedback.length }}）</button>
        <button class="btn btn-sm" :disabled="!selected.length || !!reviewingIds.length || busy" @click="deleteRecords(selected)">批量删除所选</button>
        <button class="btn btn-sm" :disabled="!selected.length || !!reviewingIds.length" @click="selected = []">清空选择</button>
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="reviewNotice" role="status">{{ reviewNotice }}</p>
      <p class="feedback-hint">加入反馈池仅收集人工确认样本；在「数据集」生成反馈草稿、划分并定稿后，才得到新数据集，不会直接覆盖原数据集文件。</p>
      <p v-if="feedbackNotice" role="status">{{ feedbackNotice }}</p>
      <ul v-if="feedbackResults.some(item => item.status !== 'success')"><li v-for="item in feedbackResults.filter(item => item.status !== 'success')" :key="item.record_id">{{ item.record_id }}：{{ item.error || '加入反馈池失败' }}</li></ul>
      <ul v-if="reviewResults.some(item => item.status !== 'success')"><li v-for="item in reviewResults.filter(item => item.status !== 'success')" :key="item.record_id">{{ item.record_id }}：{{ item.error || '核查失败' }}</li></ul>
      <table class="data-table">
        <thead>
          <tr>
            <th><input type="checkbox" aria-label="全选当前页筛选记录" :checked="allSelected" :disabled="!!reviewingIds.length || busy" @change="selectAll" /></th>
            <th>检测模型</th>
            <th>记录</th>
            <th>疑似缺陷</th>
            <th>置信度</th>
            <th>复核状态</th>
            <th>AI 核查 / 反馈</th>
            <th class="action-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in filtered" :key="d.id">
            <td><input v-model="selected" type="checkbox" :value="d.id" :aria-label="'选择记录 ' + d.id" :disabled="busy || !!reviewingIds.length" /></td>
            <td><span class="mono num">{{ d.turbineId }}</span></td>
            <td class="file-cell">{{ d.bladeFile }}</td>
            <td><span class="tag">{{ d.suspectedType }}</span></td>
            <td class="num">{{ (d.confidence * 100).toFixed(0) }}%</td>
            <td><span class="tag" :class="statusClass(d.reviewStatus)">{{ statusLabel(d.reviewStatus) }}</span></td>
            <td>{{ reviewingIds.includes(d.id) ? '正在核查此条…' : aiLabel(d.raw.ai_review_status) }} · {{ d.raw.feedback_dataset_id ? '已入反馈池' : '未入反馈池' }}</td>
            <td class="action-col">
              <button class="btn btn-sm" @click="goReview(d.id)">查看 / 复核</button>
              <button class="btn btn-sm" :disabled="busy || !!reviewingIds.length" @click="review([d.id])">{{ reviewingIds.includes(d.id) ? '正在核查…' : '仅核查此条' }}</button>
              <button v-if="d.reviewStatus === 'done'" class="btn btn-sm" :disabled="busy || !!reviewingIds.length || !!d.raw.feedback_dataset_id" @click="addFeedback([d.id])">{{ d.raw.feedback_dataset_id ? '已入反馈池' : '加入反馈' }}</button>
              <button class="btn btn-sm" :disabled="busy || !!reviewingIds.length" @click="deleteRecords([d.id])">删除此条</button>
            </td>
          </tr>
          <tr v-if="!filtered.length">
            <td colspan="8">
              <div class="empty">
                <div class="empty-title">没有匹配的记录</div>
                <div class="empty-desc">调整搜索条件或筛选状态后重试</div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <button class="btn" :disabled="busy || page <= 1" @click="page--; load()">上一页</button>
      <span>第 {{ page }} 页 / 共 {{ total }} 条</span>
      <button class="btn" :disabled="busy || page * 50 >= total" @click="page++; load()">下一页</button>
    </div>
  </div>
</template>

<style scoped>
.feedback-hint { padding: 0 14px; color: var(--text-secondary); font-size: 12px; line-height: 1.7; }
.batch-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 12px 14px; border-bottom: 1px solid var(--border); font-size: 13px; }
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
