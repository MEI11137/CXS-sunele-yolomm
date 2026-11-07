import { defineStore } from 'pinia'
import { reactive } from 'vue'
import { scanEvents, SYNC_SCOPES } from '@/lib/sync'
import type { SyncScope } from '@/lib/sync'
import type { TurnEvent } from '@/types'

export type { SyncScope }

/**
 * 后台业务事件的失效通知（Agent 改了项目/会话/数据集等，当前页面要及时反映）。
 *
 * 工具已经会发 ``projects.changed`` / ``sessions.changed`` …，但前端此前只消费
 * messages/actions，项目名改了要按 F5 才更新。这里不缓存业务数据、不复制后端
 * 逻辑，只按事件**递增版本号**；页面 watch 版本号后用自己的 loader 重读。
 *
 * 去重规则在 ``lib/sync.ts``（纯函数、有单测）：同一 ``turn_id`` 里只处理比上次
 * 更大的 seq，重复轮询不会触发连环全站刷新。
 */
export const useSyncStore = defineStore('sync', () => {
  const revision = reactive<Record<SyncScope, number>>(
    Object.fromEntries(SYNC_SCOPES.map((scope) => [scope, 0])) as Record<SyncScope, number>,
  )
  /** turnId -> 已处理到的事件序号；只用来去重，不参与业务判断。 */
  const appliedSeq = new Map<string, number>()

  function bump(scope: SyncScope): void {
    revision[scope] += 1
  }

  /** 消费一批轮次事件，返回本次真正新出现的资源域。 */
  function apply(turnId: string, incoming: TurnEvent[]): Set<SyncScope> {
    const seen = appliedSeq.get(turnId) ?? -1
    const result = scanEvents(incoming, seen)
    if (!result.scopes.size && result.lastSeq === seen) return new Set()
    appliedSeq.set(turnId, result.lastSeq)
    for (const scope of result.scopes) bump(scope)
    return result.scopes
  }

  function reset(): void {
    for (const scope of SYNC_SCOPES) revision[scope] = 0
    appliedSeq.clear()
  }

  return { revision, apply, reset }
})
