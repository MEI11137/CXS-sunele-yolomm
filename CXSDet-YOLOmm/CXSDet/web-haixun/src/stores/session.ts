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
  /** 最近一轮对话的完整结果（含任务/动作/计划），供确认卡片使用 */
  const turns = ref<Record<string, ChatTurn>>({})
  const lastTurn = computed<ChatTurn | null>(() => turns.value[currentId.value] ?? null)
  let loadVersion = 0
  const messageVersions = new Map<string, number>()

  function reset() {
    loadVersion++
    sessions.value = []
    currentId.value = ''
    loaded.value = false
    sending.value = {}
    turns.value = {}
    messageVersions.clear()
  }

  const current = () => sessions.value.find((s) => s.id === currentId.value) ?? null

  async function load(projectId: string) {
    const version = ++loadVersion
    const result = await getAdapter().listSessions(projectId)
    if (version !== loadVersion) return
    sessions.value = result
    if (!currentId.value && sessions.value.length) {
      currentId.value = sessions.value[0].id
    }
    // 会话列表不含完整消息，必须再拉一次历史，避免界面显示"空会话"
    await loadMessages(currentId.value)
    loaded.value = true
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
    currentId.value = id
    const target = sessions.value.find((s) => s.id === id)
    if (target && target.messages.length === 0) {
      await loadMessages(id)
    }
  }

  async function createSession(projectId: string, title = '新会话') {
    const s = await getAdapter().createSession(projectId, title)
    sessions.value.unshift(s)
    currentId.value = s.id
    return s
  }

  function setCurrent(id: string) {
    currentId.value = id
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
    if (!s || sending.value[s.id]) return null
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
      sending.value[s.id] = false
      // 用服务端消息 ID 校准；OpenCode 的后续输出由任务轮询读取，不能拼入旧占位消息。
      if (received) {
        try { await loadMessages(s.id) } catch { /* 保留当前回复，下一次轮询继续校准 */ }
      }
    }
  }

  return {
    sessions,
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
