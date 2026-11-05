/**
 * 复核页指针事件分支（不是再算一遍 resizeBox 公式）。
 *
 * 反例来自真实浏览器：21% 显示比例下，从框东南角 [450,456] 拖到 [475,478]，
 * 框尺寸仍是 384×307.2 —— 因为角点分支只设了 mode/handle，没设目标框索引，
 * 移动阶段整段被 `index >= 0` 判掉。这里按**同一组屏幕坐标**重放按下→移动→松开，
 * 断言框真的变了、并且能撤销。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { toImage } from '../src/lib/geometry.ts'
import type { Box, Viewport } from '../src/lib/geometry.ts'
import {
  beginDrag,
  dragBoxes,
  endDrag,
  sameBoxes,
  toolHint,
} from '../src/lib/interaction.ts'
import type { Tool } from '../src/lib/interaction.ts'

const IMAGE = { width: 1280, height: 1024 }
const REPORTED: Box = {
  class_id: 1,
  bbox: [384, 307.2, 768, 614.4],
  severity: 'medium',
  source: 'manual',
}

/** 复现协作者当时的视图：21% 比例，框东南角正好落在屏幕 [450,456]。 */
const VIEW: Viewport = {
  scale: 0.21,
  offsetX: 450 - 768 * 0.21,
  offsetY: 456 - 614.4 * 0.21,
}

function imagePoint(screen: { x: number; y: number }) {
  return toImage(screen, VIEW)
}

function begin(tool: Tool, point: { x: number; y: number }, boxes: Box[], selected: number) {
  return beginDrag({
    tool,
    editable: true,
    boxes,
    selected,
    point,
    scale: VIEW.scale,
  })
}

test('真实反例：从东南角拖拽必须改变框尺寸（角点分支带目标索引）', () => {
  const down = imagePoint({ x: 450, y: 456 })
  const intent = begin('select', down, [REPORTED], 0)
  assert.equal(intent.mode, 'resize')
  assert.equal(intent.handle, 'se')
  assert.equal(intent.index, 0, 'resize 必须带上目标框索引，否则移动阶段会整段跳过')

  const moved = dragBoxes({
    mode: intent.mode,
    index: intent.index,
    handle: intent.handle,
    origin: down,
    snapshot: [REPORTED],
    point: imagePoint({ x: 475, y: 478 }),
    image: IMAGE,
  })
  assert.ok(moved, '拖动必须产出新的框数组')
  const next = moved[0]
  assert.notEqual(next.bbox[2], REPORTED.bbox[2], '右边界必须跟着角点走')
  assert.notEqual(next.bbox[3], REPORTED.bbox[3], '下边界必须跟着角点走')
  assert.ok(next.bbox[2] > REPORTED.bbox[2] + 50)
  assert.ok(next.bbox[3] > REPORTED.bbox[3] + 50)
  // 西北角不动：只有被拖的角点变化。
  assert.equal(next.bbox[0], REPORTED.bbox[0])
  assert.equal(next.bbox[1], REPORTED.bbox[1])

  const ended = endDrag({
    mode: 'resize',
    origin: down,
    point: imagePoint({ x: 475, y: 478 }),
    snapshot: [REPORTED],
    boxes: moved,
    image: IMAGE,
    classId: 0,
  })
  assert.equal(ended.changed, true, '有实际变化就该压撤销记录')
  assert.equal(ended.added, -1)
})

test('拖框中心是移动，不是缩放', () => {
  const center = { x: (384 + 768) / 2, y: (307.2 + 614.4) / 2 }
  const down = { x: center.x, y: center.y }
  const intent = begin('select', down, [REPORTED], 0)
  assert.equal(intent.mode, 'move')
  assert.equal(intent.index, 0)
  assert.equal(intent.handle, null)

  const moved = dragBoxes({
    mode: intent.mode,
    index: intent.index,
    handle: intent.handle,
    origin: down,
    snapshot: [REPORTED],
    point: { x: down.x + 40, y: down.y + 30 },
    image: IMAGE,
  })
  assert.ok(moved)
  const [x1, y1, x2, y2] = moved[0].bbox
  assert.ok(Math.abs(x2 - x1 - 384) < 1e-6, '移动不能改变宽度')
  assert.ok(Math.abs(y2 - y1 - 307.2) < 1e-6, '移动不能改变高度')
  assert.ok(Math.abs(x1 - 424) < 1e-6 && Math.abs(y1 - 337.2) < 1e-6)
})

test('按在另一个框上时不能缩放当前选中的框', () => {
  const other: Box = { class_id: 0, bbox: [800, 700, 1000, 900], severity: 'low' }
  // 选中 0 号框，但按在 1 号框的中心（离 0 号框角点很远）。
  const down = { x: 900, y: 800 }
  const intent = begin('select', down, [REPORTED, other], 0)
  assert.equal(intent.mode, 'move')
  assert.equal(intent.index, 1, '命中谁就动谁')
})

test('空白拖拽默认平移，不因为“没选中框”就变成画框', () => {
  for (const selected of [-1, 0]) {
    const intent = begin('select', { x: 60, y: 60 }, [REPORTED], selected)
    assert.equal(intent.mode, 'pan', `selected=${selected} 时空白拖拽必须是平移`)
    assert.equal(intent.index, -1)
  }
})

test('画框只由画框工具触发，并在松开时落地', () => {
  const intent = begin('draw', { x: 100, y: 100 }, [REPORTED], 0)
  assert.equal(intent.mode, 'draw', '画框工具下按在框里也要画框')
  const ended = endDrag({
    mode: 'draw',
    origin: { x: 100, y: 100 },
    point: { x: 300, y: 260 },
    snapshot: [REPORTED],
    boxes: [REPORTED],
    image: IMAGE,
    classId: 2,
  })
  assert.equal(ended.changed, true)
  assert.equal(ended.boxes.length, 2)
  assert.equal(ended.added, 1)
  assert.deepEqual(ended.boxes[1].bbox, [100, 100, 300, 260])
  assert.equal(ended.boxes[1].class_id, 2)
})

test('画框只是点一下（没有拖动）不产生 0 面积框', () => {
  const ended = endDrag({
    mode: 'draw',
    origin: { x: 100, y: 100 },
    point: { x: 100.2, y: 100.2 },
    snapshot: [],
    boxes: [],
    image: IMAGE,
    classId: 0,
  })
  assert.equal(ended.changed, false)
  assert.equal(ended.boxes.length, 0)
})

test('平移工具按在框上也只平移', () => {
  const down = { x: (384 + 768) / 2, y: (307.2 + 614.4) / 2 }
  const intent = begin('pan', down, [REPORTED], 0)
  assert.equal(intent.mode, 'pan')
  assert.equal(intent.index, -1)
})

test('已驳回（不可编辑）时任何工具都只平移', () => {
  const down = imagePoint({ x: 450, y: 456 })
  const intent = beginDrag({
    tool: 'select',
    editable: false,
    boxes: [REPORTED],
    selected: 0,
    point: down,
    scale: VIEW.scale,
  })
  assert.equal(intent.mode, 'pan', '不可编辑时按在角点上也不能改框')
  assert.equal(intent.index, -1)
})

test('没有真实移动就不压撤销（避免 Ctrl+Z 空转）', () => {
  const ended = endDrag({
    mode: 'move',
    origin: { x: 500, y: 500 },
    point: { x: 500, y: 500 },
    snapshot: [REPORTED],
    boxes: [{ ...REPORTED, bbox: [...REPORTED.bbox] as Box['bbox'] }],
    image: IMAGE,
    classId: 0,
  })
  assert.equal(ended.changed, false)
  assert.equal(sameBoxes([REPORTED], [{ ...REPORTED, bbox: [...REPORTED.bbox] as Box['bbox'] }]), true)
})

test('缩放夹在图像范围内，不会把框拖出画布', () => {
  const down = imagePoint({ x: 450, y: 456 })
  const intent = begin('select', down, [REPORTED], 0)
  const moved = dragBoxes({
    mode: intent.mode,
    index: intent.index,
    handle: intent.handle,
    origin: down,
    snapshot: [REPORTED],
    // 远超右下边界：屏幕坐标换算后仍会被夹住
    point: imagePoint({ x: 4000, y: 4000 }),
    image: IMAGE,
  })
  assert.ok(moved)
  assert.ok(moved[0].bbox[2] <= IMAGE.width && moved[0].bbox[3] <= IMAGE.height)
  assert.ok(Math.abs(moved[0].bbox[2] - IMAGE.width) < 1e-6)
})

test('工具提示把“按下会发生什么”说清楚', () => {
  assert.match(toolHint('select', true), /平移/)
  assert.match(toolHint('draw', true), /新建/)
  assert.match(toolHint('pan', true), /不会改框/)
  assert.match(toolHint('select', false), /已驳回/)
})
