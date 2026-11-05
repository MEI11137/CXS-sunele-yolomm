/**
 * 复核画布的几何纯逻辑：缩放以指针为中心、坐标往返、框命中与改框。
 * 这些规则写错，用户看到的就是“缩放后框跑偏”。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  boxFromDrag,
  fitView,
  hitHandle,
  hitTest,
  resizeBox,
  toImage,
  toScreen,
  zoomAt,
} from '../src/lib/geometry.ts'

test('滚轮缩放以指针为中心：指针下的图像点不动', () => {
  const view = { scale: 1, offsetX: 0, offsetY: 0 }
  const pointer = { x: 120, y: 80 }
  const before = toImage(pointer, view)
  const zoomed = zoomAt(view, 2, pointer)
  const after = toImage(pointer, zoomed)
  assert.ok(Math.abs(before.x - after.x) < 1e-9)
  assert.ok(Math.abs(before.y - after.y) < 1e-9)
  assert.equal(zoomed.scale, 2)
})

test('屏幕/图像坐标往返一致（缩放 + 平移后）', () => {
  const view = { scale: 2.5, offsetX: -40, offsetY: 33 }
  const point = { x: 12.5, y: 900 }
  const round = toImage(toScreen(point, view), view)
  assert.ok(Math.abs(round.x - point.x) < 1e-9)
  assert.ok(Math.abs(round.y - point.y) < 1e-9)
})

test('适应窗口：整图可见且居中，不放大超过 100%', () => {
  const view = fitView({ width: 1280, height: 1024 }, { width: 600, height: 400 }, 10)
  assert.ok(view.scale <= 1)
  assert.ok(view.offsetX > 0 || view.scale === 1)
  const right = view.offsetX + 1280 * view.scale
  assert.ok(right <= 600 + 1e-6)
})

test('命中测试与手柄：缩放后命中范围按屏幕像素换算', () => {
  const boxes = [{ class_id: 0, bbox: [100, 100, 200, 200] as [number, number, number, number] }]
  assert.equal(hitTest(boxes, { x: 150, y: 150 }, 1), 0)
  assert.equal(hitTest(boxes, { x: 500, y: 500 }, 1), -1)
  // 放大 10 倍后，框在图像坐标里只剩很窄的边界，仍然要能点中手柄
  assert.equal(hitHandle(boxes[0], { x: 100.2, y: 100.2 }, 10), 'nw')
})

test('拖拽改框：夹在原图内且不会翻转', () => {
  const box = { class_id: 0, bbox: [20, 20, 80, 60] as [number, number, number, number] }
  const resized = resizeBox(box, 'se', { x: 500, y: 400 }, { width: 100, height: 100 })
  assert.deepEqual(resized.bbox, [20, 20, 100, 100])
  const flipped = resizeBox(box, 'nw', { x: 90, y: 90 }, { width: 100, height: 100 })
  assert.ok(flipped.bbox[0] < flipped.bbox[2])
  assert.ok(flipped.bbox[1] < flipped.bbox[3])
})

test('新框：反向拖也能生成，过小则忽略', () => {
  const image = { width: 64, height: 48 }
  const box = boxFromDrag({ x: 40, y: 30 }, { x: 10, y: 5 }, image, 1)
  assert.ok(box)
  assert.deepEqual(box!.bbox, [10, 5, 40, 30])
  assert.equal(boxFromDrag({ x: 10, y: 10 }, { x: 10.2, y: 10.2 }, image, 1), null)
})
