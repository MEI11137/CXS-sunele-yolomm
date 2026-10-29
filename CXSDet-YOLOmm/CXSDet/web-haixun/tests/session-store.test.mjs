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
const session = (id, projectId = 'project') => ({ id, project_id: projectId, title: id, created_at: 1, messages: [] })
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

test('切换权限后迟到的历史列表不能覆盖新模式，后续刷新仍能接受服务端更新', async () => {
  const store = useSessionStore()
  store.sessions = [{ ...session('a'), permission_mode: 'request' }]
  store.currentId = 'a'
  const history = deferred()
  adapter.listSessions = () => history.promise
  const loading = store.loadProjectHistory('project', true)
  store.current().permission_mode = 'assist'
  history.resolve([{ ...session('a'), permission_mode: 'request' }])
  await loading
  assert.equal(store.current().permission_mode, 'assist')
  adapter.listSessions = async () => [{ ...session('a'), permission_mode: 'full' }]
  await store.loadProjectHistory('project', true)
  assert.equal(store.current().permission_mode, 'full')
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
  requests.b.resolve([session('session-b', 'b')])
  await second
  requests.a.resolve([session('session-a', 'a')])
  await first
  assert.equal(store.currentId, 'session-b')
  assert.equal(store.sessions.length, 2)
  assert.equal(store.projectSessions('a')[0].id, 'session-a')
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

test('跨项目点击历史会话后加载真实消息，并保留每个项目各自的选择', async () => {
  const store = useSessionStore()
  adapter.listSessions = async pid => [session(pid + '-1', pid), session(pid + '-2', pid)]
  adapter.listMessages = async id => [message(id + '-message', id, 'assistant', '历史-' + id)]
  await store.load('a')
  await store.selectSession('a-2')
  await store.load('b')
  assert.equal(store.currentId, 'b-1')
  assert.equal(store.current().messages[0].content, '历史-b-1')
  await store.selectSession('a-1') // 不能在 B 中直接选 A 的会话。
  assert.equal(store.currentId, 'b-1')
  await store.load('a')
  assert.equal(store.currentId, 'a-2')
})

test('新建会话接口延迟时切到另一项目，不会把新会话串入当前项目', async () => {
  const store = useSessionStore()
  const creating = deferred()
  adapter.listSessions = async pid => [session(pid + '-existing', pid)]
  await store.load('a')
  adapter.createSession = () => creating.promise
  const pending = store.createSession('a')
  await store.load('b')
  creating.resolve(session('a-new', 'a'))
  await pending
  assert.equal(store.currentId, 'b-existing')
  assert.equal(store.projectSessions('b').length, 1)
  assert.ok(store.projectSessions('a').some(s => s.id === 'a-new'))
})

test('项目列表刷新不替换正在等待回复的会话对象', async () => {
  const store = useSessionStore()
  adapter.listSessions = async pid => [session(pid + '-1', pid)]
  await store.load('a')
  const reply = deferred()
  adapter.sendMessage = () => reply.promise
  const original = store.current()
  const pending = store.send('当前问题')
  await store.loadProjectHistory('a', true)
  assert.equal(store.current(), original)
  assert.equal(store.current().messages[0].content, '当前问题')
  adapter.listMessages = async () => [message('reply', 'a-1', 'assistant', '正确答案')]
  reply.resolve(turn('a-1', '正确答案'))
  await pending
  assert.equal(store.current().messages.at(-1).content, '正确答案')
})

test('重复点击新建只创建一次，退出后迟到的项目列表不能恢复记录', async () => {
  const store = useSessionStore()
  const created = deferred(), history = deferred()
  let calls = 0
  adapter.createSession = () => { calls++; return created.promise }
  const a = store.createSession('project'), b = store.createSession('project')
  created.resolve(session('new'))
  await Promise.all([a, b])
  assert.equal(calls, 1)
  adapter.listSessions = () => history.promise
  const pending = store.loadProjectHistory('other')
  store.reset()
  history.resolve([session('private', 'other')])
  await pending
  assert.equal(store.sessions.length, 0)
})

test('删除会话后，迟到的历史列表不能把它重新显示', async () => {
  const store = useSessionStore()
  store.sessions = [session('removed')]
  store.currentId = 'removed'
  const history = deferred()
  adapter.listSessions = () => history.promise
  const pending = store.loadProjectHistory('project')
  store.forgetSession('removed')
  history.resolve([session('removed')])
  await pending
  assert.equal(store.sessions.length, 0)
  assert.equal(store.currentId, '')
})

test('删除项目隐藏会话，恢复另一个项目不会复活已删除会话', async () => {
  const store = useSessionStore()
  store.sessions = [session('a-1','a'), session('b-1','b')]
  store.forgetSession('a-1')
  store.forgetProject('b')
  assert.equal(store.sessions.length, 0)
  adapter.listSessions = async pid => [session(pid + '-1',pid)]
  await store.revive('b')
  await store.loadProjectHistory('a',true)
  assert.equal(store.projectSessions('b').length, 1)
  assert.equal(store.projectSessions('a').length, 0)
})

test('页面导航只能进入当前项目的受支持功能', async () => {
  const { scopedPage } = await server.ssrLoadModule('/src/utils/projectRoutes.ts')
  assert.equal(scopedPage('a','/review/record-1'), '/projects/a/review/record-1')
  assert.equal(scopedPage('a','/projects/a/datasets?draft=draft-1'), '/projects/a/datasets?draft=draft-1')
  for (const path of ['/projects/b/models','https://evil.invalid','/api/agent/projects','/projects/a/../../settings','/review/..','//evil.invalid']) {
    assert.equal(scopedPage('a',path),null)
  }
})
