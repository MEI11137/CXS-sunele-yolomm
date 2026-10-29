import type { DetectionRecordSummary } from '@/types'

/** 只提交勾选、已人工确认且尚未入池的可见记录；最终资格仍由后端核对。 */
export function feedbackEligibleRecords(records: DetectionRecordSummary[], ids: string[]) {
  const selected = new Set(ids)
  return records.filter(record => selected.has(record.id) && record.human_review_status === 'confirmed'
    && !record.feedback_dataset_id && record.record_status !== 'deleted')
}
