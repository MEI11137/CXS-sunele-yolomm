/**
 * 时间线纯逻辑：事件增量合并、工具卡片、流式文本、滚动与轮询节奏。
 *
 * 这里刻意不依赖 Vue 也不依赖任何模块别名，方便用 `node --test` 直接跑；
 * 组件与 store 只负责把它的结果渲染出来。
 */

export interface TimelineEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
}

export interface ToolCallCard {
  callId: string
  tool: string
  title: string
  state: string
  durationMs: number | null
  preview: string
}

/** 距底部多少像素内算“跟随最新”。 */
export const STICK_THRESHOLD = 96

const POLL_BASE_MS = 400
const POLL_MAX_MS = 3000

export function isTerminalStatus(status: string): boolean {
  return status === 'completed' || status === 'failed' || status === 'stopped'
}

/**
 * 按 seq 合并事件：重连/刷新后可能重复拉到同一批事件，这里保证不重复渲染。
 * 事件在库里只追加，所以按 seq 排序即可还原真实发生顺序。
 */
export function mergeEvents<T extends TimelineEvent>(previous: T[], incoming: T[]): T[] {
  if (!incoming.length) return previous
  const seen = new Set(previous.map((event) => event.seq))
  const merged = previous.slice()
  for (const event of incoming) {
    if (seen.has(event.seq)) continue
    seen.add(event.seq)
    merged.push(event)
  }
  merged.sort((left, right) => left.seq - right.seq)
  return merged
}

export function lastSeq(events: TimelineEvent[]): number {
  return events.length ? events[events.length - 1].seq : 0
}

/**
 * 工具调用卡片：同一个 callID 的多次状态更新只保留最新一条，顺序按首次出现。
 * 这样“准备调用 → 执行中 → 已完成”不会刷出三张卡。
 */
export function toolCards(events: TimelineEvent[]): ToolCallCard[] {
  const cards: ToolCallCard[] = []
  const index = new Map<string, number>()
  for (const event of events) {
    if (event.type !== 'tool') continue
    const payload = event.payload ?? {}
    const rawId = String(payload.call_id ?? '')
    const key = rawId || `seq-${event.seq}`
    const tool = String(payload.tool ?? '')
    const card: ToolCallCard = {
      callId: key,
      tool,
      title: String(payload.title ?? '') || tool || '工具调用',
      state: String(payload.state ?? 'pending'),
      durationMs: typeof payload.duration_ms === 'number' ? payload.duration_ms : null,
      preview: String(payload.preview ?? ''),
    }
    const at = index.get(key)
    if (at === undefined) {
      index.set(key, cards.length)
      cards.push(card)
    } else {
      cards[at] = card
    }
  }
  return cards
}

/**
 * 流式正文：同一 part 的更新只保留最新文本。
 *
 * 不同 part 之间用空行分隔——引擎可能把一段回答拆成多个 part（例如工具调用前后各一段），
 * 直接首尾相接会把两句话粘成一句。
 */
export function streamingText(events: TimelineEvent[]): string {
  const parts: string[] = []
  const index = new Map<string, number>()
  for (const event of events) {
    if (event.type !== 'text') continue
    const payload = event.payload ?? {}
    const text = String(payload.text ?? '')
    if (!text) continue
    const key = String(payload.part_id ?? `seq-${event.seq}`)
    const at = index.get(key)
    if (at === undefined) {
      index.set(key, parts.length)
      parts.push(text)
    } else {
      parts[at] = text
    }
  }
  return parts.join('\n\n')
}

/**
 * 这一轮的最终回答是否已经落库。
 *
 * 终态时数据库里已经有同一条正文，再渲染流式文本就会**显示两遍**。
 * 这里按 ``meta.turn_id`` 精确判断，而不是看“有没有 assistant 消息”，
 * 避免把上一轮的回答误当成这一轮的。
 */
export function hasPersistedAnswer(
  messages: { role: string; meta?: Record<string, unknown> }[],
  turnId: string,
): boolean {
  if (!turnId) return false
  return messages.some(
    (message) =>
      message.role === 'assistant' && String(message.meta?.turn_id ?? '') === String(turnId),
  )
}

/**
 * 实时区（工具进度 + 流式正文）是否显示。
 *
 * 只在进行中显示；一旦进入终态就交给“本轮过程”折叠块和落库消息，
 * 否则同一段回答会在流式区和落库消息里各出现一次。
 */
export function liveSectionVisible(options: { status: string; persisted: boolean }): boolean {
  return !isTerminalStatus(options.status) && !options.persisted
}

/** 最近一条提示（notice 事件）：只在有内容时返回。 */
export function latestNotice(events: TimelineEvent[]): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].type !== 'notice') continue
    const message = String(events[index].payload?.message ?? '')
    if (message) return message
  }
  return ''
}

/** 用户上翻查看历史时不要抢滚动条：只有贴着底部才自动跟随。 */
export function nearBottom(
  metrics: { scrollHeight: number; scrollTop: number; clientHeight: number },
  threshold: number = STICK_THRESHOLD,
): boolean {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight <= threshold
}

/** 轮询退避：正常 400ms，连续失败逐步放宽到 3s，避免断网时打爆后端。 */
export function pollDelay(attempt: number): number {
  if (attempt <= 0) return POLL_BASE_MS
  return Math.min(POLL_BASE_MS * 2 ** attempt, POLL_MAX_MS)
}

export function pendingActions<T extends { status: string }>(actions: T[]): T[] {
  return actions.filter((action) => action.status === 'proposed' || action.status === 'executing')
}

const TOOL_STATE_LABELS: Record<string, string> = {
  pending: '准备调用',
  running: '执行中',
  completed: '已完成',
  error: '失败',
  pending_confirmation: '等待确认',
}

export function toolStateLabel(state: string): string {
  return TOOL_STATE_LABELS[state] ?? state
}

/**
 * 折叠起来的工具过程标题：默认不铺开，但状态要一眼能看到。
 *
 * 不能只看最后一张卡：前面失败、最后成功时也必须汇总出失败数，否则会把
 * “某工具失败”伪装成全部完成。这里只描述**工具调用**的状态，不凭工具名或
 * transport 返回猜业务/后台作业是否完成；业务状态交给已有的 job 事件、
 * 业务卡和最终回答。
 */
export function toolFoldSummary(cards: { title: string; state: string; tool?: string }[]): string {
  if (!cards.length) return '本轮工具调用'
  const head = cards.length === 1 ? '1 次工具调用' : `${cards.length} 次工具调用`
  const count = (state: string) => cards.filter((card) => card.state === state).length
  const failed = count('error')
  if (failed) return `${head} · ${failed} 项失败`
  const waiting = count('pending_confirmation')
  if (waiting) return `${head} · ${waiting} 项等待确认`
  const active = cards.filter((card) => card.state === 'pending' || card.state === 'running').length
  if (active) return `${head} · ${active} 项执行中`
  const unknown = cards.filter(
    (card) => !['completed', 'error', 'pending', 'running', 'pending_confirmation'].includes(card.state),
  ).length
  if (unknown) return `${head} · ${unknown} 项状态未知`
  return `${head} · 工具调用已结束`
}

const TURN_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '正在处理',
  waiting_action: '等待你确认',
  completed: '已完成',
  failed: '失败',
  stopped: '已停止',
}

export function turnStatusLabel(status: string): string {
  return TURN_STATUS_LABELS[status] ?? status
}

export function formatDuration(ms: number | null): string {
  if (ms === null || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = ms / 1000
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m${Math.round(seconds % 60)}s`
}
