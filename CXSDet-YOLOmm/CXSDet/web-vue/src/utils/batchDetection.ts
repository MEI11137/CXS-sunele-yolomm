export type BatchDetectionStatus = 'pending' | 'processing' | 'success' | 'failed'

export interface BatchDetectionItem {
  id: string
  rgbFile: File
  xFile?: File
  status: BatchDetectionStatus
  message: string
  recordId?: string
}

export interface PairBatchOptions {
  multimodal: boolean
  secondaryModality?: string | null
}

export const fileStem = (name: string) => {
  const basename = name.replace(/\\/g, '/').split('/').pop() || name
  const dot = basename.lastIndexOf('.')
  return (dot > 0 ? basename.slice(0, dot) : basename).trim().toLocaleLowerCase()
}

export const pairBatchFiles = (
  rgbFiles: File[],
  xFiles: File[],
  options: PairBatchOptions
): BatchDetectionItem[] => {
  const xByStem = new Map<string, File>()
  for (const file of xFiles) {
    const stem = fileStem(file.name)
    if (!xByStem.has(stem)) xByStem.set(stem, file)
  }

  const requiresX = options.multimodal && options.secondaryModality !== 'edge'
  return rgbFiles.map((rgbFile, index) => {
    const stem = fileStem(rgbFile.name)
    const xFile = options.multimodal ? xByStem.get(stem) : undefined
    const missing = requiresX && !xFile
    return {
      id: `${stem}-${index}`,
      rgbFile,
      xFile,
      status: missing ? 'failed' : 'pending',
      message: missing
        ? `缺少同名 ${options.secondaryModality || 'X'} 图片：${rgbFile.name}`
        : xFile
          ? `已配对 ${xFile.name}`
          : options.secondaryModality === 'edge'
            ? '将由 RGB 自动生成 Edge'
            : '等待检测'
    }
  })
}
