// Mock 适配器：只在演示模式（VITE_API_MODE=mock）下使用，界面上必须明确标注"模拟"。
// 真实模式下 createAdapter() 不会返回它，接口失败也绝不回退到这里。

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
  PageResult,
  PersonalConnection,
  ResourceRef,
  Session,
  Workspace,
} from '@/types'
import { BaseAdapter } from './adapter-base'
import type { DefectItem, Mode, SendMessageOptions, TaskSnapshot } from './types'
import {
  mockDefects,
  mockModels,
  mockSessions,
  mockWorkspaces,
} from './mock-data'

function clone<T>(o: T): T {
  return JSON.parse(JSON.stringify(o))
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

export class MockAdapter extends BaseAdapter {
  readonly mode: Mode = 'mock'

  private workspaces: Workspace[] = clone(mockWorkspaces)
  private defects: DefectItem[] = clone(mockDefects)
  private models: DetectionModel[] = clone(mockModels)
  private sessions: Session[] = clone(mockSessions)
  private tasks: AgentTask[] = []
  private events: AgentEvent[] = []

  /** 演示登录：不产生真实凭据，token 留空，界面按"演示模式"展示。 */
  async login(username: string): Promise<AuthUser> {
    await delay(120)
    return {
      id: 0,
      username: username.trim() || 'demo',
      realName: username.trim() || '演示用户',
      role: 2,
      token: '',
    }
  }

  async currentUser(): Promise<AuthUser | null> {
    return null
  }

  async logout(): Promise<void> {
    await delay(40)
  }

  async listWorkspaces(): Promise<Workspace[]> {
    await delay(80)
    return clone(this.workspaces)
  }

  async createWorkspace(input: {
    root_directory?: string
    name: string
    description: string
    requirements: string
  }): Promise<Workspace> {
    await delay(120)
    const ws: Workspace = {
      id: 'ws-' + Date.now(),
      name: input.name,
      root_directory: input.root_directory,
      description: input.description,
      requirements: input.requirements,
      requirements_raw: input.requirements ? { notes: input.requirements } : {},
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }
    this.workspaces.push(ws)
    return clone(ws)
  }

  async updateWorkspace(id: string, patch: Partial<Workspace>): Promise<Workspace> {
    await delay(120)
    const idx = this.workspaces.findIndex((w) => w.id === id)
    if (idx >= 0) {
      this.workspaces[idx] = {
        ...this.workspaces[idx],
        ...patch,
        updated_at: new Date().toISOString(),
      }
      return clone(this.workspaces[idx])
    }
    throw new Error('工作区不存在')
  }

  async browseLocalFolders(): Promise<import('@/types').LocalFolderListing> {
    throw new Error('本机文件夹功能需要连接真实后端')
  }

  async createLocalFolder(): Promise<{ path: string }> {
    throw new Error('本机文件夹功能需要连接真实后端')
  }

  async listDefects(_projectId: string): Promise<DefectItem[]> {
    await delay(80)
    return clone(this.defects)
  }

  async getDefect(id: string): Promise<DefectItem | null> {
    await delay(40)
    const d = this.defects.find((x) => x.id === id)
    return d ? clone(d) : null
  }

  async listModels(): Promise<PageResult<DetectionModel>> {
    await delay(60)
    return { items: clone(this.models), total: this.models.length, page: 1, pageSize: this.models.length }
  }

  async listSessions(projectId: string): Promise<Session[]> {
    await delay(60)
    return clone(this.sessions.filter((s) => s.project_id === projectId))
  }

  async listMessages(sessionId: string): Promise<ChatMessage[]> {
    await delay(40)
    const session = this.sessions.find((s) => s.id === sessionId)
    return clone(session?.messages ?? [])
  }

  async createSession(projectId: string, title: string): Promise<Session> {
    await delay(60)
    const s: Session = {
      id: 's-' + Date.now(),
      project_id: projectId,
      title: title || '新会话',
      created_at: Date.now(),
      messages: [],
    }
    this.sessions.push(s)
    return clone(s)
  }

  async sendMessage(
    sessionId: string,
    content: string,
    options: SendMessageOptions = {},
  ): Promise<ChatTurn> {
    const reply = this.mockReply(content)
    let i = 0
    while (i < reply.length) {
      if (options.signal?.aborted) {
        throw new DOMException('Aborted', 'AbortError')
      }
      const chunk = reply.slice(i, i + 3)
      i += 3
      options.onDelta?.(chunk)
      await delay(18)
    }
    const message: ChatMessage = {
      id: 'm-' + Date.now(),
      session_id: sessionId,
      role: 'assistant',
      content: reply,
      created_at: Date.now(),
    }
    return {
      session_id: sessionId,
      message,
      task: null,
      action: null,
      plan: null,
      eventsAfter: 0,
      status: 'completed',
    }
  }

  private mockReply(content: string): string {
    if (/巡检|检测/.test(content)) {
      return '我已为你生成检测计划草稿。请查看右侧计划卡片，确认目标项目、影像数量与检测模型后，再确认执行。确认前不会上传或执行任何检测。'
    }
    if (/复核|标注/.test(content)) {
      return '检测结果已进入人工复核队列。你可以到「人工复核」页面，在原图上修正类别、调整或补绘检测框，确认无缺陷或提交后即可完成该项。'
    }
    if (/模型|连接/.test(content)) {
      return '个人大模型连接与检测模型是相互独立的。你可以在「设置 → 个人连接」中配置助手使用的大模型，检测模型则在发起巡检时单独选择。'
    }
    return '我理解了。如需发起巡检、核对计划或处理复核，可以直接告诉我；涉及上传、保存或执行的操作，我会先展示计划并等待你确认。'
  }

  async createTask(input: {
    project_id: string
    session_id: string
    images: string[]
    model_id: string
  }): Promise<AgentTask> {
    await delay(150)
    const model = this.models.find((m) => m.id === input.model_id)
    const task: AgentTask = {
      id: 't-' + Date.now(),
      session_id: input.session_id,
      project_id: input.project_id,
      status: 'awaiting_plan',
      goal: '对上传影像执行光伏板缺陷检测',
      steps: [
        { id: 'st1', name: '校验影像', status: 'pending' },
        { id: 'st2', name: '加载检测模型', status: 'pending' },
        { id: 'st3', name: '执行缺陷检测', status: 'pending' },
        { id: 'st4', name: '生成复核队列', status: 'pending' },
      ],
      waiting_for: { kind: 'plan_confirm', refs: [], conditions: {} },
      business_refs: [
        {
          kind: 'model',
          id: input.model_id,
          display_name: model ? `${model.name} ${model.version}` : input.model_id,
        },
        { kind: 'image', id: 'img-batch', display_name: `${input.images.length} 张影像` },
      ],
      planVersion: 1,
      updatedAt: Date.now(),
    }
    this.tasks.push(task)
    return clone(task)
  }

  async confirmAction(actionId: string, expectedRevision: number): Promise<Confirmation> {
    await delay(150)
    return {
      action_id: actionId,
      task_id: 't-demo',
      revision: expectedRevision,
      actual_target_projects: ['ws-default'],
      parameters: {},
      parameters_summary: '将开始执行检测',
      preview: { tool_title: '执行检测' },
      tool_name: 'detection.run',
      tool_title: '执行检测',
      status: 'approved',
      execution_state: 'executed',
    }
  }

  async rejectAction(actionId: string): Promise<Confirmation> {
    await delay(80)
    return {
      action_id: actionId,
      task_id: 't-demo',
      revision: 0,
      actual_target_projects: [],
      parameters: {},
      parameters_summary: '已拒绝，任务不执行',
      preview: { tool_title: '执行检测' },
      tool_name: 'detection.run',
      tool_title: '执行检测',
      status: 'rejected',
      execution_state: 'not_executed',
    }
  }

  async listEvents(sessionId: string, afterSequence: number): Promise<AgentEvent[]> {
    await delay(40)
    const taskIds = this.tasks.filter((t) => t.session_id === sessionId).map((t) => t.id)
    return clone(
      this.events.filter((e) => taskIds.includes(e.task_id) && e.sequence > afterSequence),
    )
  }

  async getTaskSnapshot(taskId: string): Promise<TaskSnapshot> {
    await delay(40)
    const t = this.tasks.find((x) => x.id === taskId)
    return { task: t ? clone(t) : null, actions: [], note: null }
  }

  // 以下能力演示模式没有真实数据，明确报"未接入"，由页面显示空状态
  async getConnection(): Promise<PersonalConnection> {
    return { configured: false }
  }

  async listResources(_projectId: string): Promise<ResourceRef[]> {
    return []
  }

  async listRecords(): Promise<PageResult<DetectionRecordSummary>> {
    return { items: [], total: 0, page: 1, pageSize: 0 }
  }

  async listDatasets(): Promise<PageResult<DatasetSummary>> {
    return { items: [], total: 0, page: 1, pageSize: 0 }
  }
}
