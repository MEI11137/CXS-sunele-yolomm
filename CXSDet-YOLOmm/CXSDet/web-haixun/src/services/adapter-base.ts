// 适配器基类：默认每个能力都"未接入"，由具体实现按需覆盖。
//
// 这样做的目的不是省代码，而是保证"没实现的能力会明确失败"：
// 任何页面调用到尚未接入的方法，都会拿到 NotSupportedError，
// 而不是一个看起来成功的空数组或假数据。

import type {
  AgentEvent,
  AgentTask,
  AuthUser,
  ChatMessage,
  ChatTurn,
  Confirmation,
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
import {
  NotSupportedError,
  type ApiAdapter,
  type DefectItem,
  type FileAsset,
  type HumanReviewInput,
  type ManualInfo,
  type Mode,
  type PageQuery,
  type SendMessageOptions,
  type TaskSnapshot,
  type ToolInfo,
  type WorkspaceOverview,
} from './types'

export abstract class BaseAdapter implements ApiAdapter {
  abstract readonly mode: Mode

  protected unsupported(feature: string): never {
    throw new NotSupportedError(feature)
  }

  async login(_username?: string, _password?: string): Promise<AuthUser> {
    return this.unsupported('账号登录')
  }
  async register(_input?: unknown): Promise<void> {
    this.unsupported('账号注册')
  }
  async currentUser(): Promise<AuthUser | null> {
    return this.unsupported('当前用户信息')
  }
  async logout(): Promise<void> {
    this.unsupported('退出登录')
  }

  async listWorkspaces(): Promise<Workspace[]> {
    return this.unsupported('工作区列表')
  }
  async createWorkspace(_input?: unknown): Promise<Workspace> {
    return this.unsupported('创建工作区')
  }
  async updateWorkspace(_id?: string, _patch?: unknown): Promise<Workspace> {
    return this.unsupported('更新工作区')
  }
  async getOverview(): Promise<WorkspaceOverview> {
    return this.unsupported('工作区首屏聚合')
  }

  async getConnection(): Promise<PersonalConnection> {
    return this.unsupported('读取个人连接')
  }
  async saveConnection(_input?: unknown): Promise<PersonalConnection> {
    return this.unsupported('保存个人连接')
  }
  async deleteConnection(): Promise<void> {
    this.unsupported('删除个人连接')
  }
  async checkConnection(): Promise<PersonalConnection> {
    return this.unsupported('检查个人连接')
  }

  async listResources(_projectId?: string, _kinds?: ResourceKind[]): Promise<ResourceRef[]> {
    return this.unsupported('项目资源列表')
  }
  async registerResource(_projectId?: string, _input?: unknown): Promise<ResourceRef> {
    return this.unsupported('登记项目资源')
  }

  async uploadProjectFile(_projectId?: string, _file?: File): Promise<FileAsset> {
    return this.unsupported('项目文件上传')
  }
  async getFileMetadata(_fileId?: string): Promise<FileAsset> {
    return this.unsupported('文件元数据')
  }
  async fileObjectUrl(_bucket?: string, _objectKey?: string): Promise<string> {
    return this.unsupported('受保护文件读取')
  }

  async listSessions(_projectId?: string): Promise<Session[]> {
    return this.unsupported('会话列表')
  }
  async listMessages(_sessionId?: string): Promise<ChatMessage[]> {
    return this.unsupported('会话历史消息')
  }
  async createSession(_projectId?: string, _title?: string): Promise<Session> {
    return this.unsupported('创建会话')
  }
  async sendMessage(
    _sessionId?: string,
    _content?: string,
    _options?: SendMessageOptions,
  ): Promise<ChatTurn> {
    return this.unsupported('发送消息')
  }
  async listEvents(_sessionId?: string, _afterSequence?: number): Promise<AgentEvent[]> {
    return this.unsupported('任务事件')
  }

  async getTaskSnapshot(_taskId?: string): Promise<TaskSnapshot> {
    return this.unsupported('任务快照')
  }
  async confirmPlan(_taskId?: string): Promise<TaskSnapshot> {
    return this.unsupported('确认计划')
  }
  async stopTask(_taskId?: string, _reason?: string): Promise<TaskSnapshot> {
    return this.unsupported('停止任务')
  }
  async reconcileTask(_taskId?: string): Promise<TaskSnapshot> {
    return this.unsupported('核实任务状态')
  }
  async getTaskNote(_taskId?: string): Promise<Record<string, unknown>> {
    return this.unsupported('任务交接笔记')
  }

  async confirmAction(_actionId?: string, _expectedRevision?: number): Promise<Confirmation> {
    return this.unsupported('确认动作')
  }
  async rejectAction(_actionId?: string, _reason?: string): Promise<Confirmation> {
    return this.unsupported('拒绝动作')
  }

  async listTools(_projectId?: string): Promise<ToolInfo[]> {
    return this.unsupported('工具清单')
  }
  async listManuals(): Promise<ManualInfo[]> {
    return this.unsupported('任务手册')
  }
  async listGrants(): Promise<Array<Record<string, unknown>>> {
    return this.unsupported('外部工具授权')
  }

  async listRecords(
    _query?: PageQuery & { projectId?: string },
  ): Promise<PageResult<DetectionRecordSummary>> {
    return this.unsupported('检测记录列表')
  }
  async getRecord(_recordId?: string): Promise<DetectionRecordSummary | null> {
    return this.unsupported('检测记录详情')
  }
  async requestAiReview(_recordId?: string): Promise<Record<string, unknown>> {
    return this.unsupported('AI 核查')
  }
  async submitHumanReview(_recordId?: string, _input?: HumanReviewInput): Promise<Record<string, unknown>> {
    return this.unsupported('人工标签提交')
  }
  async addFeedback(_recordId?: string, _input?: unknown): Promise<Record<string, unknown>> {
    return this.unsupported('反馈回流')
  }
  async listCategories(): Promise<Array<{ id: number; name: string; label: string; color?: string }>> {
    return this.unsupported('类别表')
  }
  async listDatasets(_query?: PageQuery): Promise<PageResult<DatasetSummary>> {
    return this.unsupported('数据集列表')
  }
  async uploadDataset(_input?: unknown): Promise<Record<string, unknown>> {
    return this.unsupported('数据集导入')
  }
  async publishModel(_modelId?: string): Promise<Record<string, unknown>> {
    return this.unsupported('模型发布')
  }
  async importModelWeight(_input?: unknown): Promise<Record<string, unknown>> {
    return this.unsupported('模型权重导入')
  }
  async addFeedbackBatch(_recordIds?: string[]): Promise<Record<string, unknown>> {
    return this.unsupported('批量反馈回流')
  }
  async listModels(_query?: PageQuery): Promise<PageResult<DetectionModel>> {
    return this.unsupported('模型列表')
  }
  async getModelDetail(_modelId?: string): Promise<ModelSummary | null> {
    return this.unsupported('模型详情')
  }
  async compareModels(_parentId?: string, _childId?: string): Promise<Record<string, unknown>> {
    return this.unsupported('模型对比')
  }
  async detectImage(_input?: unknown): Promise<DetectionRecordSummary> {
    return this.unsupported('直接检测')
  }

  async listDefects(_projectId?: string): Promise<DefectItem[]> {
    return this.unsupported('复核队列')
  }
  async getDefect(_id?: string): Promise<DefectItem | null> {
    return this.unsupported('复核条目详情')
  }

  async createTask(_input?: unknown): Promise<AgentTask> {
    return this.unsupported('直接创建任务')
  }
}
