import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Session, ChatMessage, ChatTurn } from '@/types'
import { getAdapter } from '@/services'

export const useSessionStore = defineStore('session', () => {
  const sessions = ref<Session[]>([])
  const currentId = ref<string>('')
  const streaming = ref(false)
  const loaded = ref(false)
  /** 最近一轮对话的完整结果（含任务/动作/计划），供确认卡片使用 */
  const lastTurn = ref<ChatTurn | null>(null)

  const current = () => sessions.value.find((s) => s.id === currentId.value) ?? null

  async function load(projectId: string) {
    sessions.value = await getAdapter().listSessions(projectId)
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
    target.messages = await getAdapter().listMessages(sessionId)
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
    onDelta?: (delta: string) => void,
    signal?: AbortSignal,
  ): Promise<ChatMessage | null> {
    const s = current()
    if (!s) return null
    // 用户消息
    const userMsg: ChatMessage = {
      id: 'm-' + Date.now(),
      session_id: s.id,
      role: 'user',
      content,
      created_at: Date.now(),
    }
    s.messages.push(userMsg)

    // 助手流式消息占位
    const assistantMsg: ChatMessage = {
      id: 'am-' + Date.now(),
      session_id: s.id,
      role: 'assistant',
      content: '',
      created_at: Date.now(),
      streaming: true,
    }
    s.messages.push(assistantMsg)

    streaming.value = true
    try {
      // 真实模式返回的是复合结果（reply/task/action/plan），不是单一消息
      const turn = await getAdapter().sendMessage(s.id, content, onDelta, signal)
      assistantMsg.content = turn.message.content
      assistantMsg.cards = turn.message.cards
      lastTurn.value = turn
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
      streaming.value = false
    }
  }

  return {
    sessions,
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
