<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MAX_SCALE, MIN_SCALE, fitView, panBy, toImage, zoomAt } from '@/lib/geometry'
import type { Viewport } from '@/lib/geometry'

/**
 * 框内图像查看器：只做「装进框里 + 缩放 + 平移」三件事。
 *
 * 为什么不是直接给 <img> 一个 max-width：检测结果图原始尺寸可能是 4000×3000，
 * 只靠 max-width 会按父容器宽度撑开、高度继续按比例溢出卡片（用户反馈的「图超出 UI」）。
 * 这里把图放进固定高度的画布，用 lib/geometry 的 fitView 在加载后先「适应窗口」，
 * 之后滚轮/按钮缩放、拖动平移，行为与人工复核页的画布一致；只换渲染容器，
 * 不改复核画布、框坐标或任何标注语义。
 *
 * 与人工复核画布的两点差异（都在本组件内，不动全局 geometry）：
 * 1) fitView 的比例会被 MIN_SCALE(0.1) 夹住，4000×3000 的图在窄屏（≈320px 宽）下
 *    需要 ≈0.07 才能整张放进框；被夹到 0.1 就会横向溢出。所以这里在 fitView 之上
 *    补一个「精确适应比例」兜底，并让本组件的缩放下限跟随该比例（低于 0.1 也允许）。
 * 2) 结果图会在同一位置换图（新的检测记录），所以 src 变化时必须显式重置
 *    加载/失败/尺寸/拖动态，否则新图会沿用旧尺寸、或失败后再也不恢复。
 */

const PADDING = 12

const props = withDefaults(defineProps<{ src: string; alt?: string }>(), {
  alt: '检测结果',
})

const box = ref<HTMLElement | null>(null)
const view = ref<Viewport>({ scale: 1, offsetX: 0, offsetY: 0 })
const natural = ref({ width: 0, height: 0 })
const loading = ref(true)
const failed = ref(false)
const dragging = ref(false)
/** 本图在框内的「适应」比例（可能小于 MIN_SCALE，见文件头说明）。 */
const fitScale = ref(1)
/** 用户是否手动缩放过：没动过时容器变化要重新适应，动过则保留用户视角。 */
let touched = false

let dragStart: { x: number; y: number; view: Viewport } | null = null
let observer: ResizeObserver | null = null

const ready = computed(() => !loading.value && !failed.value && natural.value.width > 0)
const percent = computed(() => (ready.value ? Math.round(view.value.scale * 100) : 0))
/** 缩放下限：允许缩到「适应」比例，即使它比全局 MIN_SCALE 更小。 */
const minScale = computed(() => Math.min(MIN_SCALE, fitScale.value || MIN_SCALE))

function containerSize(): { width: number; height: number } {
  const el = box.value
  return el ? { width: el.clientWidth, height: el.clientHeight } : { width: 0, height: 0 }
}

/** 精确适应比例：不做 MIN_SCALE 截断，用于 fitView 被下限抬高时的兜底。 */
function exactFitScale(size: { width: number; height: number }): number {
  if (!natural.value.width || !natural.value.height) return 0
  if (size.width <= 0 || size.height <= 0) return 0
  const scale = Math.min(
    (size.width - PADDING * 2) / natural.value.width,
    (size.height - PADDING * 2) / natural.value.height,
    1,
  )
  return Number.isFinite(scale) && scale > 0 ? scale : 0
}

function fit(): void {
  if (!ready.value) return
  const size = containerSize()
  const exact = exactFitScale(size)
  let next = fitView(natural.value, size, PADDING)
  if (exact > 0 && exact < next.scale - 1e-9) {
    next = {
      scale: exact,
      offsetX: (size.width - natural.value.width * exact) / 2,
      offsetY: (size.height - natural.value.height * exact) / 2,
    }
  }
  view.value = next
  fitScale.value = next.scale
  touched = false
}

function centreAtScale(scale: number): void {
  if (!ready.value) return
  const size = containerSize()
  if (size.width <= 0 || size.height <= 0) return
  const target = Math.min(MAX_SCALE, Math.max(minScale.value, scale))
  view.value = {
    scale: target,
    offsetX: (size.width - natural.value.width * target) / 2,
    offsetY: (size.height - natural.value.height * target) / 2,
  }
  touched = true
}

function onLoad(event: Event): void {
  const img = event.target as HTMLImageElement
  natural.value = { width: img.naturalWidth, height: img.naturalHeight }
  loading.value = false
  failed.value = false
  fit()
}

function onError(): void {
  loading.value = false
  failed.value = true
  natural.value = { width: 0, height: 0 }
  view.value = { scale: 1, offsetX: 0, offsetY: 0 }
  fitScale.value = 1
  touched = false
}

function zoom(factor: number, pointer?: { x: number; y: number }): void {
  if (!ready.value) return
  const size = containerSize()
  const at = pointer ?? { x: size.width / 2, y: size.height / 2 }
  const target = Math.min(MAX_SCALE, Math.max(minScale.value, view.value.scale * factor))
  if (Math.abs(target - view.value.scale) < 1e-9) return
  if (target >= MIN_SCALE) {
    // 常规区间：直接用几何库的指针中心缩放（自带 MIN/MAX 夹取）。
    view.value = zoomAt(view.value, factor, at)
  } else {
    // 适应比例本来就小于 MIN_SCALE：zoomAt 会把它抬回 0.1，表现为「缩小反而变大」，
    // 这里按同一个锚点、同一个目标比例重算，保持连续。
    const anchor = toImage(at, view.value)
    view.value = {
      scale: target,
      offsetX: at.x - anchor.x * target,
      offsetY: at.y - anchor.y * target,
    }
  }
  touched = true
}

function onWheel(event: WheelEvent): void {
  if (!ready.value) return
  const el = box.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  zoom(event.deltaY < 0 ? 1.12 : 1 / 1.12, {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  })
}

function onDown(event: PointerEvent): void {
  if (!ready.value || event.button !== 0) return
  dragging.value = true
  dragStart = { x: event.clientX, y: event.clientY, view: view.value }
  ;(event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId)
}

function onMove(event: PointerEvent): void {
  if (!dragging.value || !dragStart) return
  view.value = panBy(dragStart.view, event.clientX - dragStart.x, event.clientY - dragStart.y)
  touched = true
}

function onUp(event: PointerEvent): void {
  if (!dragging.value) return
  dragging.value = false
  dragStart = null
  const el = event.currentTarget as HTMLElement | null
  if (el?.hasPointerCapture?.(event.pointerId)) el.releasePointerCapture(event.pointerId)
}

// 换图（新记录）时重置全部状态：loading/failed/尺寸/视角/拖动态。
// 模板上的 :key="props.src" 会让 <img> 整棵重建，避免旧元素的 load/error 事件串到新图上。
watch(
  () => props.src,
  () => {
    loading.value = true
    failed.value = false
    natural.value = { width: 0, height: 0 }
    view.value = { scale: 1, offsetX: 0, offsetY: 0 }
    fitScale.value = 1
    touched = false
    dragging.value = false
    dragStart = null
  },
)

onMounted(() => {
  if (typeof ResizeObserver === 'undefined' || !box.value) return
  observer = new ResizeObserver(() => {
    if (ready.value && !touched) fit()
  })
  observer.observe(box.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  dragStart = null
})
</script>

<template>
  <figure class="zoomable">
    <div class="zoomable-tools">
      <button class="zbtn" type="button" title="缩小" :disabled="!ready" @click="zoom(1 / 1.2)">−</button>
      <span class="zvalue">{{ ready ? `${percent}%` : '…' }}</span>
      <button class="zbtn" type="button" title="放大" :disabled="!ready" @click="zoom(1.2)">＋</button>
      <button class="zmini" type="button" :disabled="!ready" @click="fit">适应</button>
      <button class="zmini" type="button" :disabled="!ready" @click="centreAtScale(1)">100%</button>
      <span class="zhint">{{ failed ? '图像不可用' : loading ? '正在加载图片…' : '滚轮缩放 · 拖动平移' }}</span>
    </div>

    <div
      ref="box"
      class="zoomable-box"
      :class="{ grabbing: dragging, 'is-loading': loading, 'is-failed': failed }"
      @pointerdown="onDown"
      @pointermove="onMove"
      @pointerup="onUp"
      @pointercancel="onUp"
      @wheel.prevent="onWheel"
    >
      <img
        v-if="!failed"
        :key="props.src"
        :src="props.src"
        :alt="props.alt"
        draggable="false"
        :style="{
          left: `${view.offsetX}px`,
          top: `${view.offsetY}px`,
          width: natural.width ? `${Math.max(1, natural.width * view.scale)}px` : 'auto',
          height: natural.height ? `${Math.max(1, natural.height * view.scale)}px` : 'auto',
          visibility: loading ? 'hidden' : 'visible',
        }"
        @load="onLoad"
        @error="onError"
      />
      <p v-if="loading && !failed" class="zoomable-note" role="status">正在加载图片…</p>
      <p v-else-if="failed" class="zoomable-note error" role="alert">
        图像加载失败：文件不存在或无法读取。
      </p>
    </div>
  </figure>
</template>

<style scoped>
.zoomable {
  margin: 12px 0 0;
  min-width: 0;
  max-width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.zoomable-tools {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.zbtn {
  width: 26px;
  height: 26px;
  border: 1px solid var(--border, #343434);
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--text, #f1f1f1);
  cursor: pointer;
  line-height: 1;
  font-size: 15px;
}

.zbtn:hover:enabled,
.zmini:hover:enabled {
  border-color: var(--accent, #6da4f5);
  color: var(--accent, #6da4f5);
}

.zbtn:disabled,
.zmini:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.zvalue {
  min-width: 46px;
  text-align: center;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--text-secondary, #b4b4b4);
}

.zmini {
  border: 1px solid var(--border, #343434);
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--text, #f1f1f1);
  cursor: pointer;
  padding: 3px 10px;
  font-size: 12px;
}

.zhint {
  font-size: 12px;
  color: var(--text-secondary, #b4b4b4);
}

.zoomable-box {
  position: relative;
  overflow: hidden;
  height: clamp(260px, 46vh, 520px);
  border: 1px solid var(--border, #343434);
  border-radius: var(--radius-sm, 6px);
  background: var(--panel-secondary, #242424);
  cursor: grab;
  touch-action: none;
}

.zoomable-box.grabbing {
  cursor: grabbing;
}

.zoomable-box.is-loading,
.zoomable-box.is-failed {
  cursor: default;
}

.zoomable-box img {
  position: absolute;
  max-width: none;
  max-height: none;
  user-select: none;
  -webkit-user-drag: none;
}

.zoomable-note {
  position: absolute;
  inset: 0;
  margin: 0;
  display: grid;
  place-items: center;
  padding: 16px;
  text-align: center;
  font-size: 12px;
  color: var(--text-secondary, #b4b4b4);
}

.zoomable-note.error {
  color: var(--danger, #f0817a);
}
</style>
