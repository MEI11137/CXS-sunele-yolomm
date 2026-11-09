import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * 共享的“界面选择上下文”。
 *
 * 为什么需要它：用户在识别记录/人工复核队列里勾选的记录，必须能在**当前页面
 * 直接发送**时被助手精确感知，而不是要求用户点某个按钮、新建会话、把 ID 打进
 * 正文。这里只保存“页面 + 精确 record_ids + 筛选快照”，发送时冻结一份快照交给
 * 后端；后端仍会逐条校验记录归属与权限，前端状态不是授权。
 */
export interface UiSelectionContext {
  projectId: string
  /** 供模型理解的页面名，例如“人工复核队列”“识别记录”。 */
  page: string
  recordIds: string[]
  /** 当前筛选的文字快照，帮助模型知道选择范围，不参与权限判断。 */
  filters?: string
}

export const useSelectionStore = defineStore('selection', () => {
  const context = ref<UiSelectionContext | null>(null)

  const count = computed(() => context.value?.recordIds.length ?? 0)

  /** 覆盖注册；空选择等于清空，避免“已选 0 条却仍附带旧上下文”。 */
  function setSelection(next: UiSelectionContext | null): void {
    if (!next || !next.projectId) {
      context.value = null
      return
    }
    const recordIds = [...new Set(next.recordIds.filter(Boolean))]
    context.value = recordIds.length
      ? {
          projectId: next.projectId,
          page: next.page.slice(0, 80),
          recordIds,
          filters: next.filters,
        }
      : null
  }

  /** 只返回属于当前项目的选择，跨项目不会串。 */
  function forProject(projectId: string): UiSelectionContext | null {
    const current = context.value
    if (!current || current.projectId !== projectId || !current.recordIds.length) return null
    return current
  }

  function clear(projectId?: string): void {
    if (!projectId || context.value?.projectId === projectId) context.value = null
  }

  return { context, count, setSelection, forProject, clear }
})