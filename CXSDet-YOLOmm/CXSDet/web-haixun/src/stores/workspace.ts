import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import type { Workspace } from '@/types'
import { getAdapter } from '@/services'
import { setProjectScope } from '@/services/http'

const WS_KEY = 'haixun.currentWorkspace'

export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<Workspace[]>([])
  const currentId = ref<string>(readStoredId())
  watch(currentId, id => setProjectScope(id), { immediate: true, flush: 'sync' })
  const loading = ref(false)
  const loaded = ref(false)
  let generation = 0

  function readStoredId(): string {
    try {
      return localStorage.getItem(WS_KEY) || ''
    } catch {
      return ''
    }
  }

  const current = computed(
    () => workspaces.value.find((w) => w.id === currentId.value) ?? null,
  )

  async function load() {
    if (loaded.value) return
    const version = generation
    loading.value = true
    try {
      const items = await getAdapter().listWorkspaces()
      if (version !== generation) return
      workspaces.value = items
      // 当前工作区失效则回退到第一个
      if (!current.value && workspaces.value.length) {
        setCurrent(workspaces.value[0].id)
      }
      loaded.value = true
    } finally {
      if (version === generation) loading.value = false
    }
  }

  function reset() {
    generation++
    workspaces.value = []
    setCurrent('')
    loading.value = false
    loaded.value = false
  }

  function setCurrent(id: string) {
    currentId.value = id
    try {
      localStorage.setItem(WS_KEY, id)
    } catch {
      /* ignore */
    }
  }

  async function create(input: {
    root_directory?: string
    name: string
    description: string
    requirements: string
  }) {
    const ws = await getAdapter().createWorkspace(input)
    workspaces.value.push(ws)
    setCurrent(ws.id)
    return ws
  }

  async function update(id: string, patch: Partial<Workspace>) {
    const ws = await getAdapter().updateWorkspace(id, patch)
    const idx = workspaces.value.findIndex((w) => w.id === id)
    if (idx >= 0) workspaces.value[idx] = ws
    return ws
  }

  function forget(id: string) {
    workspaces.value = workspaces.value.filter(project => project.id !== id)
    if (currentId.value === id) setCurrent(workspaces.value.find(project => project.origin !== 'system')?.id || '')
  }

  return { workspaces, currentId, current, loading, loaded, load, reset, setCurrent, create, update, forget }
})
