import type { AgentEvent, Confirmation } from '@/types'

export interface ToolActivity { id: string; name: string; status: string; events: AgentEvent[] }
export function toolActivities(events: AgentEvent[], taskId?: string, actions: Confirmation[] = []): ToolActivity[] {
  const tools = events.filter(event => (!taskId || event.task_id === taskId) && event.type.includes('tool'))
  // OpenCode 和业务桥会分别记录同一次调用，只展示引擎视角的完整生命周期。
  const native = tools.filter(event => event.payload.engine === 'opencode')
  const rows = native.length ? native : tools
  const groups: ToolActivity[] = []
  const byId = new Map<string, ToolActivity>()
  const pending = new Map<string, ToolActivity>()
  for (const event of rows) {
    const name = String(event.payload.tool || event.type)
    const identity = String(event.payload.tool_part_id || event.payload.call_id || '')
    const status = String(event.payload.status || (event.type.includes('requested') ? 'running' : 'completed'))
    let group = identity ? byId.get(identity) : pending.get(name)
    if (!group) {
      group = { id: identity || event.event_id, name, status, events: [] }
      groups.push(group)
      if (identity) byId.set(identity, group)
    }
    group.status = status
    group.events.push(event)
    if (['completed', 'succeeded', 'error', 'failed', 'cancelled'].includes(status)) pending.delete(name)
    else pending.set(name, group)
  }
  for (const action of actions) {
    if ((taskId && action.task_id !== taskId) || action.tool_name !== 'opencode_permission' || !['rejected', 'expired'].includes(action.status)) continue
    const detail = action.parameters.detail as { tool?: { callID?: string } } | undefined
    const callId = detail?.tool?.callID
    if (!callId) continue
    for (const group of groups) {
      if (['running', 'pending'].includes(group.status) && group.events.some(event => event.payload.call_id === callId)) group.status = 'cancelled'
    }
  }
  return groups
}
