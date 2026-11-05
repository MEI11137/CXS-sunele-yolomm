/**
 * 数据回流表单的纯逻辑：**显示的值就是提交的值**，以及“这份预览还配不配定稿”。
 *
 * 真实故障（协作者在浏览器复现）：
 * 1. 动作/划分下拉显示为空，提交时 ``selections()`` 偷偷补 ``replace`` / ``train``，
 *    用户看到的和真正执行的不是一件事；
 * 2. 把模式从「另建」改回「回原修订」、改了名称，旧草稿的“定稿并生成版本”仍然可点，
 *    点下去执行的是**旧表单**的计划。
 *
 * 所以这里只有两条规则：
 * * 取值只经过 ``actionOf`` / ``splitRequired``，模板和提交共用同一份；
 * * 任何影响计划的输入都进 ``formSnapshot``，与“上次预览时的快照”不等就是过期。
 */

export type ReflowMode = 'revision' | 'new'
export type ReflowAction = 'replace' | 'append' | 'exclude'
export type ReflowSplit = 'train' | 'val' | 'test'
export type ReflowStrategy = 'even' | 'ratio' | 'manual'
export type Ratios = { train: number; val: number; test: number }

export interface ReflowForm {
  mode: ReflowMode
  datasetId: string
  name: string
  strategy: ReflowStrategy
  ratios: Ratios
  seed: number
}

export interface ReflowSelection {
  record_id: string
  action: ReflowAction
  target_sample_id?: string
  split?: ReflowSplit
}

export function defaultAction(mode: ReflowMode): ReflowAction {
  return mode === 'revision' ? 'replace' : 'append'
}

/** 下拉要显示的动作：显式设过就用它，否则就是该模式的默认值（同一函数给模板和提交用）。 */
export function actionOf(
  form: ReflowForm,
  actions: Record<string, ReflowAction>,
  recordId: string,
): ReflowAction {
  return actions[recordId] ?? defaultAction(form.mode)
}

/**
 * 这条记录是否真的需要用户选划分。
 *
 * 回原替换由服务端强制沿用父划分，所以这里**不显示**划分下拉——
 * 让用户以为可以重划分，是另一种形式的“显示与执行不一致”。
 */
export function splitRequired(
  form: ReflowForm,
  actions: Record<string, ReflowAction>,
  recordId: string,
): boolean {
  if (form.mode === 'new') return form.strategy === 'manual'
  return actionOf(form, actions, recordId) === 'append'
}

export function splitOf(
  splits: Record<string, ReflowSplit>,
  recordId: string,
): ReflowSplit {
  return splits[recordId] ?? 'train'
}

export function buildSelections(
  recordIds: string[],
  form: ReflowForm,
  actions: Record<string, ReflowAction>,
  splits: Record<string, ReflowSplit>,
  targets: Record<string, string>,
): ReflowSelection[] {
  return recordIds.map((recordId) => ({
    record_id: recordId,
    action: actionOf(form, actions, recordId),
    target_sample_id: targets[recordId] || undefined,
    split: splitRequired(form, actions, recordId) ? splitOf(splits, recordId) : undefined,
  }))
}

/** 影响计划的所有输入；变了就必须重新预览。 */
export function formSnapshot(
  form: ReflowForm,
  recordIds: string[],
  actions: Record<string, ReflowAction>,
  splits: Record<string, ReflowSplit>,
  targets: Record<string, string>,
): string {
  return JSON.stringify({
    mode: form.mode,
    dataset_id: form.mode === 'revision' ? form.datasetId : '',
    name: form.name.trim(),
    strategy: form.strategy,
    ratios: form.strategy === 'ratio' ? form.ratios : null,
    seed: Number(form.seed) || 0,
    selections: buildSelections(recordIds, form, actions, splits, targets),
  })
}

/** 没有预览过（空串）不算过期；预览过且与当前表单不同才算。 */
export function isPlanStale(previewed: string, current: string): boolean {
  return Boolean(previewed) && previewed !== current
}
