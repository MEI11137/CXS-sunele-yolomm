import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import { errorMessage, http } from '@/api/http'
import type { ChatMessage, ChatSession, Project } from '@/types'

interface MessagePage {
  messages: ChatMessage[]
  has_more: boolean
  next_before: number | null
}

/**
 * 项目、会话、消息。
 *
 * 所有数据都来自后端真实接口；客户端不做乐观伪造，失败时把后端文案放进 ``error``。
 *
 * 消息**按会话分开缓存**，并且每次读取都带一个代次（generation）：
 * 会话 A 的慢请求返回时，如果用户已经在看会话 B，就不能把 A 的内容写进当前列表。
 * 只用一个全局数组的旧写法在“A 慢、B 快”或“A 在后台跑完”时会把当前会话覆盖掉。
 */
export const useWorkspaceStore = defineStore('workspace', () => {
  const projects = ref<Project[]>([])
  const deletedProjects = ref<Project[]>([])
  const deletedSessions = ref<ChatSession[]>([])
  const sessionsByProject = reactive<Record<string, ChatSession[]>>({})
  const messagesBySession = reactive<Record<string, ChatMessage[]>>({})
  const pagingBySession = reactive<Record<string, { hasMore: boolean; nextBefore: number | null }>>({})
  const currentSessionId = ref('')
  /** sessionId -> 最近一次读取的代次；只有代次相同的返回才允许落盘。 */
  const loadGeneration = new Map<string, number>()
  const loading = ref(false)
  const error = ref('')

  /** 当前打开的会话的消息；没有选中会话时是空数组。 */
  const messages = computed<ChatMessage[]>(() =>
    currentSessionId.value ? (messagesBySession[currentSessionId.value] ?? []) : [],
  )
  const hasOlderMessages = computed<boolean>(() =>
    currentSessionId.value ? Boolean(pagingBySession[currentSessionId.value]?.hasMore) : false,
  )

  async function run<T>(task: () => Promise<T>): Promise<T | null> {
    loading.value = true
    error.value = ''
    try {
      return await task()
    } catch (failure) {
      error.value = errorMessage(failure)
      return null
    } finally {
      loading.value = false
    }
  }

  function projectById(id: string): Project | null {
    return projects.value.find((item) => item.id === id) ?? null
  }

  function upsertProject(project: Project): void {
    const index = projects.value.findIndex((item) => item.id === project.id)
    if (index >= 0) projects.value[index] = project
    else projects.value.unshift(project)
  }

  async function loadProjects(): Promise<void> {
    await run(async () => {
      const data = await http.get<{ projects: Project[] }>('/projects')
      projects.value = data.projects
    })
  }

  async function loadTrash(): Promise<void> {
    await run(async () => {
      const [deleted, active] = await Promise.all([
        http.get<{ projects: Project[] }>('/projects?trash=1'),
        http.get<{ projects: Project[] }>('/projects'),
      ])
      deletedProjects.value = deleted.projects
      deletedSessions.value = []
      for (const project of active.projects) {
        const data = await http.get<{ sessions: ChatSession[] }>(
          `/projects/${project.id}/sessions?trash=1`,
        )
        deletedSessions.value.push(...data.sessions)
      }
    })
  }

  async function createProject(name: string, description = ''): Promise<Project | null> {
    return run(async () => {
      const data = await http.post<{ project: Project }>('/projects', { name, description })
      upsertProject(data.project)
      return data.project
    })
  }

  async function renameProject(id: string, name: string): Promise<Project | null> {
    return run(async () => {
      const data = await http.put<{ project: Project }>(`/projects/${id}`, { name })
      upsertProject(data.project)
      return data.project
    })
  }

  async function deleteProject(id: string): Promise<boolean> {
    const result = await run(async () => {
      await http.del(`/projects/${id}`)
      projects.value = projects.value.filter((item) => item.id !== id)
      delete sessionsByProject[id]
      return true
    })
    return result === true
  }

  async function restoreProject(id: string): Promise<boolean> {
    const result = await run(async () => {
      await http.post(`/projects/${id}/restore`)
      deletedProjects.value = deletedProjects.value.filter((item) => item.id !== id)
      await loadProjects()
      return true
    })
    return result === true
  }

  async function bindRoot(
    projectId: string,
    path: string,
    replace = false,
  ): Promise<Project | null> {
    return run(async () => {
      const data = await http.put<{ project: Project }>(`/projects/${projectId}/root`, {
        path,
        replace,
      })
      upsertProject(data.project)
      return data.project
    })
  }

  async function unbindRoot(projectId: string): Promise<Project | null> {
    return run(async () => {
      const data = await http.del<{ project: Project }>(`/projects/${projectId}/root`)
      upsertProject(data.project)
      return data.project
    })
  }

  async function loadSessions(projectId: string): Promise<void> {
    await run(async () => {
      const data = await http.get<{ sessions: ChatSession[] }>(`/projects/${projectId}/sessions`)
      sessionsByProject[projectId] = data.sessions
    })
  }

  async function createSession(projectId: string, title: string): Promise<ChatSession | null> {
    return run(async () => {
      const data = await http.post<{ session: ChatSession }>(
        `/projects/${projectId}/sessions`,
        { title },
      )
      const list = sessionsByProject[projectId] ?? []
      sessionsByProject[projectId] = [data.session, ...list]
      return data.session
    })
  }

  async function renameSession(
    projectId: string,
    sessionId: string,
    title: string,
  ): Promise<ChatSession | null> {
    return run(async () => {
      const data = await http.put<{ session: ChatSession }>(`/sessions/${sessionId}`, { title })
      const list = sessionsByProject[projectId] ?? []
      sessionsByProject[projectId] = list.map((item) =>
        item.id === sessionId ? data.session : item,
      )
      return data.session
    })
  }

  async function deleteSession(projectId: string, sessionId: string): Promise<boolean> {
    const result = await run(async () => {
      await http.del(`/sessions/${sessionId}`)
      const list = sessionsByProject[projectId] ?? []
      sessionsByProject[projectId] = list.filter((item) => item.id !== sessionId)
      dropSessionMessages(sessionId)
      return true
    })
    return result === true
  }

  async function restoreSession(projectId: string, sessionId: string): Promise<boolean> {
    const result = await run(async () => {
      await http.post(`/sessions/${sessionId}/restore`)
      deletedSessions.value = deletedSessions.value.filter((item) => item.id !== sessionId)
      await loadSessions(projectId)
      return true
    })
    return result === true
  }

  /** 切换当前会话：只改“在看哪个”，不清别人缓存。 */
  function selectSession(sessionId: string): void {
    currentSessionId.value = sessionId
  }

  /**
   * 读取某个会话的最近一页消息。
   *
   * ``force`` 用于轮次结束后的刷新；``before`` 用于向前翻历史。
   * 返回时代次已经变了，说明期间又发起了更新的读取，这次结果直接丢弃——
   * 否则 A 会话的旧返回会把 B 会话刚加载好的消息盖掉。
   */
  async function loadMessages(
    sessionId: string,
    force = false,
    options: { before?: number } = {},
  ): Promise<void> {
    const before = options.before
    if (!before && !force && (messagesBySession[sessionId] ?? []).length) return
    const generation = (loadGeneration.get(sessionId) ?? 0) + 1
    loadGeneration.set(sessionId, generation)
    const query = before ? `?before=${before}` : ''
    try {
      const data = await http.get<MessagePage>(`/sessions/${sessionId}/messages${query}`)
      if (loadGeneration.get(sessionId) !== generation) return
      if (before) {
        const existing = messagesBySession[sessionId] ?? []
        const known = new Set(existing.map((item) => item.id))
        messagesBySession[sessionId] = [
          ...data.messages.filter((item) => !known.has(item.id)),
          ...existing,
        ]
      } else {
        messagesBySession[sessionId] = data.messages
      }
      pagingBySession[sessionId] = { hasMore: data.has_more, nextBefore: data.next_before }
      error.value = ''
    } catch (failure) {
      if (loadGeneration.get(sessionId) !== generation) return
      error.value = errorMessage(failure)
    }
  }

  /** 向前翻一页更早的消息。 */
  async function loadOlderMessages(sessionId: string): Promise<void> {
    const paging = pagingBySession[sessionId]
    if (!paging?.hasMore || paging.nextBefore === null) return
    await loadMessages(sessionId, true, { before: paging.nextBefore })
  }

  /** 本地追加一条已由后端确认的消息（发送成功后立即可见，不做假数据）。 */
  function appendMessage(sessionId: string, message: ChatMessage): void {
    const list = messagesBySession[sessionId] ?? []
    if (list.some((item) => item.id === message.id)) return
    messagesBySession[sessionId] = [...list, message]
  }

  function dropSessionMessages(sessionId: string): void {
    delete messagesBySession[sessionId]
    delete pagingBySession[sessionId]
    loadGeneration.set(sessionId, (loadGeneration.get(sessionId) ?? 0) + 1)
    if (currentSessionId.value === sessionId) currentSessionId.value = ''
  }

  function reset(): void {
    projects.value = []
    deletedProjects.value = []
    deletedSessions.value = []
    currentSessionId.value = ''
    error.value = ''
    // 清缓存的同时把代次推高：登出后旧账号还没回来的读取结果必须作废。
    for (const key of Object.keys(messagesBySession)) {
      loadGeneration.set(key, (loadGeneration.get(key) ?? 0) + 1)
      delete messagesBySession[key]
    }
    for (const key of Object.keys(pagingBySession)) delete pagingBySession[key]
    for (const key of Object.keys(sessionsByProject)) delete sessionsByProject[key]
  }

  return {
    projects,
    deletedProjects,
    deletedSessions,
    sessionsByProject,
    messages,
    messagesBySession,
    currentSessionId,
    hasOlderMessages,
    loading,
    error,
    projectById,
    loadProjects,
    loadTrash,
    createProject,
    renameProject,
    deleteProject,
    restoreProject,
    bindRoot,
    unbindRoot,
    loadSessions,
    createSession,
    renameSession,
    deleteSession,
    restoreSession,
    selectSession,
    loadMessages,
    loadOlderMessages,
    appendMessage,
    dropSessionMessages,
    reset,
  }
})
