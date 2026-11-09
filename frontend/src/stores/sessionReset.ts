/**
 * 账号级缓存复位。
 *
 * 会话失效、主动退出、换账号登录都必须在同一处清掉按“旧用户”缓存的状态：
 * 助手轮询/动作卡/偏好、业务列表/详情 watcher、工作区与连接、界面选择上下文。
 * 应急复核草稿不走这里（它在 localStorage 里按 user/project/record 隔离，
 * 由 ReviewView 自己决定是否恢复），所以复位不能碰 hx17:review-recovery:*。
 */
import { useAgentStore } from './agent'
import { useBusinessStore } from './business'
import { useConnectionStore } from './connection'
import { useSelectionStore } from './selection'
import { useWorkspaceStore } from './workspace'

export function resetUserScopedStores(): void {
  // 先停轮询/在途请求（reset 内部会推进 generation，异步返回自动作废），再清列表。
  useAgentStore().reset()
  useBusinessStore().reset()
  useWorkspaceStore().reset()
  useConnectionStore().reset()
  useSelectionStore().clear()
}
