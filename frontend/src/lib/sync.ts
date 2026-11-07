/**
 * 后台业务事件 → 需要失效的资源域（纯函数，便于单测）。
 *
 * 只做“哪些列表该重读”的映射与按 seq 去重；不缓存业务数据，也不复制后端逻辑。
 * 刻意不依赖 Vue 或模块别名，方便用 ``node --test`` 直接跑。
 */
export interface SyncEvent {
  seq: number
  type: string
}

export type SyncScope =
  | 'projects'
  | 'sessions'
  | 'datasets'
  | 'models'
  | 'documents'
  | 'manuals'
  | 'reflow'
  | 'training'

export const SYNC_SCOPES: SyncScope[] = [
  'projects',
  'sessions',
  'datasets',
  'models',
  'documents',
  'manuals',
  'reflow',
  'training',
]

const EVENT_SCOPE: Record<string, SyncScope> = {
  'projects.changed': 'projects',
  'sessions.changed': 'sessions',
  'datasets.changed': 'datasets',
  'models.changed': 'models',
  'documents.changed': 'documents',
  'manuals.changed': 'manuals',
  'reflow.changed': 'reflow',
  'training.changed': 'training',
}

export interface SyncScan {
  scopes: Set<SyncScope>
  /** 处理后的最大 seq；没有新事件时原样返回 seen。 */
  lastSeq: number
}

/**
 * 扫描一批事件：只处理 ``seq > seen`` 的部分，按 seq 升序，返回需要失效的资源域。
 * 同一事件重复到达（轮询重放、断线重连）不会重复触发刷新，也不会把用户
 * 正在编辑的内容冲掉。
 */
export function scanEvents(events: SyncEvent[], seen = -1): SyncScan {
  const fresh = events
    .filter((event) => Number(event.seq) > seen)
    .sort((left, right) => Number(left.seq) - Number(right.seq))
  if (!fresh.length) return { scopes: new Set(), lastSeq: seen }
  const scopes = new Set<SyncScope>()
  for (const event of fresh) {
    const scope = EVENT_SCOPE[String(event.type)]
    if (scope) scopes.add(scope)
  }
  return { scopes, lastSeq: Number(fresh[fresh.length - 1].seq) }
}
