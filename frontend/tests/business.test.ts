/**
 * 业务事件折叠的纯逻辑测试。
 *
 * 关心的是三条容易被写坏的规则：
 * 1. 会话隔离：别的会话的事件不能出现在当前时间线里；
 * 2. 增量合并：重连重复拉取同一段 seq 不会产生重复条目；
 * 3. 折叠摘要：卡片只给一行状态 + 可展开明细，且不会把“结论”当成最终回答。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildCards,
  formatBytes,
  inspectFieldHint,
  jobStatusLabel,
  lastJobSeq,
  mergeJobEvents,
  recordStatusLabel,
  recordsSummary,
  reviewLine,
  scopeEvents,
  verdictLabel,
} from '../src/lib/business.ts'
import type { BusinessJob, JobEvent } from '../src/types.ts'

function jobEvent(
  seq: number,
  type: string,
  payload: Record<string, unknown> = {},
  jobId = 'job_aaaaaaaaaaaaaaaa',
  sessionId = 'ses_1',
): JobEvent {
  return { seq, job_id: jobId, session_id: sessionId, type, payload, created_at: '2026-11-05T00:00:00Z' }
}

test('mergeJobEvents：重复拉取同一段 seq 不产生重复事件', () => {
  const merged = mergeJobEvents(
    [jobEvent(1, 'job.started'), jobEvent(2, 'business.detection')],
    [jobEvent(2, 'business.detection'), jobEvent(3, 'job.done')],
  )
  assert.deepEqual(
    merged.map((item) => item.seq),
    [1, 2, 3],
  )
  assert.equal(lastJobSeq(merged), 3)
  assert.equal(lastJobSeq([]), 0)
})

test('scopeEvents：别的会话的事件不会串到当前会话', () => {
  const events = [
    jobEvent(1, 'job.started', {}, 'job_1', 'ses_1'),
    jobEvent(2, 'job.started', {}, 'job_2', 'ses_2'),
  ]
  assert.deepEqual(
    scopeEvents(events, 'ses_1').map((item) => item.seq),
    [1],
  )
})

test('buildCards：一个作业一张卡，逐条结果进明细而不是顶到摘要里', () => {
  const jobs: BusinessJob[] = [
    {
      id: 'job_aaaaaaaaaaaaaaaa',
      project_id: 'prj_1',
      session_id: 'ses_1',
      kind: 'detection_batch',
      status: 'running',
      progress: {},
      result: {},
      error: null,
      created_at: '2026-11-05T00:00:00Z',
      started_at: null,
      finished_at: null,
    },
  ]
  const cards = buildCards(
    [
      jobEvent(1, 'job.started'),
      jobEvent(2, 'business.detection', {
        index: 1,
        total: 3,
        item: {
          record_id: 'det_1111111111111111',
          status: 'done',
          hit_count: 2,
          class_summary: { 单一热斑: 2 },
        },
      }),
      jobEvent(3, 'job.done'),
    ],
    jobs,
  )
  assert.equal(cards.length, 1)
  const card = cards[0]
  assert.equal(card.kind, 'detection_batch')
  assert.equal(card.title, '图片检测')
  assert.equal(card.status, 'done')
  assert.equal(card.items.length, 1)
  assert.equal(card.items[0].hitCount, 2)
  assert.equal(card.items[0].classes, '单一热斑×2')
  assert.deepEqual(card.recordIds, ['det_1111111111111111'])
})

test('buildCards：核查结论只作为明细，不会被当成最终回答', () => {
  const cards = buildCards([
    jobEvent(5, 'business.review', {
      record_id: 'det_2222222222222222',
      item: { status: 'done', review: { verdict: 'possible_false_positive' } },
    }),
  ])
  assert.equal(cards[0].summary, '结论：疑似误检')
  assert.equal(cards[0].detail.length, 1)
  assert.equal(cards[0].recordIds.length, 1)
})

test('buildCards：没有事件时靠作业列表也能显示出“在跑”', () => {
  const cards = buildCards(
    [],
    [
      {
        id: 'job_bbbbbbbbbbbbbbbb',
        project_id: 'prj_1',
        session_id: 'ses_1',
        kind: 'inspection',
        status: 'running',
        progress: {},
        result: {},
        error: null,
        created_at: '2026-11-05T00:00:00Z',
        started_at: null,
        finished_at: null,
      },
    ],
  )
  assert.equal(cards.length, 1)
  assert.equal(cards[0].title, '目录巡检')
  assert.equal(cards[0].status, 'running')
  assert.equal(jobStatusLabel('running'), '进行中')
})

test('文案：状态与结论标签统一，未知值不被吞掉', () => {
  assert.equal(recordStatusLabel('paused'), '已暂停')
  assert.equal(recordStatusLabel('weird'), 'weird')
  assert.equal(verdictLabel('needs_human_review'), '建议人工复核')
  assert.equal(verdictLabel(null), '无结论')
  assert.equal(reviewLine(null), '未核查')
  assert.equal(
    reviewLine({
      id: 'rev_1',
      detection_id: 'det_1',
      status: 'failed',
      verdict: null,
      confidence: null,
      summary: '',
      findings: {},
      model: 'm',
      provider: 'p',
      created_at: '',
      finished_at: null,
      error: { code: 'REVIEW_PROVIDER_ERROR', message: '模型服务返回 HTTP 500' },
    }),
    '核查失败：模型服务返回 HTTP 500',
  )
})

test('recordsSummary / formatBytes：空数据与大数字都不编造', () => {
  assert.equal(recordsSummary([]), '暂无记录')
  assert.equal(
    recordsSummary([
      { hit_count: 2 } as never,
      { hit_count: 0 } as never,
    ]),
    '共 2 条，其中 1 条有命中',
  )
  assert.equal(formatBytes(0), '0 B')
  assert.equal(formatBytes(2048), '2.0 KB')
  assert.equal(formatBytes(-1), '—')
})

test('巡检提示：单模态不多收配图，双模态不放过缺失，edge 才允许派生', () => {
  assert.match(
    inspectFieldHint({ mode: 'single', secondary: 'ir', hasX: false, deriveEdge: false }),
    /单模态：只使用 RGB/,
  )
  assert.match(
    inspectFieldHint({ mode: 'multimodal', secondary: 'ir', hasX: false, deriveEdge: true }),
    /必须提供 IR 配图/,
  )
  assert.match(
    inspectFieldHint({ mode: 'multimodal', secondary: 'edge', hasX: false, deriveEdge: true }),
    /派生/,
  )
  assert.match(
    inspectFieldHint({ mode: 'multimodal', secondary: 'edge', hasX: true, deriveEdge: true }),
    /必须提供 EDGE 配图/,
  )
})

test('job.summary：完成摘要来自真实结果，不被状态标签覆盖', () => {
  const events = [
    {
      seq: 1,
      job_id: 'job_1',
      session_id: 's1',
      type: 'job.done',
      payload: { summary: 'AI 核查完成：成功 2/2 条、失败 0 条。' },
      created_at: '',
    },
    {
      seq: 2,
      job_id: 'job_1',
      session_id: 's1',
      type: 'job.summary',
      payload: { summary: 'AI 核查完成：成功 2/2 条、失败 0 条。' },
      created_at: '',
    },
  ]
  const cards = buildCards(events as never, [])
  assert.equal(cards.length, 1)
  assert.match(cards[0].summary, /成功 2\/2 条/)
  assert.equal(jobStatusLabel('interrupted'), '已中断（结果未知）')
})
