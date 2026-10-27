// node --test tests/session-store.test.mjs；复用 Vite/Vue，不增加测试依赖。
import assert from 'node:assert/strict'
import { after, before, beforeEach, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, watchEffect } from 'vue'

let server, useSessionStore, adapter
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = () => new Promise(resolve => setImmediate(resolve))
const session = id => ({ id, project_id: 'project', title: id, created_at: 1, messages: [] })
const message = (id, sid, role, content) => ({ id, session_id: sid, role, content, created_at: 1 })
const turn = (sid, text) => ({ session_id: sid, message: message('server-' + sid, sid, 'assistant', text), task: null })

before(async () => {
  const root = fileURLToPath(new URL('../', import.meta.url))
  server = await createServer({ root, server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  ;({ useSessionStore } = await server.ssrLoadModule('/src/stores/session.ts'))
  adapter = (await server.ssrLoadModule('/src/services/index.ts')).getAdapter()
})
after(async () => { await server?.close() })
beforeEach(() => {
  setActivePinia(createPinia())
  adapter.listMessages = async () => []
  adapter.listSessions = async () => []
})

test('回复到达时立即响应渲染，最终使用服务端消息 ID', async () => {
  const store = useSessionStore()
  store.sessions = [session('a')]
  store.currentId = 'a'
  const reply = deferred(), history = deferred()
  adapter.sendMessage = () => reply.promise
  adapter.listMessages = () => history.promise
  const displayed = []
  const stop = watchEffect(() => displayed.push(store.current()?.messages.at(-1)?.content))
  const pending = store.send('question-a')
  reply.resolve(turn('a', 'answer-a'))
  await tick(); await nextTick()
  assert.ok(displayed.includes('answer-a'))
  history.resolve([message('user-a', 'a', 'user', 'question-a'), message('server-a', 'a', 'assistant', 'answer-a')])
  await pending
  assert.equal(store.current().messages.at(-1).id, 'server-a')
  stop()
})

test('切换会话后，A 的延迟回复不能污染 B 或提前结束 B 的请求', async () => {
  const store = useSessionStore()
  store.sessions = [session('a'), session('b')]
  const replies = { a: deferred(), b: deferred() }
  adapter.sendMessage = sid => replies[sid].promise
  adapter.listMessages = async sid => [message('server-' + sid, sid, 'assistant', 'answer-' + sid)]
  store.currentId = 'a'
  const first = store.send('question-a')
  store.currentId = 'b'
  assert.equal(store.streaming, false)
  const second = store.send('question-b')
  replies.a.resolve(turn('a', 'answer-a'))
  await first
  assert.equal(store.streaming, true)
  assert.equal(store.lastTurn, null)
  assert.ok(store.current().messages.every(item => !item.content.includes('answer-a')))
  replies.b.resolve(turn('b', 'answer-b'))
  await second
  assert.equal(store.current().messages.at(-1).content, 'answer-b')
  assert.equal(store.lastTurn.session_id, 'b')
})

test('慢历史请求不能覆盖刚发送的新消息', async () => {
  const store = useSessionStore()
  store.sessions = [session('a')]
  store.currentId = 'a'
  const history = deferred(), reply = deferred()
  adapter.listMessages = () => history.promise
  const loading = store.loadMessages('a')
  adapter.sendMessage = () => reply.promise
  const sending = store.send('new-question')
  history.resolve([message('old', 'a', 'assistant', 'old-answer')])
  await loading
  assert.equal(store.current().messages[0].content, 'new-question')
  adapter.listMessages = async () => [message('server-a', 'a', 'assistant', 'new-answer')]
  reply.resolve(turn('a', 'new-answer'))
  await sending
  assert.equal(store.current().messages.at(-1).content, 'new-answer')
})

test('项目列表乱序返回时保留后选择的项目', async () => {
  const store = useSessionStore()
  const requests = { a: deferred(), b: deferred() }
  adapter.listSessions = id => requests[id].promise
  const first = store.load('a'), second = store.load('b')
  requests.b.resolve([session('session-b')])
  await second
  requests.a.resolve([session('session-a')])
  await first
  assert.equal(store.currentId, 'session-b')
  assert.equal(store.sessions.length, 1)
})

test('退出登录后，迟到的回复不能恢复上一账号的消息与任务', async () => {
  const store = useSessionStore()
  store.sessions = [session('a')]
  store.currentId = 'a'
  const reply = deferred()
  adapter.sendMessage = () => reply.promise
  const sending = store.send('private-question')
  store.reset()
  reply.resolve(turn('a', 'private-answer'))
  await sending
  assert.equal(store.sessions.length, 0)
  assert.equal(store.lastTurn, null)
  assert.equal(store.streaming, false)
})

test('请求错误保留可见说明，不被立即拉历史抹掉', async () => {
  const store = useSessionStore()
  store.sessions = [session('a')]
  store.currentId = 'a'
  adapter.sendMessage = async () => { throw new Error('engine unavailable') }
  await store.send('question')
  assert.match(store.current().messages.at(-1).content, /engine unavailable/)
})
