import type { AgentEvent, Confirmation } from '@/types'

// Only new events from the currently displayed conversation may control its UI.
export function workspaceEffect(event: AgentEvent, projectId: string, sessionId: string, activatedAt: number) {
  const p = event.payload
  if (!['workspace.account_changed','workspace.context','workspace.download','workspace.document','workspace.ui'].includes(event.type) ||
      (event.created_at || 0) < activatedAt || p.source_project_id !== projectId || p.source_session_id !== sessionId) return null
  return { type: event.type, payload: p }
}

export function closedContext(action: Confirmation) {
  const output = action.result?.outputs as Record<string, unknown> | undefined
  if (action.execution_state !== 'executed' || output?.context_closed !== true || action.parameters.operation !== 'delete') return null
  if (action.tool_name === 'workspace_manage_project') return { kind: 'project', id: String(output.id) }
  if (action.tool_name === 'workspace_manage_session') return { kind: 'session', id: String(output.id) }
  return null
}
