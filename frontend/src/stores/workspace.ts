import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import { errorMessage, http } from '@/api/http'
import type { ChatMessage, ChatSession, Project } from '@/types'

/**
 * 项目、会话、消息。
 *
 * 所有数据都来自后端真实接口；客户端不做乐观伪造，失败时把后端文案放进 ``error``。
 */
export const useWorkspaceStore = defineStore('workspace', () => {
  const projects = ref<Project[]>([])
  const deletedProjects = ref<Project[]>([])
  const deletedSessions = ref<ChatSession[]>([])
  const sessionsByProject = reactive<Record<string, ChatSession[]>>({})
  const messages = ref<ChatMessage[]>([])
  const messagesSessionId = ref('')
  const lastNotice = ref('')
  const loading = ref(false)
  const error = ref('')

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
      if (messagesSessionId.value === sessionId) {
        messages.value = []
        messagesSessionId.value = ''
      }
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

  async function loadMessages(sessionId: string): Promise<void> {
    if (messagesSessionId.value === sessionId && messages.value.length) return
    await run(async () => {
      const data = await http.get<{ messages: ChatMessage[] }>(`/sessions/${sessionId}/messages`)
      messages.value = data.messages
      messagesSessionId.value = sessionId
      lastNotice.value = ''
    })
  }

  async function sendMessage(sessionId: string, content: string): Promise<boolean> {
    const result = await run(async () => {
      const data = await http.post<{ message: ChatMessage; notice: string }>(
        `/sessions/${sessionId}/messages`,
        { content },
      )
      if (messagesSessionId.value !== sessionId) {
        messages.value = []
        messagesSessionId.value = sessionId
      }
      messages.value = [...messages.value, data.message]
      lastNotice.value = data.notice
      return true
    })
    return result === true
  }

  function reset(): void {
    projects.value = []
    deletedProjects.value = []
    deletedSessions.value = []
    messages.value = []
    messagesSessionId.value = ''
    lastNotice.value = ''
    error.value = ''
    for (const key of Object.keys(sessionsByProject)) delete sessionsByProject[key]
  }

  return {
    projects,
    deletedProjects,
    deletedSessions,
    sessionsByProject,
    messages,
    messagesSessionId,
    lastNotice,
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
    loadMessages,
    sendMessage,
    reset,
  }
})
