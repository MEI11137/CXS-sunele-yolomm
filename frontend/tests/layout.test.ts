/**
 * 布局纯逻辑测试：右侧助手开关与左侧列表文案。
 *
 * 这两条规则一旦写错，用户侧的表现是"助手突然不见了"或"列表看起来是空的"，
 * 所以单独用 node --test 钉住。运行：npm run test:logic
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { BUSINESS_PAGES, dockState, navOwner, sessionCountLabel } from '../src/lib/layout.ts'

test('dockState：会话页永远没有右侧列，业务页才可能停靠', () => {
  assert.equal(dockState({ dockable: false, collapsed: false }), 'none')
  assert.equal(dockState({ dockable: false, collapsed: true }), 'none')
})

test('dockState：业务页默认展开，收起后只留入口而不是整块消失', () => {
  assert.equal(dockState({ dockable: true, collapsed: false }), 'open')
  assert.equal(dockState({ dockable: true, collapsed: true }), 'closed')
  // closed 与 none 必须区分：一个是"用户收起了"，一个是"这页不需要助手"。
  assert.notEqual(
    dockState({ dockable: true, collapsed: true }),
    dockState({ dockable: false, collapsed: true }),
  )
})

test('sessionCountLabel：0 条时不显示“0 个会话”', () => {
  assert.equal(sessionCountLabel(0), '还没有会话')
  assert.equal(sessionCountLabel(-1), '还没有会话')
  assert.equal(sessionCountLabel(Number.NaN), '还没有会话')
  assert.equal(sessionCountLabel(1), '1 个会话')
  assert.equal(sessionCountLabel(12), '12 个会话')
  assert.equal(sessionCountLabel(3.7), '3 个会话')
})

test('BUSINESS_PAGES：与 v1.6 六项导航一致（项目对话 + 五项，顺序和名称都不改）', () => {
  assert.deepEqual(
    BUSINESS_PAGES.map((item) => item.name),
    ['reviewQueue', 'datasets', 'models', 'inspect', 'records'],
  )
  assert.deepEqual(
    BUSINESS_PAGES.map((item) => item.label),
    ['人工复核', '数据集', '模型管理', '图片巡检', '识别记录'],
  )
  // 数据回流仍是真实页面，但不占顶栏第七项。
  assert.ok(!BUSINESS_PAGES.some((item) => item.name === 'reflow'))
})

test('navOwner：回流页高亮“数据集”，单条复核高亮“人工复核”', () => {
  assert.equal(navOwner('reflow'), 'datasets')
  assert.equal(navOwner('review'), 'reviewQueue')
  assert.equal(navOwner('datasets'), 'datasets')
  assert.equal(navOwner('session'), 'session')
  assert.equal(navOwner(undefined), '')
})
