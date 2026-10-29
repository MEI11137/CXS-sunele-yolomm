import assert from 'node:assert/strict'
import { before, after, test } from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

let server, bridge, mapper
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  bridge = await server.ssrLoadModule('/src/utils/workspaceBridge.ts')
  mapper = await server.ssrLoadModule('/src/services/mappers.ts')
})
after(async () => { await server?.close() })

test('界面请求只在当前项目当前会话处理，历史事件不会重放', () => {
  const event = { type: 'workspace.download', created_at: 1000, sequence: 5, payload: { source_project_id: 'A', source_session_id: 'chat', asset_id: 'asset' } }
  assert.equal(bridge.workspaceEffect(event, 'B', 'chat', 500), null)
  assert.equal(bridge.workspaceEffect(event, 'A', 'other', 500), null)
  assert.equal(bridge.workspaceEffect(event, 'A', 'chat', 1500), null)
  assert.equal(bridge.workspaceEffect({ ...event, type: 'tool.result' }, 'A', 'chat', 500), null)
  assert.equal(bridge.workspaceEffect(event, 'A', 'chat', 500).payload.asset_id, 'asset')
})

test('只有真实执行的自身删除能退出会话，拒绝或失败不退出', () => {
  const raw = { id: 'act', tool_name: 'workspace_manage_session', status: 'succeeded', execution_state: 'executed', parameters: { operation: 'delete' }, result: { outputs: { context_closed: true, id: 'chat' } } }
  assert.deepEqual(bridge.closedContext(mapper.mapConfirmation(raw)), { kind: 'session', id: 'chat' })
  for (const execution_state of ['unknown', 'not_executed', 'cancelled']) assert.equal(bridge.closedContext(mapper.mapConfirmation({ ...raw, execution_state })), null)
  assert.equal(bridge.closedContext(mapper.mapConfirmation({ ...raw, parameters: { operation: 'restore' } })), null)
  assert.equal(bridge.closedContext(mapper.mapConfirmation({ ...raw, result: { outputs: { context_closed: false } } })), null)
})
