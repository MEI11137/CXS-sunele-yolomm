import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

test('工具生命周期合并但独立调用和任务不合并', async () => {
  const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  try {
    const { toolActivities } = await server.ssrLoadModule('/src/utils/toolActivity.ts')
    const event = (id, status, part, task = 'task') => ({ event_id: id, sequence: Number(id), task_id: task, type: status === 'completed' ? 'tool.result' : 'tool.requested', payload: { engine: 'opencode', tool: 'read', status, tool_part_id: part } })
    const rows = toolActivities([event('1','running','a'), { ...event('2','completed','bridge'), payload: { tool: 'read', status: 'succeeded' } }, event('3','completed','a'), event('4','running','b'), event('5','completed','b'), event('6','completed','c','other')], 'task')
    assert.equal(rows.length, 2)
    assert.equal(rows[0].status, 'completed')
    assert.deepEqual(rows.map(row => row.events.length), [2, 2])
    const legacy = toolActivities([event('1','running',undefined), event('2','completed',undefined), event('3','running',undefined)])
    assert.equal(legacy.length, 2)
    const cancelled = toolActivities([
      { ...event('7', 'running', 'stale'), payload: { engine: 'opencode', tool: 'bash', status: 'running', tool_part_id: 'stale', call_id: 'call-a' } },
      { ...event('8', 'running', 'live'), payload: { engine: 'opencode', tool: 'bash', status: 'running', tool_part_id: 'live', call_id: 'call-b' } },
    ], 'task', [{ tool_name: 'opencode_permission', task_id: 'task', status: 'rejected', parameters: { detail: { tool: { callID: 'call-a' } } } }])
    assert.deepEqual(cancelled.map(row => row.status), ['cancelled', 'running'])
  } finally { await server.close() }
})
