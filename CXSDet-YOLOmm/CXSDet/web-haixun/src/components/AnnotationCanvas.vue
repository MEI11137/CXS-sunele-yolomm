<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import type { BoundingBox } from '@/types'

const props = defineProps<{
  boxes: BoundingBox[]
  imageUrl: string
  zoom: number
  mode: 'draw' | 'select' | 'pan'
  selectedBoxId: string | null
  /** 新画框使用的类别：由父组件按真实类别表给出，不能写死示例类别 */
  defaultCategory?: string
}>()

const emit = defineEmits<{
  (e: 'update:boxes', boxes: BoundingBox[]): void
  (e: 'select', id: string | null): void
  (e: 'draw', box: BoundingBox): void
  (e: 'interaction-start'): void
  (e: 'update:zoom', zoom: number): void
}>()

const canvasEl = ref<HTMLCanvasElement | null>(null)
const wrapper = ref<HTMLDivElement | null>(null)
const img = ref<HTMLImageElement | null>(null)
const imageLoaded = ref(false)
const imgNatural = ref({ w: 0, h: 0 })
const viewport = ref({ w: 800, h: 600 })
const spaceHeld = ref(false)
const hovered = ref(false)
const panning = ref(false)
let panStart = { x: 0, y: 0, left: 0, top: 0 }
let resizeObserver: ResizeObserver | undefined
let imageGeneration = 0
let wheelAnchor: { x: number; y: number; imageX: number; imageY: number } | null = null

// 拖拽绘制状态
const drawing = ref(false)
const dragStart = ref({ x: 0, y: 0 })
const dragCurrent = ref({ x: 0, y: 0 })

// 选择拖动状态
const draggingBox = ref<string | null>(null)
const dragOffset = ref({ x: 0, y: 0 })
const dragOriginal = ref<BoundingBox | null>(null)

// 画布显示尺寸
const display = computed(() => {
  if (!imgNatural.value.w || !imgNatural.value.h) return { w: 0, h: 0 }
  const scale = Math.min(viewport.value.w / imgNatural.value.w, viewport.value.h / imgNatural.value.h, 1)
  return { w: Math.max(1, Math.round(imgNatural.value.w * scale * props.zoom)), h: Math.max(1, Math.round(imgNatural.value.h * scale * props.zoom)) }
})

// 实际缩放
const cursor = computed(() => panning.value ? 'grabbing' : props.mode === 'pan' || spaceHeld.value ? 'grab' : props.mode === 'draw' ? 'crosshair' : 'default')

function loadImage() {
  const generation = ++imageGeneration
  const el = new Image()
  el.onload = () => {
    if (generation !== imageGeneration) return
    img.value = el
    imgNatural.value = { w: el.naturalWidth, h: el.naturalHeight }
    imageLoaded.value = true
    if (wrapper.value) { wrapper.value.scrollLeft = 0; wrapper.value.scrollTop = 0 }
    void nextTick(draw)
  }
  el.src = props.imageUrl
}

function canvasPos(e: PointerEvent) {
  const rect = canvasEl.value!.getBoundingClientRect()
  return {
    x: clamp((e.clientX - rect.left) / rect.width, 0, 1),
    y: clamp((e.clientY - rect.top) / rect.height, 0, 1),
  }
}

function draw() {
  const canvas = canvasEl.value
  if (!canvas || !img.value) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const d = display.value
  canvas.width = d.w
  canvas.height = d.h
  ctx.clearRect(0, 0, d.w, d.h)
  ctx.drawImage(img.value, 0, 0, d.w, d.h)

  // 绘制框
  for (const b of props.boxes) {
    const x = b.x * d.w
    const y = b.y * d.h
    const w = b.w * d.w
    const h = b.h * d.h
    const selected = b.id === props.selectedBoxId
    ctx.strokeStyle = selected ? '#1d5fbf' : severityColor(b.severity)
    ctx.lineWidth = selected ? 2.5 : 2
    ctx.strokeRect(x, y, w, h)
    // 标签
    ctx.fillStyle = selected ? '#1d5fbf' : severityColor(b.severity)
    ctx.fillRect(x, y - 18, labelWidth(b), 18)
    ctx.fillStyle = '#fff'
    ctx.font = '11px sans-serif'
    ctx.fillText(label(b), x + 4, y - 5)
  }

  // 绘制中虚线
  if (drawing.value) {
    const x = Math.min(dragStart.value.x, dragCurrent.value.x)
    const y = Math.min(dragStart.value.y, dragCurrent.value.y)
    const w = Math.abs(dragCurrent.value.x - dragStart.value.x)
    const h = Math.abs(dragCurrent.value.y - dragStart.value.y)
    ctx.strokeStyle = '#1d5fbf'
    ctx.lineWidth = 1.5
    ctx.setLineDash([4, 4])
    ctx.strokeRect(x * d.w, y * d.h, w * d.w, h * d.h)
    ctx.setLineDash([])
  }
}

function severityColor(s: string) {
  return s === 'high' ? '#b42318' : s === 'medium' ? '#8a4b0e' : '#1e7d46'
}

function label(b: BoundingBox) {
  const name = b.label || b.category || '未分类'
  return `${name}·${b.severity === 'high' ? '高' : b.severity === 'medium' ? '中' : '低'}`
}

function labelWidth(b: BoundingBox) {
  return label(b).length * 7 + 8
}

function onPointerDown(e: PointerEvent) {
  if (e.button !== 0 && e.button !== 1) return
  e.preventDefault()
  wrapper.value?.focus({ preventScroll: true })
  canvasEl.value?.setPointerCapture(e.pointerId)
  if (props.mode === 'pan' || spaceHeld.value || e.button === 1) { startPan(e); return }
  if (props.mode === 'draw') {
    drawing.value = true
    const p = canvasPos(e)
    dragStart.value = p
    dragCurrent.value = p
  } else {
    // select 模式：命中检测
    const p = canvasPos(e)
    const hit = props.boxes.find((b) => {
      return p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h
    })
    if (hit) {
      emit('interaction-start')
      draggingBox.value = hit.id
      dragOriginal.value = { ...hit }
      dragOffset.value = { x: p.x - hit.x, y: p.y - hit.y }
      emit('select', hit.id)
    } else {
      emit('select', null)
      if (props.zoom > 1) startPan(e)
    }
  }
}

function startPan(e: PointerEvent) {
  if (!wrapper.value) return
  panning.value = true
  panStart = { x: e.clientX, y: e.clientY, left: wrapper.value.scrollLeft, top: wrapper.value.scrollTop }
}

function onPointerMove(e: PointerEvent) {
  if (panning.value && wrapper.value) {
    wrapper.value.scrollLeft = panStart.left - (e.clientX - panStart.x)
    wrapper.value.scrollTop = panStart.top - (e.clientY - panStart.y)
    return
  }
  const p = canvasPos(e)
  if (drawing.value) {
    dragCurrent.value = p
    draw()
  } else if (draggingBox.value && dragOriginal.value) {
    const boxes = props.boxes.map((b) => {
      if (b.id !== draggingBox.value) return b
      const nx = clamp(p.x - dragOffset.value.x, 0, 1 - b.w)
      const ny = clamp(p.y - dragOffset.value.y, 0, 1 - b.h)
      return { ...b, x: nx, y: ny }
    })
    emit('update:boxes', boxes)
  }
}

function onPointerUp(e: PointerEvent) {
  if (e.type === 'pointercancel') drawing.value = false
  if (drawing.value) {
    const x1 = dragStart.value.x
    const y1 = dragStart.value.y
    const x2 = dragCurrent.value.x
    const y2 = dragCurrent.value.y
    const w = Math.abs(x2 - x1)
    const h = Math.abs(y2 - y1)
    if (w > 0.01 && h > 0.01) {
      const box: BoundingBox = {
        id: 'box-' + Date.now(),
        x: clamp(Math.min(x1, x2), 0, 1 - w),
        y: clamp(Math.min(y1, y2), 0, 1 - h),
        w: clamp(w, 0, 1),
        h: clamp(h, 0, 1),
        category: props.defaultCategory || '',
        severity: 'medium',
      }
      emit('draw', box)
    }
    drawing.value = false
    draw()
  }
  draggingBox.value = null
  dragOriginal.value = null
  panning.value = false
  if (canvasEl.value?.hasPointerCapture(e.pointerId)) canvasEl.value.releasePointerCapture(e.pointerId)
}

function keyDown(e: KeyboardEvent) {
  const focused = document.activeElement as HTMLElement | null
  if (focused?.matches('input, textarea, select, [contenteditable="true"]')) return
  if (e.code === 'Space' && (hovered.value || wrapper.value?.contains(focused))) { e.preventDefault(); spaceHeld.value = true }
}
function keyUp(e: KeyboardEvent) { if (e.code === 'Space') spaceHeld.value = false }
function resetKeys() { spaceHeld.value = false; panning.value = false }

function onWheel(event: WheelEvent) {
  const el = wrapper.value
  if (!el || !imageLoaded.value || drawing.value || draggingBox.value || panning.value || !event.deltaY) return
  const zoom = Math.round(clamp(props.zoom + (event.deltaY < 0 ? .1 : -.1), .5, 3) * 100) / 100
  if (zoom === props.zoom) return
  const rect = el.getBoundingClientRect()
  const x = event.clientX - rect.left - el.clientLeft
  const y = event.clientY - rect.top - el.clientTop
  wheelAnchor = { x, y, imageX: (el.scrollLeft + x) / display.value.w, imageY: (el.scrollTop + y) / display.value.h }
  emit('update:zoom', zoom)
}

defineExpose({ focusBox: (box: BoundingBox) => {
  const el = wrapper.value
  if (el) { el.scrollLeft = (box.x + box.w / 2) * display.value.w - el.clientWidth / 2; el.scrollTop = (box.y + box.h / 2) * display.value.h - el.clientHeight / 2 }
} })

function clamp(v: number, min: number, max: number) {
  return Math.max(min, Math.min(v, max))
}

watch(() => props.imageUrl, () => {
  imageLoaded.value = false
  loadImage()
})

watch(() => props.boxes, () => draw(), { deep: true })
watch(() => props.zoom, (zoom, previous) => {
  const el = wrapper.value
  const anchor = wheelAnchor
  wheelAnchor = null
  const x = el ? (el.scrollLeft + el.clientWidth / 2) / previous : 0
  const y = el ? (el.scrollTop + el.clientHeight / 2) / previous : 0
  void nextTick(() => { draw(); if (el) {
    el.scrollLeft = anchor ? anchor.imageX * display.value.w - anchor.x : x * zoom - el.clientWidth / 2
    el.scrollTop = anchor ? anchor.imageY * display.value.h - anchor.y : y * zoom - el.clientHeight / 2
  } })
})
watch(display, () => void nextTick(draw))
watch(() => props.selectedBoxId, () => draw())

onMounted(() => {
  loadImage()
  resizeObserver = new ResizeObserver(() => {
    if (wrapper.value) viewport.value = { w: wrapper.value.clientWidth, h: wrapper.value.clientHeight }
  })
  if (wrapper.value) resizeObserver.observe(wrapper.value)
  window.addEventListener('keydown', keyDown)
  window.addEventListener('keyup', keyUp)
  window.addEventListener('blur', resetKeys)
})
onBeforeUnmount(() => {
  imageGeneration++
  resizeObserver?.disconnect()
  window.removeEventListener('keydown', keyDown)
  window.removeEventListener('keyup', keyUp)
  window.removeEventListener('blur', resetKeys)
})
</script>

<template>
  <div ref="wrapper" class="anno-wrapper" tabindex="0" aria-label="标注画布，滚轮缩放；平移模式、空格加拖动或中键移动视野" @wheel.prevent="onWheel" @pointerenter="hovered = true" @pointerleave="hovered = false">
    <canvas
      ref="canvasEl"
      class="anno-canvas"
      :style="{ cursor, width: display.w + 'px', height: display.h + 'px' }"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @auxclick.prevent
    ></canvas>
  </div>
</template>

<style scoped>
.anno-wrapper {
  width: 100%;
  height: clamp(260px, 65vh, 720px);
  overflow: auto;
  overscroll-behavior: contain;
  outline-offset: -2px;
}
.anno-canvas {
  display: block;
  max-width: none;
  touch-action: none;
  user-select: none;
  border-radius: 4px;
}
</style>
