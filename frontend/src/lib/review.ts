/**
 * 复核页“从哪份版本取框”的纯逻辑。
 *
 * 这段规则单独拎出来，是因为它很容易写错，而且错了肉眼很难发现：
 * 用 `boxes.length` 判断有没有草稿/结论，会把**有效的空结果**（用户删光了所有框、
 * 或已确认无缺陷）当成“没有”，刷新后回退到模型框，旧框又冒出来。
 * 正确依据是**版本是否存在**，空数组也是结果。
 *
 * 双模态各自取各自的一套：参考模态用 `boxes`，辅模态用 `x_boxes`。
 * 两套坐标来自不同尺寸的原图，不能互相回退、换算或复制。
 */

export interface BoxLike {
  class_id: number
  bbox: number[]
}

interface VersionLike {
  boxes?: BoxLike[] | null
  x_boxes?: BoxLike[] | null
  version?: number
  updated_at?: string
}

export interface ReviewSnapshot {
  draft?: VersionLike | null
  latest_confirmed?: VersionLike | null
  latest?: { version?: number; updated_at?: string } | null
  latest_submitted?: { version?: number; updated_at?: string } | null
  record: { boxes?: BoxLike[] | null; x_boxes?: BoxLike[] | null }
}

/** 当前应该显示哪份框：草稿优先，其次最近一次确认，最后才是模型原始框。 */
export function preferredBoxes(state: ReviewSnapshot): BoxLike[] {
  if (state.draft) return state.draft.boxes ?? []
  if (state.latest_confirmed) return state.latest_confirmed.boxes ?? []
  return state.record.boxes ?? []
}

/**
 * 辅模态（X）框的取值：规则与参考模态一致，但**回顾自己的列**。
 * 老记录没有 x_boxes 列时按空处理；辅模态图不存在时页面也不会渲染第二张画布。
 */
export function preferredXBoxes(state: ReviewSnapshot): BoxLike[] {
  if (state.draft) return state.draft.x_boxes ?? []
  if (state.latest_confirmed) return state.latest_confirmed.x_boxes ?? []
  return state.record.x_boxes ?? []
}

/** 保存草稿的并发基线：最近一条版本（草稿也算），每存一次前进一版。 */
export function draftBaseVersion(state: ReviewSnapshot | null): number {
  if (!state) return 0
  return state.latest?.version ?? 0
}

/** 提交/驳回的并发基线：最近一次**已提交**版本，草稿不参与。 */
export function submitBaseVersion(state: ReviewSnapshot | null): number {
  if (!state) return 0
  return state.latest_submitted?.version ?? 0
}

/** 远端草稿变更签名：版本号 + 更新时间，任一前进都算新内容。 */
export function reviewVersionStamp(version: { version?: number; updated_at?: string } | null | undefined): string {
  if (!version) return ''
  return `${version.version ?? 0}@${version.updated_at ?? ''}`
}

/** 当前页面已加载内容的签名，用于判断远端是否改过、以及本地是否有未保存改动。 */
export function reviewLoadedStamp(state: ReviewSnapshot | null): string {
  if (!state) return ''
  const parts = [
    reviewVersionStamp(state.draft),
    reviewVersionStamp(state.latest_confirmed),
    reviewVersionStamp(state.latest_submitted),
  ]
  return parts.join('|')
}