import type { BusinessJob, DetectionRecord, JobEvent, ReviewSummary } from '@/types'

/**
 * 业务事件的**纯函数**整理层：把后端的 job_events 变成界面上可折叠的卡片。
 *
 * 三条硬规矩（对应验收要求）：
 * 1. 卡片只归**当前会话**的事件，别的会话的进度不会串到这里；
 * 2. 卡片默认折叠、只显示一行摘要，展开才看细节，不把内部推理/压缩摘要倒出来；
 * 3. 卡片不等于最终回答，最终回答仍然只来自落库消息，避免重复展示。
 */

export interface BusinessCardItem {
  recordId: string
  status: string
  hitCount: number | null
  classes: string
  error: string
}

export interface BusinessCard {
  jobId: string
  kind: string
  status: string
  title: string
  summary: string
  detail: string[]
  items: BusinessCardItem[]
  recordIds: string[]
  page: { name: string; params: Record<string, string>; query: Record<string, string> } | null
  lastSeq: number
  createdAt: string
}

const KIND_LABELS: Record<string, string> = {
  detection_batch: '图片检测',
  inspection: '目录巡检',
  ai_review: 'AI 核查',
  dataset_import: '数据集导入',
  model_import: '模型导入',
  training: '训练',
  review_notice: '人工复核',
  reflow_notice: '数据回流',
}

const STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  done: '已完成',
  partial: '部分完成',
  failed: '失败',
  interrupted: '已中断（结果未知）',
  paused: '已暂停',
}

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind
}

export function jobStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

export function verdictLabel(verdict: string | null): string {
  switch (verdict) {
    case 'consistent':
      return '与检测一致'
    case 'possible_false_positive':
      return '疑似误检'
    case 'possible_missed_detection':
      return '疑似漏检'
    case 'needs_human_review':
      return '建议人工复核'
    default:
      return verdict ? verdict : '无结论'
  }
}

export function recordStatusLabel(status: string): string {
  switch (status) {
    case 'running':
      return '检测中'
    case 'done':
      return '已完成'
    case 'failed':
      return '失败'
    case 'paused':
      return '已暂停'
    default:
      return status
  }
}

/** 事件按 seq 合并：同一 seq 只保留一份，乱序到达也不会重复。 */
export function mergeJobEvents(existing: JobEvent[], incoming: JobEvent[]): JobEvent[] {
  if (!incoming.length) return existing
  const seen = new Map<number, JobEvent>()
  for (const event of existing) seen.set(event.seq, event)
  for (const event of incoming) seen.set(event.seq, event)
  return [...seen.values()].sort((left, right) => left.seq - right.seq)
}

export function lastJobSeq(events: JobEvent[]): number {
  let last = 0
  for (const event of events) if (event.seq > last) last = event.seq
  return last
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function classesText(raw: unknown): string {
  const data = asRecord(raw)
  const parts = Object.entries(data)
    .filter(([, count]) => Number(count) > 0)
    .map(([name, count]) => `${name}×${Number(count)}`)
  return parts.join('、')
}

/**
 * 把事件流折叠成卡片：一个 job 一张卡，按第一次出现的顺序排。
 *
 * ``runningJobs`` 用来补充“这个作业还没结束时该显示什么”，避免事件里只有
 * 进度没有终态时卡片一直停在第一行。
 */
export function buildCards(events: JobEvent[], jobs: BusinessJob[] = []): BusinessCard[] {
  const byJob = new Map<string, BusinessCard>()
  const order: string[] = []

  const ensure = (jobId: string): BusinessCard => {
    let card = byJob.get(jobId)
    if (!card) {
      const known = jobs.find((item) => item.id === jobId)
      card = {
        jobId,
        kind: known?.kind ?? '',
        status: known?.status ?? '',
        title: known ? kindLabel(known.kind) : '业务作业',
        summary: known ? jobStatusLabel(known.status) : '',
        detail: [],
        items: [],
        recordIds: [],
        page: null,
        lastSeq: 0,
        createdAt: known?.created_at ?? '',
      }
      byJob.set(jobId, card)
      order.push(jobId)
    }
    return card
  }

  for (const job of jobs) {
    const card = ensure(job.id)
    card.kind = job.kind || card.kind
    card.title = kindLabel(job.kind)
    card.status = job.status
  }

  for (const event of events) {
    const card = ensure(event.job_id)
    card.lastSeq = Math.max(card.lastSeq, event.seq)
    const payload = asRecord(event.payload)
    switch (event.type) {
      case 'job.started':
        card.status = 'running'
        card.summary = '已开始'
        break
      case 'job.done':
      case 'job.partial':
      case 'job.failed':
      case 'job.paused':
      case 'job.interrupted': {
        const status = event.type.slice('job.'.length)
        card.status = status
        card.summary = jobStatusLabel(status)
        break
      }
      case 'review.submitted': {
        card.status = 'done'
        card.summary = asString(payload.summary) || '人工复核已提交'
        const recordId = asString(payload.record_id)
        if (recordId && !card.recordIds.includes(recordId)) card.recordIds.push(recordId)
        break
      }
      case 'reflow.done': {
        card.status = 'done'
        card.summary = asString(payload.summary) || '数据回流完成'
        break
      }
      case 'job.summary': {
        // 服务端用真实结果生成的完成摘要（没有额外调用模型）：作为卡片正文，不重复最终回答。
        card.summary = asString(payload.summary) || card.summary
        break
      }
      case 'business.detection': {
        const item = asRecord(payload.item)
        const recordId = asString(item.record_id)
        if (recordId) {
          card.items = [
            ...card.items.filter((entry) => entry.recordId !== recordId),
            {
              recordId,
              status: asString(item.status) || 'done',
              hitCount: asNumber(item.hit_count),
              classes: classesText(item.class_summary),
              error: asString(asRecord(item.error).message),
            },
          ]
          if (!card.recordIds.includes(recordId)) card.recordIds.push(recordId)
        }
        const index = asNumber(payload.index)
        const total = asNumber(payload.total)
        card.summary = index && total ? `检测进度 ${index}/${total}` : '检测进度更新'
        break
      }
      case 'business.review': {
        const recordId = asString(payload.record_id)
        const item = asRecord(payload.item)
        const review = asRecord(item.review)
        const verdict = asString(review.verdict)
        const line = verdict ? `结论：${verdictLabel(verdict)}` : '核查未得出结论'
        if (recordId) {
          card.detail = [...card.detail, `${recordId.slice(-6)} ${line}`].slice(-20)
          if (!card.recordIds.includes(recordId)) card.recordIds.push(recordId)
        }
        card.summary = line
        break
      }
      case 'business.inspection_discovered':
        card.summary = `发现 ${asNumber(payload.count) ?? 0} 张，本轮取 ${asNumber(payload.selected) ?? 0} 张`
        break
      case 'business.queue': {
        const count = asNumber(payload.count) ?? 0
        card.summary = `人工队列 ${count} 条`
        const page = asRecord(payload.page)
        if (asString(page.name)) {
          card.page = {
            name: asString(page.name),
            params: asRecord(page.params) as Record<string, string>,
            query: asRecord(page.query) as Record<string, string>,
          }
        }
        break
      }
      case 'progress':
        break
      default:
        break
    }
  }

  return order.map((jobId) => byJob.get(jobId)!).filter(Boolean)
}

/** 只保留属于该会话的事件：会话级轮询已经按 session 过滤，这里再兜一层。 */
export function scopeEvents(events: JobEvent[], sessionId: string): JobEvent[] {
  if (!sessionId) return events
  return events.filter((event) => !event.session_id || event.session_id === sessionId)
}

export function reviewLine(review: ReviewSummary | null | undefined): string {
  if (!review) return '未核查'
  if (review.status === 'failed') return `核查失败：${review.error?.message ?? '未知原因'}`
  if (review.status === 'running') return '核查中…'
  return `${verdictLabel(review.verdict)}（${Math.round((review.confidence ?? 0) * 100)}%）`
}

export function recordsSummary(records: DetectionRecord[]): string {
  if (!records.length) return '暂无记录'
  const hits = records.filter((item) => item.hit_count > 0).length
  return `共 ${records.length} 条，其中 ${hits} 条有命中`
}

/**
 * 本地时间短格式：同一年显示「MM-DD HH:mm」，跨年保留年份。
 * 后端给的 `created_at_local` 已带时区偏移，这里只是截短展示；
 * 完整本地时间与 UTC 边界在记录详情里，不丢时区信息。
 */
export function shortLocalTime(isoLocal: string | null | undefined): string {
  const text = String(isoLocal || '')
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/)
  if (!match) return text
  const [, year, month, day, hour, minute] = match
  const currentYear = new Date().getFullYear()
  return currentYear === Number(year)
    ? `${month}-${day} ${hour}:${minute}`
    : `${year}-${month}-${day} ${hour}:${minute}`
}

/** 人工状态展示名（与 AI 核查分列，互不混用）。 */
export function humanStatusLabel(status: string | null | undefined): string {
  return (
    {
      confirmed: '人工已确认',
      rejected: '人工已驳回',
      draft: '人工草稿',
      pending: '待人工',
    }[String(status || 'pending')] ?? String(status)
  )
}

export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) return '—'
  if (size < 1024) return `${size} B`
  const units = ['KB', 'MB', 'GB']
  let value = size / 1024
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`
}

/**
 * 手动巡检页的提示文案：模态决定了“要不要配图、能不能派生”，
 * 文案写错会让用户以为单模态也能塞红外，或者以为派生图是真红外。
 */
export function inspectSecondaryLabel(secondary: string): string {
  if (secondary === 'edge') return '边缘图（可由 RGB 派生）'
  return `${String(secondary || '').toUpperCase()} 配图`
}

export function inspectFieldHint(input: {
  mode: string
  secondary: string
  hasX: boolean
  deriveEdge: boolean
}): string {
  if (input.mode !== 'multimodal') return '这个模型是单模态：只使用 RGB；填了配图会被服务端拒绝。'
  if (input.secondary === 'edge' && !input.hasX && input.deriveEdge) {
    return '这个模型需要边缘图：不选配图时会由所选 RGB 现场派生，并在记录里标注“派生”。'
  }
  return `这个模型是双模态：必须提供 ${String(input.secondary || '').toUpperCase()} 配图，系统不会用 RGB 或空图顶替。`
}
