// Mock 适配器实现 —— 模拟响应，覆盖各种状态。
import type { ApiAdapter, Mode } from './types'
import type {
  Workspace,
  Defect,
  DetectionModel,
  Session,
  ChatMessage,
  AgentTask,
  AgentEvent,
  Confirmation,
} from '@/types'
import {
  mockWorkspaces,
  mockDefects,
  mockModels,
  mockSessions,
} from './mock-data'

function clone<T>(o: T): T {
  return JSON.parse(JSON.stringify(o))
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

export class MockAdapter implements ApiAdapter {
  mode: Mode = 'mock'

  private workspaces: Workspace[] = clone(mockWorkspaces)
  private defects: Defect[] = clone(mockDefects)
  private models: DetectionModel[] = clone(mockModels)
  private sessions: Session[] = clone(mockSessions)
  private tasks: AgentTask[] = []
  private events: AgentEvent[] = []

  async listWorkspaces(): Promise<Workspace[]> {
    await delay(80)
    return clone(this.workspaces)
  }

  async createWorkspace(input: {
    name: string
    description: string
    requirements: string
  }): Promise<Workspace> {
    await delay(120)
    const ws: Workspace = {
      id: 'ws-' + Date.now(),
      name: input.name,
      description: input.description,
      requirements: input.requirements,
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

  async listDefects(_projectId: string): Promise<Defect[]> {
    await delay(80)
    // 演示：默认工作区返回全部缺陷；新项目返回空状态
    return clone(this.defects)
  }

  async getDefect(id: string): Promise<Defect | null> {
    await delay(40)
    const d = this.defects.find((x) => x.id === id)
    return d ? clone(d) : null
  }

  async listModels(): Promise<DetectionModel[]> {
    await delay(60)
    return clone(this.models)
  }

  async listSessions(projectId: string): Promise<Session[]> {
    await delay(60)
    return clone(this.sessions.filter((s) => s.project_id === projectId))
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
    onDelta?: (delta: string) => void,
    signal?: AbortSignal,
  ): Promise<ChatMessage> {
    const reply = this.mockReply(content)
    const total = reply.length
    let i = 0
    // 流式逐字返回，模拟真实流式
    while (i < total) {
      if (signal?.aborted) {
        throw new DOMException('Aborted', 'AbortError')
      }
      const chunk = reply.slice(i, i + 3)
      i += 3
      onDelta?.(chunk)
      await delay(18)
    }
    const msg: ChatMessage = {
      id: 'm-' + Date.now(),
      session_id: sessionId,
      role: 'assistant',
      content: reply,
      created_at: Date.now(),
    }
    return clone(msg)
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
      status: 'waiting_confirm',
      steps: [
        { id: 'st1', name: '校验影像', status: 'pending' },
        { id: 'st2', name: '加载检测模型', status: 'pending' },
        { id: 'st3', name: '执行叶片检测', status: 'pending' },
        { id: 'st4', name: '生成复核队列', status: 'pending' },
      ],
      waiting_for: 'plan_confirm',
      business_refs: [
        { kind: 'model', id: input.model_id, display_name: model ? `${model.name} ${model.version}` : input.model_id },
        { kind: 'image', id: 'img-batch', display_name: `${input.images.length} 张影像` },
      ],
    }
    this.tasks.push(task)
    return clone(task)
  }

  async confirmAction(
    actionId: string,
    decision: 'approved' | 'rejected',
  ): Promise<Confirmation> {
    await delay(150)
    return {
      action_id: actionId,
      revision: 'rev-' + Date.now(),
      actual_target_projects: ['ws-default'],
      parameters_summary: { decision },
      preview: decision === 'approved' ? '将开始执行检测' : '已拒绝，任务不执行',
      status: decision === 'approved' ? 'approved' : 'rejected',
    }
  }

  async listEvents(taskId: string, afterSequence: number): Promise<AgentEvent[]> {
    await delay(40)
    return clone(this.events.filter((e) => e.task_id === taskId && e.sequence > afterSequence))
  }

  async getTaskSnapshot(taskId: string): Promise<AgentTask | null> {
    await delay(40)
    const t = this.tasks.find((x) => x.id === taskId)
    return t ? clone(t) : null
  }
}
