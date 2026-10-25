/**
 * 复核状态字典
 *
 * 取值为后端 detection_records 表真实枚举，不允许前端自造状态。
 * 侧重点：AI 复核的结论一律用「疑」「辅助」表述，人工状态才用「已确认」，
 * 避免阅读者把机器判断当成已经确诊的结论。
 */

export type Tone = 'neutral' | 'progress' | 'attention' | 'ok' | 'danger'

export interface StatusMeta {
  /** 面向使用者的名称 */
  label: string
  tone: Tone
  /** 这个状态意味着什么 */
  meaning: string
  /** 下一步该做什么；null 表示这一环已完成 */
  next: string | null
}

/** 人工复核状态：human_review_status */
export const HUMAN_STATUS: Record<string, StatusMeta> = {
  pending: {
    label: '待人工确认',
    tone: 'attention',
    meaning: '模型检测结果尚未经人确认，只是一条待核证据。',
    next: '对照原图核对标注，确认或修正后提交人工复核'
  },
  confirmed: {
    label: '已人工确认',
    tone: 'ok',
    meaning: '标注已由人工确认，可作为训练样本回流。',
    next: '加入原数据集反馈池，生成新的数据集版本'
  },
  rejected: {
    label: '已驳回',
    tone: 'danger',
    meaning: '人工判定本次检测结果不可用，不进入训练数据。',
    next: null
  }
}

/** AI 辅助复核状态：ai_review_status */
export const AI_STATUS: Record<string, StatusMeta> = {
  not_requested: {
    label: '未核查',
    tone: 'neutral',
    meaning: '还没有请求 AI 辅助核查，当前只有模型检测结果。',
    next: '发起 AI 辅助核查，获取疑点线索'
  },
  consistent: {
    label: '未见疑点',
    tone: 'progress',
    meaning: 'AI 未指出冲突，但这只是辅助结论，不等于缺陷已确诊。',
    next: '仍须人工对照原图确认标注'
  },
  possible_false_positive: {
    label: '疑有误报',
    tone: 'attention',
    meaning: 'AI 认为部分标注可能是误报，需人工判断是否删除。',
    next: '逐条核对被点出的标注框'
  },
  possible_missed_detection: {
    label: '疑有漏检',
    tone: 'attention',
    meaning: 'AI 认为可能存在未标注的缺陷，需人工确认后补框。',
    next: '核对 AI 给出的关注区域，必要时补画标注框'
  },
  needs_human_review: {
    label: '需人工研判',
    tone: 'attention',
    meaning: 'AI 无法给出确定倾向，必须由人判断。',
    next: '对照原图与结果图逐项核对'
  }
}

/** 记录处理状态：record_status */
export const RECORD_STATUS: Record<string, StatusMeta> = {
  completed: { label: '检测完成', tone: 'ok', meaning: '检测任务已正常结束。', next: null },
  processing: { label: '检测中', tone: 'progress', meaning: '检测任务正在执行。', next: null },
  failed: { label: '检测失败', tone: 'danger', meaning: '检测任务未能完成。', next: '重新提交检测' }
}

const FALLBACK: StatusMeta = {
  label: '未知状态',
  tone: 'neutral',
  meaning: '后端返回了前端未登记的状态值，请核对接口。',
  next: null
}

export function humanStatusOf(value?: string | null): StatusMeta {
  if (!value) return HUMAN_STATUS.pending
  return HUMAN_STATUS[value] || { ...FALLBACK, label: value }
}

export function aiStatusOf(value?: string | null): StatusMeta {
  if (!value) return AI_STATUS.not_requested
  return AI_STATUS[value] || { ...FALLBACK, label: value }
}

export function recordStatusOf(value?: string | null): StatusMeta {
  if (!value) return RECORD_STATUS.completed
  return RECORD_STATUS[value] || { ...FALLBACK, label: value }
}

/** 只有人工确认过的记录才允许回流训练数据 */
export function canFeedback(record: { human_review_status?: string | null }): boolean {
  return record.human_review_status === 'confirmed'
}
