/**
 * 复核页取框来源与并发基线。
 *
 * 关键反例（都来自真实页面）：
 * 1. 用户把框全删了保存草稿 → 刷新后旧框复活；
 * 2. 已确认“无缺陷” → 重进又出现模型框；
 * 3. 两个页面拿同一个基线改草稿 → 后保存的直接覆盖。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { draftBaseVersion, preferredBoxes, submitBaseVersion } from '../src/lib/review.ts'

const modelBoxes = [{ class_id: 0, bbox: [1, 1, 10, 10] as [number, number, number, number] }]

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
