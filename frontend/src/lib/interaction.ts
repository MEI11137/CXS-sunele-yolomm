/**
 * 复核画布的指针交互判定（纯函数，便于用真实序列做单测）。
 *
 * 这里只决定「这次按下意味着什么」和「移动/松开后框变成什么」，不碰 DOM、不改视图缩放：
 *
 * * 工具是显式的（选择/移动 · 平移 · 画框），**不由“当前有没有选中框”暗中决定**；
 * * 空白拖拽默认平移，只有画框工具才会新建框；
 * * 角点缩放与框移动都带 `index`，命中谁就动谁，绝不出现“命中了手柄却没有目标”的分支；
 * * 坐标永远是原图像素，视图缩放只用来把屏幕阈值（手柄 8px、命中 4px）换算成图像距离。
 */

// 带 .ts 后缀：Node 的 --experimental-strip-types 需要显式扩展名，
// tsconfig 已开 allowImportingTsExtensions，Vite 也照常解析。
import { boxFromDrag, hitHandle, hitTest, resizeBox } from './geometry.ts'
import type { Box, Handle } from './geometry.ts'

/** 标注工具：显式切换，默认 `select`（空白拖拽平移）。 */
export type Tool = 'select' | 'pan' | 'draw'
/** 一次拖拽的意图；空串表示没有按下。 */
export type DragMode = '' | 'pan' | 'move' | 'resize' | 'draw'

export interface DragIntent {
  mode: DragMode
  /** 目标框索引；`pan` / `draw` 为 -1。resize 与 move 必须是有效索引。 */
  index: number
  handle: Handle | null
}

export interface BeginInput {
  tool: Tool
  editable: boolean
  boxes: Box[]
  selected: number
  /** 已换算成原图像素的按下位置 */
  point: { x: number; y: number }
  /** 当前面板缩放（图像像素 → 屏幕像素） */
  scale: number
}

/**
 * 按下时决定拖拽意图。
 *
 * 顺序是刻意的：先认框（命中谁就动谁），再在“命中选中框”时优先认角点。
 * 早期版本在角点分支里漏了 `index`，移动阶段因 `index < 0` 整段跳过 —— 这里把
 * “resize 一定带 index”变成返回值的一部分，调用方无法再漏。
 */
export function beginDrag(input: BeginInput): DragIntent {
  const { tool, editable, boxes, selected, point, scale } = input
  // 不可编辑（已驳回）时连选中框都不该动：只有平移。
  if (!editable) return { mode: 'pan', index: -1, handle: null }
  if (tool === 'pan') return { mode: 'pan', index: -1, handle: null }
  if (tool === 'draw') return { mode: 'draw', index: -1, handle: null }

  const hit = hitTest(boxes, point, scale)
  if (hit >= 0 && hit === selected) {
    const handle = hitHandle(boxes[hit], point, scale)
    if (handle) return { mode: 'resize', index: hit, handle }
  }
  if (hit >= 0) return { mode: 'move', index: hit, handle: null }
  if (selected >= 0 && boxes[selected]) {
    const handle = hitHandle(boxes[selected], point, scale)
    if (handle) return { mode: 'resize', index: selected, handle }
  }
  // 空白处一律平移；新建框只由画框工具触发。
  return { mode: 'pan', index: -1, handle: null }
}

export interface MoveInput {
  mode: DragMode
  index: number
  handle: Handle | null
  /** 按下时的图像坐标 */
  origin: { x: number; y: number }
  /** 按下时的整份快照：每帧都从快照算，避免逐帧累积误差 */
  snapshot: Box[] | null
  point: { x: number; y: number }
  image: { width: number; height: number }
}

/** 拖动中重算框；返回 null 表示这次移动不改变任何框（调用方可原样跳过）。 */
export function dragBoxes(input: MoveInput): Box[] | null {
  const { mode, index, handle, origin, snapshot, point, image } = input
  if (index < 0 || !snapshot || !snapshot[index]) return null
  if (mode !== 'move' && mode !== 'resize') return null
  if (mode === 'resize' && !handle) return null

  const base = snapshot[index]
  let next: Box
  if (mode === 'move') {
    const [x1, y1, x2, y2] = base.bbox
    const width = x2 - x1
    const height = y2 - y1
    const dx = point.x - origin.x
    const dy = point.y - origin.y
    const nx = Math.min(Math.max(0, x1 + dx), Math.max(0, image.width - width))
    const ny = Math.min(Math.max(0, y1 + dy), Math.max(0, image.height - height))
    next = { ...base, bbox: [nx, ny, nx + width, ny + height] }
  } else {
    next = resizeBox(base, handle as Handle, point, image)
  }
  const result = snapshot.map((item, position) =>
    position === index ? { ...next, bbox: [...next.bbox] as Box['bbox'] } : item,
  )
  return result
}

export interface EndInput {
  mode: DragMode
  origin: { x: number; y: number }
  point: { x: number; y: number }
  snapshot: Box[] | null
  boxes: Box[]
  image: { width: number; height: number }
  classId: number
}

export interface EndResult {
  boxes: Box[]
  /** 是否需要压一条撤销记录；没有任何实际变化时不压，避免 Ctrl+Z 空转 */
  changed: boolean
  /** 新建框的索引；没有新建为 -1 */
  added: number
}

/** 松开时收尾：画框落地、移动/缩放判定是否真的变了。 */
export function endDrag(input: EndInput): EndResult {
  const { mode, origin, point, snapshot, boxes, image, classId } = input
  if (mode === 'draw') {
    const box = boxFromDrag(origin, point, image, classId)
    if (!box) return { boxes, changed: false, added: -1 }
    const next = [...boxes, box]
    return { boxes: next, changed: true, added: next.length - 1 }
  }
  if (!snapshot) return { boxes, changed: false, added: -1 }
  return { boxes, changed: !sameBoxes(snapshot, boxes), added: -1 }
}

/** 结构相等：bbox 与类别/严重程度都比，位置一样但改了类别也算变化。 */
export function sameBoxes(left: Box[], right: Box[]): boolean {
  if (left.length !== right.length) return false
  for (let index = 0; index < left.length; index += 1) {
    const a = left[index]
    const b = right[index]
    if (a.class_id !== b.class_id) return false
    if ((a.severity ?? '') !== (b.severity ?? '')) return false
    for (let axis = 0; axis < 4; axis += 1) {
      if (Math.abs(a.bbox[axis] - b.bbox[axis]) > 1e-6) return false
    }
  }
  return true
}

/** 工具悬浮提示：把“当前按下会发生什么”说清楚，避免靠猜。 */
export function toolHint(tool: Tool, editable: boolean): string {
  if (!editable) return '已驳回：只能查看与缩放平移'
  if (tool === 'pan') return '平移：拖拽移动视图（不会改框）'
  if (tool === 'draw') return '画框：拖拽新建一个框；改框请切到选择/移动'
  return '选择/移动：拖框移动、拖角点缩放、空白拖拽平移'
}
