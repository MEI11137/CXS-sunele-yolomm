import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import { errorCode, errorMessage, errorStatus, http } from '@/api/http'
import { isTerminalStatus, lastSeq, mergeEvents, pendingActions, pollDelay } from '@/lib/timeline'
import type {
  AgentAction,
  AgentCapabilities,
  AgentPreferences,
  ChatMessage,
  PermissionMode,
  SessionTurns,
  Turn,
  TurnDetail,
  TurnEvent,
} from '@/types'
import { useWorkspaceStore } from '@/stores/workspace'

/**
 * Agent 运行时（OpenCode 引擎 + 私有工具桥）。
 *
 * 分工：
 * * 已结束的对话内容以 ``workspace.messages``（数据库真实消息）为准；
 * * 进行中的这一轮用轮次事件（tools/text/notice）实时呈现；
 * * 页面刷新或切回来时用 ``/sessions/{id}/turns`` 找回活动轮次继续拉事件，
 *   所以关掉侧栏、刷新页面都不会中断后台那一轮。
 *
 * 一切都来自后端：这里不伪造助手回复、不乐观插入消息、失败就如实报错。
 */

const POLL_CEILING_MS = 20 * 60 * 1000

export const useAgentStore = defineStore('agent', () => {
  const workspace = useWorkspaceStore()

  const capabilities = ref<AgentCapabilities | null>(null)
  const preferences = ref<AgentPreferences | null>(null)
  const loading = ref(false)
  const sending = ref(false)
  const stopping = ref(false)
  const deciding = ref('')
  const error = ref('')
  const notice = ref('')

  /** sessionId -> 该会话当前这一轮（含刚结束的终态，用于显示失败原因） */
  const live = reactive<Record<string, Turn | null>>({})
  /** turnId -> 事件（只追加） */
  const events = reactive<Record<string, TurnEvent[]>>({})
  /** turnId -> 该轮的动作（含已处理） */
  const actions = reactive<Record<string, AgentAction[]>>({})
  /** sessionId -> 待确认动作 */
  const sessionActions = reactive<Record<string, AgentAction[]>>({})
  /** turnId -> 轮询失败原因（不覆盖整页错误，只在轮次附近提示） */
  const pollIssue = reactive<Record<string, string>>({})

  const watching = new Set<string>()
  const timers = new Map<string, ReturnType<typeof setTimeout>>()
  const pollStarted = new Map<string, number>()
  /**
   * 登录代次：登出/切换账号后自增。轮询在 await 回来后必须核对代次，
   * 否则上一个账号还没返回的响应会重新写进状态、甚至重启定时器。
   */
  let generation = 0
  /**
   * 待提交请求：网络回包丢失后的重试必须复用同一个 request_id，
   * 否则服务端会当成第二轮，重复计费、重复落库。
   */
  const pendingRequest = new Map<string, { requestId: string; content: string }>()

  const modes = computed(() => preferences.value?.modes ?? capabilities.value?.modes ?? [])
  const model = computed(() => preferences.value?.model ?? capabilities.value?.connection.model ?? null)
  const models = computed(() => preferences.value?.models ?? [])
  const ready = computed(() => Boolean(capabilities.value))
  const shellEnabled = computed(() => Boolean(capabilities.value?.runtime?.shell_enabled))

  function activeTurn(sessionId: string): Turn | null {
    return live[sessionId] ?? null
  }

  function turnEvents(turnId: string): TurnEvent[] {
    return events[turnId] ?? []
  }

  function turnActions(turnId: string): AgentAction[] {
    return actions[turnId] ?? []
  }

  function pending(sessionId: string): AgentAction[] {
    return pendingActions(sessionActions[sessionId] ?? [])
  }

  function issueFor(turnId: string): string {
    return pollIssue[turnId] ?? ''
  }

  // ------------------------------------------------------------------ 读取

  async function loadCapabilities(): Promise<void> {
    loading.value = true
    try {
      capabilities.value = await http.get<AgentCapabilities>('/agent/capabilities')
    } catch (failure) {
      error.value = errorMessage(failure)
    } finally {
      loading.value = false
    }
  }

  async function loadPreferences(): Promise<void> {
    try {
      preferences.value = await http.get<AgentPreferences>('/me/preferences')
    } catch (failure) {
      error.value = errorMessage(failure)
    }
  }

  async function setMode(mode: PermissionMode): Promise<boolean> {
    if (preferences.value?.mode === mode) return true
    try {
      preferences.value = await http.put<AgentPreferences>('/me/preferences', { mode })
      notice.value = `权限模式已切换为「${preferences.value.mode_label}」，对下一轮消息生效`
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    }
  }

  async function setModel(target: string): Promise<boolean> {
    if (preferences.value?.model === target) return true
    try {
      preferences.value = await http.put<AgentPreferences>('/me/preferences', { model: target })
      notice.value = `模型已切换为 ${target}，对下一轮消息生效`
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    }
  }

  async function refreshSessionActions(sessionId: string): Promise<void> {
    try {
      const data = await http.get<{ actions: AgentAction[] }>(`/sessions/${sessionId}/actions`)
      sessionActions[sessionId] = data.actions
    } catch {
      // 待确认动作拉取失败不能影响对话本身，下一次轮询/操作会再试。
    }
  }

  /** 打开或切回某个会话时调用：找回服务端仍在跑的轮次，继续按事件续拉。 */
  async function resumeSession(sessionId: string): Promise<void> {
    if (!sessionId) return
    const token = generation
    try {
      const data = await http.get<SessionTurns>(`/sessions/${sessionId}/turns`)
      if (token !== generation) return
      sessionActions[sessionId] = data.actions
      if (data.active) {
        live[sessionId] = data.active
        startWatching(data.active.id)
        return
      }
      // 没有活动轮次时不要直接清空：上一轮可能刚失败。
      // 先看内存里有没有刚结束的轮次，没有就取服务端最近一轮——
      // 这样刷新页面后也能看到“为什么没有回答”，而不是一片空白。
      const previous = live[sessionId]
      if (previous && isTerminalStatus(previous.status)) return
      const latest = data.turns?.[0] ?? null
      live[sessionId] = latest && isTerminalStatus(latest.status) ? latest : null
      if (latest && isTerminalStatus(latest.status) && latest.status !== 'completed') {
        // 失败/停止的那一轮，过程事件也要能展开看。
        void loadTurnEvents(latest.id)
      }
    } catch {
      // 仅用于恢复现场，失败不打断用户看历史消息。
    }
  }

  // ------------------------------------------------------------------ 发送 / 停止

  async function send(sessionId: string, content: string): Promise<boolean> {
    const text = content.trim()
    if (!text || sending.value) return false
    sending.value = true
    error.value = ''
    notice.value = ''
    // 同一段内容沿用同一个 request_id：网络断在半路时，用户再点一次发送
    // 拿到的是**同一轮**，不会变成两轮、两份计费。
    const pending = pendingRequest.get(sessionId)
    const requestId =
      pending && pending.content === text ? pending.requestId : newRequestId()
    pendingRequest.set(sessionId, { requestId, content: text })
    try {
      const data = await http.post<{
        turn: Turn
        message: ChatMessage | null
        resumed: boolean
        events: TurnEvent[]
        actions: AgentAction[]
      }>(`/sessions/${sessionId}/turns`, { content: text, request_id: requestId })
      pendingRequest.delete(sessionId)
      adopt(data.turn, data.events ?? [], data.actions ?? [])
      if (data.message) workspace.appendMessage(sessionId, data.message)
      await workspace.loadMessages(sessionId, true)
      return true
    } catch (failure) {
      if (errorCode(failure) === 'TURN_BUSY') {
        pendingRequest.delete(sessionId)
        notice.value = '该会话还有一轮没有结束，已切到正在进行的这一轮'
        await resumeSession(sessionId)
        return false
      }
      // 服务端明确拒绝（4xx/5xx）说明这次请求有确定结论，旧 ID 不能再用；
      // 状态 0 表示请求没到服务器、结果未知，必须保留同一个 request_id，
      // 让用户的重试仍然落在**同一轮**上。
      if (errorStatus(failure) >= 400) pendingRequest.delete(sessionId)
      error.value = errorMessage(failure)
      return false
    } finally {
      sending.value = false
    }
  }

  async function stop(turnId: string): Promise<void> {
    stopping.value = true
    error.value = ''
    try {
      const data = await http.post<{ stopped: boolean; already: boolean; turn: Turn }>(
        `/turns/${turnId}/stop`,
        {},
      )
      live[data.turn.session_id] = data.turn
      notice.value = data.already
        ? '这一轮已经结束，无需停止'
        : '已请求停止；后续步骤不会再执行，等引擎确认后状态会更新'
      startWatching(turnId)
    } catch (failure) {
      error.value = errorMessage(failure)
    } finally {
      stopping.value = false
    }
  }

  // ------------------------------------------------------------------ 动作确认

  async function approve(actionId: string): Promise<boolean> {
    if (deciding.value) return false
    deciding.value = actionId
    error.value = ''
    notice.value = ''
    try {
      const data = await http.post<{
        action: AgentAction | null
        executed: boolean
        result: Record<string, unknown>
        turn: Turn | null
      }>(`/actions/${actionId}/approve`, {})
      const action = data.action
      if (action) {
        upsertAction(action)
        await refreshSessionActions(action.session_id)
        if (data.turn) adopt(data.turn, [], [])
        await workspace.loadMessages(action.session_id, true)
        notice.value = data.executed ? '已执行你确认的动作' : '该动作此前已经执行过，没有重复执行'
      }
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      deciding.value = ''
    }
  }

  async function reject(actionId: string): Promise<boolean> {
    if (deciding.value) return false
    deciding.value = actionId
    error.value = ''
    try {
      const data = await http.post<{ action: AgentAction | null; rejected: boolean }>(
        `/actions/${actionId}/reject`,
        {},
      )
      if (data.action) {
        upsertAction(data.action)
        await refreshSessionActions(data.action.session_id)
        notice.value = data.rejected ? '已拒绝该动作，助手不会执行它' : '该动作已不在待确认状态'
      }
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      deciding.value = ''
    }
  }

  function upsertAction(action: AgentAction): void {
    const list = actions[action.turn_id] ?? []
    const index = list.findIndex((item) => item.id === action.id)
    actions[action.turn_id] =
      index >= 0 ? list.map((item) => (item.id === action.id ? action : item)) : [...list, action]
  }

  // ------------------------------------------------------------------ 轮询

  /** 单独拉某一轮的事件（终态追溯用；已在轮询里的那一轮不重复拉）。 */
  async function loadTurnEvents(turnId: string): Promise<void> {
    if (events[turnId]?.length) return
    const token = generation
    try {
      const detail = await http.get<TurnDetail>(
        `/turns/${turnId}?after=${lastSeq(events[turnId] ?? [])}`,
      )
      if (token !== generation) return
      events[turnId] = mergeEvents(events[turnId] ?? [], detail.events)
    } catch {
      // 追溯失败不影响主流程，用户仍能看到失败原因。
    }
  }

  function adopt(turn: Turn, incoming: TurnEvent[], turnActions: AgentAction[]): void {
    live[turn.session_id] = turn
    if (incoming.length) {
      events[turn.id] = mergeEvents(events[turn.id] ?? [], incoming)
    }
    if (turnActions.length) {
      const merged = new Map((actions[turn.id] ?? []).map((item) => [item.id, item]))
      for (const action of turnActions) merged.set(action.id, action)
      actions[turn.id] = [...merged.values()]
    }
    if (isTerminalStatus(turn.status)) {
      // 按会话刷新：缓存是按会话存的，不会把用户正在看的另一个会话覆盖掉。
      void workspace.loadMessages(turn.session_id, true)
      void refreshSessionActions(turn.session_id)
      return
    }
    startWatching(turn.id)
  }

  function startWatching(turnId: string): void {
    if (watching.has(turnId)) return
    watching.add(turnId)
    pollStarted.set(turnId, Date.now())
    schedule(turnId, 0)
  }

  function schedule(turnId: string, attempt: number): void {
    const existing = timers.get(turnId)
    if (existing !== undefined) clearTimeout(existing)
    const timer = setTimeout(() => {
      timers.delete(turnId)
      void poll(turnId, attempt)
    }, pollDelay(attempt))
    timers.set(turnId, timer)
  }

  function stopWatching(turnId: string): void {
    watching.delete(turnId)
    const timer = timers.get(turnId)
    if (timer !== undefined) {
      clearTimeout(timer)
      timers.delete(turnId)
    }
    delete pollIssue[turnId]
  }

  async function poll(turnId: string, attempt: number): Promise<void> {
    if (!watching.has(turnId)) return
    const token = generation
    const started = pollStarted.get(turnId) ?? 0
    if (started && Date.now() - started > POLL_CEILING_MS) {
      // 前端不无限轮询：后端有自己的超时，这里只是不再占用浏览器。
      pollIssue[turnId] = '这个轮次已经持续很久，界面停止自动刷新；重新打开会话可继续查看状态。'
      stopWatching(turnId)
      return
    }
    let detail: TurnDetail
    try {
      detail = await http.get<TurnDetail>(`/turns/${turnId}?after=${lastSeq(events[turnId] ?? [])}`)
    } catch (failure) {
      // 登出/换账号之后还没返回的请求：不能写状态，也不能重启定时器。
      if (token !== generation) return
      const status = errorStatus(failure)
      if (status === 401 || status === 403 || status === 404) {
        // 这些是**确定结论**（未登录/无权限/轮次不在了），退避重试永远不会成功。
        pollIssue[turnId] = errorMessage(failure)
        stopWatching(turnId)
        return
      }
      pollIssue[turnId] = '与后端的连接中断，正在重试：' + errorMessage(failure)
      schedule(turnId, attempt + 1)
      return
    }
    if (token !== generation) return
    delete pollIssue[turnId]
    live[detail.turn.session_id] = detail.turn
    events[turnId] = mergeEvents(events[turnId] ?? [], detail.events)
    if (detail.actions.length) {
      const merged = new Map((actions[turnId] ?? []).map((item) => [item.id, item]))
      for (const action of detail.actions) merged.set(action.id, action)
      actions[turnId] = [...merged.values()]
    }
    if (isTerminalStatus(detail.turn.status)) {
      stopWatching(turnId)
      // 中间态提示（“已请求停止，等待引擎确认…”）随终态一起失效。
      notice.value = ''
      await workspace.loadMessages(detail.turn.session_id, true)
      await refreshSessionActions(detail.turn.session_id)
      return
    }
    schedule(turnId, 0)
  }

  // ------------------------------------------------------------------ 复位

  function reset(): void {
    // 先推高代次：所有在途请求（轮询、恢复会话、发送）回来后都会因代次不符而作废。
    generation += 1
    pendingRequest.clear()
    for (const turnId of [...watching]) stopWatching(turnId)
    for (const key of Object.keys(live)) delete live[key]
    for (const key of Object.keys(events)) delete events[key]
    for (const key of Object.keys(actions)) delete actions[key]
    for (const key of Object.keys(sessionActions)) delete sessionActions[key]
    for (const key of Object.keys(pollIssue)) delete pollIssue[key]
    capabilities.value = null
    preferences.value = null
    error.value = ''
    notice.value = ''
    sending.value = false
    stopping.value = false
    deciding.value = ''
  }

  return {
    capabilities,
    preferences,
    loading,
    sending,
    stopping,
    deciding,
    error,
    notice,
    live,
    events,
    actions,
    sessionActions,
    pollIssue,
    modes,
    model,
    models,
    ready,
    shellEnabled,
    activeTurn,
    turnEvents,
    turnActions,
    pending,
    issueFor,
    loadCapabilities,
    loadPreferences,
    setMode,
    setModel,
    refreshSessionActions,
    resumeSession,
    loadTurnEvents,
    send,
    stop,
    approve,
    reject,
    reset,
  }
})

/** 32 位十六进制，满足后端 `^[A-Za-z0-9_-]{8,64}$`：重复点击/刷新拿到的是同一轮。 */
function newRequestId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
}
