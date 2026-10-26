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

export type TaskStatus =
  | 'waiting_confirm'
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'partial_failed'
  | 'unknown'

export interface TaskStep {
  id: string
  name: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'
}

export interface BusinessRef {
  kind: ResourceKind
  id: string
  display_name: string
}

export interface AgentTask {
  id: string
  session_id: string
  project_id: string
  status: TaskStatus
  steps: TaskStep[]
  waiting_for: string | null
  business_refs: BusinessRef[]
}

export interface Confirmation {
  action_id: string
  revision: string
  actual_target_projects: string[]
  parameters_summary: Record<string, string>
  preview: string
  status: 'pending' | 'approved' | 'rejected' | 'expired' | 'unknown'
}

export type ExecutionState = 'not_started' | 'running' | 'completed' | 'unknown'

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
  requirements: string
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
  category: string
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
  state: 'selected' | 'uploading' | 'uploaded' | 'parsed' | 'imported'
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
}
