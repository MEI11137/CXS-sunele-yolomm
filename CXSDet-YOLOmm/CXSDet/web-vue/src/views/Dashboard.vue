<!--
  巡检工作台

  这里不摆装饰性指标。链条上的每个数字都来自检测记录接口的真实字段：
  检测 → AI 辅助核查 → 人工确认 → 回流。哪一个环节积压，页面就直接指出来。
-->
<!--
版权所有 © 2025 羊羊小栈 (GJQ)

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <div class="workbench">
    <header class="page-head">
      <div class="head-text">
        <h1 class="page-title">巡检工作台</h1>
        <p class="page-sub">
          {{ greeting }}。共 {{ stats.total }} 条巡检记录，其中
          <strong class="hl">{{ stats.pending }}</strong> 条等待人工确认。
        </p>
      </div>
      <div class="head-actions">
        <el-button @click="go('/detection-records')">
          <el-icon><Document /></el-icon>
          全部记录
        </el-button>
        <el-button type="primary" @click="go('/detection')">
          <el-icon><Camera /></el-icon>
          开始图片巡检
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      show-icon
      class="load-error"
      title="无法读取巡检记录"
      :description="loadError"
    >
      <template #default>
        <el-button size="small" @click="loadRecords">重新加载</el-button>
      </template>
    </el-alert>

    <!-- 闭环链路 -->
    <section class="hx-panel loop-panel">
      <div class="hx-panel__head">
        <h2 class="hx-panel__title">巡检闭环进度</h2>
        <span class="loop-hint">
          <span class="hx-dot" :class="`dot-${bottleneck.tone}`" />
          {{ bottleneck.text }}
        </span>
      </div>
      <div v-loading="loading" class="loop-track">
        <button
          v-for="(stage, index) in stages"
          :key="stage.key"
          type="button"
          class="stage"
          :class="[`is-${stage.tone}`, { 'is-zero': stage.count === 0 }]"
          @click="go(stage.path)"
        >
          <span class="stage-top">
            <span class="stage-index hx-mono">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="stage-name">{{ stage.name }}</span>
          </span>
          <span class="stage-count hx-mono">{{ stage.count }}</span>
          <span class="stage-note">{{ stage.note }}</span>
        </button>
      </div>
    </section>

    <div class="work-grid">
      <!-- 待处理记录 -->
      <section class="hx-panel pending-panel">
        <div class="hx-panel__head">
          <div>
            <h2 class="hx-panel__title">等待人工确认</h2>
            <p class="panel-sub">模型结果只是待核证据，确认之后才会进入训练数据。</p>
          </div>
          <el-button link type="primary" @click="go('/detection-records')">查看全部</el-button>
        </div>

        <div v-loading="loading" class="pending-body">
          <EmptyState
            v-if="!loading && pendingRecords.length === 0"
            title="没有待确认的记录"
            description="新的巡检结果会先出现在这里，等你对照原图核对标注。"
          >
            <template #action>
              <el-button size="small" type="primary" @click="go('/detection')">上传一张巡检图</el-button>
            </template>
          </EmptyState>

          <ul v-else class="pending-list">
            <li v-for="record in pendingRecords" :key="record.id" class="pending-item">
              <div class="item-main">
                <div class="item-title">{{ record.title }}</div>
                <div class="item-meta">
                  <span class="hx-mono">{{ shortId(record.id) }}</span>
                  <span class="sep" />
                  <span>{{ record.model_name }}</span>
                  <span class="sep" />
                  <span>{{ formatDateTime(record.create_time) }}</span>
                </div>
              </div>
              <div class="item-status">
                <StatusPill :meta="aiStatusOf(record.ai_review_status)" size="sm" />
              </div>
              <el-button size="small" type="primary" plain @click="openRecord(record)">
                去确认
              </el-button>
            </li>
          </ul>
        </div>
      </section>

      <!-- 右栏 -->
      <aside class="side-col">
        <section class="hx-panel todo-panel">
          <div class="hx-panel__head">
            <h2 class="hx-panel__title">接下来可以做什么</h2>
          </div>
          <ul class="todo-list">
            <li v-for="todo in todos" :key="todo.key" class="todo-item">
              <span class="todo-mark" :class="`is-${todo.tone}`" />
              <div class="todo-text">
                <span class="todo-label">{{ todo.label }}</span>
                <span class="todo-desc">{{ todo.desc }}</span>
              </div>
              <el-button size="small" text type="primary" @click="go(todo.path)">前往</el-button>
            </li>
          </ul>
        </section>

        <section class="hx-panel quick-panel">
          <div class="hx-panel__head">
            <h2 class="hx-panel__title">巡检入口</h2>
          </div>
          <div class="quick-grid">
            <button
              v-for="entry in extraEntries"
              :key="entry.path"
              type="button"
              class="quick-item"
              @click="go(entry.path)"
            >
              <el-icon class="quick-icon"><component :is="entry.icon" /></el-icon>
              <span class="quick-name">{{ entry.name }}</span>
              <span class="quick-desc">{{ entry.desc }}</span>
            </button>
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Camera, Document, VideoPlay, View, Files } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { detectionApi } from '@/api/detection'
import type { DetectionRecord } from '@/api/detection'
import { formatDateTime } from '@/utils/format'
import { aiStatusOf } from '@/constants/review'
import StatusPill from '@/components/StatusPill.vue'
import EmptyState from '@/components/EmptyState.vue'

const router = useRouter()
const userStore = useUserStore()
const userInfo = computed(() => userStore.userInfo)

const records = ref<DetectionRecord[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref('')

const greeting = computed(() => {
  const hour = new Date().getHours()
  const name = userInfo.value?.realName || userInfo.value?.username || '巡检员'
  if (hour < 6) return `${name}，夜间值守辛苦了`
  if (hour < 12) return `${name}，早上好`
  if (hour < 18) return `${name}，下午好`
  return `${name}，晚上好`
})

/** 一次取够，用于统计各阶段数量；单页 200 条足够覆盖当前规模。 */
async function loadRecords() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await detectionApi.getRecords({ page: 1, page_size: 200 })
    records.value = res.records || []
    total.value = res.total ?? records.value.length
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '接口未响应，请确认后端服务已启动'
    records.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

const stats = computed(() => {
  const list = records.value
  const pending = list.filter((r) => r.human_review_status === 'pending').length
  const confirmed = list.filter((r) => r.human_review_status === 'confirmed').length
  const aiDone = list.filter((r) => r.ai_review_status && r.ai_review_status !== 'not_requested').length
  const aiSuspect = list.filter((r) =>
    ['possible_false_positive', 'possible_missed_detection', 'needs_human_review'].includes(r.ai_review_status)
  ).length
  const feedback = list.filter((r) => !!r.feedback_dataset_id).length
  return { total: total.value, pending, confirmed, aiDone, aiSuspect, feedback }
})

const stages = computed(() => [
  {
    key: 'detect',
    name: '巡检检测',
    count: stats.value.total,
    note: '已生成的检测记录',
    tone: 'done',
    path: '/detection-records'
  },
  {
    key: 'ai',
    name: 'AI 辅助核查',
    count: stats.value.aiDone,
    note: stats.value.aiSuspect
      ? `其中 ${stats.value.aiSuspect} 条提示疑点`
      : `尚未核查 ${stats.value.total - stats.value.aiDone} 条`,
    tone: 'assist',
    path: '/detection-records'
  },
  {
    key: 'review',
    name: '待人工确认',
    count: stats.value.pending,
    note: '只有人判过的才算数',
    tone: stats.value.pending ? 'attention' : 'done',
    path: '/detection-records'
  },
  {
    key: 'confirmed',
    name: '已人工确认',
    count: stats.value.confirmed,
    note: '具备回流资格',
    tone: 'done',
    path: '/detection-records'
  },
  {
    key: 'feedback',
    name: '已回流样本',
    count: stats.value.feedback,
    note: '等待新数据集版本',
    tone: stats.value.feedback ? 'done' : 'idle',
    path: userStore.isAdmin() ? '/admin/datasets' : '/detection-records'
  }
])

const bottleneck = computed(() => {
  if (loading.value) return { text: '正在统计', tone: 'neutral' }
  if (loadError.value) return { text: '接口未连接', tone: 'danger' }
  if (stats.value.pending > 0) {
    return { text: `当前积压在人工确认：${stats.value.pending} 条待核`, tone: 'attention' }
  }
  if (stats.value.confirmed > stats.value.feedback) {
    return { text: `${stats.value.confirmed - stats.value.feedback} 条已确认但还未回流`, tone: 'attention' }
  }
  if (stats.value.feedback > 0) {
    return { text: '样本已回流，可生成新数据集版本', tone: 'ok' }
  }
  return { text: '暂无待处理记录', tone: 'neutral' }
})

const pendingRecords = computed(() =>
  records.value.filter((r) => r.human_review_status === 'pending').slice(0, 6)
)

const todos = computed(() => {
  const list: Array<{ key: string; label: string; desc: string; tone: string; path: string }> = []
  const { pending, confirmed, feedback, total: all, aiDone, aiSuspect } = stats.value

  if (pending > 0) {
    list.push({
      key: 'review',
      label: `确认 ${pending} 条待核记录`,
      desc: '对照原图核对标注，确认或修正后提交',
      tone: 'attention',
      path: '/detection-records'
    })
  }
  if (all - aiDone > 0) {
    list.push({
      key: 'ai',
      label: `对 ${all - aiDone} 条记录发起 AI 辅助核查`,
      desc: '让模型先指出可能的误报与漏检',
      tone: 'sea',
      path: '/detection-records'
    })
  }
  if (aiSuspect > 0) {
    list.push({
      key: 'suspect',
      label: `${aiSuspect} 条记录存在 AI 疑点`,
      desc: '疑点只是线索，仍需人工判定',
      tone: 'attention',
      path: '/detection-records'
    })
  }
  if (confirmed - feedback > 0) {
    list.push({
      key: 'feedback',
      label: `把 ${confirmed - feedback} 条已确认记录加入反馈池`,
      desc: '生成不可变的新数据集版本',
      tone: 'ok',
      path: '/detection-records'
    })
  }
  if (feedback > 0 && userStore.isAdmin()) {
    list.push({
      key: 'train',
      label: '生成新版本并启动增量训练',
      desc: '在数据集与模型管理中完成',
      tone: 'sea',
      path: '/admin/datasets'
    })
  }
  if (list.length === 0) {
    list.push({
      key: 'start',
      label: '从一次巡检开始',
      desc: '上传现场影像，生成第一条检测记录',
      tone: 'sea',
      path: '/detection'
    })
  }
  return list
})

const entries = [
  { name: '图片巡检', desc: '单张或多张影像', path: '/detection', icon: Camera },
  { name: '视频巡检', desc: '按视频抽帧检测', path: '/video-detection', icon: VideoPlay },
  { name: '实时巡检', desc: '接入画面即时判断', path: '/realtime-detection', icon: View },
  { name: '识别记录', desc: '复核与数据回流', path: '/detection-records', icon: Document }
]

const extraEntries = computed(() =>
  userStore.isAdmin()
    ? [...entries, { name: '数据集管理', desc: '划分与版本', path: '/admin/datasets', icon: Files }]
    : entries
)

const go = (path: string) => router.push(path)

const openRecord = (record: DetectionRecord) => {
  router.push({ path: '/detection-records', query: { record: record.id } })
}

const shortId = (id: string) => (id ? id.slice(0, 8) : '—')

onMounted(loadRecords)
</script>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  gap: var(--hx-gap);
}

/* —— 页头 —— */
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--hx-line);
}

.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--hx-text);
  padding-left: 10px;
  border-left: 3px solid var(--hx-sea);
  line-height: 1.2;
}

.page-sub {
  margin: 6px 0 0 13px;
  font-size: 13px;
  color: var(--hx-text-2);
}

.hl {
  color: var(--hx-beacon);
  font-variant-numeric: tabular-nums;
}

.head-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.load-error {
  border-radius: var(--hx-radius);
}

/* —— 闭环链路 —— */
.loop-panel :deep(.hx-panel__head) {
  align-items: baseline;
}

.loop-hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--hx-text-2);
}

.dot-attention {
  background: var(--hx-beacon);
}
.dot-ok {
  background: var(--hx-ok);
}
.dot-danger {
  background: var(--hx-defect);
}
.dot-neutral {
  background: var(--hx-text-3);
}

.loop-track {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 0;
  padding: 4px;
}

.stage {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px 16px;
  border: none;
  border-right: 1px solid var(--hx-line);
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease;
}

.stage:last-child {
  border-right: none;
}

.stage:hover {
  background: #f7faf9;
}

.stage::before {
  content: '';
  position: absolute;
  top: 0;
  left: 16px;
  right: 16px;
  height: 2px;
  background: var(--hx-line-strong);
}

.stage.is-attention::before {
  background: var(--hx-beacon);
}

.stage.is-done::before {
  background: var(--hx-sea);
}

.stage.is-assist::before {
  background: var(--hx-sea-line);
}

.stage-top {
  display: flex;
  align-items: center;
  gap: 7px;
}

.stage-index {
  font-size: 10px;
  color: var(--hx-text-3);
  letter-spacing: 0.04em;
}

.stage-name {
  font-size: 12px;
  color: var(--hx-text-2);
  font-weight: 500;
}

.stage-count {
  font-size: 26px;
  line-height: 1.15;
  font-weight: 600;
  color: var(--hx-text);
}

.stage.is-attention .stage-count {
  color: var(--hx-beacon);
}

.stage.is-zero .stage-count {
  color: var(--hx-text-3);
  font-weight: 400;
}

.stage-note {
  font-size: 11px;
  color: var(--hx-text-3);
  line-height: 1.45;
}

/* —— 主栅格 —— */
.work-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.9fr) minmax(280px, 1fr);
  gap: var(--hx-gap);
  align-items: start;
}

.panel-sub {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--hx-text-3);
}

.pending-body {
  min-height: 120px;
}

.pending-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.pending-item {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 11px 16px;
  border-bottom: 1px solid var(--hx-line);
}

.pending-item:last-child {
  border-bottom: none;
}

.pending-item:hover {
  background: #fafcfb;
}

.item-main {
  min-width: 0;
  flex: 1;
}

.item-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--hx-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-meta {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 3px;
  font-size: 11px;
  color: var(--hx-text-3);
  white-space: nowrap;
  overflow: hidden;
}

.sep {
  width: 1px;
  height: 9px;
  background: var(--hx-line-strong);
  flex-shrink: 0;
}

.item-status {
  flex-shrink: 0;
}

/* —— 右栏 —— */
.side-col {
  display: flex;
  flex-direction: column;
  gap: var(--hx-gap);
}

.todo-list {
  list-style: none;
  margin: 0;
  padding: 6px 0;
}

.todo-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 9px 16px;
}

.todo-mark {
  width: 3px;
  align-self: stretch;
  min-height: 26px;
  border-radius: 1px;
  background: var(--hx-line-strong);
  flex-shrink: 0;
}

.todo-mark.is-attention {
  background: var(--hx-beacon);
}
.todo-mark.is-sea {
  background: var(--hx-sea);
}
.todo-mark.is-ok {
  background: var(--hx-ok);
}

.todo-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.todo-label {
  font-size: 13px;
  color: var(--hx-text);
  line-height: 1.4;
}

.todo-desc {
  font-size: 11px;
  color: var(--hx-text-3);
  line-height: 1.45;
}

.quick-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--hx-line);
  border-top: 1px solid var(--hx-line);
}

.quick-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 14px 16px;
  background: var(--hx-paper);
  border: none;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease;
}

.quick-item:hover {
  background: #f4f9f8;
}

.quick-icon {
  font-size: 15px;
  color: var(--hx-sea);
  margin-bottom: 2px;
}

.quick-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--hx-text);
}

.quick-desc {
  font-size: 11px;
  color: var(--hx-text-3);
}

@media (max-width: 1180px) {
  .work-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 860px) {
  .loop-track {
    grid-template-columns: 1fr 1fr;
  }
  .stage {
    border-right: none;
    border-bottom: 1px solid var(--hx-line);
  }
  .page-head {
    flex-direction: column;
    align-items: flex-start;
  }
  .pending-item {
    flex-wrap: wrap;
  }
}
</style>
