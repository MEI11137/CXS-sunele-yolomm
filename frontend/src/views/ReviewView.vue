<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import DialogShell from '@/components/DialogShell.vue'
import * as api from '@/api/business'
import { errorCode, errorMessage } from '@/api/http'
import { toPayloadBox } from '@/lib/geometry'
import type { Box, Handle, Viewport } from '@/lib/geometry'
import {
  draftBaseVersion,
  preferredBoxes,
  preferredXBoxes,
  submitBaseVersion,
} from '@/lib/review'
import { clampPoint, fitView, panBy, toImage, zoomAt } from '@/lib/geometry'
import { beginDrag, dragBoxes, endDrag, toolHint } from '@/lib/interaction'
import type { Tool } from '@/lib/interaction'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import type { ReviewClassOption, ReviewState } from '@/types'

/**
 * 人工复核页（DSH 风格）。
 *
 * * 双模态时只有两张真实可编辑画布（参考模态 / 辅模态），单模态只有一张；
 * * 两套框各用自己的原图像素坐标与图像尺寸，缩放/平移/撤销/选中互不影响；
 * * 结果图不再常驻第三栏，收进“版本与 AI 结论”里按需查看/下载；
 * * 保存草稿与人工提交分离；Agent 改草稿后按 record 级投影自动载入或提示，不偷覆盖本地改动。
 */

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const recordId = computed(() => String(route.params.recordId ?? ''))

// 从复核队列进入时带 view/flow：提交后自动进入同队列下一项，不要求返回列表再点。
const queueView = computed(() => String(route.query.view ?? '').trim())
// 批次 flow 仍是前端可见范围：列表接口没有 flow 参数，先取够数据再按 flow_id 过滤。
const queueFlow = computed(() => String(route.query.flow ?? '').trim())
// 从复核队列“开始复核所选”进入时，队列被限定为这些 record_ids；
// 刷新/上一项/下一项都保留同一限定，不能退回整个项目队列。
const queueRecordIds = computed(() =>
  String(route.query.record_ids ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean),
)
const queueIds = ref<string[]>([])
/**
 * 复核详情加载代次：load() 返回时若代次已变、或路由 record/project 已切换，
 * 就丢弃这次响应。否则慢返回的 A 记录会把 boxes/note 灌进 B 的画布，
 * 下一次保存就会把 A 的数据写进 B。
 */
let loadEpoch = 0
const queueIndex = computed(() => queueIds.value.indexOf(recordId.value))
const prevRecordId = computed(() => (queueIndex.value > 0 ? queueIds.value[queueIndex.value - 1] : ''))
const nextRecordId = computed(() => {
  const index = queueIndex.value
  return index >= 0 && index < queueIds.value.length - 1 ? queueIds.value[index + 1] : ''
})

let queueEpoch = 0

async function loadQueue(): Promise<void> {
  if (!queueView.value || !projectId.value) return
  const epoch = ++queueEpoch
  const project = projectId.value
  const view = queueView.value
  const flow = queueFlow.value
  const fixedIds = [...queueRecordIds.value]
  const stillCurrent = () =>
    epoch === queueEpoch &&
    project === projectId.value &&
    view === queueView.value &&
    flow === queueFlow.value
  // 带明确 record_ids 时，这就是本轮人工队列的稳定范围：保存草稿(pending→draft)、
  // 提交(→confirmed)只改变记录状态，不能因此按旧 view 过滤把已看项挤出去，
  // 否则“上一项/下一项”会在中途失效。顺序沿用进入队列时的可见顺序。
  // 记录本身是否仍存在/仍属于本账号，仍由每次 load() 的详情接口校验。
  if (fixedIds.length) {
    queueIds.value = fixedIds
    return
  }
  try {
    // 先让后端按 view/flow 收窄，再 limit：不能先取前 200 条再前端 filter，
    // 否则稀疏的旧批次会被截断丢失。200 是接口允许的上限。
    const data = await api.listReviewQueue(project, {
      view,
      flow: flow || undefined,
      limit: 200,
    })
    if (!stillCurrent()) return
    queueIds.value = data.items.map((item) => item.id)
  } catch {
    if (!stillCurrent()) return
    queueIds.value = []
  }
}

function goSibling(direction: 'prev' | 'next'): void {
  const target = direction === 'prev' ? prevRecordId.value : nextRecordId.value
  if (!target) return
  void router.replace({ name: 'review', params: { projectId: projectId.value, recordId: target }, query: route.query })
}

function goQueue(): void {
  void router.push({ name: 'reviewQueue', params: { projectId: projectId.value }, query: route.query })
}

function goReflow(): void {
  const dataset = state.value?.record.dataset_id
  void router.push({
    name: 'reflow',
    params: { projectId: projectId.value },
    query: { ...(dataset ? { dataset_id: dataset } : {}), record_id: recordId.value, mode: 'revision' },
  })
}

watch(recordId, () => {
  if (!recordId.value) return
  pendingRemote.value = null
  recovery.value = null
  notice.value = pendingNotice.value
  pendingNotice.value = ''
  void load()
  void loadQueue()
})
const sessionId = computed(() => workspace.currentSessionId || undefined)

type PaneKey = 'primary' | 'secondary'
const PANE_KEYS: PaneKey[] = ['primary', 'secondary']

const state = ref<ReviewState | null>(null)
const boxes = ref<Box[]>([])
const xBoxes = ref<Box[]>([])
const note = ref('')
const busy = ref('')
const error = ref('')
const notice = ref('')
// 驳回后需要跳到队列下一项时，目标记录的 watch(recordId) 会先清空 notice；
// 这里把要跨路由保留的提示暂存，由 watch 在切换后重新显示。
const pendingNotice = ref('')
const pendingRemote = ref<{ version: number; updated_at: string } | null>(null)

/**
 * 登录失效时的本地应急草稿。
 *
 * 受保护接口 401 会立刻清登录态并卸载 AppLayout；dirty guard 来不及保护
 * ReviewView 里尚未保存的框/备注。因此在 session-expiring 事件里同步把当前
 * 编辑写进 localStorage，键按 user/project/record 隔离；只有重新登录同一账号、
 * 打开同一记录时才提示恢复，并在恢复时对照 base_version 提示冲突。
 */
interface RecoveryBackup {
  user_id: number
  project_id: string
  record_id: string
  saved_at: string
  base_version: number | null
  boxes: Box[]
  x_boxes: Box[]
  note: string
}
const recovery = ref<{ key: string; data: RecoveryBackup } | null>(null)
const RECOVERY_PREFIX = 'hx17:review-recovery:'

function recoveryKey(userId: number): string {
  return `${RECOVERY_PREFIX}${userId}|${projectId.value}|${recordId.value}`
}

/** 同步备份 dirty 编辑；401 监听器在清 auth 之前派发事件，所以这里还能拿到用户 id。 */
function backupForSessionExpiry(userId: number | null): void {
  if (!dirty.value || !state.value?.record || !userId) return
  const data: RecoveryBackup = {
    user_id: userId,
    project_id: projectId.value,
    record_id: recordId.value,
    saved_at: new Date().toISOString(),
    base_version: draftBase.value,
    boxes: boxes.value.map((box) => ({ ...box, bbox: [...box.bbox] as Box['bbox'] })),
    x_boxes: xBoxes.value.map((box) => ({ ...box, bbox: [...box.bbox] as Box['bbox'] })),
    note: note.value,
  }
  try {
    localStorage.setItem(recoveryKey(userId), JSON.stringify(data))
  } catch {
    // 本地存储不可用/满了不能默默当作“已备份”：同步提示用户，
    // 此时页面尚未卸载，用户仍可看到/复制当前框与备注。
    window.alert(
      '浏览器本地存储不可用，登录失效前的未保存框/备注未能备份。' +
        '请先复制或记下这些修改，再继续重新登录。',
    )
  }
}

function onSessionExpiring(event: Event): void {
  const detail = (event as CustomEvent<{ userId?: number | null }>).detail
  backupForSessionExpiry(detail?.userId ?? auth.user?.id ?? null)
}

/** 登录恢复后只对“同 user + 同项目 + 同记录”的备份提示恢复，绝不跨账号套用。 */
function maybeOfferRecovery(): void {
  recovery.value = null
  const userId = auth.user?.id
  if (!state.value?.record || !userId) return
  const key = recoveryKey(userId)
  const raw = localStorage.getItem(key)
  if (!raw) return
  try {
    const data = JSON.parse(raw) as RecoveryBackup
    if (
      data.user_id !== userId ||
      data.project_id !== projectId.value ||
      data.record_id !== recordId.value
    ) {
      // 别的账号/别的记录留下的应急草稿：不展示、也绝不删除，
      // 等原账号重新登录后仍能恢复（清缓存不能顺手清恢复材料）。
      return
    }
    recovery.value = { key, data }
  } catch {
    // 损坏的备份不展示；同样不在这里删，避免误删其他账号的恢复材料。
  }
}

function restoreRecovery(): void {
  const pending = recovery.value
  if (!pending) return
  const data = pending.data
  const baseMatches = (draftBase.value ?? null) === (data.base_version ?? null)
  boxes.value = data.boxes.map((box) => ({ ...box, bbox: [...box.bbox] as Box['bbox'] }))
  xBoxes.value = data.x_boxes.map((box) => ({ ...box, bbox: [...box.bbox] as Box['bbox'] }))
  note.value = data.note
  selected.primary = -1
  selected.secondary = -1
  undoStack.primary = []
  undoStack.secondary = []
  recovery.value = null
  localStorage.removeItem(pending.key)
  notice.value = baseMatches
    ? '已恢复登录失效前未保存的框与备注，请核对后保存草稿。'
    : '已恢复登录失效前的修改，但服务端草稿基线已变化（base_version 不一致），请逐项核对后再保存。'
}

function discardRecovery(): void {
  const pending = recovery.value
  if (!pending) return
  localStorage.removeItem(pending.key)
  recovery.value = null
  notice.value = '已丢弃登录失效前的未保存修改。'
}

function clearRecoveryForCurrent(): void {
  const userId = auth.user?.id
  if (!userId) return
  try {
    localStorage.removeItem(recoveryKey(userId))
  } catch {
    // ignore
  }
  recovery.value = null
}
/** 每张画布自己的图片加载错误：404/坏图要落在对应画布里说清楚，不能只给空白。 */
const paneImageError = reactive<Record<PaneKey, string>>({ primary: '', secondary: '' })
const loadedProjection = ref('')
const loadedSignature = ref('')
const activePane = ref<PaneKey>('primary')

const selected = reactive<Record<PaneKey, number>>({ primary: -1, secondary: -1 })

/** 面向评委的错误文案：内部错误码不直接暴露，404 给明确下一步。 */
function formatError(exc: unknown): string {
  const code = errorCode(exc)
  const message = errorMessage(exc)
  if (code === 'RECORD_NOT_FOUND') return '这条识别记录不存在或已被删除，请返回「识别记录」重新选择。'
  return [code, message].filter(Boolean).join('：')
}
const tool = reactive<Record<PaneKey, Tool>>({ primary: 'select', secondary: 'select' })
const undoStack = reactive<Record<PaneKey, Box[][]>>({ primary: [], secondary: [] })
const pendingFit = reactive<Record<PaneKey, boolean>>({ primary: true, secondary: true })

const defaultView = (): Viewport => ({ scale: 1, offsetX: 0, offsetY: 0 })
const views = reactive<Record<PaneKey, Viewport>>({ primary: defaultView(), secondary: defaultView() })
const panes = reactive<Record<PaneKey, HTMLElement | null>>({ primary: null, secondary: null })
const paneSize = reactive<Record<PaneKey, { width: number; height: number }>>({
  primary: { width: 0, height: 0 },
  secondary: { width: 0, height: 0 },
})
const paneImage = reactive<Record<PaneKey, { width: number; height: number }>>({
  primary: { width: 0, height: 0 },
  secondary: { width: 0, height: 0 },
})

const HANDLES: Handle[] = ['nw', 'ne', 'se', 'sw']

interface DragState {
  pane: PaneKey
  mode: '' | 'pan' | 'move' | 'resize' | 'draw'
  index: number
  handle: Handle | null
  origin: { x: number; y: number }
  snapshot: Box[] | null
  current: { x: number; y: number } | null
  lastScreen: { x: number; y: number }
  undoPushed: boolean
}
const drag = ref<DragState | null>(null)

// ------------------------------------------------------------------ 派生状态

const image = computed(() => state.value?.image ?? null)
const primaryLabel = computed(() => (image.value?.primary_modality === 'ir' ? '红外 IR' : '可见光 RGB'))
const secondaryLabel = computed(() => image.value?.x_modality || '辅模态')
const hasSecondary = computed(() => Boolean(image.value?.x_url))
const visiblePanes = computed<PaneKey[]>(() => (hasSecondary.value ? PANE_KEYS : ['primary']))
const editable = computed(() => state.value?.queue !== 'rejected')
const totalBoxes = computed(() => boxes.value.length + xBoxes.value.length)
const classOptions = computed<ReviewClassOption[]>(() => state.value?.class_options ?? [])
const draftBase = computed(() => draftBaseVersion(state.value))
const submitBase = computed(() => submitBaseVersion(state.value))

const dirty = computed(() => loadedSignature.value !== '' && loadedSignature.value !== localSignature())

function imageOf(pane: PaneKey): { width: number; height: number } {
  if (pane === 'primary') return { width: image.value?.width ?? 0, height: image.value?.height ?? 0 }
  return { width: image.value?.x_width ?? 0, height: image.value?.x_height ?? 0 }
}

function boxesOf(pane: PaneKey): Box[] {
  return pane === 'primary' ? boxes.value : xBoxes.value
}

function setBoxes(pane: PaneKey, next: Box[]): void {
  if (pane === 'primary') boxes.value = next
  else xBoxes.value = next
}

function urlOf(pane: PaneKey): string {
  if (!image.value) return ''
  return pane === 'primary' ? image.value.rgb_url : (image.value.x_url ?? '')
}

function paneTitle(pane: PaneKey): string {
  return pane === 'primary' ? `参考模态 · ${primaryLabel.value}` : `辅模态 · ${secondaryLabel.value}`
}

function classLabel(classId: number): string {
  return classOptions.value.find((item) => item.index === classId)?.name ?? '未知类别'
}

function currentBox(pane: PaneKey): Box | undefined {
  const list = boxesOf(pane)
  const index = selected[pane]
  return index >= 0 ? list[index] : undefined
}

// ------------------------------------------------------------------ 本地/远端变更

function boxSignature(list: Box[]): string {
  return JSON.stringify(list.map((box) => [box.class_id, box.bbox, box.severity ?? '']))
}

function localSignature(): string {
  return `${boxSignature(boxes.value)}|${boxSignature(xBoxes.value)}|${note.value}`
}

type VersionLike = { id?: string; version?: number; status?: string; updated_at?: string }

/**
 * 当前 record 最新一条版本的比较键。
 *
 * 不能假设 version 全局单调：submit 从“最近已提交版本”+1，删除草稿后 latest.version
 * 可能从草稿 v5 回退到已确认 v2（root 探针同路径）。所以用 (id,status,version) 判变化。
 */
function newestVersion(stateValue: ReviewState | null): VersionLike | null {
  if (!stateValue) return null
  const rows: VersionLike[] = []
  for (const version of stateValue.versions ?? []) rows.push(version as VersionLike)
  for (const version of [stateValue.draft, stateValue.latest, stateValue.latest_confirmed, stateValue.latest_submitted]) {
    if (version) rows.push(version as VersionLike)
  }
  if (!rows.length) return null
  rows.sort((a, b) => {
    const left = String(a.updated_at ?? '')
    const right = String(b.updated_at ?? '')
    if (left !== right) return left < right ? 1 : -1
    return (Number(b.version) || 0) - (Number(a.version) || 0)
  })
  return rows[0]
}

function projectionKey(row: VersionLike | null | undefined): string {
  if (!row) return ''
  return `${row.id ?? ''}|${row.status ?? ''}|${Number(row.version) || 0}`
}

function setBaseline(): void {
  loadedSignature.value = localSignature()
  loadedProjection.value = projectionKey(newestVersion(state.value))
  pendingRemote.value = null
}

// ------------------------------------------------------------------ 载入/保存/提交

async function load(
  options: { silent?: boolean; message?: string; preserveViewport?: boolean } = {},
): Promise<void> {
  if (!projectId.value || !recordId.value) return
  const epoch = ++loadEpoch
  const requestProject = projectId.value
  const requestRecord = recordId.value
  const stillCurrent = () =>
    epoch === loadEpoch &&
    requestProject === projectId.value &&
    requestRecord === recordId.value
  if (!options.silent) busy.value = 'load'
  try {
    const sameRecord = state.value?.record?.id === requestRecord
    const keepViewport = Boolean(options.preserveViewport) && sameRecord && state.value !== null
    const data = await api.getReviewState(requestProject, requestRecord)
    if (!stillCurrent()) return
    if (!data.state?.record) throw new Error('记录不存在或已被删除')
    state.value = data.state
    boxes.value = preferredBoxes(data.state) as Box[]
    xBoxes.value = preferredXBoxes(data.state) as Box[]
    note.value = data.state.draft?.note ?? ''
    undoStack.primary = []
    undoStack.secondary = []
    if (keepViewport) {
      // 同一记录/图片的远程同步只换标注与状态：保留用户当前缩放/平移与选中，
      // 不把画布跳回“适应窗口”（root 审查点）。
      selected.primary = Math.min(selected.primary, boxes.value.length - 1)
      selected.secondary = Math.min(selected.secondary, xBoxes.value.length - 1)
    } else {
      selected.primary = -1
      selected.secondary = -1
      pendingFit.primary = true
      pendingFit.secondary = true
    }
    if (!hasSecondary.value) activePane.value = 'primary'
    error.value = ''
    if (options.message) notice.value = options.message
    await nextTick()
    // nextTick 期间也可能已经切到另一条记录：此时不再 measure/fit/setBaseline。
    if (!stillCurrent()) return
    measureAll()
    if (!keepViewport) {
      fit('primary')
      if (hasSecondary.value) fit('secondary')
      // 图片可能还没 decode 完；onImageLoad 会再补齐一次 fit。
    }
    setBaseline()
    maybeOfferRecovery()
  } catch (exc) {
    if (!stillCurrent()) return
    error.value = formatError(exc)
  } finally {
    if (epoch === loadEpoch && busy.value === 'load') busy.value = ''
  }
}

// 写请求的收尾也绑定“发起时是哪条记录”。服务端可能已经执行成功，但用户已经
// 放弃修改切到 B；这时只能丢弃旧 UI 回填，不能把 A 的响应写到 B，更不能重发。
function captureView() {
  return { epoch: loadEpoch, project: projectId.value, record: recordId.value }
}
function viewStillCurrent(snap: { epoch: number; project: string; record: string }): boolean {
  return snap.epoch === loadEpoch && snap.project === projectId.value && snap.record === recordId.value
}

async function saveDraft(): Promise<void> {
  if (!state.value) return
  const snap = captureView()
  busy.value = 'save'
  error.value = ''
  try {
    const data = await api.saveReviewDraft(snap.project, snap.record, {
      boxes: boxes.value.map(toPayloadBox),
      x_boxes: xBoxes.value.map(toPayloadBox),
      note: note.value,
      base_version: draftBase.value,
    })
    if (!viewStillCurrent(snap)) return
    state.value = data.state
    setBaseline()
    clearRecoveryForCurrent()
    notice.value = '草稿已保存'
  } catch (exc) {
    if (!viewStillCurrent(snap)) return
    const code = errorCode(exc)
    error.value = formatError(exc)
    if (code === 'REVIEW_CONFLICT') notice.value = '草稿已被其他页面更新；可点“刷新”载入最新版本后再改。'
  } finally {
    if (viewStillCurrent(snap) && busy.value === 'save') busy.value = ''
  }
}

async function submit(outcome: 'defect' | 'no_defect'): Promise<void> {
  if (!state.value) return
  if (outcome === 'defect' && totalBoxes.value === 0) {
    error.value = '至少在一个模态上画框后才能提交“有缺陷”；无缺陷请用“提交无缺陷”。'
    return
  }
  busy.value = 'submit'
  error.value = ''
  const snap = captureView()
  const nextTarget = nextRecordId.value
  try {
    await api.submitReview(snap.project, snap.record, {
      boxes: outcome === 'defect' ? boxes.value.map(toPayloadBox) : [],
      x_boxes: outcome === 'defect' ? xBoxes.value.map(toPayloadBox) : [],
      outcome,
      note: note.value,
      base_version: submitBase.value,
      request_key: `review-ui:${snap.record}:${Date.now()}`,
      session_id: sessionId.value,
    })
    // 提交已真实落库；但用户若已切到别的记录，只丢弃旧 UI 回填，不能覆盖 B 的画布/提示。
    if (!viewStillCurrent(snap)) return
    notice.value = outcome === 'defect' ? '已提交人工结论：有缺陷' : '已提交人工结论：无缺陷'
    await load()
    if (recordId.value !== snap.record || projectId.value !== snap.project) return
    if (queueView.value && nextTarget) {
      void router.replace({ name: 'review', params: { projectId: projectId.value, recordId: nextTarget }, query: route.query })
    }
  } catch (exc) {
    if (!viewStillCurrent(snap)) return
    error.value = formatError(exc)
  } finally {
    if (viewStillCurrent(snap) && busy.value === 'submit') busy.value = ''
  }
}

async function reject(): Promise<void> {
  if (!state.value) return
  const reason = window.prompt('驳回原因（可留空，会写入版本记录）', '')
  if (reason === null) return
  const snap = captureView()
  // 和提交一样固定“本次点击时的下一项”：驳回成功后才推进；失败/冲突不跳。
  const nextTarget = nextRecordId.value
  const inQueue = Boolean(queueView.value)
  busy.value = 'reject'
  error.value = ''
  try {
    await api.rejectReview(snap.project, snap.record, {
      reason,
      base_version: submitBase.value,
      request_key: `review-reject:${snap.record}:${Date.now()}`,
      session_id: sessionId.value,
    })
    if (!viewStillCurrent(snap)) return
    if (inQueue && nextTarget) {
      notice.value = '已驳回，进入下一项'
      pendingNotice.value = notice.value
      await load()
      // load() 会推进加载代次，不能再用旧 snap 判断；只用路由是否仍停在本条记录，
      // 防止驳回期间用户已手动切走时把页面强行拉回队列。
      if (recordId.value !== snap.record || projectId.value !== snap.project) return
      void router.replace({ name: 'review', params: { projectId: projectId.value, recordId: nextTarget }, query: route.query })
      return
    }
    notice.value = inQueue ? '已驳回，本轮队列已处理完' : '已驳回，等待重新打开'
    await load()
  } catch (exc) {
    if (!viewStillCurrent(snap)) return
    error.value = formatError(exc)
  } finally {
    if (viewStillCurrent(snap) && busy.value === 'reject') busy.value = ''
  }
}

async function reopen(): Promise<void> {
  const snap = captureView()
  busy.value = 'reopen'
  error.value = ''
  try {
    await api.reopenReview(snap.project, snap.record)
    if (!viewStillCurrent(snap)) return
    notice.value = '已重新打开，可继续编辑'
    await load()
  } catch (exc) {
    if (!viewStillCurrent(snap)) return
    error.value = formatError(exc)
  } finally {
    if (viewStillCurrent(snap) && busy.value === 'reopen') busy.value = ''
  }
}

// ------------------------------------------------------------------ 远端草稿轮询

let pollTimer: number | null = null

async function pollChanges(): Promise<void> {
  if (!state.value || !recordId.value) return
  if (document.visibilityState === 'hidden') return
  if (busy.value) return
  try {
    // 读“当前最新投影”再比较 (id,status,version)，不依赖秒级时间戳或单调 version。
    const data = await api.getReviewEvents(projectId.value, {
      latest: true,
      record_id: recordId.value,
    })
    const projection = data.projection
    if (!projection) return
    const key = projectionKey(projection)
    // load() 结束时 setBaseline() 已写入当前最新投影的 key；记录最初没有任何版本时
    // 这个 key 是空串。因此这里不能再把“第一次看到投影”当初始化跳过——那会漏掉
    // “记录原本无 review_versions → 助手写下第一条真实草稿”的场景（root 原生
    // 对话反例）。只要和基线不同就同步；dirty 时交给 pendingRemote 处理。
    if (key === loadedProjection.value) return
    if (!dirty.value) {
      await load({ silent: true, preserveViewport: true, message: '助手刚更新了这份草稿，已同步。' })
    } else {
      pendingRemote.value = { version: projection.version, updated_at: projection.updated_at }
    }
  } catch {
    // 轮询失败不打断当前编辑；下一轮再试
  }
}

function keepLocal(): void {
  pendingRemote.value = null
  notice.value = '已保留本地编辑；需要助手版本时点“刷新”或“载入更新”。'
}

async function loadRemote(): Promise<void> {
  // 载入远端版本会覆盖本地未保存编辑：dirty 时复用同一个三选决定，
  // 不另造一套弹窗；取消则写清“已保留本地编辑”。
  if (dirty.value && !(await confirmAndProceed('remote'))) {
    notice.value = '已保留本地编辑；需要助手版本时再点“载入更新”。'
    return
  }
  pendingRemote.value = null
  await load({ preserveViewport: true, message: '已载入最新草稿（保留当前缩放/平移）。' })
}

// ------------------------------------------------------------------ 画布视图

function setPane(pane: PaneKey, element: unknown): void {
  const node = element instanceof HTMLElement ? element : null
  panes[pane] = node
  if (node) measure(pane)
}

function measure(pane: PaneKey): void {
  const element = panes[pane]
  if (!element) return
  paneSize[pane] = { width: element.clientWidth, height: element.clientHeight }
}

function measureAll(): void {
  for (const pane of PANE_KEYS) measure(pane)
}

function fit(pane: PaneKey): void {
  const imageSize = imageOf(pane)
  const container = paneSize[pane]
  if (!imageSize.width || !imageSize.height) return
  views[pane] = fitView(imageSize, container, 16)
}

function resetView(pane: PaneKey): void {
  views[pane] = defaultView()
}

function zoomBy(pane: PaneKey, factor: number): void {
  const container = paneSize[pane]
  views[pane] = zoomAt(views[pane], factor, { x: container.width / 2, y: container.height / 2 })
}

function percent(pane: PaneKey): number {
  return Math.round(views[pane].scale * 100)
}

function onImageLoad(pane: PaneKey, event: Event): void {
  const target = event.target as HTMLImageElement
  paneImageError[pane] = ''
  const declared = imageOf(pane)
  if (!declared.width || !declared.height) {
    paneImage[pane] = { width: target.naturalWidth, height: target.naturalHeight }
  } else {
    paneImage[pane] = { ...declared }
  }
  measure(pane)
  if (pendingFit[pane]) {
    fit(pane)
    pendingFit[pane] = false
  }
}

function onWheel(pane: PaneKey, event: WheelEvent): void {
  const element = panes[pane]
  if (!element) return
  const rect = element.getBoundingClientRect()
  const pointer = { x: event.clientX - rect.left, y: event.clientY - rect.top }
  views[pane] = zoomAt(views[pane], event.deltaY < 0 ? 1.12 : 1 / 1.12, pointer)
}

function imageStyle(pane: PaneKey): Record<string, string> {
  const view = views[pane]
  const size = paneImage[pane].width ? paneImage[pane] : imageOf(pane)
  return {
    left: `${view.offsetX}px`,
    top: `${view.offsetY}px`,
    width: `${Math.max(1, size.width * view.scale)}px`,
    height: `${Math.max(1, size.height * view.scale)}px`,
  }
}

function boxStyle(pane: PaneKey, box: Box | null | undefined): Record<string, string> {
  const view = views[pane]
  if (!box) return {}
  const [x1, y1, x2, y2] = box.bbox
  return {
    left: `${x1 * view.scale + view.offsetX}px`,
    top: `${y1 * view.scale + view.offsetY}px`,
    width: `${Math.max(1, (x2 - x1) * view.scale)}px`,
    height: `${Math.max(1, (y2 - y1) * view.scale)}px`,
  }
}

function handleStyle(pane: PaneKey, box: Box, handle: Handle): Record<string, string> {
  // 手柄在 .box 内部（.box 已由 boxStyle 定位到画布绝对位置），因此这里只能用
  // 框自身的相对坐标：西/北边 = 0，东/南边 = 100%。早期版本又加了一次
  // offsetX/offsetY 与 scale，导致四个手柄被推到画布右下角。
  // bbox 仅用于保持签名与调试，不参与定位。
  void pane
  void box
  return {
    left: handle.includes('w') ? '0%' : '100%',
    top: handle.includes('n') ? '0%' : '100%',
  }
}

function localPoint(pane: PaneKey, event: PointerEvent): { x: number; y: number } {
  const element = panes[pane]
  if (!element) return { x: 0, y: 0 }
  const rect = element.getBoundingClientRect()
  return { x: event.clientX - rect.left, y: event.clientY - rect.top }
}

// ------------------------------------------------------------------ 框编辑

function cloneBoxes(list: Box[]): Box[] {
  return list.map((box) => ({ ...box, bbox: [...box.bbox] as [number, number, number, number] }))
}

function pushUndo(pane: PaneKey): void {
  undoStack[pane].push(cloneBoxes(boxesOf(pane)))
  if (undoStack[pane].length > 60) undoStack[pane].shift()
}

function undo(pane: PaneKey): void {
  const previous = undoStack[pane].pop()
  if (!previous) return
  setBoxes(pane, previous)
  selected[pane] = -1
}

function setTool(pane: PaneKey, next: Tool): void {
  tool[pane] = next
  activePane.value = pane
}

function onDown(pane: PaneKey, event: PointerEvent): void {
  if (event.button !== 0) return
  const element = panes[pane]
  if (!element) return
  activePane.value = pane
  if (drag.value) return
  const point = localPoint(pane, event)
  const imagePoint = toImage(point, views[pane])
  const intent = beginDrag({
    tool: tool[pane],
    editable: editable.value,
    boxes: boxesOf(pane),
    selected: selected[pane],
    point: imagePoint,
    scale: views[pane].scale,
  })
  if (intent.mode === 'move' || intent.mode === 'resize') selected[pane] = intent.index
  if (intent.mode === 'draw') selected[pane] = -1
  drag.value = {
    pane,
    mode: intent.mode,
    index: intent.index,
    handle: intent.handle,
    origin: imagePoint,
    snapshot: intent.mode === 'pan' ? null : cloneBoxes(boxesOf(pane)),
    current: intent.mode === 'draw' ? imagePoint : null,
    lastScreen: point,
    undoPushed: false,
  }
  try {
    element.setPointerCapture(event.pointerId)
  } catch {
    // 某些合成事件没有 pointerId，继续用普通事件即可
  }
}

function onBoxDown(pane: PaneKey, event: PointerEvent, index: number): void {
  selected[pane] = index
  activePane.value = pane
  onDown(pane, event)
}

function onMove(event: PointerEvent): void {
  const current = drag.value
  if (!current) return
  const pane = current.pane
  const point = localPoint(pane, event)
  if (current.mode === 'pan') {
    views[pane] = panBy(views[pane], point.x - current.lastScreen.x, point.y - current.lastScreen.y)
    current.lastScreen = point
    return
  }
  const imagePoint = clampPoint(toImage(point, views[pane]), imageOf(pane))
  if (current.mode === 'draw') {
    current.current = imagePoint
    return
  }
  if (!current.snapshot) return
  const next = dragBoxes({
    mode: current.mode,
    index: current.index,
    handle: current.handle,
    origin: current.origin,
    snapshot: current.snapshot,
    point: imagePoint,
    image: imageOf(pane),
  })
  if (!next) return
  if (!current.undoPushed) {
    pushUndo(pane)
    current.undoPushed = true
  }
  setBoxes(pane, next)
}

function onUp(event: PointerEvent): void {
  const current = drag.value
  if (!current) return
  drag.value = null
  const pane = current.pane
  const point = clampPoint(toImage(localPoint(pane, event), views[pane]), imageOf(pane))
  if (current.mode === 'draw') {
    const first = classOptions.value[0]
    const result = endDrag({
      mode: 'draw',
      origin: current.origin,
      point,
      snapshot: current.snapshot,
      boxes: boxesOf(pane),
      image: imageOf(pane),
      classId: first ? first.index : 0,
    })
    if (result.changed) {
      pushUndo(pane)
      setBoxes(pane, result.boxes)
      if (result.added >= 0) selected[pane] = result.added
    }
    current.current = null
    return
  }
  if (current.mode === 'move' || current.mode === 'resize') {
    if (!current.undoPushed && current.snapshot) {
      const same = JSON.stringify(current.snapshot) === JSON.stringify(boxesOf(pane))
      if (!same) pushUndo(pane)
    }
  }
  current.current = null
}

function draftingBox(pane: PaneKey): Box | null {
  const current = drag.value
  if (!current || current.pane !== pane || current.mode !== 'draw' || !current.current) return null
  const { x: x1, y: y1 } = current.origin
  const { x: x2, y: y2 } = current.current
  return {
    class_id: classOptions.value[0]?.index ?? 0,
    bbox: [Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)],
  }
}

function addBox(pane: PaneKey): void {
  if (!editable.value) return
  const size = imageOf(pane)
  if (!size.width || !size.height) return
  const width = Math.max(12, size.width * 0.18)
  const height = Math.max(12, size.height * 0.18)
  const x1 = (size.width - width) / 2
  const y1 = (size.height - height) / 2
  pushUndo(pane)
  const list = boxesOf(pane)
  list.push({
    class_id: classOptions.value[0]?.index ?? 0,
    bbox: [x1, y1, x1 + width, y1 + height],
    severity: 'medium',
  })
  selected[pane] = list.length - 1
  activePane.value = pane
}

function removeBox(pane: PaneKey): void {
  if (!editable.value) return
  const index = selected[pane]
  if (index < 0) return
  pushUndo(pane)
  setBoxes(pane, boxesOf(pane).filter((_, i) => i !== index))
  selected[pane] = -1
}

function setClass(pane: PaneKey, value: string): void {
  const box = currentBox(pane)
  if (!box) return
  const next = Number(value)
  if (box.class_id === next) return
  // 只改属性的编辑也要进 undo：否则用户改类别/严重度后点撤销，会跳过这次属性
  // 修改去撤销更早的框编辑。值没真正变化时不入栈，避免出现无效撤销步。
  pushUndo(pane)
  box.class_id = next
}

function setSeverity(pane: PaneKey, value: string): void {
  const box = currentBox(pane)
  if (!box) return
  if (box.severity === value) return
  pushUndo(pane)
  box.severity = value
}

function coordinateText(box: Box | null | undefined): string {
  return box ? box.bbox.map((value) => Math.round(value)).join(', ') : ''
}

function onClassChange(pane: PaneKey, event: Event): void {
  setClass(pane, (event.target as HTMLSelectElement).value)
}

function onSeverityChange(pane: PaneKey, event: Event): void {
  setSeverity(pane, (event.target as HTMLSelectElement).value)
}

function onKey(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null
  if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
  if (event.key === 'Escape') {
    selected[activePane.value] = -1
    return
  }
  const pane = activePane.value
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault()
    undo(pane)
    return
  }
  if ((event.key === 'Delete' || event.key === 'Backspace') && selected[pane] >= 0) {
    event.preventDefault()
    removeBox(pane)
  }
}

// ------------------------------------------------------------------ 生命周期

function onResize(): void {
  measureAll()
}

function onVisibility(): void {
  if (document.visibilityState === 'visible') void pollChanges()
}

function onBeforeUnload(event: BeforeUnloadEvent): void {
  if (!dirty.value) return
  event.preventDefault()
  // 现代浏览器只显示通用提示；这是刷新/关页的最后一道防线。
  event.returnValue = ''
}

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('resize', onResize)
  window.addEventListener('beforeunload', onBeforeUnload)
  window.addEventListener('hx17:session-expiring', onSessionExpiring)
  document.addEventListener('visibilitychange', onVisibility)
  await load()
  await loadQueue()
  pollTimer = window.setInterval(() => void pollChanges(), 2500)
})

onBeforeUnmount(() => {
  // 卸载后任何在途详情响应都作废，避免已离开页面还去写状态。
  loadEpoch += 1
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('beforeunload', onBeforeUnload)
  window.removeEventListener('hx17:session-expiring', onSessionExpiring)
  document.removeEventListener('visibilitychange', onVisibility)
  if (pollTimer !== null) window.clearInterval(pollTimer)
})

// 路由内离开/上一项/下一项：dirty 时用一次三选弹窗（保存并离开/放弃修改/继续编辑），
// 而不是连续两次 window.confirm。同组件 params 更新不会触发 onBeforeRouteLeave，
// 所以必须同时覆盖 onBeforeRouteUpdate，否则“下一项”会直接丢未保存修改。
type LeaveChoice = 'save' | 'discard' | 'cancel'
type LeaveIntent = 'leave' | 'refresh' | 'remote'
// 离开页面、刷新、载入远端版本都复用同一个三选弹窗，只是按钮文案不同；
// 避免多个弹窗叠加，也避免刷新按钮绕过未保存修改保护。
const leaveIntent = ref<LeaveIntent>('leave')
const leaveOpen = ref(false)
let leaveChoiceResolver: ((choice: LeaveChoice) => void) | null = null
let leaveChoicePromise: Promise<LeaveChoice> | null = null

function askLeaveChoice(intent: LeaveIntent = 'leave'): Promise<LeaveChoice> {
  leaveIntent.value = intent
  leaveOpen.value = true
  // 复用同一个待决 Promise：等待期间再次触发导航（前进/后退/连点）不能覆盖 resolver。
  if (!leaveChoicePromise) {
    leaveChoicePromise = new Promise((resolve) => {
      leaveChoiceResolver = resolve
    })
  }
  return leaveChoicePromise
}

function resolveLeaveChoice(choice: LeaveChoice): void {
  if (!leaveChoicePromise) return
  leaveOpen.value = false
  leaveChoiceResolver?.(choice)
  leaveChoiceResolver = null
  leaveChoicePromise = null
}

async function confirmAndProceed(intent: LeaveIntent = 'leave'): Promise<boolean> {
  const choice = await askLeaveChoice(intent)
  if (choice === 'cancel') return false
  if (choice === 'save') {
    await saveDraft()
    // 保存失败（含 401 会话失效）时留在本页，绝不返回 true 造成“已保存”误判。
    return !dirty.value
  }
  return true
}

/** 工具栏“刷新”：纯查看直接刷新；有本地修改时复用三选保护，不静默覆盖。 */
async function refresh(): Promise<void> {
  if (busy.value) return
  if (dirty.value && !(await confirmAndProceed('refresh'))) return
  await load({ preserveViewport: true, message: '已刷新为服务端最新版本（保留当前缩放/平移）。' })
}

onBeforeRouteLeave(async () => {
  // 401 已触发会话失效：未保存编辑已在 session-expiring 里同步备份，
  // 必须让统一登录跳转通过，不能再用 dirty 弹窗把用户留在已失效页面。
  if (!auth.isAuthenticated) return true
  if (!dirty.value) return true
  return confirmAndProceed()
})

onBeforeRouteUpdate(async (to) => {
  // 纯 query 变化（view/flow/record_ids）不改变当前记录，不打扰编辑。
  if (!auth.isAuthenticated) return true
  const sameRecord =
    to.params.recordId === route.params.recordId && to.params.projectId === route.params.projectId
  if (sameRecord || !dirty.value) return true
  return confirmAndProceed()
})

function goBack(): void {
  // 从队列进入时，返回应回到同一个队列/筛选，而不是总跳识别记录。
  if (queueView.value) {
    goQueue()
    return
  }
  if (projectId.value) void router.push(`/projects/${projectId.value}/records`)
  else void router.push('/projects')
}

const hintText = computed(() => {
  if (!editable.value) return '已驳回：只能查看与缩放平移；需要重新编辑时点“重新打开”。'
  return '框坐标始终是各自原图像素；滚轮缩放只改视图。保存草稿不会产生人工结论。'
})

const latestVersionText = computed(() => {
  const latest = state.value?.latest
  if (!latest) return '暂无版本'
  const kind = latest.status === 'draft' ? '草稿' : latest.status === 'confirmed' ? '已确认' : '已驳回'
  return `v${latest.version} · ${kind}`
})

const aiReview = computed(() => state.value?.ai_review ?? null)
</script>

<template>
  <main class="page review-page">
    <header class="review-head">
      <div class="review-head-main">
        <h1>人工复核</h1>
        <p class="subtle">
          <template v-if="state">
            {{ state.record.id }} ·
            {{ state.human_status === 'pending' ? '待复核' : state.human_status === 'draft' ? '草稿中' : state.human_status === 'confirmed' ? '已确认' : '已驳回' }}
            · {{ latestVersionText }}
          </template>
        </p>
      </div>
      <div class="review-head-actions">
        <span v-if="state" class="chip">
          参考 {{ primaryLabel }}<template v-if="hasSecondary"> + 辅 {{ secondaryLabel }}</template>
        </span>
        <button v-if="queueView" class="small ghost" :disabled="!prevRecordId" @click="goSibling('prev')">上一项</button>
        <button v-if="queueView" class="small ghost" :disabled="!nextRecordId" @click="goSibling('next')">下一项</button>
        <button v-if="state?.human_status === 'confirmed'" class="small ghost" @click="goReflow">去回流</button>
        <button class="small ghost" :disabled="busy === 'load'" @click="refresh">刷新</button>
        <button class="small ghost" @click="goBack">{{ queueView ? '返回队列' : '返回记录' }}</button>
      </div>
    </header>

    <p v-if="error" class="banner danger">{{ error }}</p>
    <p v-if="notice" class="banner notice-line">{{ notice }}</p>

    <section v-if="recovery" class="banner remote-line recovery-line" role="status">
      <span>检测到登录失效前未保存的框/备注（{{ recovery.data.saved_at }}）。</span>
      <button class="small primary" type="button" @click="restoreRecovery">恢复修改</button>
      <button class="small ghost" type="button" @click="discardRecovery">丢弃</button>
    </section>
    <section v-if="pendingRemote" class="banner remote-line">
      <span>助手已更新这份复核（v{{ pendingRemote.version }}）。</span>
      <span class="remote-actions">
        <button class="small" @click="loadRemote">载入更新</button>
        <button class="small ghost" @click="keepLocal">保留本地编辑</button>
      </span>
    </section>

    <div v-if="state" class="review-stage" :class="{ single: !hasSecondary }">
      <section
        v-for="pane in visiblePanes"
        :key="pane"
        class="pane-card"
        :class="{ active: activePane === pane }"
      >
        <header class="pane-head">
          <div class="pane-title-row">
            <span class="pane-title">{{ paneTitle(pane) }}</span>
            <span class="chip subtle-chip">{{ imageOf(pane).width }}×{{ imageOf(pane).height }}</span>
            <span class="chip subtle-chip">{{ boxesOf(pane).length }} 框</span>
            <span v-if="activePane === pane" class="chip active-chip">正在编辑</span>
          </div>
          <div class="pane-tools">
            <button class="icon-btn" title="缩小" @click="zoomBy(pane, 1 / 1.2)">−</button>
            <span class="zoom-value">{{ percent(pane) }}%</span>
            <button class="icon-btn" title="放大" @click="zoomBy(pane, 1.2)">＋</button>
            <button class="small ghost" @click="fit(pane)">适应</button>
            <button class="small ghost" @click="resetView(pane)">100%</button>
          </div>
        </header>

        <div class="pane-toolbar">
          <div class="seg" role="group" aria-label="画布工具">
            <button :class="{ on: tool[pane] === 'select' }" @click="setTool(pane, 'select')">选择</button>
            <button :class="{ on: tool[pane] === 'pan' }" @click="setTool(pane, 'pan')">平移</button>
            <button :class="{ on: tool[pane] === 'draw' }" @click="setTool(pane, 'draw')">画框</button>
          </div>
          <button class="small ghost" :disabled="!editable" @click="addBox(pane)">加框</button>
          <button class="small ghost" :disabled="!editable || selected[pane] < 0" @click="removeBox(pane)">删除</button>
          <button
            class="small ghost"
            :disabled="!editable || undoStack[pane].length === 0"
            @click="undo(pane)"
          >
            撤销
          </button>
          <span class="tool-hint">{{ toolHint(tool[pane], editable) }}</span>
        </div>

        <div
          :ref="(el) => setPane(pane, el)"
          class="canvas"
          :class="{ grabbing: drag?.mode === 'pan' }"
          @pointerdown="onDown(pane, $event)"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointercancel="onUp"
          @wheel.prevent="onWheel(pane, $event)"
        >
          <img
            v-if="urlOf(pane)"
            :src="urlOf(pane)"
            :style="imageStyle(pane)"
            alt=""
            draggable="false"
            @load="onImageLoad(pane, $event)"
            @error="paneImageError[pane] = '图像加载失败：文件不存在或无法读取，请刷新或检查记录文件。'"
          />
          <div v-if="paneImageError[pane]" class="pane-image-error" role="alert">
            {{ paneImageError[pane] }}
          </div>
          <p v-else-if="!urlOf(pane)" class="pane-empty">这条记录没有该模态图片，只有参考模态可标注。</p>
          <div
            v-for="(box, index) in boxesOf(pane)"
            :key="`${pane}-${index}`"
            class="box"
            :class="{ selected: selected[pane] === index }"
            :style="boxStyle(pane, box)"
            @pointerdown.stop="onBoxDown(pane, $event, index)"
          >
            <span class="box-label">{{ classLabel(box.class_id) }}</span>
            <span
              v-for="handle in HANDLES"
              v-show="selected[pane] === index"
              :key="handle"
              class="handle"
              :style="handleStyle(pane, box, handle)"
            />
          </div>
          <div v-if="draftingBox(pane)" class="box drafting" :style="boxStyle(pane, draftingBox(pane))" />
        </div>

        <div v-if="currentBox(pane)" class="box-editor">
          <label>
            <span>类别</span>
            <select
              aria-label="类别"
              :value="currentBox(pane)?.class_id ?? 0"
              :disabled="!editable"
              @change="onClassChange(pane, $event)"
            >
              <option v-for="option in classOptions" :key="option.index" :value="option.index">
                {{ option.name }}
              </option>
            </select>
          </label>
          <label>
            <span>严重度</span>
            <select
              aria-label="严重度"
              :value="currentBox(pane)?.severity || 'medium'"
              :disabled="!editable"
              @change="onSeverityChange(pane, $event)"
            >
              <option value="low">轻微</option>
              <option value="medium">一般</option>
              <option value="high">严重</option>
            </select>
          </label>
          <span class="coords mono">{{ coordinateText(currentBox(pane)) }}</span>
          <button class="small ghost" @click="selected[pane] = -1">取消选择</button>
        </div>
      </section>
    </div>

    <section v-if="state" class="review-action">
      <label class="field grow">
        <span>备注（只随草稿保存，不产生人工结论）</span>
        <input v-model="note" :disabled="!editable" placeholder="可选：说明缺陷位置或复核依据" />
      </label>
      <div class="action-buttons">
        <button class="small" :disabled="!!busy || !editable" @click="saveDraft">
          {{ busy === 'save' ? '保存中…' : '保存草稿' }}
        </button>
        <button class="small primary" :disabled="!!busy || !editable || totalBoxes === 0" @click="submit('defect')">
          提交有缺陷（{{ totalBoxes }} 框）
        </button>
        <button class="small ghost" :disabled="!!busy || !editable" @click="submit('no_defect')">提交无缺陷</button>
        <button class="small ghost danger" :disabled="!!busy || !editable" @click="reject">驳回</button>
        <button v-if="state.queue === 'rejected'" class="small" :disabled="!!busy" @click="reopen">重新打开</button>
      </div>
      <p class="hint">{{ hintText }}</p>
    </section>

    <details v-if="state" class="meta-details">
      <summary>
        版本与 AI 结论
        <span class="subtle"> · {{ latestVersionText }}<template v-if="state.draft"> · 草稿 v{{ state.draft.version }}</template></span>
      </summary>
      <div class="meta-body">
        <section class="meta-block">
          <h2>AI 辅助结论</h2>
          <p v-if="!aiReview" class="subtle">这条记录还没有 AI 复核结论。</p>
          <template v-else>
            <p class="ai-verdict">
              {{ aiReview.status === 'failed' ? 'AI 复核失败' : aiReview.verdict || '参考结论' }}
              <span v-if="aiReview.confidence !== null" class="subtle">（置信度 {{ Math.round((aiReview.confidence ?? 0) * 100) }}%）</span>
            </p>
            <p v-if="aiReview.summary" class="ai-summary">{{ aiReview.summary }}</p>
            <ul v-if="aiReview.findings?.items?.length" class="ai-findings">
              <li v-for="(item, index) in aiReview.findings.items" :key="index">{{ item.note }}</li>
            </ul>
            <p v-if="aiReview.error" class="subtle">错误：{{ aiReview.error.code }} · {{ aiReview.error.message }}</p>
          </template>
        </section>
        <section v-if="state.image.result_url" class="meta-block">
          <h2>检测结果图</h2>
          <p class="subtle">结果图只用于快速比对，不参与人工结论。</p>
          <a class="result-link" :href="state.image.result_url" target="_blank" rel="noreferrer">下载 / 打开结果图</a>
        </section>
      </div>
    </details>

    <DialogShell title="有未保存的修改" :open="leaveOpen" @close="resolveLeaveChoice('cancel')">
      <p v-if="leaveIntent === 'refresh'">刷新会用服务端最新版本覆盖当前框和备注，要先保存本地草稿吗？</p>
      <p v-else-if="leaveIntent === 'remote'">载入助手更新会覆盖当前框和备注，要先保存本地草稿吗？</p>
      <p v-else>当前复核的框或备注还没有保存。离开前要先保存草稿吗？</p>
      <p class="subtle">保存草稿不会生成人工结论；“放弃修改”只丢弃本次未保存的编辑。</p>
      <template #footer>
        <button class="ghost" type="button" @click="resolveLeaveChoice('cancel')">继续编辑</button>
        <button class="ghost danger-text" type="button" @click="resolveLeaveChoice('discard')">放弃修改</button>
        <button class="primary" type="button" :disabled="!!busy" @click="resolveLeaveChoice('save')">
          {{ leaveIntent === 'refresh' ? '保存并刷新' : leaveIntent === 'remote' ? '保存并载入' : '保存并离开' }}
        </button>
      </template>
    </DialogShell>
  </main>
</template>

<style scoped>
.review-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 1180px;
}

.review-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.review-head-main h1 {
  margin: 0 0 4px;
  font-size: 19px;
  font-weight: 650;
}

.review-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.chip {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel);
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.subtle-chip {
  height: 20px;
  padding: 0 7px;
  font-size: 11px;
}

.active-chip {
  height: 20px;
  padding: 0 7px;
  font-size: 11px;
  border-color: var(--accent);
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, transparent);
}

.review-stage {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  align-items: stretch;
}

.review-stage.single {
  grid-template-columns: minmax(0, 1fr);
}

.pane-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
  overflow: hidden;
}

.pane-card.active {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
}

.pane-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex-wrap: wrap;
}

.pane-title {
  font-size: 13px;
  font-weight: 600;
}

.pane-tools {
  display: flex;
  align-items: center;
  gap: 4px;
}

.icon-btn {
  width: 26px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
}

.icon-btn:hover {
  background: var(--panel-secondary);
  color: var(--text);
}

.zoom-value {
  min-width: 42px;
  text-align: center;
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.pane-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}

.seg {
  display: inline-flex;
  padding: 2px;
  gap: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--panel-secondary);
}

.seg button {
  height: 22px;
  padding: 0 9px;
  border: 0;
  border-radius: calc(var(--radius-sm) - 1px);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.seg button.on {
  background: var(--panel);
  color: var(--text);
  box-shadow: 0 1px 2px rgb(0 0 0 / 8%);
}

.tool-hint {
  flex: 1;
  min-width: 0;
  color: var(--text-secondary);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.canvas {
  position: relative;
  height: 320px;
  margin: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--panel-secondary);
  overflow: hidden;
  touch-action: none;
  cursor: crosshair;
}

.pane-image-error {
  position: absolute;
  left: 12px;
  right: 12px;
  top: 12px;
  z-index: 3;
  padding: 8px 10px;
  border: 1px solid var(--danger, #b42318);
  border-radius: 8px;
  background: var(--panel);
  color: var(--danger, #b42318);
  font-size: 12px;
  line-height: 1.5;
}

.canvas.grabbing {
  cursor: grabbing;
}

.canvas img {
  position: absolute;
  user-select: none;
  pointer-events: none;
}

.pane-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  margin: 0;
  padding: 16px;
  text-align: center;
  color: var(--text-secondary);
  font-size: 12px;
}

.box {
  position: absolute;
  border: 1.5px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  cursor: move;
}

.box.selected {
  border-color: #e39a2e;
  background: color-mix(in srgb, #e39a2e 10%, transparent);
}

.box.drafting {
  pointer-events: none;
  border-style: dashed;
}

.box-label {
  position: absolute;
  top: -18px;
  left: -1.5px;
  max-width: 160px;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--accent);
  color: #fff;
  font-size: 10px;
  line-height: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.handle {
  position: absolute;
  width: 9px;
  height: 9px;
  margin: -5px 0 0 -5px;
  border: 1.5px solid var(--accent);
  border-radius: 2px;
  background: #fff;
}

.box-editor {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px 8px;
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}

.box-editor label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.box-editor select {
  height: 26px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--panel);
  color: var(--text);
  font-size: 12px;
  padding: 0 6px;
}

.coords {
  font-size: 11px;
  color: var(--text-secondary);
}

.review-action {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
}

.review-action .field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}

.review-action input {
  height: 30px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--panel);
  color: var(--text);
  padding: 0 9px;
  font-size: 13px;
}

.action-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.hint {
  margin: 0;
  color: var(--text-secondary);
  font-size: 11px;
}

.remote-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.remote-actions {
  display: inline-flex;
  gap: 6px;
}

.meta-details {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
  padding: 0 12px;
}

.meta-details summary {
  cursor: pointer;
  padding: 10px 0;
  font-size: 13px;
  font-weight: 600;
}

.meta-body {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 16px;
  padding: 0 0 12px;
}

.meta-block h2 {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 600;
}

.ai-verdict {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 600;
}

.ai-summary {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.ai-findings {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-secondary);
}

.result-link {
  font-size: 12px;
  color: var(--accent);
}

@media (max-width: 1100px) {
  .review-stage {
    grid-template-columns: minmax(0, 1fr);
  }

  .review-stage.single {
    grid-template-columns: minmax(0, 1fr);
  }

  .canvas {
    height: 300px;
  }

  .meta-body {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>