// 接口层入口 —— 根据配置选择 mock 或 real 适配器。
// 当前后端未就绪，使用 mock；真实联调时切换为 real。
import type { ApiAdapter } from './types'
import { MockAdapter } from './mock-adapter'

// 后端就绪后，将此处切换为 real adapter 实例。
// 正式运行接口失败不得回退为模拟成功，故 real 实现需独立抛出错误。
export function createAdapter(): ApiAdapter {
  // TODO: 接入真实后端时改为 new RealAdapter(...)
  return new MockAdapter()
}

// 全局单例
let _adapter: ApiAdapter | null = null
export function getAdapter(): ApiAdapter {
  if (!_adapter) {
    _adapter = createAdapter()
  }
  return _adapter
}

export const CURRENT_MODE = 'mock'
