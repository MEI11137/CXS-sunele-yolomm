<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const recordId = computed(() => String(route.params.recordId ?? ''))
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
const pendingRemote = ref<{ version: number; updated_at: string } | null>(null)
/** 每张画布自己的图片加载错误：404/坏图要落在对应画布里说清楚，不能只给空白。 */
const paneImageError = reactive<Record<PaneKey, string>>({ primary: '', secondary: '' })
const loadedProjection = ref('')
const loadedSignature = ref('')
const activePane = ref<PaneKey>('primary')

const selected = reactive<Record<PaneKey, number>>({ primary: -1, secondary: -1 })
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
  if (!options.silent) busy.value = 'load'
  try {
    const sameRecord = state.value?.record?.id === recordId.value
    const keepViewport = Boolean(options.preserveViewport) && sameRecord && state.value !== null
    const data = await api.getReviewState(projectId.value, recordId.value)
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
    measureAll()
    if (!keepViewport) {
      fit('primary')
      if (hasSecondary.value) fit('secondary')
      // 图片可能还没 decode 完；onImageLoad 会再补齐一次 fit。
    }
    setBaseline()
  } catch (exc) {
    error.value = [errorCode(exc), errorMessage(exc)].filter(Boolean).join('：')
  } finally {
    if (busy.value === 'load') busy.value = ''
  }
}

async function saveDraft(): Promise<void> {
  if (!state.value) return
  busy.value = 'save'
  error.value = ''
  try {
    const data = await api.saveReviewDraft(projectId.value, recordId.value, {
      boxes: boxes.value.map(toPayloadBox),
      x_boxes: xBoxes.value.map(toPayloadBox),
      note: note.value,
      base_version: draftBase.value,
    })
    state.value = data.state
    setBaseline()
    notice.value = '草稿已保存'
  } catch (exc) {
    const code = errorCode(exc)
    error.value = [code, errorMessage(exc)].filter(Boolean).join('：')
    if (code === 'REVIEW_CONFLICT') notice.value = '草稿已被其他页面更新；可点“刷新”载入最新版本后再改。'
  } finally {
    busy.value = ''
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
  try {
    await api.submitReview(projectId.value, recordId.value, {
      boxes: outcome === 'defect' ? boxes.value.map(toPayloadBox) : [],
      x_boxes: outcome === 'defect' ? xBoxes.value.map(toPayloadBox) : [],
      outcome,
      note: note.value,
      base_version: submitBase.value,
      request_key: `review-ui:${recordId.value}:${Date.now()}`,
      session_id: sessionId.value,
    })
    notice.value = outcome === 'defect' ? '已提交人工结论：有缺陷' : '已提交人工结论：无缺陷'
    await load()
  } catch (exc) {
    error.value = [errorCode(exc), errorMessage(exc)].filter(Boolean).join('：')
  } finally {
    busy.value = ''
  }
}

async function reject(): Promise<void> {
  if (!state.value) return
  const reason = window.prompt('驳回原因（可留空，会写入版本记录）', '')
  if (reason === null) return
  busy.value = 'reject'
  error.value = ''
  try {
    await api.rejectReview(projectId.value, recordId.value, {
      reason,
      base_version: submitBase.value,
      request_key: `review-reject:${recordId.value}:${Date.now()}`,
      session_id: sessionId.value,
    })
    notice.value = '已驳回，等待重新打开'
    await load()
  } catch (exc) {
    error.value = [errorCode(exc), errorMessage(exc)].filter(Boolean).join('：')
  } finally {
    busy.value = ''
  }
}

async function reopen(): Promise<void> {
  busy.value = 'reopen'
  error.value = ''
  try {
    await api.reopenReview(projectId.value, recordId.value)
    notice.value = '已重新打开，可继续编辑'
    await load()
  } catch (exc) {
    error.value = [errorCode(exc), errorMessage(exc)].filter(Boolean).join('：')
  } finally {
    busy.value = ''
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

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibility)
  await load()
  pollTimer = window.setInterval(() => void pollChanges(), 2500)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('visibilitychange', onVisibility)
  if (pollTimer !== null) window.clearInterval(pollTimer)
})

function goBack(): void {
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
        <button class="small ghost" :disabled="busy === 'load'" @click="load()">刷新</button>
        <button class="small ghost" @click="goBack">返回记录</button>
      </div>
    </header>

    <p v-if="error" class="banner danger">{{ error }}</p>
    <p v-if="notice" class="banner notice-line">{{ notice }}</p>

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