export interface ApiEnvelope<T> {
  code: number
  msg: string
  error?: string
  data: T
}

export interface HealthData {
  status: string
  service: string
  version: string
  stage: string
  checked_at: string
}

export type FeatureFlags = Record<string, boolean>

export interface MetaData {
  service: string
  version: string
  stage: string
  serve_dist: boolean
  features: FeatureFlags
  initialized: boolean
  auth: string
}

export type UserRole = 'admin' | 'member'
export type UserStatus = 'active' | 'disabled'

export interface User {
  id: number
  username: string
  display_name: string
  role: UserRole
  status: UserStatus
  created_at: string
  updated_at: string
}

export interface Project {
  id: string
  name: string
  description: string
  root_path: string | null
  root_bound_at: string | null
  status: 'active' | 'deleted'
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export interface ChatSession {
  id: string
  project_id: string
  title: string
  status: 'active' | 'deleted'
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export type ChatRole = 'user' | 'assistant' | 'system'

export interface ChatMessage {
  id: number
  session_id: string
  role: ChatRole
  content: string
  meta: Record<string, unknown>
  created_at: string
}

export interface DirectoryEntry {
  name: string
  path: string
  is_dir: boolean
  size: number
  is_link: boolean
}

export interface BrowseResult {
  path: string | null
  base: string | null
  entries: DirectoryEntry[]
  blocked: number
  roots?: string[]
}

export interface RootGrant {
  user_id: number
  username: string
  display_name: string
  granted_at: string
}

export interface AllowedRoot {
  id: number
  path: string
  label: string
  created_at: string
  /** 管理员列表里才有；普通用户只看得到自己获授的目录。 */
  grants?: RootGrant[]
}

export interface ProviderPreset {
  id: string
  label: string
  base_url: string
  models: string[]
  note: string
}

export interface CapabilityEvidence {
  step?: string
  status?: number
  outcome?: string
  latency_ms?: number
  finish_reason?: string | null
  /** 固定说明，不是上游原文（上游可能回显凭据）。 */
  note?: string | null
  tool_calls?: boolean
  [key: string]: unknown
}

export type AuthState =
  | 'verified'
  | 'rejected'
  | 'payment_required'
  | 'rate_limited'
  | 'upstream_error'
  | 'network_error'
  | 'unverified'
  | 'superseded'

export interface Capabilities {
  auth?: boolean
  chat?: boolean
  vision?: boolean
  tools?: boolean
  auth_state?: AuthState | string
  checked_at?: string
  evidence?: Record<string, CapabilityEvidence>
  [key: string]: unknown
}

export interface ModelConnection {
  provider: string
  base_url: string
  model: string
  display_name: string
  key_hint: string
  key_set: boolean
  capabilities: Capabilities
  capabilities_stale: boolean
  checked_at: string | null
  updated_at: string
  summary?: string
  /** check 期间配置被改动时为 false，结论已丢弃。 */
  applied?: boolean
}

export interface AuditEntry {
  id: number
  at: string
  actor_id: number | null
  action: string
  target: string
  detail: string
}

/* ---------------- Agent 轮次 / 事件 / 动作 ---------------- */

export type TurnStatus =
  | 'queued'
  | 'running'
  | 'waiting_action'
  | 'completed'
  | 'failed'
  | 'stopped'

export type PermissionMode = 'request' | 'assist' | 'full'

export interface Turn {
  id: string
  session_id: string
  project_id: string
  kind: 'user' | 'continuation'
  mode: PermissionMode
  provider: string
  model: string
  status: TurnStatus
  stop_requested: boolean
  error_code: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface TurnEvent {
  seq: number
  turn_id: string
  /** text / tool / notice / error / final / turn.status / action / question / ui.navigate */
  type: string
  payload: Record<string, unknown>
  created_at: string
}

export type ActionStatus =
  | 'proposed'
  | 'executing'
  | 'done'
  | 'failed'
  | 'rejected'
  | 'expired'

export interface AgentAction {
  id: string
  turn_id: string
  session_id: string
  tool: string
  risk: 'read' | 'write' | 'destructive' | string
  title: string
  preview: string
  status: ActionStatus
  result: Record<string, unknown>
  error: { code: string | null; message: string | null } | null
  created_at: string
  decided_at: string | null
}

export interface ModeOption {
  id: PermissionMode
  label: string
  hint: string
}

export interface AgentPreferences {
  mode: PermissionMode
  mode_label: string
  mode_hint: string
  model: string | null
  models: string[]
  modes: ModeOption[]
  updated_at: string | null
}

export interface AgentToolSpec {
  name: string
  title: string
  risk: string
  requires_root: boolean
}

export interface AgentCapabilities {
  runtime: Record<string, unknown>
  tools: AgentToolSpec[]
  services: Record<string, { available: boolean; note: string }>
  connection: {
    configured: boolean
    provider: string | null
    model: string | null
    fingerprint: string | null
  }
  modes: ModeOption[]
}

export interface TurnDetail {
  turn: Turn
  events: TurnEvent[]
  actions: AgentAction[]
}

export interface SessionTurns {
  turns: Turn[]
  active: Turn | null
  actions: AgentAction[]
}

// --------------------------------------------------------------------- 业务（WB17-03）
// 字段与后端 public() 一一对应：不猜、不补后端没有的字段。

export interface AssetSummary {
  id: string
  project_id: string
  kind: 'image' | 'archive' | 'weight' | 'document'
  role: string | null
  origin: string
  filename: string
  mime: string
  size: number
  sha256: string
  width: number | null
  height: number | null
  source_path: string | null
  parent_id: string | null
  created_at: string
  /** 上传时所在会话（用于追溯“这张图是哪次对话带来的”） */
  meta: Record<string, unknown>
  url: string
}

export interface DatasetSummary {
  id: string
  project_id: string
  name: string
  modalities: string[]
  secondary_modality: string | null
  classes: string[]
  root_rel: string
  source: string
  sample_count: number
  split_counts: Record<string, number>
  status: string
  notes: string
  created_at: string
}

export interface DatasetSample {
  id: string
  split: string
  stem: string
  rgb_rel: string
  x_rel: string | null
  label_rel: string | null
  width: number
  height: number
  boxes: number[][]
}

export interface ModelClass {
  index: number
  name: string
}

export interface ModelSummary {
  id: string
  project_id: string
  name: string
  family: string
  model_mode: 'single' | 'multimodal'
  secondary_modality: string | null
  dataset_id: string | null
  weight_sha256: string
  weight_size: number
  classes: ModelClass[]
  class_map: Record<string, number>
  status: 'imported' | 'published' | 'archived'
  published_at: string | null
  notes: string
  created_at: string
}

export interface ReviewSummary {
  id: string
  detection_id: string
  status: 'running' | 'done' | 'failed' | 'interrupted'
  verdict: string | null
  confidence: number | null
  summary: string
  findings: { items?: { kind: string; note: string }[] }
  model: string
  provider: string
  created_at: string
  finished_at: string | null
  error: { code: string; message: string } | null
}

export interface DetectionBox {
  class_id: number
  class_name: string
  confidence: number
  /** [x1, y1, x2, y2]，像素坐标（原图尺寸内） */
  bbox: number[]
}

export interface DetectionRecord {
  id: string
  project_id: string
  session_id: string | null
  job_id: string | null
  model_id: string
  dataset_id: string | null
  status: 'running' | 'done' | 'failed' | 'paused'
  confidence: number
  boxes: DetectionBox[]
  class_summary: Record<string, number>
  hit_count: number
  rgb_asset_id: string
  x_asset_id: string | null
  x_origin: string
  created_at: string
  /** 本地时区的展示时间（后端换算，前端不自己猜时区） */
  created_at_local: string
  finished_at: string | null
  error: { code: string; message: string } | null
  image_url: string
  result_url: string | null
  request_key: string
  latest_review?: ReviewSummary | null
}

export interface RecordFiltersState {
  status: string
  model_id: string
  has_hits: boolean | null
  date_from: string
  date_to: string
}

export interface RecordPage {
  records: DetectionRecord[]
  has_more: boolean
  next_before: string | null
  total_in_range: number
  filters: {
    status: string | null
    model_id: string | null
    session_id: string | null
    has_hits: boolean | null
    applied: Record<string, unknown>
  }
}

export interface BusinessJob {
  id: string
  project_id: string
  session_id: string | null
  kind: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'partial' | 'interrupted'
  progress: Record<string, unknown>
  result: Record<string, unknown>
  error: { code: string; message: string } | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface JobEvent {
  seq: number
  job_id: string
  session_id: string | null
  type: string
  payload: Record<string, unknown>
  created_at: string
}

export interface InspectionQueueItem {
  record_id: string
  hit_count: number
  classes: Record<string, number>
  status: string
  created_at_local: string
  page: { name: string; params: Record<string, string>; query: Record<string, string> }
}
