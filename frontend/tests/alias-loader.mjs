// 让 Node 能解析前端源码里的 `@/` 别名（仅测试用）。
// Vite 在构建时处理这个别名；契约测试直接跑真实适配器，所以补一个小解析器。
import { pathToFileURL } from 'node:url'

const SRC = new URL('../src/', import.meta.url)

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('@/')) {
    const url = new URL(specifier.slice(2), SRC)
    const hasExtension = /\.[a-z]+$/i.test(url.pathname)
    return nextResolve(hasExtension ? url.href : `${url.href}.ts`, context)
  }
  return nextResolve(specifier, context)
}
