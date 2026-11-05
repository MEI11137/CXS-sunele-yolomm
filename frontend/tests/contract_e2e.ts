/**
 * 真实前端适配器 → 真实 HTTP 后端的契约测试（由 scripts/contract_frontend.py 驱动）。
 *
 * 这里 import 的是 `frontend/src/api/business.ts` 本身，不是重写的副本：
 * 路径、方法、请求体、响应字段有任何一处不一致，这个脚本就会失败。
 * 浏览器里靠 document.cookie 取 CSRF；Node 里用一个最小 document 桩 + fetch 注入 Cookie。
 */
import { readFile } from 'node:fs/promises'

const env = process.env
const base = (env.HX17_BASE || '').replace(/\/$/, '')
const cookie = env.HX17_COOKIE || ''
const projectId = env.HX17_PROJECT || ''
const zipPath = env.HX17_ZIP || ''
const weightPath = env.HX17_WEIGHT || ''

if (!base || !cookie || !projectId || !zipPath) {
  console.error('缺少 HX17_BASE / HX17_COOKIE / HX17_PROJECT / HX17_ZIP')
  process.exit(2)
}

interface GlobalShim {
  __HX17_API_BASE__?: string
  document?: { cookie: string }
  fetch: typeof fetch
}
const shim = globalThis as unknown as GlobalShim
shim.__HX17_API_BASE__ = `${base}/api`
shim.document = { cookie }

const nativeFetch = globalThis.fetch
globalThis.fetch = ((input: RequestInfo | URL, init: RequestInit = {}) => {
  const headers = new Headers(init.headers ?? {})
  headers.set('cookie', cookie)
  return nativeFetch(input, { ...init, headers })
}) as typeof fetch

const api = await import('@/api/business')

const report: Record<string, unknown> = { steps: [] }
function note(step: string, data: unknown): void {
  ;(report.steps as unknown[]).push({ step, ok: true, data })
}
function fail(step: string, error: unknown): never {
  ;(report.steps as unknown[]).push({
    step,
    ok: false,
    error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
    code: (error as { code?: string })?.code ?? '',
    details: (error as { details?: unknown })?.details ?? null,
  })
  console.log(JSON.stringify(report, null, 2))
  process.exit(1)
}

try {
  const zip = await readFile(zipPath)
  const file = new File([zip], 'dataset.zip', { type: 'application/zip' })
  const asset = await api.uploadAsset(projectId, file)
  note('upload_zip', { id: asset.id, kind: asset.kind, size: asset.size, sha256: asset.sha256 })
  if (asset.kind !== 'archive') throw new Error(`上传后类型是 ${asset.kind}，不是 archive`)

  const name = `契约数据集-${Date.now()}`
  const imported = await api.importDataset(projectId, {
    name,
    asset_id: asset.id,
    modalities: ['rgb', 'ir'],
    secondary_modality: 'ir',
  })
  note('import_dataset', imported.dataset)
  if (!imported.dataset.sample_count) throw new Error('导入后的样本数为 0')

  const samples = await api.datasetSamples(projectId, imported.dataset.id)
  note('dataset_samples', {
    count: samples.samples.length,
    with_x: samples.samples.filter((item) => Boolean(item.x_rel)).length,
  })
  if (samples.samples.length !== imported.dataset.sample_count) {
    throw new Error('样本清单数量与数据集统计不一致')
  }

  const events = await api.jobEvents(projectId, { after: 0 })
  note('job_events', { count: events.events.length, last_seq: events.last_seq })
  if (typeof events.last_seq !== 'number') throw new Error('job-events 没有返回数字 last_seq')

  if (weightPath && env.HX17_SKIP_MODEL !== '1') {
    const weight = await readFile(weightPath)
    const weightFile = new File([weight], 'best.pt', { type: 'application/octet-stream' })
    const weightAsset = await api.uploadAsset(projectId, weightFile)
    note('upload_weight', { id: weightAsset.id, kind: weightAsset.kind, size: weightAsset.size })
    if (weightAsset.kind !== 'weight') throw new Error(`权重上传后类型是 ${weightAsset.kind}`)

    const modelName = `契约模型-${Date.now()}`
    const model = await api.importModel(projectId, {
      name: modelName,
      family: 'yolomm',
      model_mode: 'multimodal',
      secondary_modality: 'ir',
      dataset_id: imported.dataset.id,
      class_map: { '0': 0, '1': 1 },
      asset_id: weightAsset.id,
      notes: '契约测试导入',
    })
    note('import_model', model.model)
    const published = await api.publishModel(projectId, model.model.id)
    note('publish_model', published.model)
    if (published.model.status !== 'published') throw new Error('发布后状态不是 published')
  }

  const listed = await api.listDatasets(projectId)
  note('list_datasets', { count: listed.datasets.length })
  const models = await api.listModels(projectId)
  note('list_models', { count: models.models.length })
  console.log(JSON.stringify(report, null, 2))
} catch (error) {
  fail('contract', error)
}
