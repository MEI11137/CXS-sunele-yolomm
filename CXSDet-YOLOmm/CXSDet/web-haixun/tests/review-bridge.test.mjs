import assert from 'node:assert/strict'
import { before, after, test } from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

let server, bridge, savedWindow, savedCustomEvent
before(async () => {
  savedWindow = globalThis.window
  savedCustomEvent = globalThis.CustomEvent
  globalThis.window = { dispatchEvent() {} }
  globalThis.CustomEvent = class { constructor(type, options) { this.type = type; this.detail = options.detail } }
  server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  bridge = await server.ssrLoadModule('/src/utils/reviewBridge.ts')
})
after(async () => { await server?.close(); globalThis.window = savedWindow; globalThis.CustomEvent = savedCustomEvent })

test('复核页面草稿只能附带到相同项目，发送快照不随之后编辑变化', () => {
  bridge.reviewPage.value = { project_id: 'A', record_id: 'record', draft_revision: 1793000000000,
    expected_version: 2, annotations: [{ class_name: '热斑', class_id: 0, bbox: [.1,.2,.3,.4] }],
    selected_index: 0, mode: 'pan', view: 'original', zoom: 2 }
  assert.equal(bridge.reviewContext('B'), undefined)
  const sent = bridge.reviewContext('A')
  bridge.reviewPage.value.annotations[0].bbox[0] = .2
  assert.equal(sent.annotations[0].bbox[0], .1)
  assert.equal(sent.draft_revision, 1793000000000)
})

test('工具控制请求可以等页面加载，不能串项目或重复应用', () => {
  bridge.dispatchReviewEvent('review.view', { project_id: 'A', record_id: 'one', command: 'zoom', value: '1.5' })
  assert.deepEqual(bridge.takeReviewEvents('B', 'one'), [])
  assert.deepEqual(bridge.takeReviewEvents('A', 'two'), [])
  const ready = bridge.takeReviewEvents('A', 'one')
  assert.equal(ready.length, 1)
  assert.equal(ready[0].payload.value, '1.5')
  assert.deepEqual(bridge.takeReviewEvents('A', 'one'), [])
})

test('驳回有独立状态，不再计入待复核或确认样本', async () => {
  const { reviewStatus, isPendingReview } = await server.ssrLoadModule('/src/utils/reviewStatus.ts')
  assert.equal(reviewStatus('rejected'), 'rejected')
  assert.equal(isPendingReview('rejected'), false)
  assert.equal(isPendingReview('confirmed'), false)
  assert.equal(isPendingReview('no_defect'), false)
  assert.equal(isPendingReview('pending'), true)
  assert.equal(reviewStatus('confirmed'), 'done')
})

test('确认或拒绝接受业务动作响应包，原生快照通过动作接口核实', async () => {
  const originalFetch = globalThis.fetch
  const { RealAdapter } = await server.ssrLoadModule('/src/services/real-adapter.ts')
  const adapter = new RealAdapter()
  const action = { action_id: 'action-1', task_id: 'task', status: 'rejected', preview: {}, revision: 1 }
  const json = data => new Response(JSON.stringify({ code: 200, data }), { status: 200, headers: { 'content-type': 'application/json' } })
  try {
    globalThis.fetch = async () => json({ action, task: { id: 'task' } })
    assert.equal((await adapter.rejectAction('action-1')).status, 'rejected')
    let calls = 0
    globalThis.fetch = async () => json(++calls === 1 ? { task: { id: 'task' }, action: null } : { ...action, status: 'succeeded' })
    assert.equal((await adapter.confirmAction('action-1', 1)).action_id, 'action-1')
    assert.equal(calls, 2)
  } finally { globalThis.fetch = originalFetch }
})

test('手动删除预览、删除和恢复均附带项目范围，取消不发送写入', async () => {
  const originalFetch = globalThis.fetch, originalConfirm = window.confirm
  const api = await server.ssrLoadModule('/src/services/http.ts')
  const { changeResource } = await server.ssrLoadModule('/src/utils/resourceLifecycle.ts')
  const calls = []
  api.setProjectScope('project-A')
  try {
    globalThis.fetch = async (url, options) => {
      calls.push({ url, ...options })
      return new Response(JSON.stringify({ code: 200, data: { name: '测试数据', project_id: 'project-A', revision: 7, impact: '保留文件', blocked_reasons: [] } }), { status: 200 })
    }
    window.confirm = () => false
    assert.equal(await changeResource('dataset', 'dataset-1', 'delete'), false)
    assert.equal(calls.length, 1)
    assert.equal(calls[0].headers['X-Project-Id'], 'project-A')
    window.confirm = () => true
    assert.equal(await changeResource('model', 'model-1', 'restore'), true)
    assert.equal(calls.length, 3)
    assert.ok(calls.every(call => call.headers['X-Project-Id'] === 'project-A'))
    assert.equal(calls[2].method, 'POST')
    assert.deepEqual(JSON.parse(calls[2].body), { operation: 'restore', expected_revision: 7 })
    await api.request('/api/agent/connections')
    assert.equal(calls[3].headers['X-Project-Id'], undefined)
  } finally { api.setProjectScope(''); globalThis.fetch = originalFetch; window.confirm = originalConfirm }
})

test('单条核查只请求指定ID，批量核查只传所选ID且去重', async () => {
  const originalFetch = globalThis.fetch
  const { RealAdapter } = await server.ssrLoadModule('/src/services/real-adapter.ts')
  const adapter = new RealAdapter(), calls = []
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, ...options }); return new Response(JSON.stringify({ code: 200, data: {} }), { status: 200 }) }
    await adapter.requestAiReview('record-one')
    assert.equal(calls.length, 1)
    assert.ok(calls[0].url.endsWith('/api/detection/records/record-one/ai-review'))
    await adapter.requestAiReviews(['record-two', 'record-two', 'record-three'])
    assert.ok(calls[1].url.endsWith('/api/detection/records/ai-review/batch'))
    assert.deepEqual(JSON.parse(calls[1].body), { record_ids: ['record-two', 'record-three'] })
    await assert.rejects(() => adapter.requestAiReviews([]))
    assert.equal(calls.length, 2)
  } finally { globalThis.fetch = originalFetch }
})

test('批量反馈仅选出人工确认且未入池的记录，不包含未选、已删除或驳回项', async () => {
  const { feedbackEligibleRecords } = await server.ssrLoadModule('/src/utils/feedbackSelection.ts')
  const record = (id, status = 'confirmed', extra = {}) => ({ id, human_review_status: status, record_status: 'completed', ...extra })
  const rows = [record('one'), record('two'), record('pending', 'pending'), record('rejected', 'rejected'), record('pooled', 'confirmed', { feedback_dataset_id: 'source' }), record('deleted', 'confirmed', { record_status: 'deleted' }), record('unselected')]
  assert.deepEqual(feedbackEligibleRecords(rows, ['one', 'one', 'two', 'pending', 'rejected', 'pooled', 'deleted', 'missing']).map(row => row.id), ['one', 'two'])
  assert.deepEqual(feedbackEligibleRecords(rows, []), [])
})

test('批量反馈请求传递所选ID并去重，空选择或超过50条不发送', async () => {
  const originalFetch = globalThis.fetch
  const { RealAdapter } = await server.ssrLoadModule('/src/services/real-adapter.ts')
  const { setProjectScope } = await server.ssrLoadModule('/src/services/http.ts')
  const adapter = new RealAdapter(), calls = []
  setProjectScope('project-A')
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, ...options }); return new Response(JSON.stringify({ code: 200, data: { succeeded: 2, failed: 0, items: [] } }), { status: 200 }) }
    await adapter.addFeedbackBatch(['one', 'one', 'two'])
    assert.ok(calls[0].url.endsWith('/api/detection/records/feedback/batch'))
    assert.deepEqual(JSON.parse(calls[0].body), { record_ids: ['one', 'two'] })
    assert.equal(calls[0].headers['X-Project-Id'], 'project-A')
    await assert.rejects(() => adapter.addFeedbackBatch([]))
    await assert.rejects(() => adapter.addFeedbackBatch(Array.from({ length: 51 }, (_, i) => String(i))))
    assert.equal(calls.length, 1)
  } finally { setProjectScope(''); globalThis.fetch = originalFetch }
})

test('数据集页面显式读取正式和草稿；训练选择器默认查询不混入草稿', async () => {
  const originalFetch = globalThis.fetch
  const { RealAdapter } = await server.ssrLoadModule('/src/services/real-adapter.ts')
  const adapter = new RealAdapter(), calls = []
  try {
    globalThis.fetch = async url => {
      calls.push(new URL(url, 'http://localhost'))
      return new Response(JSON.stringify({ code: 200, data: { total: 2, datasets: [{ id: 'formal', record_kind: 'formal' }, { id: 'draft', record_kind: 'draft', workflow_status: 'draft', sample_count: 8 }] } }), { status: 200 })
    }
    const result = await adapter.listDatasets({ page: 1, pageSize: 200, recordKind: 'all' })
    assert.equal(calls[0].searchParams.get('record_kind'), 'all')
    assert.deepEqual(result.items.map(row => row.record_kind), ['formal', 'draft'])
    assert.equal(result.items[1].sample_count, 8)
    await adapter.listDatasets({ page: 1 })
    assert.equal(calls[1].searchParams.has('record_kind'), false)
  } finally { globalThis.fetch = originalFetch }
})
