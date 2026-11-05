<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBusinessStore } from '@/stores/business'
import { useWorkspaceStore } from '@/stores/workspace'
import { recordStatusLabel, recordsSummary, reviewLine, verdictLabel } from '@/lib/business'
import type { DetectionRecord, RecordFiltersState } from '@/types'

/**
 * 识别记录页。
 *
 * 只展示**当前项目**的真实记录：没有数据就是空表，不铺任何示例行。
 * 交互上有意区分三种动作：
 * * 勾选 + “核查所选”：只核查勾中的记录（一条也行），不会顺手核查全表；
 * * 每行的“核查这条”：只核查这一条；
 * * 刷新/翻页/筛选都带上当前条件，并把服务端换算后的 UTC 边界显示出来。
 */

const route = useRoute()
const router = useRouter()
const business = useBusinessStore()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const page = computed(() => business.records[projectId.value] ?? null)
const records = computed<DetectionRecord[]>(() => page.value?.records ?? [])
const filters = computed<RecordFiltersState>(() => business.filtersFor(projectId.value))
const models = computed(() => business.models[projectId.value] ?? [])
const selected = ref<string[]>([])
const openRecordId = ref('')
const openRecord = computed(() => records.value.find((item) => item.id === openRecordId.value) ?? null)

const appliedBounds = computed(() => {
  const applied = (page.value?.filters.applied ?? {}) as Record<string, unknown>
  return {
    from: String(applied.utc_from ?? ''),
    to: String(applied.utc_to_exclusive ?? ''),
    offset: Number(applied.offset_minutes ?? 0),
  }
})

onMounted(async () => {
  if (!business.models[projectId.value]) await business.loadModels(projectId.value)
  await business.loadRecords(projectId.value, { ...filters.value })
})

// 进入页面时如果 URL 带了 record 参数（助手给的队列定位），直接打开那一条。
watch(
  () => route.query.record,
  async (value) => {
    const recordId = String(value ?? '')
    if (!recordId) return
    if (!records.value.some((item) => item.id === recordId)) {
      await business.loadRecords(projectId.value, { ...filters.value, limit: 50 })
    }
    openRecordId.value = recordId
  },
  { immediate: true },
)

async function reload(): Promise<void> {
  selected.value = []
  await business.loadRecords(projectId.value, { ...filters.value })
}

async function loadMore(): Promise<void> {
  const cursor = page.value?.next_before
  if (!cursor) return
  const previous = records.value
  await business.loadRecords(projectId.value, { ...filters.value, before: cursor })
  const incoming = business.records[projectId.value]?.records ?? []
  const seen = new Set(previous.map((item) => item.id))
  business.records[projectId.value] = {
    ...(business.records[projectId.value] as NonNullable<typeof page.value>),
    records: [...previous, ...incoming.filter((item) => !seen.has(item.id))],
  }
}

function toggle(recordId: string): void {
  selected.value = selected.value.includes(recordId)
    ? selected.value.filter((item) => item !== recordId)
    : [...selected.value, recordId]
}

function toggleAll(): void {
  selected.value =
    selected.value.length === records.value.length ? [] : records.value.map((item) => item.id)
}

async function reviewOne(record: DetectionRecord): Promise<void> {
  await business.reviewSelected(projectId.value, [record.id])
}

async function reviewChecked(): Promise<void> {
  await business.reviewSelected(projectId.value, [...selected.value])
}

function openSession(sessionId: string | null): void {
  if (!sessionId) return
  void router.push({ name: 'session', params: { projectId: projectId.value, sessionId } })
}

function sessionTitle(sessionId: string | null): string {
  if (!sessionId) return '手工检测'
  const list = workspace.sessionsByProject[projectId.value] ?? []
  return list.find((item) => item.id === sessionId)?.title ?? sessionId.slice(-6)
}

const counters = reactive({ hits: 0 })
watch(records, (list) => {
  counters.hits = list.filter((item) => item.hit_count > 0).length
})
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>识别记录</h1>
        <p class="subtitle">
          {{ recordsSummary(records) }}
          <span v-if="page" class="hint">· 筛选范围内共 {{ page.total_in_range }} 条</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" :disabled="business.loading" @click="reload">
          刷新
        </button>
        <button
          class="primary small"
          type="button"
          :disabled="!selected.length || business.busy === 'review'"
          @click="reviewChecked"
        >
          核查所选（{{ selected.length }}）
        </button>
      </div>
    </header>

    <p v-if="business.error" class="banner banner-error" role="alert">
      {{ business.error }}
      <button class="link" type="button" @click="business.error = ''">知道了</button>
    </p>
    <p v-else-if="business.notice" class="banner banner-ok" role="status">{{ business.notice }}</p>

    <div class="card">
      <div class="filters">
        <label class="inline-field">
          <span class="hint">状态</span>
          <select v-model="filters.status" @change="reload">
            <option value="">全部</option>
            <option value="done">已完成</option>
            <option value="running">检测中</option>
            <option value="failed">失败</option>
            <option value="paused">已暂停</option>
          </select>
        </label>
        <label class="inline-field">
          <span class="hint">模型</span>
          <select v-model="filters.model_id" @change="reload">
            <option value="">全部</option>
            <option v-for="model in models" :key="model.id" :value="model.id">{{ model.name }}</option>
          </select>
        </label>
        <label class="inline-field">
          <span class="hint">命中</span>
          <select
            :value="filters.has_hits === null ? '' : filters.has_hits ? '1' : '0'"
            @change="
              filters.has_hits =
                ($event.target as HTMLSelectElement).value === ''
                  ? null
                  : ($event.target as HTMLSelectElement).value === '1';
              reload()
            "
          >
            <option value="">全部</option>
            <option value="1">有命中</option>
            <option value="0">无命中</option>
          </select>
        </label>
        <label class="inline-field">
          <span class="hint">起始日期</span>
          <input v-model="filters.date_from" type="date" @change="reload" />
        </label>
        <label class="inline-field">
          <span class="hint">结束日期</span>
          <input v-model="filters.date_to" type="date" @change="reload" />
        </label>
      </div>
      <p class="hint">
        日期按本地日期解释（UTC{{ appliedBounds.offset >= 0 ? '+' : '' }}{{ appliedBounds.offset / 60 }}），
        实际查询区间 {{ appliedBounds.from || '不限' }} → {{ appliedBounds.to || '不限' }}（UTC）。
      </p>

      <table v-if="records.length" class="table">
        <thead>
          <tr>
            <th style="width: 34px">
              <input
                type="checkbox"
                :checked="selected.length === records.length && records.length > 0"
                aria-label="全选本页"
                @change="toggleAll"
              />
            </th>
            <th>时间（本地）</th>
            <th>状态</th>
            <th>命中</th>
            <th>类别</th>
            <th>AI 核查</th>
            <th>来源会话</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="record in records" :key="record.id">
            <td>
              <input
                type="checkbox"
                :checked="selected.includes(record.id)"
                :aria-label="`选择记录 ${record.id}`"
                @change="toggle(record.id)"
              />
            </td>
            <td class="mono">{{ record.created_at_local }}</td>
            <td>
              <span class="tag" :class="record.status === 'done' ? 'tag-on' : 'tag-off'">
                {{ recordStatusLabel(record.status) }}
              </span>
              <p v-if="record.error" class="hint form-error">{{ record.error.message }}</p>
            </td>
            <td>{{ record.hit_count }}</td>
            <td>
              <span v-if="!Object.keys(record.class_summary).length" class="hint">—</span>
              <span v-for="(count, name) in record.class_summary" :key="name" class="tag tag-off">
                {{ name }}×{{ count }}
              </span>
            </td>
            <td>
              <span :title="record.latest_review?.summary ?? ''">{{ reviewLine(record.latest_review) }}</span>
            </td>
            <td>
              <button class="link" type="button" @click="openSession(record.session_id)">
                {{ sessionTitle(record.session_id) }}
              </button>
            </td>
            <td class="row-actions">
              <button class="ghost small" type="button" @click="openRecordId = record.id">详情</button>
              <button
                class="ghost small"
                type="button"
                :disabled="record.status !== 'done' || business.busy === 'review'"
                @click="reviewOne(record)"
              >
                核查这条
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!business.loading" class="empty-hint">
        这个项目还没有识别记录。可以在对话里让助手跑一次检测，或在下方选择图片手工检测。
      </p>
      <div v-if="page?.has_more" class="card-actions">
        <button class="ghost small" type="button" @click="loadMore">加载更多</button>
      </div>
    </div>

    <div v-if="openRecord" class="modal-backdrop" @click.self="openRecordId = ''">
      <div class="modal" role="dialog" aria-label="识别记录详情">
        <header class="modal-header">
          <h2>记录 {{ openRecord.id.slice(-8) }}</h2>
          <button class="link" type="button" @click="openRecordId = ''">关闭</button>
        </header>
        <div class="modal-body">
          <div class="kv">
            <span>状态</span><span>{{ recordStatusLabel(openRecord.status) }}</span>
            <span>置信度</span><span>{{ openRecord.confidence }}</span>
            <span>命中</span><span>{{ openRecord.hit_count }}</span>
            <span>X 模态</span>
            <span>{{ openRecord.x_origin === 'derived' ? '由 RGB 派生（edge）' : openRecord.x_asset_id ? '用户提供' : '未使用' }}</span>
            <span>请求键</span><span class="mono">{{ openRecord.request_key }}</span>
          </div>

          <div class="image-pair">
            <figure>
              <img :src="openRecord.image_url" alt="原始图片" />
              <figcaption class="hint">原图（字节与上传时一致）</figcaption>
            </figure>
            <figure v-if="openRecord.result_url">
              <img :src="openRecord.result_url" alt="检测结果" />
              <figcaption class="hint">结果图（框已按原图坐标绘制）</figcaption>
            </figure>
          </div>

          <table v-if="openRecord.boxes.length" class="table">
            <thead>
              <tr><th>类别</th><th>置信度</th><th>框（x1, y1, x2, y2）</th></tr>
            </thead>
            <tbody>
              <tr v-for="(box, index) in openRecord.boxes" :key="index">
                <td>{{ box.class_name }}</td>
                <td>{{ box.confidence }}</td>
                <td class="mono">{{ box.bbox.map((value) => Math.round(value)).join(', ') }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">这次检测没有框（可能是背景图，或置信度阈值偏高）。</p>

          <div v-if="openRecord.latest_review" class="card">
            <p class="card-head">AI 核查：{{ verdictLabel(openRecord.latest_review.verdict) }}</p>
            <p class="body-text">{{ openRecord.latest_review.summary || openRecord.latest_review.error?.message }}</p>
            <p class="hint">
              模型 {{ openRecord.latest_review.model }} · {{ openRecord.latest_review.status }} ·
              这只代表 AI 建议，人工复核需要在复核流程里单独提交。
            </p>
          </div>
        </div>
        <footer class="modal-footer">
          <button
            class="primary small"
            type="button"
            :disabled="openRecord.status !== 'done' || business.busy === 'review'"
            @click="reviewOne(openRecord)"
          >
            核查这条
          </button>
        </footer>
      </div>
    </div>
  </section>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  margin-bottom: 8px;
}

.image-pair {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}

.image-pair img {
  width: 100%;
  border-radius: 8px;
  border: 1px solid var(--border);
}
</style>
