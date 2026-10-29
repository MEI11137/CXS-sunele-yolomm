import { defineStore } from 'pinia'
import { ref, reactive, computed } from 'vue'
import type { Session, ChatMessage, ChatTurn, Attachment } from '@/types'
import type { SendMessageOptions } from '@/services'
import { getAdapter } from '@/services'

export const useSessionStore = defineStore('session', () => {
  const sessions = ref<Session[]>([])
  const currentId = ref<string>('')
  const sending = ref<Record<string, boolean>>({})
  const streaming = computed(() => !!sending.value[currentId.value])
  const loaded = ref(false)
  const activeProjectId = ref('')
  const projectErrors = ref<Record<string, string>>({})
  const projectLoading = ref<Record<string, boolean>>({})
  const selectedByProject = new Map<string, string>()
  const historyLoaded = new Set<string>()
  const historyRequests = new Map<string, Promise<void>>()
  const createRequests = new Map<string, Promise<Session>>()
  const creating = ref<Record<string, boolean>>({})
  let epoch = 0
  const queuedAttachments = ref<Record<string, Attachment[]>>({})
  function queueAttachments(projectId: string, values: Attachment[]) { queuedAttachments.value[projectId] = [...(queuedAttachments.value[projectId] || []), ...values] }
  const removedSessions = new Set<string>()
  const removedProjects = new Set<string>()
  /** 最近一轮对话的完整结果（含任务/动作/计划），供确认卡片使用 */
  const turns = ref<Record<string, ChatTurn>>({})
  const lastTurn = computed<ChatTurn | null>(() => turns.value[currentId.value] ?? null)
  let loadVersion = 0
  const messageVersions = new Map<string, number>()

  function reset() {
    epoch++
    queuedAttachments.value = {}
    loadVersion++
    sessions.value = []
    currentId.value = ''
    loaded.value = false
    sending.value = {}
    turns.value = {}
    messageVersions.clear()
    activeProjectId.value = ''
    projectErrors.value = {}
    projectLoading.value = {}
    creating.value = {}
    selectedByProject.clear()
    historyLoaded.clear()
    historyRequests.clear()
    createRequests.clear()
    removedSessions.clear()
    removedProjects.clear()
  }

  const current = () => sessions.value.find((s) => s.id === currentId.value) ?? null

  function projectSessions(projectId: string) {
    return sessions.value.filter(s => s.project_id === projectId)
      .sort((a, b) => (b.updated_at || b.created_at) - (a.updated_at || a.created_at))
  }

  async function loadProjectHistory(projectId: string, refresh = false): Promise<void> {
    if (!projectId || (!refresh && historyLoaded.has(projectId))) return
    const existing = historyRequests.get(projectId)
    if (existing) return existing
    const token = epoch
    const knownIds = new Set(projectSessions(projectId).map(s => s.id))
    const requestedModes = new Map(projectSessions(projectId).map(s => [s.id, s.permission_mode]))
    projectLoading.value[projectId] = true
    projectErrors.value[projectId] = ''
    const request = (async () => {
      try {
        const result = (await getAdapter().listSessions(projectId)).filter(s => s.project_id === projectId)
        if (token !== epoch || removedProjects.has(projectId)) return
        const visible = result.filter(s => !removedSessions.has(s.id))
        const returnedIds = new Set(visible.map(s => s.id))
        const current = new Map(sessions.value.map(s => [s.id, s]))
        const merged = visible.map(s => {
          const previous = current.get(s.id)
          if (!previous) return s
          // 列表请求发出后用户切换了权限，旧列表不能把新选择覆盖回去。
          const permissionMode = previous.permission_mode !== requestedModes.get(s.id) ? previous.permission_mode : s.permission_mode
          Object.assign(previous, { ...s, messages: previous.messages, permission_mode: permissionMode })
          return previous
        })
        // 保留其他项目与请求期间刚创建的会话，不丢弃正在接收的回复。
        sessions.value = [...sessions.value.filter(s => s.project_id !== projectId ||
          (!knownIds.has(s.id) && !returnedIds.has(s.id))), ...merged]
        historyLoaded.add(projectId)
      } catch (e) {
        if (token === epoch) projectErrors.value[projectId] = (e as Error).message
        throw e
      } finally {
        if (token === epoch) {
          projectLoading.value[projectId] = false
          historyRequests.delete(projectId)
        }
      }
    })()
    historyRequests.set(projectId, request)
    return request
  }

  async function load(projectId: string) {
    const version = ++loadVersion
    activeProjectId.value = projectId
    loaded.value = false
    currentId.value = selectedByProject.get(projectId) || projectSessions(projectId)[0]?.id || ''
    if (!projectId) return
    await loadProjectHistory(projectId)
    if (version !== loadVersion || projectId !== activeProjectId.value) return
    const choices = projectSessions(projectId)
    const selected = selectedByProject.get(projectId)
    currentId.value = choices.find(s => s.id === selected)?.id || choices[0]?.id || ''
    // 会话列表不含完整消息，必须再拉一次历史，避免界面显示"空会话"
    await loadMessages(currentId.value)
    if (version === loadVersion) loaded.value = true
  }

  async function loadMessages(sessionId: string) {
    if (!sessionId) return
    const target = sessions.value.find((s) => s.id === sessionId)
    if (!target) return
    if (sending.value[sessionId]) return
    const version = (messageVersions.get(sessionId) ?? 0) + 1
    messageVersions.set(sessionId, version)
    const messages = await getAdapter().listMessages(sessionId)
    if (!sending.value[sessionId] && messageVersions.get(sessionId) === version && sessions.value.includes(target)) {
      target.messages = messages
    }
  }

  /** 切换会话时按需补拉历史 */
  async function selectSession(id: string) {
    const target = sessions.value.find((s) => s.id === id)
    if (!target || (activeProjectId.value && target.project_id !== activeProjectId.value)) return
    selectedByProject.set(target.project_id, id)
    currentId.value = id
    if (target && target.messages.length === 0) {
      await loadMessages(id)
    }
  }

  async function createSession(projectId: string, title = '新会话') {
    if (!projectId) throw new Error('请先选择工作区')
    const previous = createRequests.get(projectId)
    if (previous) return previous
    const token = epoch
    creating.value[projectId] = true
    const request = (async () => {
      try {
        const s = await getAdapter().createSession(projectId, title)
        if (token !== epoch || removedProjects.has(projectId)) return s
        if (s.project_id !== projectId) throw new Error('会话所属工作区不匹配')
        sessions.value.unshift(s)
        selectedByProject.set(projectId, s.id)
        if (!activeProjectId.value || activeProjectId.value === projectId) currentId.value = s.id
        return s
      } finally {
        if (token === epoch) {
          creating.value[projectId] = false
          createRequests.delete(projectId)
        }
      }
    })()
    createRequests.set(projectId, request)
    return request
  }

  function setCurrent(id: string) {
    void selectSession(id)
  }

  function forgetSession(id: string) {
    removedSessions.add(id)
    const target = sessions.value.find(s => s.id === id)
    sessions.value = sessions.value.filter(s => s.id !== id)
    delete turns.value[id]
    if (target && selectedByProject.get(target.project_id) === id) selectedByProject.delete(target.project_id)
    if (currentId.value === id) currentId.value = projectSessions(activeProjectId.value)[0]?.id || ''
  }

  function forgetProject(id: string) {
    removedProjects.add(id)
    const chats = projectSessions(id)
    for (const chat of chats) delete turns.value[chat.id]
    if (chats.some(chat => chat.id === currentId.value)) currentId.value = ''
    sessions.value = sessions.value.filter(chat => chat.project_id !== id)
    historyLoaded.delete(id)
    selectedByProject.delete(id)
  }

  async function revive(projectId: string, sessionId?: string) {
    removedProjects.delete(projectId)
    if (sessionId) removedSessions.delete(sessionId)
    historyLoaded.delete(projectId)
    await loadProjectHistory(projectId, true)
  }

  function appendMessage(msg: ChatMessage) {
    const s = current()
    if (s) s.messages.push(msg)
  }

  async function send(
    content: string,
    options: SendMessageOptions = {},
    /** 随消息一起展示的附件（仅用于界面回显，真实内容靠 resourceRefs） */
    attachedFiles: Attachment[] = [],
  ): Promise<ChatMessage | null> {
    const s = current()
    if (!s || sending.value[s.id] || (activeProjectId.value && s.project_id !== activeProjectId.value)) return null
    const token = epoch
    messageVersions.set(s.id, (messageVersions.get(s.id) ?? 0) + 1)
    // 用户消息
    const userMsg: ChatMessage = {
      id: 'm-' + Date.now(),
      session_id: s.id,
      role: 'user',
      content,
      created_at: Date.now(),
      attachments: attachedFiles.length ? attachedFiles : undefined,
    }
    s.messages.push(userMsg)
    if (['新会话', '未命名会话'].includes(s.title)) s.title = content.trim().split('\n')[0].slice(0, 60)
    s.updated_at = Date.now()

    // 助手流式消息占位
    const assistantMsg = reactive<ChatMessage>({
      id: 'am-' + Date.now(),
      session_id: s.id,
      role: 'assistant',
      content: '',
      created_at: Date.now(),
      streaming: true,
    })
    s.messages.push(assistantMsg)

    sending.value[s.id] = true
    let received = false
    try {
      // 真实模式返回的是复合结果（reply/task/action/plan），不是单一消息
      const turn = await getAdapter().sendMessage(s.id, content, options)
      if (!sessions.value.includes(s)) return null
      assistantMsg.content = turn.message.content
      assistantMsg.cards = turn.message.cards
      turns.value[s.id] = turn
      received = true
      assistantMsg.streaming = false
      return assistantMsg
    } catch (e) {
      if ((e as DOMException).name === 'AbortError') {
        assistantMsg.content += '\n\n（已停止生成）'
      } else {
        assistantMsg.content = '（请求失败：' + (e as Error).message + '）'
      }
      assistantMsg.streaming = false
      return assistantMsg
    } finally {
      if (token === epoch) sending.value[s.id] = false
      // 用服务端消息 ID 校准；OpenCode 的后续输出由任务轮询读取，不能拼入旧占位消息。
      if (received && token === epoch) {
        try { await loadMessages(s.id) } catch { /* 保留当前回复，下一次轮询继续校准 */ }
      }
    }
  }

  return {
    sessions,
    queuedAttachments,
    queueAttachments,
    activeProjectId,
    projectErrors,
    projectLoading,
    creating,
    projectSessions,
    loadProjectHistory,
    forgetSession,
    forgetProject,
    revive,
    reset,
    currentId,
    streaming,
    loaded,
    lastTurn,
    current,
    load,
    loadMessages,
    selectSession,
    createSession,
    setCurrent,
    appendMessage,
    send,
  }
})
