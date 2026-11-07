/**
 * 复核页取框来源与并发基线。
 *
 * 关键反例（都来自真实页面）：
 * 1. 用户把框全删了保存草稿 → 刷新后旧框复活；
 * 2. 已确认“无缺陷” → 重进又出现模型框；
 * 3. 两个页面拿同一个基线改草稿 → 后保存的直接覆盖；
 * 4. 双模态把 IR 的 x_boxes 当成参考模态框，或反过来用 RGB 框填 IR。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  draftBaseVersion,
  preferredBoxes,
  preferredXBoxes,
  reviewLoadedStamp,
  reviewVersionStamp,
  submitBaseVersion,
} from '../src/lib/review.ts'

const modelBoxes = [{ class_id: 0, bbox: [1, 1, 10, 10] as [number, number, number, number] }]
const irBoxes = [{ class_id: 0, bbox: [70, 80, 85, 110] as [number, number, number, number] }]

test('有效空草稿不会回退到模型框', () => {
  const state = { draft: { boxes: [], version: 2 }, record: { boxes: modelBoxes } }
  assert.deepEqual(preferredBoxes(state), [])
})

test('确认无缺陷后重进仍为空', () => {
  const state = {
    draft: null,
    latest_confirmed: { boxes: [], version: 3 },
    record: { boxes: modelBoxes },
  }
  assert.deepEqual(preferredBoxes(state), [])
})

test('没有草稿也没有结论时才显示模型框', () => {
  const state = { draft: null, latest_confirmed: null, record: { boxes: modelBoxes } }
  assert.deepEqual(preferredBoxes(state), modelBoxes)
})

test('草稿优先于最近确认', () => {
  const state = {
    draft: { boxes: [{ class_id: 1, bbox: [2, 2, 8, 8] as [number, number, number, number] }] },
    latest_confirmed: { boxes: [] },
    record: { boxes: modelBoxes },
  }
  assert.deepEqual(preferredBoxes(state)[0].class_id, 1)
})

test('草稿基线与提交基线分开：草稿每次保存都前进一版', () => {
  const state = {
    draft: { boxes: [], version: 4 },
    latest: { version: 4 },
    latest_submitted: { version: 1 },
    record: { boxes: [] },
  }
  assert.equal(draftBaseVersion(state), 4)
  assert.equal(submitBaseVersion(state), 1)
  assert.equal(draftBaseVersion(null), 0)
  assert.equal(submitBaseVersion({ latest_submitted: null, record: { boxes: [] } }), 0)
})

test('X 框取自己的列：草稿 x_boxes 优先，空数组也是有效清空', () => {
  const state = {
    draft: { boxes: modelBoxes, x_boxes: [], version: 5 },
    latest_confirmed: { boxes: [], x_boxes: irBoxes, version: 4 },
    record: { boxes: modelBoxes, x_boxes: irBoxes },
  }
  assert.deepEqual(preferredXBoxes(state), [])
  assert.deepEqual(preferredBoxes(state), modelBoxes)
})

test('X 框回退顺序与参考模态一致，但不会借用参考模态的框', () => {
  const state = {
    draft: null,
    latest_confirmed: { boxes: modelBoxes, x_boxes: irBoxes, version: 4 },
    record: { boxes: modelBoxes, x_boxes: [] },
  }
  assert.deepEqual(preferredXBoxes(state), irBoxes)
  // 旧记录没有 x_boxes 列时按空处理，不能把 boxes 复制过去
  const legacy = { draft: null, latest_confirmed: null, record: { boxes: modelBoxes } }
  assert.deepEqual(preferredXBoxes(legacy), [])
})

test('版本签名/加载签名能区分版本前进与未变', () => {
  const a = { draft: { version: 2, updated_at: 't1' }, latest_confirmed: null, latest_submitted: null, record: { boxes: [] } }
  const b = { draft: { version: 3, updated_at: 't2' }, latest_confirmed: null, latest_submitted: null, record: { boxes: [] } }
  assert.notEqual(reviewLoadedStamp(a), reviewLoadedStamp(b))
  assert.equal(reviewVersionStamp({ version: 3, updated_at: 't2' }), '3@t2')
  assert.equal(reviewVersionStamp(null), '')
  assert.equal(reviewLoadedStamp(null), '')
})