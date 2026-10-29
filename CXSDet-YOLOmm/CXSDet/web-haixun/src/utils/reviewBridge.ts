import { shallowRef } from 'vue'
import type { HumanReviewAnnotation } from '@/services'

export interface ReviewPageContext {
  project_id: string
  record_id: string
  expected_version: number
  draft_revision: number
  annotations: HumanReviewAnnotation[]
  selected_index: number | null
  view: 'original' | 'result' | 'compare'
  mode: 'select' | 'draw' | 'pan'
  zoom: number
}

export const reviewPage = shallowRef<ReviewPageContext | null>(null)
const pending: Array<{ type: string; payload: Record<string, unknown>; at: number }> = []
export function reviewContext(projectId: string): ReviewPageContext | undefined {
  const page = reviewPage.value
  return page?.project_id === projectId ? JSON.parse(JSON.stringify(page)) as ReviewPageContext : undefined
}
export function dispatchReviewEvent(type: string, payload: Record<string, unknown>) {
  pending.push({ type, payload, at: Date.now() })
  if (pending.length > 20) pending.shift()
  window.dispatchEvent(new CustomEvent('haixun:review', { detail: { type, payload } }))
}
export function takeReviewEvents(projectId: string, recordId: string) {
  const selected = pending.filter(item => item.payload.project_id === projectId && item.payload.record_id === recordId && Date.now() - item.at < 60000)
  for (let i = pending.length - 1; i >= 0; i--) if (selected.includes(pending[i]) || Date.now() - pending[i].at >= 60000) pending.splice(i, 1)
  return selected
}
