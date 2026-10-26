// 接口适配层契约。
//
// 两条实现：
// * MockAdapter —— 只用于离线演示，明确标识"模拟"，绝不在真实模式下回退到它。
// * RealAdapter —— 全部走真实 HTTP（/api/agent 与业务接口）。
//
// 任何"后端还没有"的能力都在这里显式声明为 unsupported，调用时抛
// NotSupportedError；不允许返回看起来成功的空数据。

import type {
  AgentEvent,
  AgentTask,
  AuthUser,
  ChatMessage,
  ChatTurn,
  Confirmation,
  ConnectionCapabilities,
  DatasetSummary,
  DetectionModel,
  DetectionRecordSummary,
  ModelSummary,
  PageResult,
  PersonalConnection,
  ResourceRef,
  ResourceKind,
  Session,
  Workspace,
} from '@/types'

export type Mode = 'mock' | 'real'

/** 后端尚未提供的能力。调用即失败，不伪造成功。 */
export class NotSupportedError extends Error {
  readonly feature: string

  constructor(feature: string) {
    super(`当前后端尚未提供该能力：${feature}`)
    this.name = 'NotSupportedError'
    this.feature = feature
  }
}

export interface WorkspaceOverview {
  version: string
  project: Workspace
  projects: Workspace[]
  connection: PersonalConnection
  capabilities: ConnectionCapabilities
  toolCount: number
  manualCount: number
  sessions: Session[]
}

export interface FileAsset {
  file_id: string
  file_name: string
  file_size: number
  content_type: string
  bucket: string
  object_key: string
  url: string
}

export interface TaskSnapshot {
  task: AgentTask | null
  actions: Confirmation[]
  note: Record<string, unknown> | null
}

export interface ToolInfo {
  serverId?: string
  version?: string
  source?: string
  billable?: boolean
  name: string
  title: string
  description: string
  risk: string
  kind: string
  requires_confirmation: boolean
  available: boolean
}

export interface ManualInfo {
  id: string
  name: string
  version: number
  summary: string
  source: string
  updated_at: number
}

/** 人工标注：后端要求 0-1 归一化 bbox 与类别 id 严格对应类别表。 */
export interface HumanReviewAnnotation {
  class_name: string
  class_id: number
  bbox: [number, number, number, number]
}

export interface HumanReviewInput {
  decision: 'confirmed' | 'no_defect' | 'rejected'
  annotations?: HumanReviewAnnotation[]
  /** 乐观锁版本：必须来自最近一次读取，不能自己加一 */
  expectedVersion: number
  comment?: string
}

export interface PageQuery {
  page?: number
  pageSize?: number
  search?: string
}

/** 复核队列条目（由检测记录映射而来，字段名沿用旧页面）。 */
export interface DefectItem {
  id: string
  turbineId: string
  bladeFile: string
  thumbnail: string
  suspectedType: string
  confidence: number
  reviewStatus: 'pending' | 'reviewing' | 'done'
  severity: 'low' | 'medium' | 'high'
}

export interface SendMessageOptions {
  /** 已登记的项目资源 id：后端只认这个，不认文件名 */
  resourceRefs?: Array<{ id: string; kind?: ResourceKind; display_name?: string }>
  onDelta?: (delta: string) => void
  signal?: AbortSignal
}

export interface ApiAdapter {
  readonly mode: Mode

  // ---------------------------------------------------------------- 账号
  login(username: string, password: string): Promise<AuthUser>
  register(input: {
    username: string
    password: string
    realName?: string
    email?: string
    phone?: string
  }): Promise<void>
  currentUser(): Promise<AuthUser | null>
  logout(): Promise<void>

  // -------------------------------------------------------------- 工作区
  listWorkspaces(): Promise<Workspace[]>
  createWorkspace(input: {
    name: string
    description: string
    requirements: string
  }): Promise<Workspace>
  updateWorkspace(id: string, patch: Partial<Workspace>): Promise<Workspace>
  getOverview(): Promise<WorkspaceOverview>

  // ---------------------------------------------------------- 个人连接
  getConnection(): Promise<PersonalConnection>
  saveConnection(input: {
    provider: string
    apiUrl: string
    model: string
    apiKey?: string
  }): Promise<PersonalConnection>
  deleteConnection(): Promise<void>
  checkConnection(): Promise<PersonalConnection>

  // -------------------------------------------------------------- 资源
  listResources(projectId: string, kinds?: ResourceKind[]): Promise<ResourceRef[]>
  registerResource(
    projectId: string,
    input: { kind: ResourceKind; displayName: string; metadata?: Record<string, unknown> },
  ): Promise<ResourceRef>

  // -------------------------------------------------------------- 文件
  uploadProjectFile(projectId: string, file: File): Promise<FileAsset>
  getFileMetadata(fileId: string): Promise<FileAsset>
  /** 受保护图片：带 Token 取 blob 再转临时 URL，不把 Token 塞进 img 标签。 */
  fileObjectUrl(bucket: string, objectKey: string): Promise<string>

  // -------------------------------------------------------------- 会话
  listSessions(projectId: string): Promise<Session[]>
  /** 会话列表不含完整消息，必须显式拉取历史。 */
  listMessages(sessionId: string): Promise<ChatMessage[]>
  createSession(projectId: string, title: string): Promise<Session>
  sendMessage(sessionId: string, content: string, options?: SendMessageOptions): Promise<ChatTurn>
  listEvents(sessionId: string, afterSequence: number): Promise<AgentEvent[]>

  // -------------------------------------------------------------- 任务
  getTaskSnapshot(taskId: string): Promise<TaskSnapshot>
  listTasks(projectId: string): Promise<AgentTask[]>
  createFeedbackDraft(datasetId: string): Promise<Record<string, unknown>>
  listDraftSamples(draftId: string, page: number): Promise<Record<string, unknown>>
  assignDraftSamples(draftId: string, sampleIds: string[], split: string): Promise<Record<string, unknown>>
  finalizeDraft(draftId: string): Promise<Record<string, unknown>>
  startTraining(parentId: string, datasetId: string, epochs: number, batchSize: number): Promise<Record<string, unknown>>
  /** planVersion 传了就校验：防止确认到用户没看过的替换计划 */
  confirmPlan(taskId: string, planVersion?: number): Promise<TaskSnapshot>
  stopTask(taskId: string, reason?: string): Promise<TaskSnapshot>
  reconcileTask(taskId: string): Promise<TaskSnapshot>
  getTaskNote(taskId: string): Promise<Record<string, unknown>>

  // -------------------------------------------------------------- 动作
  confirmAction(actionId: string, expectedRevision: number): Promise<Confirmation>
  rejectAction(actionId: string, reason?: string): Promise<Confirmation>

  // -------------------------------------------------- 工具 / 手册 / 授权
  listTools(projectId?: string): Promise<ToolInfo[]>
  listManuals(): Promise<ManualInfo[]>
  listGrants(): Promise<Array<Record<string, unknown>>>
  listMcpConnections(): Promise<Array<Record<string, unknown>>>
  listDirectories(projectId: string): Promise<Array<Record<string, unknown>>>
  authorizeDirectory(projectId: string, root: string, label: string): Promise<Record<string, unknown>>
  disableDirectory(id: string): Promise<void>
  saveMcpConnection(input: Record<string, unknown>): Promise<Record<string, unknown>>
  discoverMcp(connectionId: string): Promise<Record<string, unknown>>
  grantTool(input: Record<string, unknown>): Promise<Record<string, unknown>>
  revokeGrant(id: string): Promise<void>
  listDocuments(projectId: string): Promise<Array<Record<string, unknown>>>
  parseDocument(assetId: string, title: string): Promise<Record<string, unknown>>
  searchDocument(id: string, query: string): Promise<Record<string, unknown>>

  // -------------------------------------------------------------- 业务
  listRecords(query?: PageQuery & { projectId?: string }): Promise<PageResult<DetectionRecordSummary>>
  getRecord(recordId: string): Promise<DetectionRecordSummary | null>
  requestAiReview(recordId: string): Promise<Record<string, unknown>>
  submitHumanReview(recordId: string, input: HumanReviewInput): Promise<Record<string, unknown>>
  addFeedback(recordId: string, input: { datasetId: string }): Promise<Record<string, unknown>>
  /** 批量回流：每条记录自动回到它所属模型的原训练数据集 */
  addFeedbackBatch(recordIds: string[]): Promise<Record<string, unknown>>
  listCategories(): Promise<Array<{ id: number; name: string; label: string; color?: string }>>
  /**
   * 明确业务动作入口：用户显式选定模型与影像后直接推理。
   * 与"由 Agent 规划"是两条路，二者都不绕过人工确认。
   */
  detectImage(input: {
    modelId: string
    file: File
    xFile?: File
    confidence: number
    title?: string
  }): Promise<DetectionRecordSummary>
  listDatasets(query?: PageQuery): Promise<PageResult<DatasetSummary>>
  /** 上传一个 YOLO 结构 ZIP 并登记为数据集（后端校验结构后才算成功） */
  uploadDataset(input: {
    projectId?: string
    file: File
    name: string
    description?: string
    datasetMode: 'single' | 'multimodal'
    secondaryModality?: string
    classNames?: string[]
  }): Promise<Record<string, unknown>>
  listModels(query?: PageQuery): Promise<PageResult<DetectionModel>>
  /** 模型详情：含真实 status(0-4)、发布状态与指标，列表 DTO 不够用 */
  getModelDetail(modelId: string): Promise<ModelSummary | null>
  compareModels(parentId: string, childId: string): Promise<Record<string, unknown>>
  /** 发布模型：后端按本人/项目范围归档同模态旧版本 */
  publishModel(modelId: string): Promise<Record<string, unknown>>
  /** 导入外部训练好的 .pt 权重，登记为草稿模型（需选来源数据集） */
  importModelWeight(input: {
    projectId?: string
    name: string
    description?: string
    datasetId: string
    weight: File
  }): Promise<Record<string, unknown>>

  // ------------------------------------------------- 兼容旧页面的读接口
  /** 复核队列：真实实现由检测记录映射，不再使用内置假数据。 */
  listDefects(projectId: string): Promise<DefectItem[]>
  getDefect(id: string): Promise<DefectItem | null>

  /** 真实模式下任务只能由对话产生，直连创建必须显式失败。 */
  createTask(input: {
    project_id: string
    session_id: string
    images: string[]
    model_id: string
  }): Promise<AgentTask>
}

export type { Workspace, DetectionModel, Session, ChatMessage, AgentTask }
