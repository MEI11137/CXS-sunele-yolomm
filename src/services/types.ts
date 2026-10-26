// 接口适配层：所有新增页面通过这一层调用后端。
// 目前后端未就绪，统一走 mock 实现（明确标识「模拟」）。
// 接入真实接口时，仅需新增 real 实现并切换 adapter 配置。

import type {
  Workspace,
  Defect,
  DetectionModel,
  Session,
  ChatMessage,
  AgentTask,
  AgentEvent,
  Confirmation,
  AgentError,
} from '@/types'

export type Mode = 'mock' | 'real'

export interface ApiAdapter {
  mode: Mode

  // 工作区
  listWorkspaces(): Promise<Workspace[]>
  createWorkspace(input: { name: string; description: string; requirements: string }): Promise<Workspace>
  updateWorkspace(id: string, patch: Partial<Workspace>): Promise<Workspace>

  // 缺陷
  listDefects(projectId: string): Promise<Defect[]>
  getDefect(id: string): Promise<Defect | null>

  // 模型
  listModels(): Promise<DetectionModel[]>

  // 会话
  listSessions(projectId: string): Promise<Session[]>
  createSession(projectId: string, title: string): Promise<Session>
  sendMessage(
    sessionId: string,
    content: string,
    onDelta?: (delta: string) => void,
    signal?: AbortSignal,
  ): Promise<ChatMessage>

  // 任务
  createTask(input: {
    project_id: string
    session_id: string
    images: string[]
    model_id: string
  }): Promise<AgentTask>
  confirmAction(actionId: string, decision: 'approved' | 'rejected'): Promise<Confirmation>

  // 事件
  listEvents(taskId: string, afterSequence: number): Promise<AgentEvent[]>
  getTaskSnapshot(taskId: string): Promise<AgentTask | null>
}

export type { Workspace, Defect, DetectionModel, Session, ChatMessage, AgentTask, AgentError }
