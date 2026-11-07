/**
 * 后台事件失效逻辑：只测“哪些资源域要重读、重复事件不重复触发”的规则本身。
 * 运行：npm run test:logic
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { scanEvents } from '../src/lib/sync.ts'

test('项目改名事件只失效项目列表，并记录最新 seq', () => {
  const result = scanEvents([
    { seq: 3, type: 'tool' },
    { seq: 4, type: 'projects.changed' },
  ])
  assert.deepEqual([...result.scopes], ['projects'])
  assert.equal(result.lastSeq, 4)
})

test('重复到达的同一事件不再触发刷新', () => {
  const events = [
    { seq: 5, type: 'projects.changed' },
    { seq: 6, type: 'sessions.changed' },
  ]
  const first = scanEvents(events, 4)
  assert.deepEqual([...first.scopes].sort(), ['projects', 'sessions'])
  const again = scanEvents(events, first.lastSeq)
  assert.equal(again.scopes.size, 0, '同一批事件重复消费应当是空操作')
  assert.equal(again.lastSeq, 6)
})

test('乱序到达按 seq 处理，且不会回退已处理位置', () => {
  const result = scanEvents(
    [
      { seq: 9, type: 'datasets.changed' },
      { seq: 7, type: 'models.changed' },
      { seq: 8, type: 'text' },
    ],
    6,
  )
  assert.deepEqual([...result.scopes].sort(), ['datasets', 'models'])
  assert.equal(result.lastSeq, 9)
})

test('未知事件类型不触发任何列表刷新', () => {
  const result = scanEvents([{ seq: 2, type: 'text' }, { seq: 3, type: 'action' }], 1)
  assert.equal(result.scopes.size, 0)
  assert.equal(result.lastSeq, 3)
})

test('资料/手册/草稿/训练各有独立失效域', () => {
  const result = scanEvents([
    { seq: 1, type: 'documents.changed' },
    { seq: 2, type: 'manuals.changed' },
    { seq: 3, type: 'reflow.changed' },
    { seq: 4, type: 'training.changed' },
  ])
  assert.deepEqual(
    [...result.scopes].sort(),
    ['documents', 'manuals', 'reflow', 'training'],
  )
})
