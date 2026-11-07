/**
 * 布局纯逻辑：右侧助手停靠开关、左侧会话列表的文案。
 *
 * 和 `timeline.ts` 一样不依赖 Vue，方便 `node --test` 直接跑；
 * 组件只负责把这里的判断结果渲染出来。规则集中在这里，是为了避免
 * "会话页显示一份助手、业务页又显示一份"这类由组件各自判断导致的分叉。
 */

/** 右侧助手在业务页的三种状态。 */
export type DockState = 'none' | 'open' | 'closed'

/**
 * 右侧助手该不该出现。
 *
 * * `dockable=false`（会话页/设置页）：助手要么在主区，要么不该有右侧列；
 * * 业务页默认展开，用户收起后只留一个"打开助手"的入口，不把助手弄丢。
 */
export function dockState(options: { dockable: boolean; collapsed: boolean }): DockState {
  if (!options.dockable) return 'none'
  return options.collapsed ? 'closed' : 'open'
}

/** 左侧会话条数文案：0 条时说明"还没有"，避免显示"0 个会话"这种空话。 */
export function sessionCountLabel(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return '还没有会话'
  return `${Math.trunc(count)} 个会话`
}

/**
 * 项目顶部导航：**与 v1.6 完全一致的六项**（项目对话 + 下面五项）。
 *
 * v1.6 `utils/projectRoutes.ts` 的顺序是
 * 人工复核 → 数据集 → 模型管理 → 图片巡检 → 识别记录，
 * 不在这里再发明新顺序或第七个入口。
 *
 * 数据回流仍保留真实路由/页面/功能，入口收在「数据集」页里，
 * 不占顶栏；高亮归属见 :func:`navOwner`。
 */
export const BUSINESS_PAGES = [
  { name: 'reviewQueue', label: '人工复核' },
  { name: 'datasets', label: '数据集' },
  { name: 'models', label: '模型管理' },
  { name: 'inspect', label: '图片巡检' },
  { name: 'records', label: '识别记录' },
] as const

/**
 * 顶部导航的高亮归属。
 *
 * 有些页面不在顶栏里，但要高亮它所属的那一项：
 * * `reflow`（数据回流）属于「数据集」；
 * * `review`（单条复核）属于「人工复核」。
 */
export function navOwner(routeName: string | symbol | null | undefined): string {
  const name = typeof routeName === 'string' ? routeName : ''
  if (name === 'reflow') return 'datasets'
  if (name === 'review') return 'reviewQueue'
  return name
}
