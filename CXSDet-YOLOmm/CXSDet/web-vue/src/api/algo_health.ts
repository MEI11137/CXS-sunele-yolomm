// MD5: 6dc04dff32bdbb8fe3a32d67eda314c7
/*
 * 版权所有©2025 羊羊小栈(GJQ) | 作者: 羊羊小栈 | 时间: 2025-09-06 23:44:27
 *
 * 本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
 * 违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
 */

import { request } from './request'

export interface HealthResponse {
  status: string
  service: string
  version: string
}

// 检查算法服务健康状态
export const checkAlgoHealth = () => {
  return request.get<HealthResponse>('/health/health_check')
} 