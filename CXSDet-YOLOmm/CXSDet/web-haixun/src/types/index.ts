// 与任务书「共同字段基线」对齐的类型定义

export type ResourceKind =
  | 'image'
  | 'dataset'
  | 'model'
  | 'record'
  | 'manual'
  | 'session'
  | 'feedback'

export interface ResourceRef {
  id: string
  kind: ResourceKind
  project_id: string
  display_name: string
  revision: string
  allowed_actions: string[]
}

// 与后端 agent/tasks.py 的 STATUS_* 一一对应。
// 只有 running/completed/failed 表示"业务在跑/跑完/跑失败"，
// awaiting_* 表示在等人或等业务，不能当作"排队中"。
export type TaskStatus =
  | 'planning'
  | 'awaiting_input'
  | 'awaiting_plan'
  | 'awaiting_action'
  | 'awaiting_human'
  | 'awaiting_business'
  | 'running'
  | 'completed'
  | 'partial_failed'
  | 'failed'
  | 'stopped'
  | 'interrupted'
  | 'unknown'

export type StepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export interface TaskStep {
  /** 后端步骤 id，必须保留，用于回写步骤状态 */
  id: string
  /** 展示名；后端字段是 title，在映射层统一成 name */
  name: string
  status: StepStatus
  /** 该步骤原始标题，排查用 */
  title?: string
}

export interface BusinessRef {
  kind: ResourceKind
  id: string
  display_name: string
}

/**
 * 结构化等待条件。后端 waiting_for 是对象（含 kind/refs/conditions），
 * 不能退化成字符串，否则会丢掉业务引用。
 */
export interface WaitingFor {
  kind: string
  refs: Array<Record<string, unknown>>
  conditions: Record<string, unknown>
  /** 兼容历史字符串形态的原始值 */
  raw?: string
}

export interface AgentTask {
  id: string
  session_id: string
  project_id: string
  status: TaskStatus
  goal: string
  steps: TaskStep[]
  waiting_for: WaitingFor | null
  business_refs: BusinessRef[]
  summary?: string
  planVersion: number
  updatedAt: number
}

/** 动作预览：后端是对象（含 tool_title 等），不是一段字符串。 */
export interface ActionPreview {
  tool_title?: string
  [key: string]: unknown
}

export interface Confirmation {
  action_id: string
  task_id: string
  /** 版本号是整数，不能当字符串做加减比较 */
  revision: number
  actual_target_projects: string[]
  parameters: Record<string, unknown>
  /** 后端 parameters_summary 是字符串，界面按一行摘要展示 */
  parameters_summary: string
  preview: ActionPreview
  tool_name: string
  tool_title: string
  status: 'pending' | 'approved' | 'rejected' | 'expired' | 'unknown'
  execution_state: ExecutionState
  operation_id?: string | null
}

/** 与后端 agent/errors.py 的 ExecutionState 一致。 */
export type ExecutionState = 'not_executed' | 'partial' | 'executed' | 'unknown' | 'cancelled'

export interface AgentEvent {
  event_id: string
  sequence: number
  task_id: string
  type: string
  payload: Record<string, unknown>
}

export interface AgentError {
  code: string
  message: string
  execution_state: ExecutionState
}

export interface Workspace {
  id: string
  name: string
  description: string
  /** 后端 requirements 是对象，界面展示的是其中的 notes；结构保留在 requirements_raw */
  requirements: string
  requirements_raw: Record<string, unknown>
  owner_id?: number
  origin?: string
  status?: string
  created_at: string
  updated_at: string
}

export interface Defect {
  id: string
  turbineId: string
  bladeFile: string
  thumbnail: string
  suspectedType: string
  confidence: number
  reviewStatus: 'pending' | 'reviewing' | 'done'
  severity: 'low' | 'medium' | 'high'
}

export interface BoundingBox {
  id: string
  // 归一化坐标 0..1，相对原图
  x: number
  y: number
  w: number
  h: number
  /** 后端 class_name（提交时用） */
  category: string
  /** 展示用名称（如中文类别名），不参与提交 */
  label?: string
  severity: 'low' | 'medium' | 'high'
}

export interface Annotation {
  imageId: string
  boxes: BoundingBox[]
  noDefect: boolean
}

export interface DetectionModel {
  id: string
  name: string
  version: string
  type: 'personal' | 'system'
  status: 'available' | 'training' | 'unavailable'
  metrics?: { mAP50: number; mAP5095: number; precision: number; recall: number }
}

export interface ChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  attachments?: Attachment[]
  cards?: CardPayload[]
  created_at: number
  streaming?: boolean
}

export interface Attachment {
  id: string
  name: string
  type: string
  size: number
  source: string
  state: 'selected' | 'uploading' | 'uploaded' | 'failed'
  /** 本机文件本体：上传前必须保留，否则无法重试 */
  file?: File
  /** 后端文件资产 id（受保护存储） */
  fileId?: string
  /** 登记后的 Agent 资源 id，发消息时作为 resource_refs */
  resourceId?: string
  error?: string
}

export type CardPayload =
  | { kind: 'plan'; task: AgentTask }
  | { kind: 'action'; confirmation: Confirmation }
  | { kind: 'tool'; summary: string; detail: string }
  | { kind: 'result'; summary: string }
  | { kind: 'error'; error: AgentError }

export interface Session {
  id: string
  project_id: string
  title: string
  created_at: number
  messages: ChatMessage[]
  /** 后端会话摘要字段 */
  updated_at?: number
  message_count?: number
  manual_id?: string | null
}

/**
 * 一轮对话的完整结果。
 * 后端 POST /sessions/:id/messages 返回的是复合结果（reply/task/action/plan/events_after），
 * 不是单一 ChatMessage，所以适配层用这个结构承载，页面再按需取卡片。
 */
export interface ChatTurn {
  session_id: string
  message: ChatMessage
  task: AgentTask | null
  action: Confirmation | null
  plan: Record<string, unknown> | null
  eventsAfter: number
  status: TaskStatus
}

// ---------------------------------------------------------------------------
// 账号
// ---------------------------------------------------------------------------

export interface AuthUser {
  id: number
  username: string
  realName: string
  role: number
  avatar?: string
  token: string
}

// ---------------------------------------------------------------------------
// 个人连接
// ---------------------------------------------------------------------------

/** 单项能力检测结果：ok / failed / unsupported / unchecked。 */
export interface CapabilityEntry {
  status: string
  message?: string
  reason?: string
  checked_at?: string
}

export interface PersonalConnection {
  configured: boolean
  provider?: string
  api_url?: string
  model?: string
  /** 后端脱敏提示（只给首尾几位），接口绝不回读明文 Key */
  key_hint?: string
  status?: string
  capabilities?: Record<string, CapabilityEntry>
  missing_capabilities?: string[]
  failure_reason?: string | null
  last_checked_at?: string
  updated_at?: string
  checked_now?: boolean
}

/** 能力集合：键为 chat / vision / tools 等。 */
export type ConnectionCapabilities = Record<string, CapabilityEntry>

// ---------------------------------------------------------------------------
// 业务 DTO（来自 C 分支真实接口，字段名保持后端原样，映射只发生在 services/mappers.ts）
// ---------------------------------------------------------------------------

export interface CategoryInfo {
  id: number
  name: string
  /** 展示用中文名 */
  label: string
  color?: string
  severity?: 'low' | 'medium' | 'high'
}

export interface DatasetSummary {
  id: string
  name: string
  description: string
  status: number
  dataset_mode: 'single' | 'multimodal'
  primary_modality: string
  secondary_modality?: string
  sample_count: number
  upload_time: string
  class_names: string[]
  record_kind: 'formal' | 'draft'
  workflow_status?: string
}

export interface ModelSummary {
  id: string
  name: string
  version: string
  description: string
  status: number
  model_mode: 'single' | 'multimodal'
  dataset_id?: string
  dataset_name?: string
  is_published: boolean
  is_personal: boolean
  create_time: string
  metrics?: { mAP50: number; mAP5095: number; precision: number; recall: number }
}

export interface DetectionRecordSummary {
  id: string
  title: string
  description: string
  model_id: string
  model_name: string
  original_image_url: string
  result_image_url: string
  secondary_image_url?: string
  detection_count: number
  confidence_threshold: number
  create_time: string
  /** 后端是字符串：completed / failed / ... */
  record_status: string
  /** not_requested / pending / running / done / failed */
  ai_review_status: string
  human_review_status: string
  ai_review_result?: unknown
  corrected_annotations?: unknown
  feedback_dataset_id?: string | null
  review_version?: number
  class_names: string[]
  /** 检测框（后端 detection_result 解析后） */
  detections: DetectionBox[]
}

export interface DetectionBox {
  bbox: [number, number, number, number] | number[]
  confidence: number
  class: number
  class_name: string
}

export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
}

