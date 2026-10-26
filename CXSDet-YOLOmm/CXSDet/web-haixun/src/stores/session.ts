import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Session, ChatMessage } from '@/types'
import { getAdapter } from '@/services'

export const useSessionStore = defineStore('session', () => {
  const sessions = ref<Session[]>([])
  const currentId = ref<string>('')
  const streaming = ref(false)
  const loaded = ref(false)

  const current = () => sessions.value.find((s) => s.id === currentId.value) ?? null

  async function load(projectId: string) {
    sessions.value = await getAdapter().listSessions(projectId)
    if (!currentId.value && sessions.value.length) {
      currentId.value = sessions.value[0].id
    }
    loaded.value = true
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
      const full = await getAdapter().sendMessage(s.id, content, onDelta, signal)
      assistantMsg.content = full.content
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
    current,
    load,
    createSession,
    setCurrent,
    appendMessage,
    send,
  }
})
