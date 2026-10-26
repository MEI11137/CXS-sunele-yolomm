<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import type { BoundingBox } from '@/types'

const props = defineProps<{
  boxes: BoundingBox[]
  imageUrl: string
  zoom: number
  mode: 'draw' | 'select'
  selectedBoxId: string | null
  /** 新画框使用的类别：由父组件按真实类别表给出，不能写死示例类别 */
  defaultCategory?: string
}>()

const emit = defineEmits<{
  (e: 'update:boxes', boxes: BoundingBox[]): void
  (e: 'select', id: string | null): void
  (e: 'draw', box: BoundingBox): void
}>()

const canvasEl = ref<HTMLCanvasElement | null>(null)
const wrapper = ref<HTMLDivElement | null>(null)
const img = ref<HTMLImageElement | null>(null)
const imageLoaded = ref(false)
const imgNatural = ref({ w: 0, h: 0 })

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
  const maxW = wrapper.value?.clientWidth ?? 800
  const scale = Math.min(maxW / imgNatural.value.w, 1)
  return { w: imgNatural.value.w * scale, h: imgNatural.value.h * scale }
})

// 实际缩放
const effZoom = computed(() => props.zoom)

function loadImage() {
  const el = new Image()
  el.onload = () => {
    img.value = el
    imgNatural.value = { w: el.naturalWidth, h: el.naturalHeight }
    imageLoaded.value = true
    draw()
  }
  el.src = props.imageUrl
}

function canvasPos(e: MouseEvent) {
  const rect = canvasEl.value!.getBoundingClientRect()
  return {
    x: (e.clientX - rect.left) / rect.width,
    y: (e.clientY - rect.top) / rect.height,
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

function onMouseDown(e: MouseEvent) {
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
      draggingBox.value = hit.id
      dragOriginal.value = { ...hit }
      dragOffset.value = { x: p.x - hit.x, y: p.y - hit.y }
      emit('select', hit.id)
    } else {
      emit('select', null)
    }
  }
}

function onMouseMove(e: MouseEvent) {
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

function onMouseUp() {
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
}

function clamp(v: number, min: number, max: number) {
  return Math.max(min, Math.min(v, max))
}

watch(() => props.imageUrl, () => {
  imageLoaded.value = false
  loadImage()
})

watch(() => props.boxes, () => draw(), { deep: true })
watch(() => props.zoom, () => draw())
watch(() => props.selectedBoxId, () => draw())

onMounted(() => {
  loadImage()
  window.addEventListener('mouseup', onMouseUp)
})
onBeforeUnmount(() => {
  window.removeEventListener('mouseup', onMouseUp)
})
</script>

<template>
  <div ref="wrapper" class="anno-wrapper" :style="{ transform: `scale(${effZoom})`, transformOrigin: 'top left' }">
    <canvas
      ref="canvasEl"
      class="anno-canvas"
      :style="{ cursor: mode === 'draw' ? 'crosshair' : 'move' }"
      @mousedown="onMouseDown"
      @mousemove="onMouseMove"
    ></canvas>
  </div>
</template>

<style scoped>
.anno-wrapper {
  width: 100%;
  transition: transform 0.15s ease;
}
.anno-canvas {
  display: block;
  max-width: 100%;
  border-radius: 4px;
}
</style>
