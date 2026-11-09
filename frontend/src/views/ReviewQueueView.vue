<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import { useSelectionStore } from '@/stores/selection'
import type { DetectionRecord } from '@/types'

/** 本轮人工队列：从真实记录/复核服务读取，按 flow_id 或指定 record_ids 定位。 */
const route = useRoute()
const router = useRouter()
const selection = useSelectionStore()
const projectId = computed(() => String(route.params.projectId ?? ''))
const flowId = computed(() => String(route.query.flow ?? ''))
type Bucket = 'pending' | 'draft' | 'rejected' | 'confirmed'
const BUCKETS: Bucket[] = ['pending', 'draft', 'rejected', 'confirmed']
function normalizeView(raw: unknown): Bucket {
  const value = String(raw ?? '')
  return (BUCKETS as string[]).includes(value) ? (value as Bucket) : 'pending'
}
const view = ref<Bucket>(normalizeView(route.query.view))
const countScopeLabel = computed(() => {
  if (requestedIds.value.length) return '所选记录'
  if (flowId.value) return '当前批次'
  return '整个项目'
})
const items = ref<DetectionRecord[]>([])
const counts = ref<Record<string, number>>({})
const flowIds = ref<string[]>([])
const selected = ref<string[]>([])
const error = ref('')
const loading = ref(false)
let requestEpoch = 0

/** 列表主文本用真实图片名；没有文件名才退回记录 ID（不伪造）。 */
function recordName(record: DetectionRecord): string {
  return (record.rgb_filename || '').trim() || record.id
}

const requestedIds = computed(() =>
  String(route.query.record_ids ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean),
)
const visible = computed(() =>
  flowId.value ? items.value.filter((item) => item.flow_id === flowId.value) : items.value,
)

function label(bucket: string): string {
  return { pending: '待复核', draft: '有草稿', rejected: '已驳回', confirmed: '已确认' }[bucket] ?? bucket
}

/** 当前可见范围内真正被勾选的记录（顺序与表格一致）。 */
const selectedVisible = computed(() =>
  visible.value.filter((record) => selected.value.includes(record.id)),
)

/**
 * 勾选不是“摆设”：它自动注册成共享界面选择，下一条普通消息发送时冻结快照交给后端，
 * 助手因此能精确知道“我选中的这些”是哪些 record_id。这里不新建会话、不跳页。
 */
function syncSelection(): void {
  selection.setSelection(
    selectedVisible.value.length
      ? {
          projectId: projectId.value,
          page: '人工复核队列',
          recordIds: selectedVisible.value.map((record) => record.id),
          filters: `视图 ${label(view.value)}${flowId.value ? ` / 批次 ${flowId.value}` : ''}`,
        }
      : null,
  )
}

async function load(): Promise<void> {
  const epoch = ++requestEpoch
  const project = projectId.value
  const requestedView = view.value
  const requestedFlow = flowId.value
  const fixedIds = [...requestedIds.value]
  const stillCurrent = () =>
    epoch === requestEpoch &&
    project === projectId.value &&
    requestedView === view.value &&
    requestedFlow === flowId.value &&
    fixedIds.join(',') === requestedIds.value.join(',')
  loading.value = true
  error.value = ''
  try {
    const data = await api.listReviewQueue(project, {
      view: requestedView,
      record_ids: fixedIds.length ? fixedIds : undefined,
      flow: requestedFlow || undefined,
      limit: 100,
    })
    if (!stillCurrent()) return
    items.value = data.items
    counts.value = data.counts
    flowIds.value = data.flow_ids
    // 筛选/视图/项目切换后，历史勾选不能继续偷偷指向看不见的记录。
    selected.value = selected.value.filter((id) => data.items.some((item) => item.id === id))
    syncSelection()
  } catch (failure) {
    if (!stillCurrent()) return
    error.value = errorMessage(failure)
  } finally {
    // 旧请求收尾不能提前结束新请求的 loading。
    if (stillCurrent()) loading.value = false
  }
}

function open(recordId: string): void {
  // 单条进入也带当前视图和可见队列：画布上一项/下一项/返回队列不脱离当前筛选。
  const visibleIds = visible.value.map((record) => record.id)
  void router.push({
    name: 'review',
    params: { projectId: projectId.value, recordId },
    query: {
      view: view.value,
      ...(flowId.value ? { flow: flowId.value } : {}),
      ...(visibleIds.length ? { record_ids: visibleIds.join(',') } : {}),
    },
  })
}

/** 用选中的记录组成一个只包含它们的复核队列，逐条人工判断。 */
function startSelectedReview(): void {
  const ids = selectedVisible.value.map((record) => record.id)
  if (!ids.length) return
  void router.push({
    name: 'review',
    params: { projectId: projectId.value, recordId: ids[0] },
    query: { view: view.value, record_ids: ids.join(',') },
  })
}

/** 只聚焦右侧输入框：选择已登记在共享上下文里，普通发送即可附录，不建会话、不跳页。 */
function focusAssistant(): void {
  window.dispatchEvent(new CustomEvent('hx17:focus-composer'))
}

function clearSelection(): void {
  selected.value = []
  syncSelection()
}

function setView(bucket: Bucket): void {
  if (view.value === bucket) return
  view.value = bucket
  // URL 与视图保持同步：从画布返回、刷新都回到用户离开时的视图。
  void router.replace({
    name: 'reviewQueue',
    params: { projectId: projectId.value },
    query: { ...route.query, view: bucket },
  })
}

function withFlow(id: string): void {
  const query: Record<string, string> = { view: view.value }
  if (id) query.flow = id
  void router.push({ name: 'reviewQueue', params: { projectId: projectId.value }, query })
}

watch(selectedVisible, syncSelection)
// 输入框“移除”等外部清空：本地勾选必须同步取消，避免页面还显示已选但发送已无上下文。
watch(
  () => selection.context,
  (current) => {
    if (!current && selected.value.length) selected.value = []
  },
)
onMounted(load)
onUnmounted(() => {
  if (selection.context?.page === '人工复核队列') selection.clear(projectId.value)
})
watch(
  () => route.query.view,
  (raw) => {
    const next = normalizeView(raw)
    if (next === view.value) return
    view.value = next
  },
)
watch([view, projectId, flowId, requestedIds], () => {
  selected.value = []
  if (selection.context?.page === '人工复核队列') selection.clear(projectId.value)
  void load()
})
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>人工复核队列</h1>
        <p class="subtitle">
          <span v-if="flowId">本轮批次 <span class="mono">{{ flowId }}</span></span>
          <span v-else>当前项目的真实记录</span>
          <span class="hint"> · AI 核查结论只作参考，人工状态独立</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="load">刷新</button>
      </div>
    </header>

    <p v-if="error" class="banner banner-error" role="alert">{{ error }}</p>

    <div class="queue-bar">
      <div class="segmented" role="group" aria-label="复核视图">
        <button
          v-for="bucket in (['pending', 'draft', 'rejected', 'confirmed'] as const)"
          :key="bucket"
          type="button"
          :aria-pressed="view === bucket"
          @click="setView(bucket)"
        >
          {{ label(bucket) }} {{ counts[bucket] ?? 0 }}
        </button>
      </div>
      <span class="hint">
        {{ selected.length ? `已选 ${selected.length} 条` : `${visible.length} 条记录` }}
        · 计数按{{ countScopeLabel }}
      </span>
      <template v-if="selectedVisible.length">
        <button class="primary small" type="button" @click="startSelectedReview">
          开始复核所选（{{ selectedVisible.length }}）
        </button>
        <button
          class="ghost small"
          type="button"
          :title="`聚焦右侧输入框；已勾选的 ${selectedVisible.length} 条记录会随下一条普通消息自动附带，无需新建会话`"
          @click="focusAssistant"
        >
          向助手提问（{{ selectedVisible.length }}）
        </button>
        <button class="link" type="button" @click="clearSelection">清空选择</button>
      </template>
    </div>

    <div v-if="flowIds.length" class="card">
      <p class="card-head">本项目的巡检批次</p>
      <div class="flow-list">
        <button
          class="link small"
          :class="{ active: !flowId }"
          type="button"
          :aria-pressed="!flowId"
          @click="withFlow('')"
        >全部批次</button>
        <button
          v-for="id in flowIds"
          :key="id"
          class="link small"
          :class="{ active: flowId === id }"
          type="button"
          :aria-pressed="flowId === id"
          @click="withFlow(id)"
        >{{ id }}</button>
      </div>
    </div>

    <table v-if="visible.length" class="table">
      <thead>
        <tr>
          <th></th><th>记录</th><th>批次</th><th>命中</th><th>类别</th><th>检测时间</th><th>AI 建议</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="record in visible" :key="record.id">
          <td><input v-model="selected" type="checkbox" :value="record.id" /></td>
          <td>
            <span class="record-name" :title="record.id">{{ recordName(record) }}</span>
            <span v-if="record.rgb_filename" class="record-id mono">{{ record.id }}</span>
          </td>
          <td class="mono">{{ record.flow_id || '—' }}</td>
          <td>{{ record.hit_count }}</td>
          <td>{{ Object.keys(record.class_summary).join('、') || '—' }}</td>
          <td>{{ record.created_at_local }}</td>
          <td>{{ record.latest_review?.verdict || '未核查' }}</td>
          <td><button class="ghost small" type="button" @click="open(record.id)">开始复核</button></td>
        </tr>
      </tbody>
    </table>
    <p v-else-if="!loading" class="empty-hint">
      {{ flowId ? '当前批次在这个视图里没有记录。' : '这个视图里没有记录。' }}
    </p>
  </section>
</template>

<style scoped>
.queue-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}

.flow-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
}

.flow-list .link.active {
  font-weight: 650;
  text-decoration: underline;
}

.record-name {
  display: block;
  font-weight: 600;
}

.record-id {
  display: block;
  font-size: 11px;
  color: var(--muted);
  word-break: break-all;
}
</style>
