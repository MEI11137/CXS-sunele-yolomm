// 真实适配器：所有能力都走本机后端的真实接口。
//
// 约束：
// * 失败一律抛错，不回退到 MockAdapter，也不返回"看起来成功"的空数据。
// * 身份只来自 Token，请求体不传 user_id / owner_id。
// * 后端确实还没提供的能力（文档检索、MCP 目录管理）保持 unsupported，
//   由 BaseAdapter 抛出 NotSupportedError，界面如实提示"尚未接入"。

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
import { http } from './http'
import {
  mapAuthUser,
  mapCategories,
  mapChatTurn,
  mapConfirmation,
  mapDataset,
  mapEvent,
  mapMessage,
  mapModel,
  mapRecord,
  mapSession,
  mapTask,
  mapWorkspace,
  mergeRequirements,
  toDetectionModel,
  toMillis,
  toString,
} from './mappers'
import { BaseAdapter } from './adapter-base'
import type {
  DefectItem,
  FileAsset,
  HumanReviewInput,
  ManualInfo,
  Mode,
  PageQuery,
  SendMessageOptions,
  TaskSnapshot,
  ToolInfo,
  WorkspaceOverview,
} from './types'

type Raw = Record<string, unknown>

function raw(value: unknown): Raw {
  return (value && typeof value === 'object' ? value : {}) as Raw
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

export class RealAdapter extends BaseAdapter {
  readonly mode: Mode = 'real'

  // ------------------------------------------------------------------ 账号

  async login(username: string, password: string): Promise<AuthUser> {
    const data = await http.post<unknown>('/api/user/login', { username, password })
    const user = mapAuthUser(data)
    if (!user) {
      throw new Error('登录返回缺少身份或 Token，请检查后端登录接口')
    }
    return user
  }

  async register(input: {
    username: string
    password: string
    realName?: string
    email?: string
    phone?: string
  }): Promise<void> {
    await http.post('/api/user/register', {
      username: input.username,
      password: input.password,
      realName: input.realName ?? '',
      email: input.email ?? '',
      phone: input.phone ?? '',
    })
  }

  async currentUser(): Promise<AuthUser | null> {
    const data = await http.get<unknown>('/api/user/current')
    return mapAuthUser(data)
  }

  async logout(): Promise<void> {
    await http.post('/api/user/logout')
  }

  // ---------------------------------------------------------------- 工作区

  async listWorkspaces(): Promise<Workspace[]> {
    const data = await http.get<unknown>('/api/agent/projects')
    return asArray(data).map(mapWorkspace)
  }

  async createWorkspace(input: {
    name: string
    description: string
    requirements: string
  }): Promise<Workspace> {
    const project = await http.post<unknown>('/api/agent/projects', {
      name: input.name,
      description: input.description,
    })
    const created = mapWorkspace(project)
    const notes = input.requirements.trim()
    if (!notes) return created
    // requirements 是对象且只有 notes 可写：单独 PATCH，避免创建接口忽略它
    return this.updateWorkspace(created.id, { requirements: notes })
  }

  async updateWorkspace(id: string, patch: Partial<Workspace>): Promise<Workspace> {
    const body: Raw = {}
    if (patch.name !== undefined) body.name = patch.name
    if (patch.description !== undefined) body.description = patch.description

    if (patch.requirements !== undefined) {
      // 整体覆盖会抹掉其他结构化要求，先取现状再合并 notes
      const current = mapWorkspace(await http.get<unknown>(`/api/agent/projects/${id}`))
      body.requirements = mergeRequirements(current.requirements_raw, patch.requirements)
    }

    const updated = await http.patch<unknown>(`/api/agent/projects/${id}`, body)
    return mapWorkspace(updated)
  }

  async getOverview(): Promise<WorkspaceOverview> {
    const data = raw(await http.get<unknown>('/api/agent/workspace'))
    return {
      version: toString(data.version),
      project: mapWorkspace(data.project),
      projects: asArray(data.projects).map(mapWorkspace),
      connection: raw(data.connection) as unknown as PersonalConnection,
      capabilities: raw(data.capabilities) as unknown as ConnectionCapabilities,
      toolCount: Number(data.tool_count ?? 0),
      manualCount: Number(data.manual_count ?? 0),
      sessions: asArray(data.sessions).map(mapSession) as unknown as Session[],
    }
  }

  // ------------------------------------------------------------ 个人连接

  async getConnection(): Promise<PersonalConnection> {
    const data = raw(await http.get<unknown>('/api/agent/connection'))
    return data as unknown as PersonalConnection
  }

  async saveConnection(input: {
    provider: string
    apiUrl: string
    model: string
    apiKey?: string
  }): Promise<PersonalConnection> {
    const body: Raw = {
      provider: input.provider,
      api_url: input.apiUrl,
      model: input.model,
    }
    // 留空表示沿用已保存的 Key，不发空字符串覆盖
    if (input.apiKey) body.api_key = input.apiKey
    const data = await http.put<unknown>('/api/agent/connection', body)
    return raw(data) as unknown as PersonalConnection
  }

  async deleteConnection(): Promise<void> {
    await http.del('/api/agent/connection')
  }

  async checkConnection(): Promise<PersonalConnection> {
    // 后端返回的是完整连接视图（含 capabilities 与 checked_now），不是裸能力表
    const data = await http.post<unknown>('/api/agent/connection/check')
    return raw(data) as unknown as PersonalConnection
  }

  // ---------------------------------------------------------------- 资源

  async listResources(projectId: string, kinds?: ResourceKind[]): Promise<ResourceRef[]> {
    const data = await http.get<unknown>(`/api/agent/projects/${projectId}/resources`, {
      query: { kinds: kinds?.join(',') },
    })
    return asArray(data).map((item) => {
      const obj = raw(item)
      return {
        id: toString(obj.id),
        kind: toString(obj.kind) as ResourceKind,
        project_id: toString(obj.project_id),
        display_name: toString(obj.display_name),
        revision: toString(obj.revision, '0'),
        allowed_actions: asArray(obj.allowed_actions).map((a) => toString(a)),
      }
    })
  }

  async registerResource(
    projectId: string,
    input: { kind: ResourceKind; displayName: string; metadata?: Record<string, unknown> },
  ): Promise<ResourceRef> {
    const data = await http.post<unknown>(`/api/agent/projects/${projectId}/resources`, {
      kind: input.kind,
      display_name: input.displayName,
      metadata: input.metadata,
    })
    const obj = raw(data)
    return {
      id: toString(obj.id),
      kind: toString(obj.kind) as ResourceKind,
      project_id: toString(obj.project_id),
      display_name: toString(obj.display_name),
      revision: toString(obj.revision, '0'),
      allowed_actions: asArray(obj.allowed_actions).map((a) => toString(a)),
    }
  }

  // ---------------------------------------------------------------- 文件

  async uploadProjectFile(projectId: string, file: File): Promise<FileAsset> {
    const form = new FormData()
    form.append('file', file, file.name)
    const data = raw(await http.upload<unknown>(`/api/file/project/${projectId}/upload`, form))
    return {
      file_id: toString(data.file_id),
      file_name: file.name,
      file_size: Number(data.size ?? file.size),
      content_type: toString(data.mime_type) || file.type,
      bucket: toString(data.bucket),
      object_key: toString(data.object_key),
      url: toString(data.url),
    }
  }

  async getFileMetadata(fileId: string): Promise<FileAsset> {
    const data = raw(await http.get<unknown>(`/api/file/metadata/${fileId}`))
    return {
      file_id: toString(data.file_id) || fileId,
      file_name: toString(data.original_name) || toString(data.file_name),
      file_size: Number(data.size ?? 0),
      content_type: toString(data.mime_type),
      bucket: toString(data.bucket),
      object_key: toString(data.object_key),
      url: toString(data.url),
    }
  }

  async fileObjectUrl(bucket: string, objectKey: string): Promise<string> {
    // 受保护图片：<img> 带不了 Bearer 头，用带凭据的 blob 请求再转临时 URL
    return http.blobUrl(`/api/file/${bucket}/${objectKey}`)
  }

  // ---------------------------------------------------------------- 会话

  async listSessions(projectId: string): Promise<Session[]> {
    const data = await http.get<unknown>('/api/agent/sessions', {
      query: { project_id: projectId || undefined },
    })
    return asArray(data).map(mapSession) as unknown as Session[]
  }

  async listMessages(sessionId: string): Promise<ChatMessage[]> {
    const data = await http.get<unknown>(`/api/agent/sessions/${sessionId}/messages`)
    return asArray(data).map(mapMessage).filter((m): m is ChatMessage => !!m)
  }

  async createSession(projectId: string, title: string): Promise<Session> {
    const data = await http.post<unknown>('/api/agent/sessions', {
      project_id: projectId || undefined,
      title,
    })
    return mapSession(data) as unknown as Session
  }

  async sendMessage(
    sessionId: string,
    content: string,
    options: SendMessageOptions = {},
  ): Promise<ChatTurn> {
    // 后端这一轮是同步返回复合结果（reply/task/action/plan），没有 token 流；
    // 不伪造逐字输出，界面按"等待结果"处理。
    // 附件只以已登记资源 id 传递，绝不把文件名当输入。
    const body: Raw = { text: content }
    if (options.resourceRefs?.length) {
      body.resource_refs = options.resourceRefs.map((ref) => ({ id: ref.id }))
    }
    const data = await http.post<unknown>(`/api/agent/sessions/${sessionId}/messages`, body, {
      signal: options.signal,
    })
    return mapChatTurn(sessionId, data)
  }

  async listEvents(sessionId: string, afterSequence: number): Promise<AgentEvent[]> {
    const data = raw(
      await http.get<unknown>(`/api/agent/sessions/${sessionId}/events`, {
        query: { after: afterSequence },
      }),
    )
    return asArray(data.events).map(mapEvent).filter((e): e is AgentEvent => !!e)
  }

  // ---------------------------------------------------------------- 任务

  private async snapshot(taskId: string): Promise<TaskSnapshot> {
    const data = raw(await http.get<unknown>(`/api/agent/tasks/${taskId}`))
    return {
      task: mapTask(data.task),
      actions: asArray(data.actions)
        .map(mapConfirmation)
        .filter((a): a is Confirmation => !!a),
      note: data.note ? raw(data.note) : null,
    }
  }

  async getTaskSnapshot(taskId: string): Promise<TaskSnapshot> {
    return this.snapshot(taskId)
  }

  async listTasks(projectId: string): Promise<AgentTask[]> {
    return asArray(await http.get('/api/agent/tasks', { query: { project_id: projectId, limit: 200 } }))
      .map(mapTask).filter((t): t is AgentTask => !!t)
  }
  async createFeedbackDraft(datasetId: string): Promise<Raw> {
    return raw(await http.post(`/api/dataset/${datasetId}/feedback-version`))
  }
  async listDraftSamples(draftId: string, page: number): Promise<Raw> {
    return raw(await http.get(`/api/dataset/drafts/${draftId}/samples`, { query: { page, page_size: 50 } }))
  }
  async assignDraftSamples(draftId: string, sampleIds: string[], split: string): Promise<Raw> {
    return raw(await http.post(`/api/dataset/drafts/${draftId}/assign`, { sample_ids: sampleIds, split }))
  }
  async finalizeDraft(draftId: string): Promise<Raw> {
    return raw(await http.post(`/api/dataset/drafts/${draftId}/finalize`))
  }
  async startTraining(parentId: string, datasetId: string, epochs: number, batchSize: number): Promise<Raw> {
    return raw(await http.post(`/api/model/${parentId}/incremental`, { feedback_dataset_id: datasetId, epochs, batch_size: batchSize }))
  }

  async confirmPlan(taskId: string, planVersion?: number): Promise<TaskSnapshot> {
    const body = planVersion === undefined ? {} : { plan_version: planVersion }
    await http.post<unknown>(`/api/agent/tasks/${taskId}/plan/confirm`, body)
    return this.snapshot(taskId)
  }

  async stopTask(taskId: string, reason?: string): Promise<TaskSnapshot> {
    await http.post<unknown>(`/api/agent/tasks/${taskId}/stop`, { reason: reason ?? '' })
    return this.snapshot(taskId)
  }

  async reconcileTask(taskId: string): Promise<TaskSnapshot> {
    await http.post<unknown>(`/api/agent/tasks/${taskId}/reconcile`, { auto_continue: true })
    return this.snapshot(taskId)
  }

  async getTaskNote(taskId: string): Promise<Record<string, unknown>> {
    return raw(await http.get<unknown>(`/api/agent/tasks/${taskId}/note`))
  }

  // ---------------------------------------------------------------- 动作

  async confirmAction(actionId: string, expectedRevision: number): Promise<Confirmation> {
    // 版本必须是整数且来自最近一次读取，避免"确认了旧目标"
    const data = await http.post<unknown>(`/api/agent/actions/${actionId}/confirm`, {
      expected_revision: expectedRevision,
    })
    const confirmation = mapConfirmation(data)
    if (!confirmation) throw new Error('确认接口返回的动作结构无法识别')
    return confirmation
  }

  async rejectAction(actionId: string, reason?: string): Promise<Confirmation> {
    const data = await http.post<unknown>(`/api/agent/actions/${actionId}/reject`, {
      reason: reason ?? '',
    })
    const confirmation = mapConfirmation(data)
    if (!confirmation) throw new Error('拒绝接口返回的动作结构无法识别')
    return confirmation
  }

  // ---------------------------------------------------- 工具 / 手册 / 授权

  async listTools(projectId?: string): Promise<ToolInfo[]> {
    const data = await http.get<unknown>('/api/agent/tools', {
      query: { project_id: projectId },
    })
    return asArray(data).map((item) => {
      const obj = raw(item)
      return {
        name: toString(obj.name),
        title: toString(obj.title) || toString(obj.name),
        serverId: toString(obj.server_id), version: toString(obj.version), source: toString(obj.source), billable: obj.billable === true,
        description: toString(obj.description),
        risk: toString(obj.risk),
        kind: toString(obj.kind),
        requires_confirmation: obj.requires_confirmation === true,
        available: obj.available !== false,
      }
    })
  }

  async listManuals(): Promise<ManualInfo[]> {
    const data = await http.get<unknown>('/api/agent/manuals')
    return asArray(data).map((item) => {
      const obj = raw(item)
      return {
        id: toString(obj.id),
        name: toString(obj.name),
        version: Number(obj.version ?? 1),
        summary: toString(obj.summary),
        source: toString(obj.source),
        updated_at: toMillis(obj.updated_at),
      }
    })
  }

  async listGrants(): Promise<Array<Record<string, unknown>>> {
    const data = await http.get<unknown>('/api/agent/grants')
    return asArray(data).map(raw)
  }

  async listMcpConnections(): Promise<Raw[]> { return asArray(await http.get('/api/agent/mcp/connections')).map(raw) }
  async listDirectories(projectId: string): Promise<Raw[]> { return asArray(await http.get('/api/agent/directories', { query: { project_id: projectId } })).map(raw) }
  async authorizeDirectory(projectId: string, root: string, label: string): Promise<Raw> { return raw(await http.post('/api/agent/directories', { project_id: projectId, root, label })) }
  async disableDirectory(id: string): Promise<void> { await http.del(`/api/agent/directories/${id}`) }
  async saveMcpConnection(input: Raw): Promise<Raw> { return raw(await http.post('/api/agent/mcp/connections', input)) }
  async discoverMcp(id: string): Promise<Raw> { return raw(await http.post(`/api/agent/mcp/connections/${id}/discover`)) }
  async grantTool(input: Raw): Promise<Raw> { return raw(await http.post('/api/agent/grants', input)) }
  async revokeGrant(id: string): Promise<void> { await http.del(`/api/agent/grants/${id}`) }
  async listDocuments(projectId: string): Promise<Raw[]> { return asArray(await http.get('/api/agent/documents', { query: { project_id: projectId } })).map(raw) }
  async parseDocument(assetId: string, title: string): Promise<Raw> { return raw(await http.post('/api/agent/documents/parse', { asset_id: assetId, title })) }
  async searchDocument(id: string, query: string): Promise<Raw> { return raw(await http.get(`/api/agent/documents/${id}/search`, { query: { q: query } })) }

  // ---------------------------------------------------------------- 业务

  async listRecords(
    query: PageQuery & { projectId?: string } = {},
  ): Promise<PageResult<DetectionRecordSummary>> {
    const data = raw(
      await http.get<unknown>('/api/detection/records', {
        query: {
          page: query.page ?? 1,
          page_size: query.pageSize ?? 20,
          search: query.search,
        },
      }),
    )
    const items = asArray(data.records).map(mapRecord)
    return {
      items,
      total: Number(data.total ?? items.length),
      page: Number(data.page ?? query.page ?? 1),
      pageSize: Number(data.page_size ?? query.pageSize ?? 20),
    }
  }

  async getRecord(recordId: string): Promise<DetectionRecordSummary | null> {
    const data = await http.get<unknown>(`/api/detection/records/${recordId}`)
    return data ? mapRecord(data) : null
  }

  async requestAiReview(recordId: string): Promise<Record<string, unknown>> {
    return raw(await http.post<unknown>(`/api/detection/records/${recordId}/ai-review`))
  }

  async submitHumanReview(
    recordId: string,
    input: HumanReviewInput,
  ): Promise<Record<string, unknown>> {
    return raw(
      await http.post<unknown>(`/api/detection/records/${recordId}/human-review`, {
        expected_version: input.expectedVersion,
        annotations: input.annotations ?? [],
        decision: input.decision,
      }),
    )
  }

  async addFeedback(recordId: string, input: { datasetId: string }): Promise<Record<string, unknown>> {
    return raw(
      await http.post<unknown>(`/api/detection/records/${recordId}/feedback`, {
        dataset_id: input.datasetId,
      }),
    )
  }

  async listCategories(): Promise<Array<{ id: number; name: string; label: string; color?: string }>> {
    const data = await http.get<unknown>('/api/model/classes')
    return mapCategories(data).map((item) => ({
      id: item.id,
      name: item.name,
      label: item.label,
      color: item.color,
    }))
  }

  async listDatasets(query: PageQuery = {}): Promise<PageResult<DatasetSummary>> {
    const data = raw(
      await http.get<unknown>('/api/dataset/list', {
        query: {
          page: query.page ?? 1,
          page_size: query.pageSize ?? 20,
          search: query.search,
        },
      }),
    )
    const items = asArray(data.datasets).map(mapDataset)
    return {
      items,
      total: Number(data.total ?? items.length),
      page: Number(data.page ?? query.page ?? 1),
      pageSize: Number(data.page_size ?? query.pageSize ?? 20),
    }
  }

  async listModels(query: PageQuery = {}): Promise<PageResult<DetectionModel>> {
    const data = raw(
      await http.get<unknown>('/api/model/list', {
        query: {
          page: query.page ?? 1,
          page_size: query.pageSize ?? 20,
          search: query.search,
        },
      }),
    )
    const items = asArray(data.models).map(mapModel).map(toDetectionModel)
    return {
      items,
      total: Number(data.total ?? items.length),
      page: Number(data.page ?? query.page ?? 1),
      pageSize: Number(data.page_size ?? query.pageSize ?? 20),
    }
  }

  async detectImage(input: {
    modelId: string
    file: File
    xFile?: File
    confidence: number
    title?: string
  }): Promise<DetectionRecordSummary> {
    const form = new FormData()
    form.append('model_id', input.modelId)
    form.append('confidence_threshold', String(input.confidence))
    if (input.title) form.append('title', input.title)
    form.append('image', input.file, input.file.name)
    if (input.xFile) form.append('x_image', input.xFile, input.xFile.name)

    // 后端在推理成功后自动落库并返回 record_id，前端不能自己编造记录
    const data = raw(await http.upload<unknown>('/api/detection/detect', form))
    const recordId = toString(data.record_id)
    if (!recordId) {
      throw new Error('检测接口未返回记录号，结果不可确认')
    }
    if (data.record_saved === false) {
      throw new Error('检测完成但记录未保存，无法进入复核队列')
    }
    const record = await this.getRecord(recordId)
    if (!record) {
      throw new Error('检测记录已生成但读取失败：' + recordId)
    }
    return record
  }

  async uploadDataset(input: {
    projectId?: string
    file: File
    name: string
    description?: string
    datasetMode: 'single' | 'multimodal'
    secondaryModality?: string
    classNames?: string[]
  }): Promise<Record<string, unknown>> {
    const form = new FormData()
    form.append('file', input.file, input.file.name)
    if (input.projectId) form.append('project_id', input.projectId)
    form.append('name', input.name)
    form.append('description', input.description ?? '')
    form.append('dataset_mode', input.datasetMode)
    if (input.datasetMode === 'multimodal' && input.secondaryModality) {
      form.append('secondary_modality', input.secondaryModality)
    }
    if (input.classNames?.length) form.append('class_names', input.classNames.join(','))
    return raw(await http.upload<unknown>('/api/dataset/upload', form))
  }

  async publishModel(modelId: string): Promise<Record<string, unknown>> {
    return raw(await http.post<unknown>(`/api/model/${modelId}/publish`))
  }

  async importModelWeight(input: {
    projectId?: string
    name: string
    description?: string
    datasetId: string
    weight: File
  }): Promise<Record<string, unknown>> {
    const form = new FormData()
    form.append('name', input.name)
    form.append('description', input.description ?? '')
    form.append('dataset_id', input.datasetId)
    form.append('weight', input.weight, input.weight.name)
    if (input.projectId) form.append('project_id', input.projectId)
    // 后端会真实验证权重可加载，失败即报错
    return raw(await http.upload<unknown>('/api/model/import', form))
  }

  async addFeedbackBatch(recordIds: string[]): Promise<Record<string, unknown>> {
    return raw(
      await http.post<unknown>('/api/detection/records/feedback/batch', { record_ids: recordIds }),
    )
  }

  async getModelDetail(modelId: string): Promise<ModelSummary | null> {
    const data = await http.get<unknown>(`/api/model/${modelId}`)
    if (!data || typeof data !== 'object') return null
    const summary = mapModel(data)
    return { ...summary, is_personal: true }
  }

  async compareModels(parentId: string, childId: string): Promise<Record<string, unknown>> {
    return raw(await http.get<unknown>(`/api/model/${parentId}/compare/${childId}`))
  }

  // --------------------------------------------------- 兼容旧页面的读接口

  async listDefects(_projectId: string): Promise<DefectItem[]> {
    // 复核队列 = 未完成人工复核的检测记录，不再使用内置假数据
    const page = await this.listRecords({ page: 1, pageSize: 50 })
    return page.items.map((record) => {
      const top = record.detections[0]
      const confidence = record.detections.reduce(
        (max, item) => (item.confidence > max ? item.confidence : max),
        0,
      )
      return {
        id: record.id,
        turbineId: record.title || record.id.slice(0, 8),
        bladeFile: record.original_image_url,
        thumbnail: record.result_image_url || record.original_image_url,
        suspectedType: top?.class_name || record.class_names[0] || '未标注',
        confidence,
        reviewStatus:
          record.human_review_status === 'pending'
            ? 'pending'
            : record.human_review_status === 'confirmed' || record.human_review_status === 'no_defect'
              ? 'done'
              : 'reviewing',
        severity: confidence >= 0.8 ? 'high' : confidence >= 0.5 ? 'medium' : 'low',
      }
    })
  }

  async getDefect(id: string): Promise<DefectItem | null> {
    const record = await this.getRecord(id)
    if (!record) return null
    const items = await this.listDefects('')
    return items.find((item) => item.id === record.id) ?? null
  }

  // ------------------------------------------------------------ 明确不支持

  async createTask(): Promise<AgentTask> {
    // 任务只能由对话产生；页面直连"创建任务"等于绕过计划与确认
    return this.unsupported('直接创建任务（请通过对话发起，由 Agent 生成计划后确认）')
  }
}
