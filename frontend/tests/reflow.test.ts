/**
 * 回流表单：显示即提交 + 预览过期。
 *
 * 两个反例都来自真实浏览器：
 * 1. 动作/划分下拉显示为空，提交时偷偷补 replace/train；
 * 2. 把模式从「另建」改回「回原修订」并改名后，「定稿并生成版本」仍可点，
 *    点下去执行的是旧草稿的计划（用户以为新表单生效了）。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  actionOf,
  buildSelections,
  defaultAction,
  formSnapshot,
  isPlanStale,
  splitOf,
  splitRequired,
} from '../src/lib/reflow.ts'
import type { ReflowForm } from '../src/lib/reflow.ts'

const FORM: ReflowForm = {
  mode: 'revision',
  datasetId: 'dst_1',
  name: '回原修订',
  strategy: 'even',
  ratios: { train: 7, val: 2, test: 1 },
  seed: 7,
}

const ids = ['det_a', 'det_b']

function snapshot(
  form: ReflowForm,
  actions: Record<string, 'replace' | 'append' | 'exclude'> = {},
  splits: Record<string, 'train' | 'val' | 'test'> = {},
  targets: Record<string, string> = {},
  records: string[] = ids,
): string {
  return formSnapshot(form, records, actions, splits, targets)
}

test('动作下拉显示的默认值就是提交的默认值', () => {
  assert.equal(defaultAction('revision'), 'replace')
  assert.equal(defaultAction('new'), 'append')
  // 没显式选过时，显示与提交都来自 actionOf，不会出现“空着但提交 replace”。
  assert.equal(actionOf(FORM, {}, 'det_a'), 'replace')
  assert.equal(actionOf({ ...FORM, mode: 'new' }, {}, 'det_a'), 'append')
  assert.equal(actionOf(FORM, { det_a: 'exclude' }, 'det_a'), 'exclude')

  const selections = buildSelections(ids, FORM, {}, {}, {})
  assert.deepEqual(
    selections.map((item) => item.action),
    ['replace', 'replace'],
  )
})

test('回原替换不提交划分，也不显示划分下拉（沿用父划分）', () => {
  assert.equal(splitRequired(FORM, {}, 'det_a'), false)
  const selections = buildSelections(ids, FORM, {}, { det_a: 'val' }, {})
  assert.equal(selections[0].split, undefined, '回原替换的划分由服务端沿用父划分')
})

test('只有真的需要划分时才要求选：追加、逐样本、另建+manual', () => {
  assert.equal(splitRequired(FORM, { det_a: 'append' }, 'det_a'), true)
  assert.equal(splitRequired({ ...FORM, mode: 'new', strategy: 'even' }, {}, 'det_a'), false)
  assert.equal(splitRequired({ ...FORM, mode: 'new', strategy: 'ratio' }, {}, 'det_a'), false)
  assert.equal(splitRequired({ ...FORM, mode: 'new', strategy: 'manual' }, {}, 'det_a'), true)

  const appended = buildSelections(['det_a'], FORM, { det_a: 'append' }, {}, {})
  assert.equal(appended[0].split, 'train', '需要划分时给出下拉上显示的那个值')
  assert.equal(splitOf({ det_a: 'test' }, 'det_a'), 'test')
})

test('改了名称/模式/动作/反馈集合，旧预览就过期', () => {
  const base = snapshot(FORM)
  assert.equal(isPlanStale(base, base), false)
  assert.equal(isPlanStale('', snapshot(FORM)), false, '没预览过不算过期')

  assert.equal(isPlanStale(base, snapshot({ ...FORM, name: '改个名' })), true)
  assert.equal(isPlanStale(base, snapshot({ ...FORM, mode: 'new' })), true)
  assert.equal(isPlanStale(base, snapshot({ ...FORM, datasetId: 'dst_2' })), true)
  assert.equal(isPlanStale(base, snapshot({ ...FORM, seed: 8 })), true)
  assert.equal(isPlanStale(base, snapshot({ ...FORM, strategy: 'ratio' })), true)
  assert.equal(isPlanStale(base, snapshot(FORM, { det_a: 'append' })), true)
  assert.equal(isPlanStale(base, snapshot(FORM, {}, { det_a: 'val' })), false, '回原替换下划分不参与计划')
  assert.equal(isPlanStale(base, snapshot(FORM, {}, {}, {}, ['det_a'])), true, '勾选集合变了也算过期')
  assert.equal(isPlanStale(base, snapshot(FORM, {}, {}, { det_a: 'smp_1' })), true)
})

test('另建模式下非比例策略忽略比例，比例值不该让预览反复过期', () => {
  const form: ReflowForm = { ...FORM, mode: 'new', strategy: 'even' }
  const a = snapshot(form)
  const b = snapshot({ ...form, ratios: { train: 1, val: 0, test: 0 } })
  assert.equal(isPlanStale(a, b), false, '不参与计划的值变化不该判过期')
  const c = snapshot({ ...form, strategy: 'ratio' })
  assert.equal(isPlanStale(a, c), true)
})

test('快照包含完整提交内容，能区分同名不同划分的选择', () => {
  const a = snapshot(FORM, { det_a: 'append' }, { det_a: 'train' })
  const b = snapshot(FORM, { det_a: 'append' }, { det_a: 'val' })
  assert.notEqual(a, b)
  assert.match(a, /"split":"train"/)
})
