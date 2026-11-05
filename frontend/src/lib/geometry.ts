/**
 * 复核画布的几何换算（纯函数，便于单测）。
 *
 * 坐标约定：
 * * 业务坐标永远是**原图像素**（x 向右、y 向下，0..width / 0..height）；
 * * 视图状态是「先缩放再平移」，平移量是屏幕像素：screen = image * scale + offset；
 * * 缩放以指针为中心：指针下的图像点在缩放前后必须停在同一个屏幕位置。
 */

export interface Viewport {
  scale: number
  offsetX: number
  offsetY: number
}

export interface Box {
  class_id: number
  bbox: [number, number, number, number]
  severity?: string
  confidence?: number
  source?: string
  class_name?: string
}

export const MIN_SCALE = 0.1
export const MAX_SCALE = 12

export function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return 1
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale))
}

/** 屏幕坐标（相对画布容器）→ 图像像素坐标。 */
export function toImage(
  point: { x: number; y: number },
  view: Viewport,
): { x: number; y: number } {
  return {
    x: (point.x - view.offsetX) / view.scale,
    y: (point.y - view.offsetY) / view.scale,
  }
}

/** 图像像素坐标 → 屏幕坐标。 */
export function toScreen(
  point: { x: number; y: number },
  view: Viewport,
): { x: number; y: number } {
  return {
    x: point.x * view.scale + view.offsetX,
    y: point.y * view.scale + view.offsetY,
  }
}

/** 以指针为中心缩放：指针下的图像点保持不动。 */
export function zoomAt(
  view: Viewport,
  factor: number,
  pointer: { x: number; y: number },
): Viewport {
  const anchor = toImage(pointer, view)
  const scale = clampScale(view.scale * factor)
  return {
    scale,
    offsetX: pointer.x - anchor.x * scale,
    offsetY: pointer.y - anchor.y * scale,
  }
}

/** 适应窗口：整张图放进容器并居中（不放大超过 1，避免小图糊成块）。 */
export function fitView(
  image: { width: number; height: number },
  container: { width: number; height: number },
  padding = 12,
): Viewport {
  if (!image.width || !image.height || !container.width || !container.height) {
    return { scale: 1, offsetX: 0, offsetY: 0 }
  }
  const scale = clampScale(
    Math.min(
      (container.width - padding * 2) / image.width,
      (container.height - padding * 2) / image.height,
      1,
    ),
  )
  return {
    scale,
    offsetX: (container.width - image.width * scale) / 2,
    offsetY: (container.height - image.height * scale) / 2,
  }
}

/** 拖拽平移：直接改 offset。 */
export function panBy(view: Viewport, dx: number, dy: number): Viewport {
  return { ...view, offsetX: view.offsetX + dx, offsetY: view.offsetY + dy }
}

export function clampPoint(
  point: { x: number; y: number },
  image: { width: number; height: number },
): { x: number; y: number } {
  return {
    x: Math.min(image.width, Math.max(0, point.x)),
    y: Math.min(image.height, Math.max(0, point.y)),
  }
}

/** 归一化矩形（0-1），用于画框位置；不改变业务坐标。 */
export function normRect(
  bbox: [number, number, number, number],
  image: { width: number; height: number },
): { left: number; top: number; width: number; height: number } {
  if (!image.width || !image.height) return { left: 0, top: 0, width: 0, height: 0 }
  const [x1, y1, x2, y2] = bbox
  return {
    left: x1 / image.width,
    top: y1 / image.height,
    width: Math.max(0, x2 - x1) / image.width,
    height: Math.max(0, y2 - y1) / image.height,
  }
}

export type Handle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'

/** 命中测试：返回最上面（最后绘制）的框索引，阈值按屏幕像素换算。 */
export function hitTest(
  boxes: Box[],
  point: { x: number; y: number },
  scale: number,
  padding = 4,
): number {
  const slack = padding / Math.max(scale, 0.001)
  for (let index = boxes.length - 1; index >= 0; index -= 1) {
    const [x1, y1, x2, y2] = boxes[index].bbox
    if (
      point.x >= x1 - slack &&
      point.x <= x2 + slack &&
      point.y >= y1 - slack &&
      point.y <= y2 + slack
    ) {
      return index
    }
  }
  return -1
}

/** 命中缩放手柄；返回 null 表示不在任何手柄上。 */
export function hitHandle(
  box: Box,
  point: { x: number; y: number },
  scale: number,
  size = 8,
): Handle | null {
  const [x1, y1, x2, y2] = box.bbox
  const half = size / 2 / Math.max(scale, 0.001)
  const spots: Record<Handle, [number, number]> = {
    nw: [x1, y1],
    n: [(x1 + x2) / 2, y1],
    ne: [x2, y1],
    e: [x2, (y1 + y2) / 2],
    se: [x2, y2],
    s: [(x1 + x2) / 2, y2],
    sw: [x1, y2],
    w: [x1, (y1 + y2) / 2],
  }
  for (const [name, [hx, hy]] of Object.entries(spots) as [Handle, [number, number]][]) {
    if (Math.abs(point.x - hx) <= half && Math.abs(point.y - hy) <= half) return name
  }
  return null
}

/** 拖拽手柄改框：保证 x1<x2、y1<y2，并夹在图像范围内。 */
export function resizeBox(
  box: Box,
  handle: Handle,
  point: { x: number; y: number },
  image: { width: number; height: number },
): Box {
  let [x1, y1, x2, y2] = box.bbox
  const p = clampPoint(point, image)
  if (handle.includes('w')) x1 = Math.min(p.x, x2 - 1)
  if (handle.includes('e')) x2 = Math.max(p.x, x1 + 1)
  if (handle.includes('n')) y1 = Math.min(p.y, y2 - 1)
  if (handle.includes('s')) y2 = Math.max(p.y, y1 + 1)
  return {
    ...box,
    bbox: [
      Math.max(0, Math.min(x1, x2)),
      Math.max(0, Math.min(y1, y2)),
      Math.min(image.width, Math.max(x1, x2)),
      Math.min(image.height, Math.max(y1, y2)),
    ],
  }
}

/** 从拖拽起止点生成一个规范化新框（支持反向拖）。 */
export function boxFromDrag(
  start: { x: number; y: number },
  end: { x: number; y: number },
  image: { width: number; height: number },
  classId: number,
): Box | null {
  const a = clampPoint(start, image)
  const b = clampPoint(end, image)
  const x1 = Math.min(a.x, b.x)
  const y1 = Math.min(a.y, b.y)
  const x2 = Math.max(a.x, b.x)
  const y2 = Math.max(a.y, b.y)
  if (x2 - x1 < 1 || y2 - y1 < 1) return null
  return { class_id: classId, bbox: [x1, y1, x2, y2], severity: 'medium', source: 'manual' }
}

/** 把像素框转成后端契约需要的数组，避免前端多带字段。 */
export function toPayloadBox(box: Box): {
  class_id: number
  bbox: [number, number, number, number]
  severity: string
  source: string
} {
  return {
    class_id: Number(box.class_id),
    bbox: [
      Math.round(box.bbox[0] * 1000) / 1000,
      Math.round(box.bbox[1] * 1000) / 1000,
      Math.round(box.bbox[2] * 1000) / 1000,
      Math.round(box.bbox[3] * 1000) / 1000,
    ],
    severity: box.severity || 'medium',
    source: box.source || 'manual',
  }
}
