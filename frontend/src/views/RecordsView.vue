<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import { useBusinessStore } from '@/stores/business'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  humanStatusLabel,
  recordStatusLabel,
  reviewLine,
  shortLocalTime,
  verdictLabel,
} from '@/lib/business'
import type { DetectionBox, DetectionRecord, RecordFiltersState } from '@/types'

/**
 * 识别记录页。
 *
 * 首屏回答三个问题：这是什么文件、AI 与人工各自说了什么、下一步做什么。
 * 技术信息（完整时间/UTC 边界、请求键、原始 ID）收进详情。
 * 批量动作只作用于明确勾选的范围，勾选后才出现批量工具栏；
 * 加入反馈只有人工已确认的记录可用（后端同样强制 REVIEW_REQUIRED）。
 * 筛选与回收站视图同步到 URL，刷新/后退后范围真实。
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
// 深链定位用的"脱离列表"记录：目标可能不在当前筛选/前 50 条里，按 ID 授权
// 直读（与列表同一人工状态/结论版本口径）。出现在列表里时优先用列表版本（更新鲜）。
const detachedRecord = ref<DetectionRecord | null>(null)
const showDeleted = ref(Boolean(route.query.deleted))
const showAdvanced = ref(false)
const openRecord = computed(
  () => records.value.find((item) => item.id === openRecordId.value) ?? detachedRecord.value,
)
const searchDraft = ref(String(route.query.q ?? ''))

// 行内「更多」菜单：页面层 fixed + backdrop（与数据集页同一套实现）。
const menuFor = ref<DetectionRecord | null>(null)
const menuPos = ref({ left: 0, top: 0 })
const menuBox = ref<HTMLElement | null>(null)

const appliedBounds = computed(() => {
  const applied = (page.value?.filters.applied ?? {}) as Record<string, unknown>
  return {
    from: String(applied.utc_from ?? ''),
    to: String(applied.utc_to_exclusive ?? ''),
    offset: Number(applied.offset_minutes ?? 0),
  }
})

const hitsOnPage = computed(() => records.value.filter((item) => item.hit_count > 0).length)
// 只有人工已确认、且当前确认版本**未入反馈池/未回流**的记录能加入反馈
// （后端同样强制 REVIEW_REQUIRED；服务端 feedback_status 是同一口径）。
const feedbackEligible = computed(() =>
  selected.value.filter((id) => {
    const record = records.value.find((item) => item.id === id)
    return record?.human_status === 'confirmed' && (record.feedback_status ?? 'none') === 'none'
  }),
)

/** 紧凑行保留**实际检测置信度**：最高框分数。区别于检测阈值（record.confidence）
 * 和 AI 建议置信度；无命中显示 —，不补 0% 假值。 */
function topBoxOf(record: DetectionRecord): DetectionBox | null {
  if (!record.boxes.length) return null
  return record.boxes.reduce((best, box) => (box.confidence > best.confidence ? box : best))
}

function feedbackStatusLabel(record: DetectionRecord): string {
  if (record.human_status !== 'confirmed') return ''
  const status = record.feedback_status ?? 'none'
  if (status === 'pending') return '已入反馈池'
  if (status === 'reflowed') return '已回流'
  return '未入池'
}

onMounted(async () => {
  if (!business.models[projectId.value]) await business.loadModels(projectId.value)
  hydrateFiltersFromUrl()
  await business.loadRecords(projectId.value, { ...filters.value, deleted: deletedParam() })
})

function hydrateFiltersFromUrl(): void {
  const query = route.query
  const f = filters.value
  f.status = String(query.status ?? '')
  f.model_id = String(query.model ?? '')
  f.has_hits = query.hits === '1' ? true : query.hits === '0' ? false : null
  f.date_from = String(query.from ?? '')
  f.date_to = String(query.to ?? '')
  f.q = String(query.q ?? '')
  searchDraft.value = f.q
}

function syncFiltersToUrl(): void {
  const f = filters.value
  const query: Record<string, string> = {}
  if (f.status) query.status = f.status
  if (f.model_id) query.model = f.model_id
  if (f.has_hits !== null) query.hits = f.has_hits ? '1' : '0'
  if (f.date_from) query.from = f.date_from
  if (f.date_to) query.to = f.date_to
  if (f.q) query.q = f.q
  if (showDeleted.value) query.deleted = '1'
  void router.replace({ query })
}

function deletedParam(): string {
  return showDeleted.value ? 'only' : 'exclude'
}

// 进入页面时如果 URL 带了 record 参数（助手给的队列定位），直接打开那一条。
// 定位不限于当前筛选或前 50 条：列表里找不到就按 ID 走授权读取（getRecord），
// 不能因为"这页没显示"就打不开真实存在的记录。
watch(
  () => route.query.record,
  async (value) => {
    const recordId = String(value ?? '')
    if (!recordId) return
    if (!records.value.some((item) => item.id === recordId)) {
      await business.loadRecords(
        projectId.value,
        { ...filters.value, deleted: deletedParam(), limit: 50 },
      )
    }
    if (!records.value.some((item) => item.id === recordId)) {
      try {
        const { record } = await api.getRecord(projectId.value, recordId)
        detachedRecord.value = record
      } catch (failure) {
        business.error = `定位记录失败：${errorMessage(failure)}`
        return
      }
    }
    openRecordId.value = recordId
  },
  { immediate: true },
)

// 关闭详情弹窗时同步清掉脱离列表的记录，避免下一条深链显示旧数据。
watch(openRecordId, (value) => {
  if (!value) detachedRecord.value = null
})

async function reload(): Promise<void> {
  selected.value = []
  syncFiltersToUrl()
  await business.loadRecords(projectId.value, { ...filters.value, deleted: deletedParam() })
}

async function loadMore(): Promise<void> {
  const cursor = page.value?.next_before
  if (!cursor) return
  const previous = records.value
  await business.loadRecords(projectId.value, { ...filters.value, before: cursor, deleted: deletedParam() })
  const incoming = business.records[projectId.value]?.records ?? []
  const seen = new Set(previous.map((item) => item.id))
  business.records[projectId.value] = {
    ...(business.records[projectId.value] as NonNullable<typeof page.value>),
    records: [...previous, ...incoming.filter((item) => !seen.has(item.id))],
  }
}

/** 搜索框回车/失焦才应用，不每敲一个字就打一次接口。 */
function applySearch(): void {
  filters.value.q = searchDraft.value.trim()
  void reload()
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

function clearSelection(): void {
  selected.value = []
}

async function reviewOne(record: DetectionRecord): Promise<void> {
  hideMenu()
  await business.reviewSelected(projectId.value, [record.id])
}

function openReview(record: DetectionRecord): void {
  void router.push({ name: 'review', params: { projectId: projectId.value, recordId: record.id } })
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

// ------------------------------------------------------------------ 反馈

async function feedbackChecked(): Promise<void> {
  if (!feedbackEligible.value.length) {
    business.error =
      '勾选的记录里没有可加入反馈的：只有人工已确认、且当前确认版本未入池/未回流的才能加入'
    return
  }
  await addFeedbackIds([...feedbackEligible.value])
}

async function addFeedbackIds(ids: string[]): Promise<void> {
  let created = 0
  let existing = 0
  let failed = 0
  let firstError = ''
  for (const id of ids) {
    try {
      const data = await api.addFeedback(projectId.value, id)
      // 区分新增与已存在：created=false 是"当前版本已在池里"，不是刚加入。
      if (data.created) created += 1
      else existing += 1
    } catch (failure) {
      failed += 1
      if (!firstError) firstError = `${id.slice(-6)}：${errorMessage(failure)}`
    }
  }
  const skipped = selected.value.length - ids.length
  if (failed) {
    // 失败保留勾选与页面状态（什么都没变），用户可以直接重试。
    business.error = `有 ${failed} 条加入反馈失败（${firstError}），本次失败项未写入；勾选仍保留，可重试`
    return
  }
  business.error = ''
  const parts = [`新增 ${created} 条`]
  if (existing) parts.push(`已在池 ${existing} 条`)
  const extra = skipped ? `；另有 ${skipped} 条不可反馈（未确认或已在池），未加入` : ''
  business.notice = `反馈：${parts.join('、')}${extra}`
  // 状态变了（可反馈数、入池标记），刷新当前筛选范围而不是保留旧状态。
  await reload()
}

function addFeedbackFromMenu(record: DetectionRecord): void {
  hideMenu()
  void addFeedbackIds([record.id])
}

// ------------------------------------------------------------------ 回收站

async function deleteChecked(): Promise<void> {
  if (!window.confirm(`把选中的 ${selected.value.length} 条记录移入回收站？文件保留，可在回收站恢复。`)) return
  try {
    const data = await api.deleteRecords(projectId.value, [...selected.value])
    const missing = data.items?.filter((item) => item.status === 'missing') ?? []
    business.error = ''
    business.notice =
      `已移入回收站 ${data.deleted} 条（软删除，文件保留）` +
      (missing.length ? `；${missing.length} 条不存在或无权限` : '')
    await reload()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

async function restoreChecked(): Promise<void> {
  try {
    const data = await api.restoreRecords(projectId.value, [...selected.value])
    business.error = ''
    business.notice = `已恢复 ${data.restored} 条`
    await reload()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

async function toggleDeleted(): Promise<void> {
  showDeleted.value = !showDeleted.value
  await reload()
}

// ------------------------------------------------------------------ 导出

/** 当前显示的记录（含加载更多累积）就是导出范围，文件内容与看到的一致。 */
function exportRecords(): void {
  const ids = records.value.map((item) => item.id)
  if (!ids.length) {
    business.error = '当前没有可导出的记录'
    return
  }
  const link = document.createElement('a')
  link.href = api.recordsExportUrl(projectId.value, ids)
  link.download = 'records.csv'
  link.click()
}

function exportSelected(): void {
  if (!selected.value.length) return
  const link = document.createElement('a')
  link.href = api.recordsExportUrl(projectId.value, [...selected.value])
  link.download = 'records-selected.csv'
  link.click()
}

// ------------------------------------------------------------------ 行内菜单

function openMenu(record: DetectionRecord, event: MouseEvent): void {
  const trigger = event.currentTarget as HTMLElement
  const rect = trigger.getBoundingClientRect()
  const width = 184
  const estimated = 176
  const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))
  const below = rect.bottom + 4
  const top =
    below + estimated > window.innerHeight - 8 ? Math.max(8, rect.top - estimated - 4) : below
  menuFor.value = record
  menuPos.value = { left, top }
  void nextTick(() => menuBox.value?.querySelector<HTMLElement>('button')?.focus())
}

function hideMenu(): void {
  menuFor.value = null
}

function displayName(record: DetectionRecord): string {
  return record.rgb_filename || `记录 ${record.id.slice(-8)}`
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
          本页 {{ records.length }} 条 · 命中 {{ hitsOnPage }} 条
          <span v-if="page" class="hint">· 筛选范围内共 {{ page.total_in_range }} 条</span>
          <span v-if="showDeleted" class="hint">· 正在查看回收站</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" :disabled="business.loading" @click="reload">
          刷新
        </button>
        <button class="ghost small" type="button" @click="toggleDeleted">
          {{ showDeleted ? '返回在用记录' : '回收站' }}
        </button>
        <button
          class="ghost small"
          type="button"
          :disabled="!records.length"
          :title="records.length ? `导出当前显示的 ${records.length} 条` : ''"
          @click="exportRecords"
        >
          导出
        </button>
      </div>
    </header>

    <p v-if="business.error" class="banner banner-error" role="alert">
      {{ business.error }}
      <button class="link" type="button" @click="business.error = ''">知道了</button>
    </p>
    <p v-else-if="business.notice" class="banner banner-ok" role="status">{{ business.notice }}</p>

    <!-- 勾选后的批量工具栏：只作用于明确范围 -->
    <div v-if="selected.length" class="selection-bar" role="toolbar" aria-label="批量操作">
      <span class="hint">已选 {{ selected.length }} 条 · 可反馈 {{ feedbackEligible.length }} 条</span>
      <button
        v-if="showDeleted"
        class="ghost small"
        type="button"
        @click="restoreChecked"
      >
        恢复所选
      </button>
      <template v-else>
        <button
          class="primary small"
          type="button"
          :disabled="!selected.length || business.busy === 'review'"
          @click="business.reviewSelected(projectId, [...selected])"
        >
          批量核查（{{ selected.length }}）
        </button>
        <button
          class="ghost small"
          type="button"
          :disabled="!feedbackEligible.length"
          :title="feedbackEligible.length ? '' : '只有人工已确认、且当前确认版本未入池/未回流的记录能加入反馈'"
          @click="feedbackChecked"
        >
          加入反馈（{{ feedbackEligible.length }}）
        </button>
        <button class="ghost small" type="button" @click="exportSelected">导出所选</button>
        <button class="ghost small danger" type="button" @click="deleteChecked">移入回收站</button>
      </template>
      <button class="link" type="button" @click="clearSelection">清空选择</button>
    </div>

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
        <form class="inline-field" @submit.prevent="applySearch">
          <span class="hint">搜索</span>
          <input
            v-model="searchDraft"
            type="search"
            placeholder="按图片文件名搜索"
            @keydown.escape="searchDraft = filters.q; applySearch()"
            @blur="searchDraft !== filters.q && applySearch()"
          />
        </form>
        <button class="ghost small" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced">
          {{ showAdvanced ? '收起筛选' : '筛选' }}
        </button>
      </div>
      <div v-if="showAdvanced" class="filters filters-advanced">
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
        <p class="hint bounds-hint">
          日期按本地日期解释（UTC{{ appliedBounds.offset >= 0 ? '+' : '' }}{{ appliedBounds.offset / 60 }}），
          实际查询区间 <span class="mono">{{ appliedBounds.from || '不限' }}</span> →
          <span class="mono">{{ appliedBounds.to || '不限' }}</span>（UTC）。
        </p>
      </div>

      <table v-if="records.length" class="table rec-table">
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
            <th>文件 / 时间（本地）</th>
            <th>状态</th>
            <th class="num" title="命中框数与最高检测置信度（区别于检测阈值与 AI 建议置信度）">命中 · 最高分</th>
            <th>类别</th>
            <th>AI 核查</th>
            <th>人工 / 反馈</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="record in records" :key="record.id">
            <td>
              <input
                type="checkbox"
                :checked="selected.includes(record.id)"
                :aria-label="`选择 ${displayName(record)}`"
                @change="toggle(record.id)"
              />
            </td>
            <td class="rec-name">
              <strong :title="record.rgb_filename || record.rgb_asset_id">{{ displayName(record) }}</strong>
              <small class="hint">{{ shortLocalTime(record.created_at_local) }}</small>
            </td>
            <td>
              <span class="tag" :class="record.status === 'done' ? 'tag-on' : 'tag-off'">
                {{ recordStatusLabel(record.status) }}
              </span>
              <p v-if="record.error" class="hint form-error">{{ record.error.message }}</p>
            </td>
            <td class="num">
              {{ record.hit_count }}
              <small
                v-if="topBoxOf(record)"
                class="hint"
                :title="`最高框：${topBoxOf(record)?.class_name}`"
              >
                · {{ (topBoxOf(record) as DetectionBox).confidence.toFixed(2) }}
              </small>
              <small v-else-if="record.status === 'done'" class="hint">· —</small>
            </td>
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
              <span
                class="tag"
                :class="record.human_status === 'confirmed' ? 'tag-on' : 'tag-off'"
              >
                {{ humanStatusLabel(record.human_status) }}
              </span>
              <small v-if="feedbackStatusLabel(record)" class="hint rec-feedback">
                {{ feedbackStatusLabel(record) }}
              </small>
            </td>
            <td class="row-actions">
              <button class="ghost small" type="button" @click="openRecordId = record.id">详情</button>
              <button
                class="ghost small"
                type="button"
                :disabled="record.status !== 'done'"
                :title="record.status === 'done' ? '' : '检测完成后才能人工复核'"
                @click="openReview(record)"
              >
                复核
              </button>
              <button
                class="ghost small"
                type="button"
                aria-haspopup="menu"
                :aria-expanded="menuFor?.id === record.id"
                @click="openMenu(record, $event)"
              >
                更多
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!business.loading" class="empty-hint">
        {{
          showDeleted
            ? '回收站是空的。移入回收站的记录会保留文件，随时可以恢复。'
            : '这个项目还没有识别记录。可以在对话里让助手跑一次检测，或在图片巡检里选择图片手工检测。'
        }}
      </p>
      <div v-if="page?.has_more" class="card-actions">
        <button class="ghost small" type="button" @click="loadMore">加载更多</button>
      </div>
    </div>

    <!-- 唯一的行内「更多」菜单：页面层 + fixed -->
    <template v-if="menuFor">
      <div class="row-menu-backdrop" @click="hideMenu"></div>
      <div
        ref="menuBox"
        class="row-menu"
        :style="{ left: menuPos.left + 'px', top: menuPos.top + 'px' }"
        role="menu"
        aria-label="记录操作"
        @keydown.esc="hideMenu"
      >
        <button
          type="button"
          role="menuitem"
          :disabled="menuFor.status !== 'done' || business.busy === 'review'"
          @click="reviewOne(menuFor)"
        >
          仅核查这条（AI 参考）
        </button>
        <button
          type="button"
          role="menuitem"
          :disabled="menuFor.human_status !== 'confirmed' || (menuFor.feedback_status ?? 'none') !== 'none'"
          :title="
            menuFor.human_status === 'confirmed' && (menuFor.feedback_status ?? 'none') === 'none'
              ? ''
              : '只有人工已确认、且当前确认版本未入池/未回流的记录能加入反馈'
          "
          @click="addFeedbackFromMenu(menuFor)"
        >
          加入反馈{{ (menuFor.feedback_status ?? 'none') !== 'none' ? '（已在池）' : '' }}
        </button>
        <button type="button" role="menuitem" @click="openRecordId = menuFor.id; hideMenu()">
          查看详情
        </button>
        <button
          type="button"
          role="menuitem"
          :disabled="!menuFor.session_id"
          @click="openSession(menuFor.session_id); hideMenu()"
        >
          {{ menuFor.session_id ? '打开来源会话' : '手工检测（无来源会话）' }}
        </button>
      </div>
    </template>

    <div v-if="openRecord" class="modal-backdrop" @click.self="openRecordId = ''">
      <div class="modal" role="dialog" aria-label="识别记录详情">
        <header class="modal-header">
          <h2>{{ displayName(openRecord) }}</h2>
          <button class="link" type="button" @click="openRecordId = ''">关闭</button>
        </header>
        <div class="modal-body">
          <dl class="kv">
            <div><dt>状态</dt><dd>{{ recordStatusLabel(openRecord.status) }}</dd></div>
            <div><dt>人工状态</dt><dd>
              {{ humanStatusLabel(openRecord.human_status) }}
              <template v-if="openRecord.review_version">（第 {{ openRecord.review_version }} 版结论）</template>
            </dd></div>
            <div><dt>反馈</dt><dd>{{ feedbackStatusLabel(openRecord) || '—（未确认，还不能入池）' }}</dd></div>
            <div><dt>检测阈值</dt><dd>{{ openRecord.confidence }}（当时运行的阈值，不是框分数）</dd></div>
            <div><dt>命中</dt><dd>{{ openRecord.hit_count }}</dd></div>
            <div><dt>时间（本地）</dt><dd class="mono">{{ openRecord.created_at_local }}</dd></div>
            <div><dt>时间（UTC）</dt><dd class="mono">{{ openRecord.created_at }}</dd></div>
            <div><dt>来源会话</dt><dd>
              <button v-if="openRecord.session_id" class="link" type="button" @click="openSession(openRecord.session_id)">
                {{ sessionTitle(openRecord.session_id) }}
              </button>
              <template v-else>手工检测</template>
            </dd></div>
            <div><dt>主图模态</dt><dd>{{ openRecord.primary_modality === 'ir' ? '红外 IR（原图本身是 IR，没有 RGB 替身）' : '可见光 RGB' }}</dd></div>
            <div><dt>X 模态</dt><dd>{{ openRecord.x_origin === 'derived' ? '由 RGB 派生（edge）' : openRecord.x_asset_id ? '用户提供' : '未使用' }}</dd></div>
            <div><dt>资产 ID</dt><dd class="mono">{{ openRecord.rgb_asset_id }}</dd></div>
            <div><dt>请求键</dt><dd class="mono">{{ openRecord.request_key }}</dd></div>
          </dl>

          <div class="image-pair">
            <figure>
              <img :src="openRecord.image_url" alt="原始图片" />
              <figcaption class="hint">{{ openRecord.primary_modality === 'ir' ? 'IR 原图（字节与上传时一致）' : '原图（字节与上传时一致）' }}</figcaption>
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
          <button class="ghost small" type="button" @click="openRecordId = ''; void openReview(openRecord)">
            在人工复核中打开
          </button>
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

.filters-advanced {
  padding-top: 8px;
  border-top: 1px dashed var(--border);
}

.bounds-hint {
  flex-basis: 100%;
  margin: 0;
}

.selection-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 10px;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: var(--panel-secondary);
}

.rec-table {
  font-size: 13px;
}

.rec-table th,
.rec-table td {
  padding: 8px 10px;
  vertical-align: middle;
}

.rec-table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.rec-name strong {
  display: block;
  max-width: 320px;
  overflow-wrap: anywhere;
  font-weight: 600;
  line-height: 1.45;
}

.rec-name small {
  display: block;
  margin-top: 2px;
  font-variant-numeric: tabular-nums;
}

.rec-feedback {
  display: block;
  margin-top: 2px;
  font-size: 12px;
}

.rec-table td.num small {
  font-variant-numeric: tabular-nums;
  color: var(--text-secondary);
}

.row-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
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
