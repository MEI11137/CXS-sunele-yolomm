import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Workspace } from '@/types'
import { getAdapter } from '@/services'

const WS_KEY = 'haixun.currentWorkspace'

export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<Workspace[]>([])
  const currentId = ref<string>(readStoredId())
  const loading = ref(false)
  const loaded = ref(false)

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
    loading.value = true
    try {
      workspaces.value = await getAdapter().listWorkspaces()
      // 当前工作区失效则回退到第一个
      if (!current.value && workspaces.value.length) {
        setCurrent(workspaces.value[0].id)
      }
      loaded.value = true
    } finally {
      loading.value = false
    }
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

  return { workspaces, currentId, current, loading, loaded, load, setCurrent, create, update }
})
