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
  /** 用户自己写的项目要求：注入助手提示词，但**不能**改变工具与权限。 */
  requirements: string
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
  /** 导入时命中已有内容：返回的是既有数据集，不是新建。 */
  reused?: boolean;
  id: string
  project_id: string
  name: string
  modalities: string[]
  /** 主模态：原图本身是什么（rgb / ir）。单 IR 数据集这里是 ir。 */
  primary_modality?: string
  /** 参考模态：labels 坐标空间对应的模态；reflow 显式选择后可能不同于导入时。 */
  reference_modality?: string | null
  secondary_modality: string | null
  classes: string[]
  root_rel: string
  source: string
  sample_count: number
  split_counts: Record<string, number>
  status: string
  deleted_at: string | null
  notes: string
  created_at: string
  parent_dataset_id: string | null
  kind: 'import' | 'revision' | 'new'
  seed: number | null
  split_strategy: string
  manifest_rel: string | null
  export_rel: string | null
  export_url: string | null
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
  boxes: Record<string, unknown>[]
  sha256?: string | null
  pixel_sha256?: string | null
  origin_sample_key?: string | null
  origin_record_id?: string | null
  review_version_id?: string | null
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
  /** 绑定数据集的主模态（rgb / ir）：单 IR 模型的输入原图就是红外图 */
  dataset_primary_modality?: string | null
  weight_sha256: string
  weight_size: number
  classes: ModelClass[]
  class_map: Record<string, number>
  status: 'imported' | 'published' | 'superseded' | 'failed' | 'archived'
  published_at: string | null
  notes: string
  created_at: string
  source?: 'imported' | 'trained'
  parent_model_id?: string | null
  training_run_id?: string | null
  metrics?: ModelMetrics
  deleted_at?: string | null
  /** 发布接口额外返回：这次发布取代了本项目哪些旧模型。 */
  replaced?: { id: string; name: string }[]
}

export interface ModelMetrics {
  values?: Record<string, number | null>
  conditions?: Record<string, string | number | null>
  split?: string
  source?: string
  dataset_id?: string
  run_id?: string
  images?: number | null
  epochs_run?: number | null
}

export interface TrainingRun {
  id: string
  project_id: string
  dataset_id: string
  parent_model_id: string | null
  base_weight_rel: string
  name: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  params: Record<string, number | string>
  metrics: ModelMetrics
  conditions: Record<string, string | number | null>
  run_dir_rel: string
  log_rel: string
  pid: number | null
  output_model_id: string | null
  error: { code: string | null; message: string | null } | null
  job_id: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface TrainingPlan {
  dataset: { id: string; name: string; samples: number; classes: string[] }
  parent: { id: string; name: string; dataset_id: string | null; metrics?: ModelMetrics } | null
  base_weight_rel: string
  params: Record<string, number | string | boolean>
  conditions: Record<string, string | number | null>
  name: string
  warnings: string[]
  ready: boolean
  single_ir?: boolean
  primary_modality?: string | null
}

/** 训练设备真实能力（部署机探测结果，不是写死的产品限制） */
export interface TrainingDeviceCapabilities {
  cpu: boolean
  cuda_available: boolean
  /** 可用 GPU 卡数（探测可用时 > 0，用于生成 cuda:N 选项） */
  cuda_count: number
  detail: string
}

export interface TrainingComparison {
  run: TrainingRun
  parent: {
    model: { id: string; name: string } | null
    values: Record<string, number | null>
    conditions: Record<string, string | number | null>
  }
  child: {
    values: Record<string, number | null>
    conditions: Record<string, string | number | null>
    split?: string
    source?: string
  }
  delta: Record<string, number>
  comparable: boolean
  differences: { key: string; parent: unknown; child: unknown }[]
  note: string
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
  flow_id: string | null
  deleted_at: string | null
  model_id: string
  dataset_id: string | null
  status: 'running' | 'done' | 'failed' | 'paused'
  confidence: number
  boxes: DetectionBox[]
  class_summary: Record<string, number>
  hit_count: number
  rgb_asset_id: string
  /** RGB 原图的真实文件名（列表展示用，身份仍以资产 ID 为准） */
  rgb_filename?: string | null
  /** 主模态（来自模型绑定的数据集）：ir 表示这张“原图”是红外图，不是可见光 */
  primary_modality?: 'rgb' | 'ir'
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
  human_status?: 'pending' | 'draft' | 'confirmed' | 'rejected'
  /** 最近一次已提交人工结论的版本号（草稿不算；与 human_status 一起返回） */
  review_version?: number | null
  /** 当前人工确认版本的反馈状态：未入池 none / 已入反馈池 pending / 已回流 reflowed（服务端口径） */
  feedback_status?: 'none' | 'pending' | 'reflowed'
  bucket?: string
  draft?: ReviewVersion | null
  review?: ReviewVersion | null
}

export interface RecordFiltersState {
  status: string
  model_id: string
  has_hits: boolean | null
  date_from: string
  date_to: string
  /** 按原图文件名模糊搜索 */
  q: string
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

// --------------------------------------------------------------------- 人工复核 / 回流

export interface ReviewVersion {
  id: string
  detection_id: string
  version: number
  status: 'draft' | 'confirmed' | 'rejected'
  outcome: 'defect' | 'no_defect' | null
  boxes: DetectionBox[]
  /** 辅模态（X）框：与 boxes 各用各的原图像素坐标，互不复制/换算。 */
  x_boxes?: DetectionBox[]
  class_table: { index: number; name: string }[]
  image_width: number
  image_height: number
  x_image_width?: number | null
  x_image_height?: number | null
  reference_modality?: string | null
  secondary_modality?: string | null
  model_id: string | null
  dataset_id: string | null
  note: string
  reject_reason: string | null
  created_at: string
  updated_at: string
  submitted_at: string | null
}

export interface ReviewClassOption {
  index: number
  name: string
  native_class_id: number | null
  native_class_name: string | null
  source: 'dataset' | 'model'
}

export interface ReviewState {
  record: DetectionRecord
  ai_review?: ReviewSummary | null
  human_status: 'pending' | 'draft' | 'confirmed' | 'rejected'
  class_options: ReviewClassOption[]
  versions: ReviewVersion[]
  draft: ReviewVersion | null
  latest: ReviewVersion | null
  latest_submitted: ReviewVersion | null
  latest_confirmed: ReviewVersion | null
  latest_rejected: ReviewVersion | null
  pending: boolean
  queue: 'pending' | 'draft' | 'rejected' | 'confirmed'
  /** 驳回后又显式重开（生成版本更大的草稿）：可编辑，但生效结论仍是驳回。 */
  reopened?: boolean
  image: {
    width: number
    height: number
    rgb_url: string
    x_url: string | null
    x_modality: string | null
    /** 辅模态原始 token（ir/rgb/...）；x_modality 仍是显示文案。 */
    x_modality_token?: string | null
    /** 辅模态原图的真实像素尺寸；单模态或旧记录为 0。 */
    x_width?: number
    x_height?: number
    result_url: string | null
    /** 主图真实模态：单 IR 记录这里是 ir，原图本身就是红外图。 */
    primary_modality?: 'rgb' | 'ir'
  }
}

export interface FeedbackSummary {
  id: string
  detection_id: string
  review_version_id: string
  dataset_id: string | null
  status: 'pending' | 'reflowed' | 'void'
  note: string
  created_at: string
  reflowed_at: string | null
  review: {
    version: number
    outcome: string | null
    // 参考模态的框数；旧字段保持原语义，不把辅模态（X）框合进来。
    box_count: number
    image: { width: number; height: number }
    // 双模态：辅模态的框数与真实尺寸各自独立，缺失时按单模态显示。
    x_box_count?: number
    x_modality?: string | null
    x_image?: { width: number; height: number } | null
    reference_modality?: string | null
    secondary_modality?: string | null
  } | null
}

export interface DatasetReport {
  dataset_id: string
  sample_count: number
  duplicate_groups: { identity: string; samples: { id: string; split: string; stem: string }[] }[]
  cross_split_leakage: { identity: string; splits: string[]; stems: string[] }[]
  annotation_conflicts: { identity: string; stems: string[]; variants: number }[]
  identity_missing: number
  identity_derived?: number
  identity_problems?: { sample_id: string; split: string; stem: string; code: string; message: string }[]
  identity_states?: Record<string, number>
  note: string
}

export interface RecordMatch {
  record_id: string
  status: 'new' | 'unique' | 'ambiguous' | 'candidate' | 'missing'
  identity?: { sha256: string; pixel_sha256: string; width: number; height: number }
  matches: { sample_id: string; split: string; stem: string; matched_by?: string }[]
  note?: string
}

export interface ReflowDraft {
  id: string
  name: string
  mode: 'revision' | 'new'
  parent_dataset_id: string | null
  strategy: 'even' | 'ratio' | 'manual'
  ratios: Record<string, number>
  seed: number
  selections: { record_id: string; action: string; split?: string; review_version_id?: string; target_sample_id?: string }[]
  plan: ReflowPlan
  status: 'editing' | 'ready' | 'failed' | 'finalized' | 'deleted'
  error: { code: string; message: string } | null
  dataset_id: string | null
  created_at: string
  updated_at: string
  finalized_at: string | null
  deleted_at: string | null
}

export interface ReflowPlan {
  mode: 'revision' | 'new'
  dataset_id: string | null
  parent_dataset_id: string | null
  counts: { replace: number; append: number; excluded: number; kept: number; conflicts: number }
  reference_modality?: string | null
  secondary_modality?: string | null
  items: {
    record_id: string
    action: string
    split: string | null
    stem: string | null
    outcome: string | null
    boxes: number
    x_boxes?: number
    reference_modality?: string | null
    secondary_modality?: string | null
    target_sample_id?: string | null
    matches: { sample_id: string; split: string; stem: string }[]
  }[]
  conflicts: { record_id: string; code: string; message: string; matches?: { sample_id: string; split: string; stem: string }[] }[]
  classes: string[]
  splits?: Record<string, number>
  note?: string
  signature?: string
}

// --------------------------------------------------------------- 资料与手册（WB17-05）

export interface ProjectDocument {
  id: string
  project_id: string
  name: string
  filename: string
  kind: 'pdf' | 'docx' | 'txt' | 'md'
  kind_label: string
  size: number
  page_count: number
  char_count: number
  status: 'ready' | 'failed'
  created_at: string
  deleted_at: string | null
  error: { code: string; message: string } | null
  /** page = PDF 真实页；logical = Word/txt/md 按段落聚合的逻辑页 */
  paging: 'page' | 'logical'
}

export interface DocumentPage {
  page: number
  text: string
}

export interface DocumentWindow {
  document: ProjectDocument
  page: number
  span: number
  total_pages: number
  pages: DocumentPage[]
  note: string
}

export interface DocumentStats {
  documents: number
  pages: number
  chars: number
}

export interface DocumentHit {
  document_id: string
  name: string
  kind: string
  page: number
  count: number
  snippet: string
}

export interface DocumentSearchResult {
  query: string
  hits: DocumentHit[]
  total: number
  note: string
}

export interface ManualSummary {
  id: string
  name: string
  description: string
  builtin: boolean
  project_id: string | null
  active_version_id: string | null
  active_version: number | null
  created_at: string
  editable: boolean
  note: string
}

export interface ManualVersion {
  id: string
  version: number
  note: string
  created_at: string
}

export interface ManualDetail {
  manual: ManualSummary
  active_version: number | null
  content: string
  versions: ManualVersion[]
  note: string
}

// --------------------------------------------------------------- 外部 MCP（WB17-05）

export interface McpTool {
  name: string
  title: string
  description: string
  schema: Record<string, unknown>
  read_only: boolean
  authorization: 'pending' | 'allowed' | 'denied'
  discovered_at: string
  callable: boolean
}

export interface McpServer {
  id: string
  name: string
  transport: 'stdio' | 'http'
  command: string
  args: string[]
  cwd_rel: string
  url: string
  enabled: boolean
  admin_only: boolean
  status: 'unknown' | 'connected' | 'error' | 'disabled'
  last_error: string | null
  last_seen_at: string | null
  created_at: string
  secret_keys: string[]
  secret_hints: Record<string, string>
  note: string
  tools: McpTool[]
}

export interface McpDiscoverResult {
  ok: boolean
  server: McpServer
  new_tools?: string[]
  dropped_tools?: string[]
  error?: { code: string; message: string }
  note: string
}

export interface McpCallResult {
  ok: boolean
  tool: string
  text: string
  duration_ms: number
  error?: { code: string; message: string } | null
  replayed?: boolean
  note?: string
}

export interface McpCallAudit {
  id: string
  tool: string
  ok: boolean
  error: string | null
  duration_ms: number
  created_at: string
}
