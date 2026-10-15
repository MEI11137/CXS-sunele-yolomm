// MD5: f810543f229cea946c3a7102f679cf55
/*
 * 版权所有 © 2025 羊羊小栈 (GJQ)
 *
 * 作者：羊羊小栈
 * 时间：2025-09-06 23:44:27
 *
 * 本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
 * 违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
 */

/// <reference types="vite/client" />

declare module 'virtual:build-info' {
  const buildInfo: {
    version: string
    lastUpdate: string
  }

  export default buildInfo
}

declare module 'element-plus/dist/locale/zh-cn.mjs'
