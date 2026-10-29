import assert from 'node:assert/strict'
import { after, before, beforeEach, test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

let server, api, useAuthStore, useWorkspaceStore, savedStorage
const storage = new Map()
before(async () => {
  savedStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) }
  server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  ;({ useAuthStore } = await server.ssrLoadModule('/src/stores/auth.ts'))
  ;({ useWorkspaceStore } = await server.ssrLoadModule('/src/stores/workspace.ts'))
  api = (await server.ssrLoadModule('/src/services/index.ts')).getAdapter()
})
after(async () => { await server?.close(); globalThis.localStorage = savedStorage })
beforeEach(() => {
  storage.clear()
  setActivePinia(createPinia())
  api.login = async () => ({ id: 1, username: 'fixture', role: 'admin', token: 'fixture-only-token' })
  api.listWorkspaces = async () => [{ id: 'own-project', name: '本人的项目' }]
})

test('首次登录会重新载入项目，不沿用匿名空列表', async () => {
  const workspace = useWorkspaceStore()
  workspace.loaded = true
  await useAuthStore().login('fixture', 'test-only')
  assert.equal(workspace.loaded, true)
  assert.equal(workspace.currentId, 'own-project')
  assert.equal(workspace.workspaces.length, 1)
})

test('登录前迟到的列表不能覆盖登录后项目', async () => {
  let resolve
  const old = new Promise(done => { resolve = done })
  let calls = 0
  api.listWorkspaces = () => ++calls === 1 ? old : Promise.resolve([{ id: 'own-project', name: '本人的项目' }])
  const workspace = useWorkspaceStore()
  const pending = workspace.load()
  await useAuthStore().login('fixture', 'test-only')
  resolve([{ id: 'old-project', name: '旧请求的项目' }])
  await pending
  assert.deepEqual(workspace.workspaces.map(project => project.id), ['own-project'])
})

test('清理账号状态后旧请求不能重新恢复项目', async () => {
  let resolve
  api.listWorkspaces = () => new Promise(done => { resolve = done })
  const workspace = useWorkspaceStore()
  const pending = workspace.load()
  workspace.reset()
  resolve([{ id: 'old-project', name: '不能恢复的项目' }])
  await pending
  assert.equal(workspace.workspaces.length, 0)
  assert.equal(workspace.loaded, false)
  assert.equal(workspace.currentId, '')
})
