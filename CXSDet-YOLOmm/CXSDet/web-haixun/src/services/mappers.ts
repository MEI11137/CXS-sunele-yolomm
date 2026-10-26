// 后端字段 → 前端 DTO 的集中转换层。
//
// 原则：
// * 所有"名字不一样 / 类型不一样 / 状态枚举不一样"的处理只写在这里，页面不再各写一套。
// * 转换不出来就明确降级（unknown / 空数组），绝不猜成成功。
// * 后端多数字段原样保留，方便排查，不做无意义的裁剪。

import type {
  AgentEvent,
  AgentTask,
  BusinessRef,
  CategoryInfo,
  ChatMessage,
  ChatTurn,
  Confirmation,
  DatasetSummary,
  DetectionBox,
  DetectionRecordSummary,
  DetectionModel,
  ExecutionState,
  ModelSummary,
  ResourceKind,
  StepStatus,
  TaskStatus,
  WaitingFor,
  Workspace,
} from '@/types'
import type { AuthUser } from '@/types'

type Raw = Record<string, unknown>

function raw(value: unknown): Raw {
  return (value && typeof value === 'object' ? value : {}) as Raw
}

function str(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number') return String(value)
  return fallback
}

function num(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

function bool(value: unknown, fallback = false): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return value === 'true' || value === '1'
  return fallback
}

/**
 * 时间统一成毫秒时间戳。
 * 后端有三种形态：ISO 字符串、秒级数字、毫秒级数字；不能直接 new Date(数字) 交给界面。
 */
export function toMillis(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    // 小于 1e12 视为秒级
    return value < 1_000_000_000_000 ? Math.round(value * 1000) : value
  }
  if (typeof value === 'string' && value.trim()) {
    const asNumber = Number(value)
    if (Number.isFinite(asNumber)) return toMillis(asNumber, fallback)
    const parsed = Date.parse(value.includes('T') ? value : value.replace(' ', 'T'))
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

function isoOrEmpty(value: unknown): string {
  if (typeof value === 'string') return value
  const ms = toMillis(value, 0)
  return ms ? new Date(ms).toISOString() : ''
}

function parseJsonLike(value: unknown): unknown {
  if (typeof value !== 'string') return value
  const text = value.trim()
  if (!text || (text[0] !== '{' && text[0] !== '[')) return value
  try {
    return JSON.parse(text)
  } catch {
    return value
  }
}

/**
 * 任务状态映射。
 * 后端有 awaiting_* 五种等待态，前端不能压成 queued/waiting_confirm，
 * 否则"等人确认"和"等业务跑完"会混在一起。
 */
export function mapTaskStatus(value: unknown): TaskStatus {
  const status = str(value)
  const known: TaskStatus[] = [
    'planning',
    'awaiting_input',
    'awaiting_plan',
    'awaiting_action',
    'awaiting_human',
    'awaiting_business',
    'running',
    'completed',
    'partial_failed',
    'failed',
    'stopped',
    'interrupted',
    'unknown',
  ]
  if ((known as string[]).includes(status)) return status as TaskStatus
  return 'unknown'
}

function mapStepStatus(value: unknown): StepStatus {
  const status = str(value)
  if (status === 'pending' || status === 'running' || status === 'done' || status === 'failed' || status === 'skipped') {
    return status
  }
  if (status === 'completed' || status === 'success') return 'done'
  if (status === 'error') return 'failed'
  return 'pending'
}

function mapExecutionState(value: unknown): ExecutionState {
  const state = str(value)
  if (state === 'not_executed' || state === 'partial' || state === 'executed' || state === 'unknown' || state === 'cancelled') {
    return state
  }
  // 旧字段值兼容：not_started/running/completed 已经废弃，不再沿用其语义
  if (state === 'completed') return 'executed'
  return 'unknown'
}

/**
 * waiting_for 结构化为对象。
 * 后端是 {kind, refs, conditions}，早期前端当成字符串，直接丢业务引用。
 */
export function mapWaitingFor(value: unknown): WaitingFor | null {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'string') {
    return { kind: value, refs: [], conditions: {}, raw: value }
  }
  const obj = raw(value)
  const kind = str(obj.kind) || str(obj.type) || 'unknown'
  const refs = Array.isArray(obj.refs) ? (obj.refs as Array<Record<string, unknown>>) : []
  return {
    kind,
    refs,
    conditions: raw(obj.conditions),
  }
}

function mapBusinessRefs(value: unknown): BusinessRef[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => {
    const obj = raw(item)
    return {
      kind: (str(obj.kind, 'image') as ResourceKind) || 'image',
      id: str(obj.id),
      display_name: str(obj.display_name) || str(obj.displayName),
    }
  })
}

/** 任务快照 → AgentTask。 */
export function mapTask(value: unknown): AgentTask | null {
  if (!value || typeof value !== 'object') return null
  const obj = raw(value)
  const id = str(obj.id)
  if (!id) return null

  const stepsRaw = Array.isArray(obj.steps) ? obj.steps : []
  const steps = stepsRaw.map((item) => {
    const step = raw(item)
    const title = str(step.title) || str(step.name) || `步骤 ${num(step.index) + 1}`
    return {
      // 后端步骤 id 可能是 step_id / id，保留一个确定值
      id: str(step.id) || str(step.step_id),
      name: title,
      title,
      status: mapStepStatus(step.status),
    }
  })

  return {
    id,
    session_id: str(obj.session_id),
    project_id: str(obj.project_id),
    status: mapTaskStatus(obj.status),
    goal: str(obj.goal),
    steps,
    waiting_for: mapWaitingFor(obj.waiting_for),
    business_refs: mapBusinessRefs(obj.business_refs),
    summary: str(obj.summary) || undefined,
    planVersion: num(obj.plan_version),
    updatedAt: toMillis(obj.updated_at, Date.now()),
  }
}

/** 动作视图 → Confirmation。 */
export function mapConfirmation(value: unknown): Confirmation | null {
  if (!value || typeof value !== 'object') return null
  const obj = raw(value)
  const actionId = str(obj.action_id) || str(obj.id)
  if (!actionId) return null

  const previewRaw = parseJsonLike(obj.preview)
  const preview = typeof previewRaw === 'object' && previewRaw !== null ? raw(previewRaw) : {}

  const summary =
    typeof obj.parameters_summary === 'string'
      ? obj.parameters_summary
      : JSON.stringify(obj.parameters_summary ?? {})

  const statusRaw = str(obj.status)
  const status: Confirmation['status'] =
    statusRaw === 'pending' || statusRaw === 'approved' || statusRaw === 'rejected' || statusRaw === 'expired'
      ? statusRaw
      : 'unknown'

  const targets = Array.isArray(obj.target_projects)
    ? obj.target_projects
    : Array.isArray(obj.actual_target_projects)
      ? obj.actual_target_projects
      : []

  return {
    action_id: actionId,
    task_id: str(obj.task_id),
    revision: num(obj.revision),
    actual_target_projects: targets.map((item) => str(item)).filter(Boolean),
    parameters: raw(obj.parameters),
    parameters_summary: summary,
    preview: preview as Confirmation['preview'],
    tool_name: str(obj.tool_name),
    tool_title: str(obj.tool_title) || str(preview.tool_title) || str(obj.tool_name),
    status,
    execution_state: mapExecutionState(obj.execution_state),
    operation_id: typeof obj.operation_id === 'string' ? obj.operation_id : null,
  }
}

export function mapEvent(value: unknown): AgentEvent | null {
  if (!value || typeof value !== 'object') return null
  const obj = raw(value)
  const eventId = str(obj.event_id) || str(obj.id)
  if (!eventId) return null
  return {
    event_id: eventId,
    sequence: num(obj.sequence),
    task_id: str(obj.task_id),
    type: str(obj.type),
    payload: raw(obj.payload),
  }
}

/** 项目 → Workspace：把 requirements 对象里的 notes 提出来给界面，原结构保留。 */
export function mapWorkspace(value: unknown): Workspace {
  const obj = raw(value)
  const requirements = raw(obj.requirements)
  return {
    id: str(obj.id),
    name: str(obj.name),
    description: str(obj.description),
    requirements: str(requirements.notes),
    requirements_raw: requirements,
    owner_id: obj.owner_id === undefined ? undefined : num(obj.owner_id),
    origin: str(obj.origin) || undefined,
    status: str(obj.status) || undefined,
    created_at: isoOrEmpty(obj.created_at),
    updated_at: isoOrEmpty(obj.updated_at),
  }
}

/**
 * requirements 提交：只改 notes，其余键保持原样。
 * 不能整对象覆盖，否则编辑一次目标说明就把其他结构化要求抹掉了。
 */
export function mergeRequirements(
  existing: Record<string, unknown>,
  notes: string,
): Record<string, unknown> {
  return { ...existing, notes }
}

export function mapSession(value: unknown) {
  const obj = raw(value)
  return {
    id: str(obj.id),
    project_id: str(obj.project_id),
    title: str(obj.title) || '未命名会话',
    created_at: toMillis(obj.created_at, Date.now()),
    updated_at: toMillis(obj.updated_at, toMillis(obj.created_at, Date.now())),
    message_count: obj.message_count === undefined ? undefined : num(obj.message_count),
    manual_id: typeof obj.manual_id === 'string' ? obj.manual_id : null,
    messages: [] as ChatMessage[],
  }
}

export function mapMessage(value: unknown): ChatMessage | null {
  if (!value || typeof value !== 'object') return null
  const obj = raw(value)
  const id = str(obj.id) || str(obj.message_id)
  if (!id) return null
  const roleRaw = str(obj.role)
  const role = roleRaw === 'user' || roleRaw === 'system' ? roleRaw : 'assistant'
  return {
    id,
    session_id: str(obj.session_id),
    role,
    content: str(obj.content) || str(obj.text),
    created_at: toMillis(obj.created_at, Date.now()),
    cards: undefined,
  }
}

/**
 * 一轮对话返回包 → ChatTurn。
 * 后端返回 {reply, task, action, plan, status, events_after}，不是单一 ChatMessage。
 */
export function mapChatTurn(sessionId: string, value: unknown): ChatTurn {
  const obj = raw(value)
  const task = mapTask(obj.task)
  const action = mapConfirmation(obj.action)
  const replyText = str(obj.reply) || str(obj.message)

  const cards: ChatMessage['cards'] = []
  if (task && task.status === 'awaiting_plan') cards.push({ kind: 'plan', task })
  if (action && action.status === 'pending') cards.push({ kind: 'action', confirmation: action })

  const message: ChatMessage = {
    id: `turn-${sessionId}-${Date.now()}`,
    session_id: sessionId,
    role: 'assistant',
    content: replyText,
    created_at: Date.now(),
    cards: cards.length ? cards : undefined,
  }

  return {
    session_id: str(obj.session_id) || sessionId,
    message,
    task,
    action,
    plan: obj.plan && typeof obj.plan === 'object' ? (obj.plan as Record<string, unknown>) : null,
    eventsAfter: num(obj.events_after),
    status: mapTaskStatus(obj.status),
  }
}

/**
 * 登录/当前用户 → AuthUser。
 * 后端返回 {userInfo:{id,username,realName,role,...}, token}，token 在外层。
 */
export function mapAuthUser(value: unknown): AuthUser | null {
  if (!value || typeof value !== 'object') return null
  const obj = raw(value)
  const info = raw(obj.userInfo ?? obj.user_info)
  const user = Object.keys(info).length ? info : obj
  const token = str(obj.token) || str(user.token)
  const id = num(user.id ?? user.userId ?? user.user_id)
  if (!token || !id) return null
  return {
    id,
    username: str(user.username) || str(user.userName),
    realName: str(user.realName) || str(user.real_name) || str(user.username),
    role: num(user.role, 2),
    avatar: str(user.avatarUrl) || str(user.avatar_url) || undefined,
    token,
  }
}

// ---------------------------------------------------------------------------
// 业务 DTO
// ---------------------------------------------------------------------------

export function mapCategories(value: unknown): CategoryInfo[] {
  const obj = raw(value)
  const source = obj.classes ?? obj
  if (!source || typeof source !== 'object') return []
  return Object.entries(source as Raw).map(([key, item], index) => {
    const entry = raw(item)
    return {
      id: index,
      name: key,
      label: str(entry.chinese_name) || key,
      color: str(entry.color) || undefined,
      severity: undefined,
    }
  })
}

function parseClassNames(value: unknown): string[] {
  const parsed = parseJsonLike(value)
  if (Array.isArray(parsed)) return parsed.map((item) => str(item)).filter(Boolean)
  if (typeof parsed === 'string' && parsed) return parsed.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
  return []
}

export function mapDataset(value: unknown): DatasetSummary {
  const obj = raw(value)
  const structure = raw(parseJsonLike(obj.structure_info))
  const isDraft = str(obj.record_kind) === 'draft'
  return {
    id: str(obj.id),
    name: str(obj.name),
    description: str(obj.description),
    status: num(obj.status),
    dataset_mode: str(obj.dataset_mode, 'single') === 'multimodal' ? 'multimodal' : 'single',
    primary_modality: str(obj.primary_modality, 'image'),
    secondary_modality: str(obj.secondary_modality) || undefined,
    sample_count:
      num(obj.sample_count) ||
      num(structure.train_images) + num(structure.valid_images) + num(structure.test_images),
    upload_time: isoOrEmpty(obj.upload_time ?? obj.create_time),
    class_names: parseClassNames(obj.class_names),
    record_kind: isDraft ? 'draft' : 'formal',
    workflow_status: str(obj.workflow_status) || undefined,
  }
}

export function mapModel(value: unknown): ModelSummary {
  const obj = raw(value)
  const metrics = raw(parseJsonLike(obj.metrics_json))
  const status = num(obj.status)
  return {
    id: str(obj.id),
    name: str(obj.name),
    version: str(obj.version) || 'v1',
    description: str(obj.description),
    status,
    model_mode: str(obj.model_mode, 'single') === 'multimodal' ? 'multimodal' : 'single',
    dataset_id: str(obj.dataset_id) || undefined,
    dataset_name: str(obj.dataset_name) || undefined,
    is_published: status === 3 || str(obj.publish_status) === 'published',
    // 本人模型与系统默认模型：后端用 visible_ids 过滤，这里用是否带 user_id 判断个人归属
    is_personal: obj.user_id !== undefined && obj.user_id !== null,
    create_time: isoOrEmpty(obj.create_time),
    metrics: Object.keys(metrics).length
      ? {
          mAP50: num(metrics.mAP50 ?? metrics.map50),
          mAP5095: num(metrics.mAP50_95 ?? metrics.map5095),
          precision: num(metrics.precision),
          recall: num(metrics.recall),
        }
      : undefined,
  }
}

export function mapDetectionBoxes(value: unknown): DetectionBox[] {
  const parsed = parseJsonLike(value)
  if (!Array.isArray(parsed)) return []
  return parsed.map((item) => {
    const obj = raw(item)
    return {
      bbox: (Array.isArray(obj.bbox) ? obj.bbox : [0, 0, 0, 0]) as DetectionBox['bbox'],
      confidence: num(obj.confidence),
      class: num(obj.class),
      class_name: str(obj.class_name) || str(obj.name),
    }
  })
}

export function mapRecord(value: unknown): DetectionRecordSummary {
  const obj = raw(value)
  const detections = mapDetectionBoxes(obj.detection_result)
  return {
    id: str(obj.id),
    title: str(obj.title),
    description: str(obj.description),
    model_id: str(obj.model_id),
    model_name: str(obj.model_name),
    original_image_url: str(obj.original_image_url),
    result_image_url: str(obj.result_image_url),
    secondary_image_url: str(obj.secondary_image_url) || undefined,
    detection_count: detections.length,
    confidence_threshold: num(obj.confidence_threshold),
    create_time: isoOrEmpty(obj.create_time),
    record_status: str(obj.record_status, 'unknown'),
    ai_review_status: str(obj.ai_review_status, 'not_requested'),
    human_review_status: str(obj.human_review_status, 'pending'),
    ai_review_result: parseJsonLike(obj.ai_review_result),
    corrected_annotations: parseJsonLike(obj.corrected_annotations),
    feedback_dataset_id: str(obj.feedback_dataset_id) || null,
    review_version: obj.review_version === undefined ? undefined : num(obj.review_version),
    class_names: parseClassNames(obj.class_names),
    detections,
  }
}

/** 模型列表 → 旧页面用的 DetectionModel（保留旧接口，避免一次改全部页面）。 */
export function toDetectionModel(model: ModelSummary): DetectionModel {
  const status: DetectionModel['status'] =
    model.status === 0 ? 'training' : model.status === 2 || model.status === 4 ? 'unavailable' : 'available'
  return {
    id: model.id,
    name: model.name,
    version: model.version,
    type: model.is_personal ? 'personal' : 'system',
    status,
    metrics: model.metrics,
  }
}

export { bool as toBool, num as toNumber, str as toString }
