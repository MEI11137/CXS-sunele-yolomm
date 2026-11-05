<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorCode, errorMessage } from '@/api/http'
import { toPayloadBox } from '@/lib/geometry'
import type { Box, Handle, Viewport } from '@/lib/geometry'
import { draftBaseVersion, preferredBoxes, submitBaseVersion } from '@/lib/review'
import { clampPoint, fitView, panBy, toImage, zoomAt } from '@/lib/geometry'
import { beginDrag, dragBoxes, endDrag, toolHint } from '@/lib/interaction'
import type { Tool } from '@/lib/interaction'
import { useWorkspaceStore } from '@/stores/workspace'
import type { ReviewClassOption, ReviewState } from '@/types'

/**
 * 人工复核页。
 *
 * * 两张图各自独立缩放/平移：滚轮以指针为中心，拖拽平移，可适应窗口或按比例按钮；
 * * 框坐标永远是原图像素，缩放与滚动只改视图，不改 bbox；
 * * 草稿与提交分离：保存草稿不会产生人工结论；确认/驳回才写版本。
 */

const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const recordId = computed(() => String(route.params.recordId ?? ''))
const sessionId = computed(() => workspace.currentSessionId || undefined)

const state = ref<ReviewState | null>(null)
const boxes = ref<Box[]>([])
const selected = ref(-1)
const note = ref('')
const busy = ref('')
const error = ref('')
const notice = ref('')
const undoStack = ref<Box[][]>([])

type PaneKey = 'rgb' | 'x' | 'result'
const defaultView = (): Viewport => ({ scale: 1, offsetX: 0, offsetY: 0 })
const views = reactive<Record<PaneKey, Viewport>>({
  rgb: defaultView(),
  x: defaultView(),
  result: defaultView(),
})
const panes = reactive<Record<PaneKey, HTMLElement | null>>({ rgb: null, x: null, result: null })
const paneSize = reactive<Record<PaneKey, { width: number; height: number }>>({
  rgb: { width: 0, height: 0 },
  x: { width: 0, height: 0 },
  result: { width: 0, height: 0 },
})
const drag = reactive({
  mode: '' as '' | 'pan' | 'move' | 'resize' | 'draw',
  pane: 'rgb' as PaneKey,
  handle: null as Handle | null,
  start: { x: 0, y: 0 },
  origin: { x: 0, y: 0 },
  index: -1,
  snapshot: null as Box[] | null,
  current: { x: 0, y: 0 },
  moved: false,
})

// 工具是显式的：默认「选择/移动」（空白拖拽 = 平移），画框必须主动切过去。
// 旧版靠“有没有选中框”决定空白拖拽是画框还是平移，用户根本没法预期。
const tool = ref<Tool>('select')

const image = computed(() => state.value?.image ?? { width: 0, height: 0, rgb_url: '', x_url: null })
const options = computed<ReviewClassOption[]>(() => state.value?.class_options ?? [])
const editable = computed(() =>
  Boolean(state.value) && state.value?.latest_submitted?.status !== 'rejected',
)
const hintText = computed(() => toolHint(tool.value, editable.value))
const baseVersion = computed(() => submitBaseVersion(state.value))
// 草稿保存的并发基线是“最近一条版本”（草稿也算，每存一次前进一版）；
// 提交/驳回的基线仍是“最近一次已提交版本”，两者不能混用，否则会误判冲突。
const draftBase = computed(() => draftBaseVersion(state.value))
const classSource = computed(() => (options.value[0]?.source === 'dataset' ? '数据集类别' : '模型自带类别'))
const hasX = computed(() => Boolean(state.value?.image.x_url))

function setPane(pane: PaneKey, element: HTMLElement | null): void {
  panes[pane] = element
  if (element) {
    const rect = element.getBoundingClientRect()
    paneSize[pane] = { width: rect.width, height: rect.height }
  }
}

function fit(pane: PaneKey): void {
  if (!paneSize[pane].width) {
    const element = panes[pane]
    if (element) {
      const rect = element.getBoundingClientRect()
      paneSize[pane] = { width: rect.width, height: rect.height }
    }
  }
  views[pane] = fitView(image.value, paneSize[pane])
}

function zoomBy(pane: PaneKey, factor: number): void {
  const size = paneSize[pane]
  views[pane] = zoomAt(views[pane], factor, { x: size.width / 2, y: size.height / 2 })
}

function reset(pane: PaneKey): void {
  views[pane] = { scale: 1, offsetX: 0, offsetY: 0 }
}

function percent(pane: PaneKey): string {
  return `${Math.round(views[pane].scale * 100)}%`
}

function localPoint(pane: PaneKey, event: PointerEvent | WheelEvent): { x: number; y: number } {
  const element = panes[pane] ?? (event.currentTarget as HTMLElement)
  const rect = element.getBoundingClientRect()
  return { x: event.clientX - rect.left, y: event.clientY - rect.top }
}

function onWheel(pane: PaneKey, event: WheelEvent): void {
  event.preventDefault()
  const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12
  views[pane] = zoomAt(views[pane], factor, localPoint(pane, event))
}

/** 只有主键、且没有正在进行的拖拽才起手；否则会把拖拽拆成两段。 */
function onDown(event: PointerEvent, forcedIndex = -1): void {
  if (!state.value || drag.mode) return
  if (event.button !== 0) return
  event.preventDefault()
  const pane: PaneKey = 'rgb'
  // 指针捕获挂在**画布**上而不是框元素上：拖动中框的 DOM 可能被补丁替换，
  // 捕获在框上会丢事件；画布是稳定的，移动/松开一定还能收到。
  panes[pane]?.setPointerCapture(event.pointerId)
  if (forcedIndex >= 0) selected.value = forcedIndex

  const local = localPoint(pane, event)
  const raw = toImage(local, views[pane])
  const point = clampPoint(raw, image.value)
  drag.start = local
  drag.origin = point
  drag.pane = pane
  drag.current = point
  drag.moved = false
  drag.snapshot = boxes.value.map((item) => ({ ...item, bbox: [...item.bbox] as Box['bbox'] }))

  const intent = beginDrag({
    tool: tool.value,
    editable: editable.value,
    boxes: boxes.value,
    selected: selected.value,
    point,
    scale: views[pane].scale,
  })
  drag.mode = intent.mode
  drag.index = intent.index
  drag.handle = intent.handle
}

function onMove(event: PointerEvent): void {
  if (!drag.mode) return
  const local = localPoint('rgb', event)
  if (drag.mode === 'pan') {
    const dx = local.x - drag.start.x
    const dy = local.y - drag.start.y
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true
    views.rgb = panBy(views.rgb, dx, dy)
    drag.start = local
    return
  }
  const point = clampPoint(toImage(local, views.rgb), image.value)
  drag.current = point
  if (Math.abs(point.x - drag.origin.x) + Math.abs(point.y - drag.origin.y) > 0.5) drag.moved = true
  const next = dragBoxes({
    mode: drag.mode,
    index: drag.index,
    handle: drag.handle,
    origin: drag.origin,
    snapshot: drag.snapshot,
    point,
    image: image.value,
  })
  if (next) boxes.value = next
}

function onUp(): void {
  const mode = drag.mode
  const snapshot = drag.snapshot
  const point = drag.current
  if (mode === 'draw' || mode === 'move' || mode === 'resize') {
    const ended = endDrag({
      mode,
      origin: drag.origin,
      point,
      snapshot,
      boxes: boxes.value,
      image: image.value,
      classId: options.value[0]?.index ?? 0,
    })
    if (ended.changed) {
      pushHistory(snapshot)
      boxes.value = ended.boxes
      if (ended.added >= 0) selected.value = ended.added
    }
  } else if (mode === 'pan' && !drag.moved && tool.value === 'select') {
    // 空白处单击（没有拖动）= 取消选中；拖动才是平移，两者不混。
    selected.value = -1
  }
  drag.mode = ''
  drag.handle = null
  drag.index = -1
  drag.snapshot = null
  drag.moved = false
}

function pushHistory(snapshot: Box[] | null): void {
  if (!snapshot) return
  undoStack.value = [...undoStack.value.slice(-30), snapshot]
}

function undo(): void {
  const last = undoStack.value.pop()
  if (!last) return
  boxes.value = last.map((item) => ({ ...item, bbox: [...item.bbox] as Box['bbox'] }))
  selected.value = -1
}

function addBox(): void {
  if (!editable.value || !image.value.width) return
  pushHistory(boxes.value.map((item) => ({ ...item, bbox: [...item.bbox] as Box['bbox'] })))
  const width = image.value.width
  const height = image.value.height
  boxes.value = [
    ...boxes.value,
    {
      class_id: options.value[0]?.index ?? 0,
      bbox: [width * 0.3, height * 0.3, width * 0.6, height * 0.6],
      severity: 'medium',
      source: 'manual',
    },
  ]
  selected.value = boxes.value.length - 1
  // 新建后回到“选择/移动”，让角点手柄立刻可用；工具状态在工具条上可见。
  tool.value = 'select'
}

function removeBox(index = selected.value): void {
  if (!editable.value || index < 0) return
  pushHistory(boxes.value.map((item) => ({ ...item, bbox: [...item.bbox] as Box['bbox'] })))
  boxes.value = boxes.value.filter((_, position) => position !== index)
  selected.value = -1
}

function onKey(event: KeyboardEvent): void {
  if (!editable.value) return
  const tag = (event.target as HTMLElement | null)?.tagName ?? ''
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return
  if (event.key === 'Delete' || event.key === 'Backspace') {
    event.preventDefault()
    removeBox()
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault()
    undo()
  }
  if (event.key === 'Escape') {
    // 画框工具下按 Esc 退回选择/移动，避免“以为在改框、实际在画新框”。
    if (tool.value === 'draw') tool.value = 'select'
    selected.value = -1
  }
}

function boxStyle(box: Box): Record<string, string> {
  return {
    left: `${box.bbox[0]}px`,
    top: `${box.bbox[1]}px`,
    width: `${box.bbox[2] - box.bbox[0]}px`,
    height: `${box.bbox[3] - box.bbox[1]}px`,
    outlineWidth: `${Math.max(1, 2 / views.rgb.scale)}px`,
    fontSize: `${Math.max(9, 12 / views.rgb.scale)}px`,
    '--handle-size': `${8 / views.rgb.scale}px`,
  }
}

function startPan(pane: PaneKey, event: PointerEvent): void {
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  drag.mode = 'pan'
  drag.pane = pane
  drag.start = localPoint(pane, event)
}

function movePan(pane: PaneKey, event: PointerEvent): void {
  if (drag.mode !== 'pan' || drag.pane !== pane) return
  const local = localPoint(pane, event)
  views[pane] = panBy(views[pane], local.x - drag.start.x, local.y - drag.start.y)
  drag.start = local
}

function endPan(): void {
  drag.mode = ''
  drag.pane = 'rgb'
}

async function load(): Promise<void> {
  busy.value = 'load'
  error.value = ''
  try {
    const data = await api.getReviewState(projectId.value, recordId.value)
    // 契约：GET /review 与草稿/重开一样返回 {state: {...}}。形状不对就明确报错，
    // 不能让 TypeError 被 catch 成一条看不懂的提示、再继续渲染空页面。
    if (!data || !data.state || !data.state.record) {
      throw new Error('复核接口返回结构不符合预期（缺少 state.record）；请刷新后重试。')
    }
    state.value = data.state
    // 用**版本是否存在**决定框来源，不能用 boxes.length：
    // 有效草稿可以是空数组（用户删光了所有框），已确认无缺陷也必须是空的。
    const source = preferredBoxes(data.state)
    boxes.value = (source ?? []).map((item) => ({
      class_id: item.class_id,
      bbox: [...item.bbox] as Box['bbox'],
      severity: (item as unknown as { severity?: string }).severity ?? 'medium',
      source: (item as unknown as { source?: string }).source ?? 'model',
    }))
    note.value = data.state.draft?.note ?? ''
    selected.value = -1
    undoStack.value = []
    requestAnimationFrame(() => {
      fit('rgb')
      if (hasX.value) fit('x')
      fit('result')
    })
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    busy.value = ''
  }
}

function payload() {
  return boxes.value.map(toPayloadBox)
}

async function saveDraft(): Promise<void> {
  if (!state.value) return
  busy.value = 'draft'
  error.value = ''
  notice.value = ''
  try {
    const data = await api.saveReviewDraft(projectId.value, recordId.value, {
      boxes: payload(),
      note: note.value,
      base_version: draftBase.value,
    })
    state.value = data.state
    notice.value = '草稿已保存；它还不是人工结论。'
  } catch (failure) {
    if (errorCode(failure) === 'REVIEW_CONFLICT') {
      error.value = '另一个页面的草稿更新，本次保存没有写入；页面上的编辑还在，请刷新对照后再保存。'
    } else {
      error.value = errorMessage(failure)
    }
  } finally {
    busy.value = ''
  }
}

async function submit(outcome: 'defect' | 'no_defect'): Promise<void> {
  if (!state.value) return
  if (outcome === 'defect' && boxes.value.length === 0) {
    error.value = '确认有缺陷时至少需要一个框；如果想表达“无缺陷”，用另一个按钮。'
    return
  }
  busy.value = 'submit'
  error.value = ''
  try {
    const data = await api.submitReview(projectId.value, recordId.value, {
      boxes: outcome === 'no_defect' ? [] : payload(),
      outcome,
      note: note.value,
      base_version: baseVersion.value,
      request_key: `review:${recordId.value}:${baseVersion.value + 1}:${Date.now()}`,
      session_id: sessionId.value,
    })
    notice.value = `已提交第 ${data.version.version} 版人工结论`
    await load()
  } catch (failure) {
    if (errorCode(failure) === 'REVIEW_CONFLICT') {
      error.value = '这条记录已有更新的版本，本次没有提交；当前编辑仍保留在页面上，请刷新对照后再提交。'
    } else {
      error.value = errorMessage(failure)
    }
  } finally {
    busy.value = ''
  }
}

async function reject(): Promise<void> {
  if (!state.value) return
  busy.value = 'reject'
  error.value = ''
  try {
    await api.rejectReview(projectId.value, recordId.value, {
      reason: note.value,
      base_version: baseVersion.value,
      request_key: `reject:${recordId.value}:${baseVersion.value + 1}:${Date.now()}`,
      session_id: sessionId.value,
    })
    notice.value = '已驳回；这条记录已离开待复核队列，需要时可显式重开。'
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    busy.value = ''
  }
}

async function reopen(): Promise<void> {
  if (!state.value) return
  busy.value = 'reopen'
  error.value = ''
  try {
    const data = await api.reopenReview(projectId.value, recordId.value)
    state.value = data.state
    notice.value = '已重开为草稿，可以继续编辑。'
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    busy.value = ''
  }
}

function backToRecords(): void {
  void router.push({ name: 'records', params: { projectId: projectId.value } })
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  void load()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <section class="page review-page">
    <header class="page-header">
      <div>
        <h1>人工复核</h1>
        <p class="subtitle">
          记录 <span class="mono">{{ recordId }}</span>
          <span v-if="state" class="hint">
            · 队列：{{ state.queue === 'pending' ? '待复核' : state.queue === 'draft' ? '有草稿' : state.queue === 'rejected' ? '已驳回' : '已确认' }}
            · 已确认版本 v{{ baseVersion }}（不是待处理张数）
            <span v-if="state?.draft"> · 草稿修订 v{{ state.draft.version }}</span>
            · 类别来自{{ classSource }}
            <span v-if="state?.ai_review"> · AI 建议：{{ state.ai_review.verdict || '无结论' }}（仅供参考）</span>
          </span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" @click="backToRecords">返回记录</button>
        <button class="ghost small" type="button" :disabled="busy !== ''" @click="load">刷新</button>
      </div>
    </header>

    <p v-if="error" class="banner banner-error" role="alert">{{ error }}</p>
    <p v-else-if="notice" class="banner banner-ok" role="status">{{ notice }}</p>

    <div class="review-grid">
      <div class="review-pane">
        <div class="pane-head">
          <span class="pane-title">可见光 RGB</span>
          <span class="hint">{{ hintText }} · 滚轮缩放 · Del 删除 · Ctrl+Z 撤销</span>
          <div class="pane-tools">
            <button class="ghost small" type="button" @click="zoomBy('rgb', 1 / 1.2)">−</button>
            <span class="mono zoom-label">{{ percent('rgb') }}</span>
            <button class="ghost small" type="button" @click="zoomBy('rgb', 1.2)">＋</button>
            <button class="ghost small" type="button" @click="fit('rgb')">适应窗口</button>
            <button class="ghost small" type="button" @click="reset('rgb')">100%</button>
          </div>
        </div>
        <div
          :ref="(element) => setPane('rgb', element as HTMLElement | null)"
          class="canvas"
          :class="[`tool-${tool}`, { dragging: drag.mode !== '' }]"
          @wheel="onWheel('rgb', $event)"
          @pointerdown="onDown($event)"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointercancel="onUp"
        >
          <div
            v-if="state"
            class="canvas-layer"
            :style="{ transform: `translate(${views.rgb.offsetX}px, ${views.rgb.offsetY}px) scale(${views.rgb.scale})` }"
          >
            <img
              :src="state.image.rgb_url"
              :width="image.width || undefined"
              :height="image.height || undefined"
              alt="RGB 原图"
              draggable="false"
              @load="fit('rgb')"
            />
            <div
              v-for="(box, index) in boxes"
              :key="index"
              class="review-box"
              :class="{ selected: index === selected }"
              :style="boxStyle(box)"
              @pointerdown.stop="onDown($event, index)"
            >
              <span class="review-box-label">
                {{ options.find((item) => item.index === box.class_id)?.name ?? box.class_id }}
              </span>
              <span
                v-for="handle in (['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'] as const)"
                v-show="index === selected"
                :key="handle"
                class="resize-handle"
                :class="`handle-${handle}`"
              />
            </div>
            <div
              v-if="drag.mode === 'draw'"
              class="review-box drafting"
              :style="{
                left: `${Math.min(drag.origin.x, drag.current.x)}px`,
                top: `${Math.min(drag.origin.y, drag.current.y)}px`,
                width: `${Math.abs(drag.current.x - drag.origin.x)}px`,
                height: `${Math.abs(drag.current.y - drag.origin.y)}px`,
                outlineWidth: `${Math.max(1, 2 / views.rgb.scale)}px`,
              }"
            />
          </div>
          <p v-else-if="error" class="empty-hint">加载失败：{{ error }}</p>
          <p v-else class="empty-hint">正在加载记录…</p>
        </div>
      </div>

      <div class="review-pane">
        <div class="pane-head">
          <span class="pane-title">{{ hasX ? state?.image.x_modality || '配套图' : '配套图' }}</span>
          <div class="pane-tools">
            <button class="ghost small" type="button" @click="zoomBy('x', 1 / 1.2)">−</button>
            <span class="mono zoom-label">{{ percent('x') }}</span>
            <button class="ghost small" type="button" @click="zoomBy('x', 1.2)">＋</button>
            <button class="ghost small" type="button" @click="fit('x')">适应窗口</button>
            <button class="ghost small" type="button" @click="reset('x')">100%</button>
          </div>
        </div>
        <div
          :ref="(element) => setPane('x', element as HTMLElement | null)"
          class="canvas"
          @wheel="onWheel('x', $event)"
          @pointerdown="startPan('x', $event)"
          @pointermove="movePan('x', $event)"
          @pointerup="endPan"
          @pointercancel="endPan"
        >
          <div
            v-if="state && state.image.x_url"
            class="canvas-layer"
            :style="{ transform: `translate(${views.x.offsetX}px, ${views.x.offsetY}px) scale(${views.x.scale})` }"
          >
            <img
              :src="state.image.x_url"
              :width="image.width || undefined"
              :height="image.height || undefined"
              alt="配套图"
              draggable="false"
              @load="fit('x')"
            />
          </div>
          <p v-else-if="!state" class="empty-hint">正在加载记录…</p>
          <p v-else class="empty-hint">这条记录没有配套图（单模态，或上传时未提供）</p>
        </div>
      </div>

      <div class="review-pane">
        <div class="pane-head">
          <span class="pane-title">检测结果图</span>
          <div class="pane-tools">
            <button class="ghost small" type="button" @click="zoomBy('result', 1 / 1.2)">−</button>
            <span class="mono zoom-label">{{ percent('result') }}</span>
            <button class="ghost small" type="button" @click="zoomBy('result', 1.2)">＋</button>
            <button class="ghost small" type="button" @click="fit('result')">适应窗口</button>
            <button class="ghost small" type="button" @click="reset('result')">100%</button>
          </div>
        </div>
        <div
          :ref="(element) => setPane('result', element as HTMLElement | null)"
          class="canvas"
          @wheel="onWheel('result', $event)"
          @pointerdown="startPan('result', $event)"
          @pointermove="movePan('result', $event)"
          @pointerup="endPan"
          @pointercancel="endPan"
        >
          <div
            v-if="state && state.image.result_url"
            class="canvas-layer"
            :style="{ transform: `translate(${views.result.offsetX}px, ${views.result.offsetY}px) scale(${views.result.scale})` }"
          >
            <img
              :src="state.image.result_url"
              :width="image.width || undefined"
              :height="image.height || undefined"
              alt="检测结果图"
              draggable="false"
              @load="fit('result')"
            />
          </div>
          <p v-else-if="!state" class="empty-hint">正在加载记录…</p>
          <p v-else class="empty-hint">这条记录没有结果图（检测失败或旧记录）；RGB 原图在上方，不会被当成结果图</p>
        </div>
      </div>
    </div>

    <div class="card review-editor">
      <div class="tool-bar" role="toolbar" aria-label="标注工具">
        <button
          class="ghost small tool-btn"
          :class="{ selected: tool === 'select' }"
          type="button"
          data-tool="select"
          :aria-pressed="tool === 'select'"
          :disabled="!editable"
          @click="tool = 'select'"
        >
          选择/移动
        </button>
        <button
          class="ghost small tool-btn"
          :class="{ selected: tool === 'pan' }"
          type="button"
          data-tool="pan"
          :aria-pressed="tool === 'pan'"
          @click="tool = 'pan'"
        >
          平移
        </button>
        <button
          class="ghost small tool-btn"
          :class="{ selected: tool === 'draw' }"
          type="button"
          data-tool="draw"
          :aria-pressed="tool === 'draw'"
          :disabled="!editable"
          @click="tool = 'draw'"
        >
          画框
        </button>
        <span class="hint tool-hint">{{ hintText }}</span>
      </div>

      <div class="inline-form">
        <button class="ghost small" type="button" :disabled="!editable" @click="addBox">加一个框</button>
        <button class="ghost small" type="button" :disabled="!editable || selected < 0" @click="removeBox()">删除选中</button>
        <button class="ghost small" type="button" :disabled="!undoStack.length" @click="undo">撤销</button>
        <span class="hint">共 {{ boxes.length }} 个框 · 像素坐标，缩放不影响数值</span>
      </div>

      <div v-if="selected >= 0" class="form-grid">
        <label class="field">
          <span>类别（{{ classSource }}）</span>
          <select v-model.number="boxes[selected].class_id">
            <option v-for="option in options" :key="option.index" :value="option.index">
              {{ option.name }}（索引 {{ option.index }}）
            </option>
          </select>
        </label>
        <label class="field">
          <span>严重程度</span>
          <select v-model="boxes[selected].severity">
            <option value="low">轻微</option>
            <option value="medium">一般</option>
            <option value="high">严重</option>
          </select>
        </label>
        <p class="hint mono">
          [[{{ Math.round(boxes[selected].bbox[0]) }}, {{ Math.round(boxes[selected].bbox[1]) }}],
          [{{ Math.round(boxes[selected].bbox[2]) }}, {{ Math.round(boxes[selected].bbox[3]) }}]]
        </p>
      </div>

      <label class="field">
        <span>复核备注（可选）</span>
        <input v-model="note" maxlength="1000" placeholder="例如：边框附近反光，确认为误检" />
      </label>

      <div class="form-actions">
        <button class="primary small" type="button" :disabled="busy !== '' || !editable" @click="saveDraft">
          {{ busy === 'draft' ? '保存中…' : '保存草稿' }}
        </button>
        <button class="primary small" type="button" :disabled="busy !== '' || !editable" @click="submit('defect')">
          确认有缺陷
        </button>
        <button class="ghost small" type="button" :disabled="busy !== '' || !editable" @click="submit('no_defect')">
          确认无缺陷
        </button>
        <button class="ghost small" type="button" :disabled="busy !== '' || !editable" @click="reject">驳回</button>
        <button
          v-if="state?.latest_rejected"
          class="ghost small"
          type="button"
          :disabled="busy !== ''"
          @click="reopen"
        >
          重开为草稿
        </button>
      </div>
      <p class="hint">
        提交/驳回只能由你本人在此页面完成；助手可以读状态、解释 AI 建议、按口述改草稿，但不能替你确认。
      </p>
      <p v-if="state?.versions.length" class="hint">
        历史：{{ state.versions.map((item) => `v${item.version} ${item.status}`).join(' · ') }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.review-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.review-pane {
  min-width: 0;
}
.pane-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.pane-title {
  font-weight: 600;
}
.pane-tools {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 4px;
}
.zoom-label {
  min-width: 44px;
  text-align: center;
  font-size: 12px;
}
.canvas {
  position: relative;
  height: 420px;
  overflow: hidden;
  border: 1px solid var(--border, #2a2a2a);
  border-radius: 8px;
  background: #0d0d0d;
  cursor: grab;
  touch-action: none;
}
/* 光标就是“当前工具”的第一反馈：选择=默认箭头、平移=抓手、画框=十字。 */
.canvas.tool-select {
  cursor: default;
}
.canvas.tool-pan,
.canvas.tool-pan .review-box {
  cursor: grab;
}
.canvas.tool-pan.dragging,
.canvas.tool-pan.dragging .review-box {
  cursor: grabbing;
}
.canvas.tool-draw {
  cursor: crosshair;
}
.canvas.tool-draw .review-box {
  cursor: crosshair;
}
.tool-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.tool-hint {
  margin-left: 4px;
}
.canvas-layer {
  position: absolute;
  left: 0;
  top: 0;
  transform-origin: 0 0;
  will-change: transform;
}
.canvas img {
  display: block;
  user-select: none;
  pointer-events: none;
}
.review-box {
  position: absolute;
  outline: 2px solid #ff5a5a;
  outline-offset: 0;
  background: rgba(255, 90, 90, 0.08);
  cursor: move;
}
.review-box.selected {
  outline-color: #ffd166;
  background: rgba(255, 209, 102, 0.15);
}
.review-box.drafting {
  outline-style: dashed;
  pointer-events: none;
}
.resize-handle {
  position: absolute;
  width: var(--handle-size, 8px);
  height: var(--handle-size, 8px);
  background: #ffd166;
  border: 1px solid #171717;
  pointer-events: none;
}
.handle-nw { left: -4px; top: -4px; }
.handle-n { left: 50%; top: -4px; transform: translateX(-50%); }
.handle-ne { right: -4px; top: -4px; }
.handle-e { right: -4px; top: 50%; transform: translateY(-50%); }
.handle-se { right: -4px; bottom: -4px; }
.handle-s { left: 50%; bottom: -4px; transform: translateX(-50%); }
.handle-sw { left: -4px; bottom: -4px; }
.handle-w { left: -4px; top: 50%; transform: translateY(-50%); }
.review-box-label {
  position: absolute;
  left: 0;
  top: -1.4em;
  white-space: nowrap;
  background: rgba(0, 0, 0, 0.72);
  color: #fff;
  padding: 0 4px;
  border-radius: 3px;
}
.review-editor {
  margin-top: 12px;
}
@media (max-width: 1400px) {
  .review-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 900px) {
  .review-grid {
    grid-template-columns: 1fr;
  }
}
</style>
