/**
 * 复核页“从哪份版本取框”的纯逻辑。
 *
 * 这段规则单独拎出来，是因为它很容易写错，而且错了肉眼很难发现：
 * 用 `boxes.length` 判断有没有草稿/结论，会把**有效的空结果**（用户删光了所有框、
 * 或已确认无缺陷）当成“没有”，刷新后回退到模型框，旧框又冒出来。
 * 正确依据是**版本是否存在**，空数组也是结果。
 */

export interface BoxLike {
  class_id: number
  bbox: number[]
}

export interface ReviewSnapshot {
  draft?: { boxes?: BoxLike[] | null; version?: number } | null
  latest_confirmed?: { boxes?: BoxLike[] | null; version?: number } | null
  latest?: { version?: number } | null
  latest_submitted?: { version?: number } | null
  record: { boxes?: BoxLike[] | null }
}

/** 当前应该显示哪份框：草稿优先，其次最近一次确认，最后才是模型原始框。 */
export function preferredBoxes(state: ReviewSnapshot): BoxLike[] {
  if (state.draft) return state.draft.boxes ?? []
  if (state.latest_confirmed) return state.latest_confirmed.boxes ?? []
  return state.record.boxes ?? []
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
