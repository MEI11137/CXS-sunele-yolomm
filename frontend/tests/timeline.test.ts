/**
 * 时间线纯逻辑测试：只测规则本身（合并、卡片聚合、流式拼接、滚动与轮询节奏），
 * 不镜像实现细节。运行：npm run test:logic
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  hasPersistedAnswer,
  isTerminalStatus,
  lastSeq,
  latestNotice,
  liveSectionVisible,
  mergeEvents,
  nearBottom,
  pendingActions,
  pollDelay,
  streamingText,
  toolCards,
  toolStateLabel,
  turnStatusLabel,
} from '../src/lib/timeline.ts'

function event(seq: number, type: string, payload: Record<string, unknown> = {}) {
  return { seq, type, payload }
}

test('mergeEvents：重连重复拉取不产生重复事件，且按 seq 归位', () => {
  const first = [event(1, 'turn.status'), event(2, 'text')]
  const merged = mergeEvents(first, [event(2, 'text'), event(4, 'final'), event(3, 'tool')])
  assert.deepEqual(
    merged.map((item) => item.seq),
    [1, 2, 3, 4],
  )
  assert.equal(lastSeq(merged), 4)
})

test('mergeEvents：没有新事件时返回原数组（不触发多余渲染）', () => {
  const first = [event(1, 'text')]
  assert.equal(mergeEvents(first, []), first)
})

test('toolCards：同一 call_id 的多次状态更新只出一张卡，取最后状态', () => {
  const cards = toolCards([
    event(1, 'tool', { call_id: 'c1', tool: 'hx_list_files', state: 'pending', title: '列出目录' }),
    event(2, 'tool', { call_id: 'c1', tool: 'hx_list_files', state: 'running', title: '列出目录' }),
    event(3, 'tool', {
      call_id: 'c1',
      tool: 'hx_list_files',
      state: 'completed',
      title: '列出目录',
      duration_ms: 42,
      preview: '3 个文件',
    }),
    event(4, 'tool', { call_id: 'c2', tool: 'hx_read_file', state: 'completed', title: '读取文件' }),
  ])
  assert.equal(cards.length, 2)
  assert.equal(cards[0].state, 'completed')
  assert.equal(cards[0].durationMs, 42)
  assert.equal(cards[0].preview, '3 个文件')
  assert.equal(cards[1].callId, 'c2')
})

test('toolCards：没有标题时回落到工具名，避免出现一张空白卡', () => {
  const cards = toolCards([event(1, 'tool', { call_id: 'x', tool: 'hx_search_files', state: 'running' })])
  assert.equal(cards[0].title, 'hx_search_files')
})

test('streamingText：同一 part 只取最新文本，多 part 按顺序分段不粘连', () => {
  const text = streamingText([
    event(1, 'text', { part_id: 'p1', text: '目录' }),
    event(2, 'text', { part_id: 'p1', text: '目录里有 3 个文件' }),
    event(3, 'text', { part_id: 'p2', text: '需要我读哪个？' }),
    event(4, 'tool', { call_id: 'c1', state: 'completed' }),
  ])
  // 两个 part 是两段话，不能首尾相接拼成一句。
  assert.equal(text, '目录里有 3 个文件\n\n需要我读哪个？')
})

test('hasPersistedAnswer：按 meta.turn_id 判断这一轮是否已落库', () => {
  const messages = [
    { role: 'user', meta: { turn_id: 't1' } },
    { role: 'assistant', meta: { turn_id: 't1' } },
  ]
  assert.equal(hasPersistedAnswer(messages, 't1'), true)
  assert.equal(hasPersistedAnswer(messages, 't2'), false)
  assert.equal(
    hasPersistedAnswer([{ role: 'assistant', meta: {} }], 't1'),
    false,
    '没有 turn_id 的旧消息不能被当成这一轮的回答',
  )
})

test('liveSectionVisible：进行中显示实时区，终态交给落库消息', () => {
  assert.equal(liveSectionVisible({ status: 'running', persisted: false }), true)
  assert.equal(liveSectionVisible({ status: 'completed', persisted: true }), false)
  assert.equal(
    liveSectionVisible({ status: 'completed', persisted: false }),
    false,
    '终态即使落库还没回来也不叠加流式文本，避免二次显示',
  )
  assert.equal(liveSectionVisible({ status: 'failed', persisted: false }), false)
})

test('latestNotice：取最后一条非空提示', () => {
  const notice = latestNotice([
    event(1, 'notice', { message: '已请求停止' }),
    event(2, 'text', { text: '正文' }),
    event(3, 'notice', { message: '' }),
  ])
  assert.equal(notice, '已请求停止')
  assert.equal(latestNotice([event(1, 'text', { text: 'x' })]), '')
})

test('nearBottom：贴着底部才跟随，上翻后不抢滚动条', () => {
  const atBottom = { scrollHeight: 1000, scrollTop: 600, clientHeight: 400 }
  assert.equal(nearBottom(atBottom), true)
  const scrolledUp = { scrollHeight: 1000, scrollTop: 200, clientHeight: 400 }
  assert.equal(nearBottom(scrolledUp), false)
  // 内容不足一屏时 scrollTop 为 0，也必须算“在底部”，否则永远不会跟随新消息
  assert.equal(nearBottom({ scrollHeight: 300, scrollTop: 0, clientHeight: 400 }), true)
})

test('pollDelay：正常快轮询，连续失败逐步退避到上限', () => {
  assert.equal(pollDelay(0), 400)
  assert.equal(pollDelay(1), 800)
  assert.equal(pollDelay(2), 1600)
  assert.equal(pollDelay(9), 3000)
})

test('isTerminalStatus：只有三种真正的终态', () => {
  assert.equal(isTerminalStatus('completed'), true)
  assert.equal(isTerminalStatus('failed'), true)
  assert.equal(isTerminalStatus('stopped'), true)
  assert.equal(isTerminalStatus('waiting_action'), false)
  assert.equal(isTerminalStatus('running'), false)
})

test('pendingActions：只留下需要人处理的动作', () => {
  const list = [
    { id: 'a', status: 'proposed' },
    { id: 'b', status: 'done' },
    { id: 'c', status: 'executing' },
    { id: 'd', status: 'rejected' },
  ]
  assert.deepEqual(
    pendingActions(list).map((item) => item.id),
    ['a', 'c'],
  )
})

test('状态标签：未知值原样显示，不吞掉后端新状态', () => {
  assert.equal(toolStateLabel('completed'), '已完成')
  assert.equal(toolStateLabel('weird'), 'weird')
  assert.equal(turnStatusLabel('waiting_action'), '等待你确认')
  assert.equal(turnStatusLabel('unknown_state'), 'unknown_state')
})
