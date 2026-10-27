// 接口层入口 —— 按显式环境配置选择 real / mock。
//
// 配置项（.env.development / .env.local）：
//   VITE_API_MODE=real | mock     默认 real，绝不写死 mock
//   VITE_API_BASE=                留空走 Vite 的 /api 代理
//
// 真实模式下任何失败都向上抛错，不会静默回退到 MockAdapter。

import type { ApiAdapter, Mode } from './types'
import { MockAdapter } from './mock-adapter'
import { RealAdapter } from './real-adapter'

function resolveMode(): Mode {
  const configured = (import.meta.env.VITE_API_MODE ?? '').toLowerCase()
  if (configured === 'mock') return 'mock'
  // 未配置时按真实模式跑：宁可报错，也不静默用假数据
  return 'real'
}

export const CURRENT_MODE: Mode = resolveMode()

export function createAdapter(): ApiAdapter {
  if (CURRENT_MODE === 'mock') {
    // eslint-disable-next-line no-console
    console.warn('[海巡智检] 当前为演示模式（VITE_API_MODE=mock），页面数据为模拟数据')
    return new MockAdapter()
  }
  return new RealAdapter()
}

// 全局单例
let _adapter: ApiAdapter | null = null

export function getAdapter(): ApiAdapter {
  if (!_adapter) {
    _adapter = createAdapter()
  }
  return _adapter
}

/** 账号切换时重建适配器，避免带上前一个账号的缓存状态。 */
export function resetAdapter(): void {
  _adapter = null
}

export * from './types'
export { http, ApiError, getToken, setToken, onUnauthorized } from './http'
