import { onMounted, onBeforeUnmount } from 'vue'
import { request } from '@/services/http'
import { useWorkspaceStore } from '@/stores/workspace'

export type ResourceKind = 'dataset' | 'model' | 'draft'
export interface DeletedResource { kind: ResourceKind; id: string; name: string; revision: number; deleted_at: string }

export async function changeResource(kind: ResourceKind, id: string, operation: 'delete' | 'restore'): Promise<boolean> {
  const path = '/api/agent/resources/lifecycle/' + kind + '/' + encodeURIComponent(id)
  const preview = await request<{ name: string; project_id: string; revision: number; impact: string; blocked_reasons: string[] }>(path, { query: { operation } })
  if (preview.blocked_reasons.length) throw new Error(preview.blocked_reasons.join('；'))
  if (!window.confirm(`${operation === 'delete' ? '删除' : '恢复'}「${preview.name}」？\n项目：${preview.project_id}\n版本：${preview.revision}\n${preview.impact}`)) return false
  await request(path, { method: 'POST', body: { operation, expected_revision: preview.revision } })
  return true
}

export function useResourceRefresh(refresh: () => unknown) {
  const workspace = useWorkspaceStore()
  const listener = (event: Event) => { if ((event as CustomEvent).detail?.project_id === workspace.currentId) void refresh() }
  onMounted(() => window.addEventListener('haixun:resources-changed', listener))
  onBeforeUnmount(() => window.removeEventListener('haixun:resources-changed', listener))
}
