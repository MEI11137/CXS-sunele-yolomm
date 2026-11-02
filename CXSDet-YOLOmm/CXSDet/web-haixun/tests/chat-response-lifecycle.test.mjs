import assert from 'node:assert/strict'
import { before, after, beforeEach, test } from 'node:test'
import { createServer } from 'vite'
import { createPinia, setActivePinia, getActivePinia } from 'pinia'
import { createRenderer, ssrContextKey } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { fileURLToPath } from 'node:url'

let server, api, http, useSessionStore, useWorkspaceStore, Progress, savedFetch
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  ;({ useSessionStore } = await server.ssrLoadModule('/src/stores/session.ts'))
  ;({ useWorkspaceStore } = await server.ssrLoadModule('/src/stores/workspace.ts'))
  Progress = (await server.ssrLoadModule('/src/components/AgentProgress.vue')).default
  api = (await server.ssrLoadModule('/src/services/index.ts')).getAdapter()
  http = await server.ssrLoadModule('/src/services/http.ts')
  savedFetch = globalThis.fetch
})
beforeEach(() => { setActivePinia(createPinia()); globalThis.fetch = savedFetch })
after(async () => { globalThis.fetch = savedFetch; await server?.close() })

test('任务查询失败时，执行后的最终文字仍自动刷新，无需再次发送', async () => {
  const store = useSessionStore(), workspace = useWorkspaceStore()
  workspace.currentId = 'p'
  store.sessions = [{ id: 's', project_id: 'p', title: 'test', messages: [], created_at: 1 }]
  store.currentId = 's'
  api.listTasks = async () => { throw new Error('task endpoint temporarily unavailable') }
  api.listEvents = async () => []
  let messagesRead = 0
  api.listMessages = async () => {
    messagesRead++
    return [{ id: 'final', session_id: 's', role: 'assistant', content: '数据集检查完成', created_at: 2 }]
  }
  const node = () => ({ children: [], props: {} })
  const renderer = createRenderer({
    createElement: node, createText: node, createComment: node,
    setText() {}, setElementText() {}, patchProp() {},
    insert(child, parent) { if (parent) parent.children.push(child) }, remove() {},
    parentNode() { return null }, nextSibling() { return null },
  })
  const app = renderer.createApp({ ...Progress, render: () => null })
  app.use(getActivePinia())
  app.provide(ssrContextKey, { modules: new Set() })
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  try {
    app.mount(node())
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(messagesRead, 1)
    assert.equal(store.current().messages.at(-1)?.content, '数据集检查完成')
  } finally { app.unmount() }
})

test('后台只读请求超时后可再次查询，不重复发送写入请求', async () => {
  let requests = 0, mode = 'stall'
  globalThis.fetch = async (_url, options) => {
    requests++
    assert.equal(options.cache, 'no-store')
    if (mode === 'stall') return new Promise((_, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('timeout', 'AbortError')), { once: true })
    })
    return new Response(JSON.stringify({ code: 200, data: ['final reply'] }))
  }
  await assert.rejects(http.http.get('/api/read', { timeoutMs: 10 }), e => e.code === 'READ_TIMEOUT')
  assert.equal(requests, 1)
  mode = 'ok'
  assert.deepEqual(await http.http.get('/api/read', { timeoutMs: 100 }), ['final reply'])
  assert.equal(requests, 2)
})
