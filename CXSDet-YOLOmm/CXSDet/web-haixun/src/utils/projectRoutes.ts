export const projectPages = [
  { page: 'review', label: '人工复核' },
  { page: 'datasets', label: '数据集' },
  { page: 'models', label: '模型管理' },
  { page: 'inspection', label: '图片巡检' },
  { page: 'records', label: '识别记录' },
]
export function projectPath(id: string, page = 'chat') {
  return `/projects/${encodeURIComponent(id)}/${page}`
}
export function scopedPage(projectId: string, path: string): string | null {
  const prefix = `/projects/${encodeURIComponent(projectId)}`
  const relative = path.startsWith(prefix + '/') ? path.slice(prefix.length) : path
  if (/^\/(chat(?:\/[A-Za-z0-9_-]+)?|review(?:\/[A-Za-z0-9_-]+)?|datasets|models|inspection|records|manual|extensions)(?:\?[^#\\]*)?$/.test(relative))
    return prefix + relative
  return null
}
