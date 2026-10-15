// MD5: a7756956b03408b063c18b4d94c47df5
/*
 * 版权所有©2025 羊羊小栈(GJQ)
 *
 * 作者:羊羊小栈
 * 时间:2025-09-06 23:44:27
 *
 * 本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
 * 违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
 */

// 分页查询参数
export interface PageDTO {
  current: number // 当前页码
  size: number // 每页大小
}

// 分页返回结果
export interface PageVO<T> {
  records: T[] // 数据列表
  total: number // 总条数
  size: number // 每页大小
  current: number // 当前页码
  pages: number // 总页数
}

// 通用响应结果
export interface Result<T> {
  code: number // 状态码
  msg: string // 消息
  data: T // 数据
}